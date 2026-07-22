"""Collect and review redacted real-log candidates for SL100 quality evals."""
from __future__ import annotations

import argparse
import hashlib
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from iot_ops_agent.integrations.elasticsearch import (
    DEFAULT_ERROR_QUERY,
    SERVICE_INDEX_PREFIXES,
    SHANGHAI_TZ,
    facts_from_es_search,
    list_indices,
    search_logs,
)
from iot_ops_agent.diagnosis.incident import build_incident_report, render_incident_report
from iot_ops_agent.diagnosis.incident_types import INCIDENT_TYPE_CATALOG, INCIDENT_TYPE_KEYS, incident_type_label
from iot_ops_agent.diagnosis.log_core import local_diagnosis, redact_text
from iot_ops_agent.diagnosis.real_cases import (
    LOCAL_CANDIDATES_PATH,
    LOCAL_CASES_PATH,
    append_jsonl,
    evidence_fingerprint,
    incident_dedup_key,
    incident_signature,
    replay_snapshot_from_report,
    local_time_from_es,
    read_jsonl,
    write_jsonl,
)


def _candidate_id(service: str, index: str, es_id: str, fingerprint: str) -> str:
    value = f"{service}|{index}|{es_id}|{fingerprint}"
    return "real-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _available_days(service: str, start: datetime, end: datetime) -> list[date]:
    days = []
    for item in list_indices(service=service):
        index = str(item.get("index", ""))
        try:
            day = date.fromisoformat(index[-10:])
        except ValueError:
            continue
        if start.date() <= day <= end.date():
            days.append(day)
    return sorted(set(days), reverse=True)


def collect_candidates(
    *,
    since_days: int,
    per_service: int,
    reviewed_ids: set[str] | None = None,
    reviewed_dedup_keys: set[str] | None = None,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    now = (now or datetime.now(SHANGHAI_TZ)).astimezone(SHANGHAI_TZ)
    start = now - timedelta(days=max(1, since_days))
    candidates: list[dict[str, Any]] = []
    errors: list[str] = []
    reviewed_ids = reviewed_ids or set()
    reviewed_dedup_keys = reviewed_dedup_keys or set()

    for service in SERVICE_INDEX_PREFIXES:
        try:
            days = _available_days(service, start, now)
        except Exception as exc:  # noqa: BLE001 - collection should continue for other services.
            errors.append(f"{service} 索引发现失败: {exc}")
            continue

        seen_dedup_keys = set()
        selected = 0
        for day in days:
            try:
                result = search_logs(
                    service=service,
                    keyword=DEFAULT_ERROR_QUERY,
                    date_text=day.isoformat(),
                    size=200,
                )
            except Exception as exc:  # noqa: BLE001 - another day may still be readable.
                errors.append(f"{service} {day}: {exc}")
                continue
            for hit in result["hits"]:
                fingerprint = evidence_fingerprint(hit["message"])
                signature = incident_signature(hit["message"])
                dedup_key = incident_dedup_key(service, signature)
                candidate_id = _candidate_id(service, str(hit.get("index", "")), str(hit.get("id", "")), fingerprint)
                if dedup_key in seen_dedup_keys or dedup_key in reviewed_dedup_keys or candidate_id in reviewed_ids:
                    continue
                seen_dedup_keys.add(dedup_key)
                candidates.append({
                    "id": candidate_id,
                    "service": service,
                    "index": hit.get("index", ""),
                    "es_id": hit.get("id", ""),
                    "timestamp": hit.get("timestamp", ""),
                    "around": local_time_from_es(str(hit["timestamp"])),
                    "around_minutes": 10,
                    "keyword": DEFAULT_ERROR_QUERY,
                    "fingerprint": fingerprint,
                    "signature": signature,
                    "dedup_key": dedup_key,
                    "query": f"{local_time_from_es(str(hit['timestamp']))} {service} 日志异常",
                })
                selected += 1
                if selected >= max(1, per_service):
                    break
            if selected >= max(1, per_service):
                break

    return candidates, errors


def load_candidate_hit(candidate: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    result = search_logs(
        service=candidate["service"],
        keyword=candidate.get("keyword", DEFAULT_ERROR_QUERY),
        around_text=candidate["around"],
        around_minutes=int(candidate.get("around_minutes", 10)),
        size=200,
    )
    hit = next(
        (
            item
            for item in result["hits"]
            if item.get("id") == candidate.get("es_id") and item.get("index") == candidate.get("index")
        ),
        None,
    )
    return hit, result


def candidate_report(candidate: dict[str, Any]) -> dict[str, Any] | None:
    hit, result = load_candidate_hit(candidate)
    if not hit:
        return None
    facts = facts_from_es_search({
        **result,
        "hits": [hit],
        "total": {"value": 1, "relation": "eq"},
        "raw_returned": 1,
        "redaction_dropped_count": 0,
        "source_status": "ok",
    })
    return build_incident_report(
        query=candidate["query"],
        analysis={"facts": facts, "diagnosis": local_diagnosis(facts)},
        plan={"services": [candidate["service"]], "time_window": result["time_window"]},
    )


def hydrate_reviewed_case_dedup_keys(cases: list[dict[str, Any]]) -> tuple[set[str], list[str], bool]:
    """Backfill legacy case signatures by re-reading only their saved ES reference.

    The historical case format intentionally stores no raw log text.  For an
    old case without a signature, this function performs one read-only lookup,
    calculates a one-way signature in memory, and writes only that signature
    back to the local private case file.
    """
    keys: set[str] = set()
    errors: list[str] = []
    changed = False
    for case in cases:
        lookup = case.get("lookup", {})
        service = str(lookup.get("service", ""))
        signature = str(lookup.get("signature", ""))
        if not service:
            errors.append(f"历史案例 {case.get('id', 'unknown')} 缺少服务名，无法参与同类去重。")
            continue
        if not signature:
            try:
                hit, _ = load_candidate_hit({
                    "service": service,
                    "index": lookup.get("index", ""),
                    "es_id": lookup.get("es_id", ""),
                    "around": lookup.get("around", ""),
                    "around_minutes": lookup.get("around_minutes", 10),
                    "keyword": lookup.get("keyword", DEFAULT_ERROR_QUERY),
                })
            except Exception:  # noqa: BLE001 - keep the reviewed ID even when source data has expired.
                errors.append(f"历史案例 {case.get('id', 'unknown')} 当前无法补全去重特征。")
                continue
            if not hit:
                errors.append(f"历史案例 {case.get('id', 'unknown')} 已不在日志源，无法补全去重特征。")
                continue
            signature = incident_signature(str(hit["message"]))
            lookup["signature"] = signature
            changed = True

        dedup_key = incident_dedup_key(service, signature)
        if lookup.get("dedup_key") != dedup_key:
            lookup["dedup_key"] = dedup_key
            changed = True
        keys.add(dedup_key)
    return keys, errors, changed


def _choose_incident_types(predicted_types: list[str]) -> list[str]:
    """Present a closed taxonomy menu so labels cannot drift into free text."""
    recommended = [key for key, _ in INCIDENT_TYPE_CATALOG if key in predicted_types]
    print("\n标准问题类型：")
    for index, (key, label) in enumerate(INCIDENT_TYPE_CATALOG, start=1):
        marker = "（系统推荐）" if key in recommended else ""
        print(f"{index}. {label} [{key}]{marker}")

    default_text = "、".join(incident_type_label(key) for key in recommended) or "无"
    while True:
        choice = input(f"选择类型编号（可输入 1,2；直接回车使用推荐：{default_text}）: ").strip()
        if not choice and recommended:
            return recommended
        indexes = [item.strip() for item in choice.split(",") if item.strip()]
        if not indexes or not all(item.isdigit() and 1 <= int(item) <= len(INCIDENT_TYPE_CATALOG) for item in indexes):
            print("请输入菜单中的编号，例如 11 或 6,7；不能输入自定义文字。")
            continue
        selected = [INCIDENT_TYPE_CATALOG[int(item) - 1][0] for item in indexes]
        return list(dict.fromkeys(selected))


def _choose_risk_level(default_risk: str) -> str:
    while True:
        risk = input(f"真实风险 low/medium/high（直接回车使用 {default_risk}）: ").strip().lower() or default_risk
        if risk in {"low", "medium", "high"}:
            return risk
        print("风险只能是 low、medium 或 high。")


def _review_case(candidate: dict[str, Any], report: dict[str, Any]) -> dict[str, Any] | None:
    print("\n" + render_incident_report(report))
    print("\n你的判断：1=真实故障，2=正常行为，3=证据不足，4=新类型待补充，q=停止")
    choice = input("选择: ").strip().lower()
    if choice == "q":
        return None
    if choice not in {"1", "2", "3", "4"}:
        print("输入无效，跳过这条。")
        return {"skip": True}

    predicted_types = report.get("facts_summary", {}).get("incidents", [])
    if choice == "1":
        incident_types = _choose_incident_types([item for item in predicted_types if item in INCIDENT_TYPE_KEYS])
        risk = _choose_risk_level(report.get("risk_level", "medium"))
        verdict = "incident"
        review_status = "confirmed"
    elif choice == "2":
        incident_types = []
        risk = "low"
        verdict = "normal"
        review_status = "confirmed"
    elif choice == "4":
        incident_types = []
        risk = "unknown"
        verdict = "uncertain"
        review_status = "taxonomy_pending"
    else:
        incident_types = []
        risk = "unknown"
        verdict = "uncertain"
        review_status = "uncertain"

    note_prompt = "请说明建议新增的类型和现象（可直接回车）: " if choice == "4" else "备注（可直接回车）: "
    note = input(note_prompt).strip()
    reviewed_case = {
        "id": candidate["id"],
        "query": candidate["query"],
        "lookup": {
            "service": candidate["service"],
            "index": candidate["index"],
            "es_id": candidate["es_id"],
            "around": candidate["around"],
            "around_minutes": candidate["around_minutes"],
            "keyword": candidate["keyword"],
            "fingerprint": candidate["fingerprint"],
            "signature": candidate.get("signature", ""),
            "dedup_key": candidate.get("dedup_key", ""),
        },
        "expected": {
            "verdict": verdict,
            "incident_types": incident_types,
            "risk_level": risk,
            "services": [candidate["service"]],
        },
        "reviewer_note": redact_text(note),
        "review_status": review_status,
    }
    if review_status == "confirmed" and report.get("evidence"):
        reviewed_case["snapshot"] = replay_snapshot_from_report(candidate, report)
    return reviewed_case


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a private, reviewed real-log eval set for SL100.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect = subparsers.add_parser("collect", help="Collect distinct, redacted ES log candidates.")
    collect.add_argument("--since-days", type=int, default=30)
    collect.add_argument("--per-service", type=int, default=10)
    review = subparsers.add_parser("review", help="Review pending candidates interactively.")
    review.add_argument("--limit", type=int, default=5)
    review.add_argument("--candidates", default=str(LOCAL_CANDIDATES_PATH))
    review.add_argument("--cases", default=str(LOCAL_CASES_PATH))
    snapshot = subparsers.add_parser("snapshot", help="Backfill safe replay snapshots for legacy reviewed cases.")
    snapshot.add_argument("--cases", default=str(LOCAL_CASES_PATH))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "snapshot":
        cases_path = Path(args.cases)
        cases = read_jsonl(cases_path)
        updated = unavailable = 0
        for case in cases:
            if case.get("snapshot") or case.get("review_status") != "confirmed":
                continue
            lookup = case.get("lookup", {})
            candidate = {
                **lookup,
                "id": case.get("id", ""),
                "query": case.get("query", ""),
                "timestamp": "",
            }
            try:
                report = candidate_report(candidate)
                if report is None:
                    unavailable += 1
                    continue
                case["snapshot"] = replay_snapshot_from_report(candidate, report)
                updated += 1
            except Exception:  # noqa: BLE001 - expired source data remains explicitly unavailable.
                unavailable += 1
        if updated:
            write_jsonl(cases_path, cases)
        print(f"已补全 {updated} 条脱敏回放快照；{unavailable} 条历史证据已不可用。")
        return 0 if unavailable == 0 else 1

    if args.command == "collect":
        reviewed_cases = read_jsonl(LOCAL_CASES_PATH)
        reviewed_ids = {case.get("id") for case in reviewed_cases}
        reviewed_dedup_keys, migration_errors, migration_changed = hydrate_reviewed_case_dedup_keys(reviewed_cases)
        if migration_changed:
            write_jsonl(LOCAL_CASES_PATH, reviewed_cases)
            print("已为历史案例补全去重特征；只保存指纹，不保存原始日志正文。")
        candidates, errors = collect_candidates(
            since_days=args.since_days,
            per_service=args.per_service,
            reviewed_ids=reviewed_ids,
            reviewed_dedup_keys=reviewed_dedup_keys,
        )
        write_jsonl(LOCAL_CANDIDATES_PATH, candidates)
        print(f"已保存 {len(candidates)} 条候选案例到 {LOCAL_CANDIDATES_PATH}。候选文件不包含原始日志正文。")
        for error in [*migration_errors, *errors]:
            print(f"- 跳过数据源: {error}")
        print("下一步: uv run iot-ops cases review")
        return 0

    candidates_path = Path(args.candidates)
    cases_path = Path(args.cases)
    candidates = read_jsonl(candidates_path)
    reviewed = {case.get("id") for case in read_jsonl(cases_path)}
    if not candidates:
        print("没有候选案例。请先运行 collect。")
        return 2

    completed = 0
    for candidate in candidates:
        if candidate["id"] in reviewed:
            continue
        try:
            report = candidate_report(candidate)
        except Exception as exc:  # noqa: BLE001 - leave the candidate pending for later retry.
            print(f"候选 {candidate['id']} 当前无法读取: {exc}")
            continue
        if report is None:
            print(f"候选 {candidate['id']} 已不在 ES 或被安全过滤，跳过。")
            continue
        reviewed_case = _review_case(candidate, report)
        if reviewed_case is None:
            break
        if reviewed_case.get("skip"):
            continue
        append_jsonl(cases_path, reviewed_case)
        completed += 1
        if completed >= max(1, args.limit):
            break

    print(f"本次完成 {completed} 条评审。下一步: uv run iot-ops eval real")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
