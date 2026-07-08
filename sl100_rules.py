"""
M3: deterministic SL100 log rule analyzer.

This CLI is the non-AI layer of the diagnosis pipeline. It reads logs,
redacts sensitive values, extracts structured facts, and prints JSON.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sl100_log_core import extract_rule_facts_from_paths


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract deterministic SL100 log facts.")
    parser.add_argument("logs", nargs="+", help="One or more SL100 log file paths.")
    parser.add_argument("--tail-lines", type=int, default=800, help="Read only the last N lines per file.")
    parser.add_argument("--output", help="Optional path to save facts JSON.")
    parser.add_argument("--summary", action="store_true", help="Print a compact human-readable summary.")
    parser.add_argument("--fail-on-high", action="store_true", help="Exit 1 when extracted risk level is high.")
    return parser.parse_args()


def render_summary(facts: dict) -> str:
    lines = [
        "=" * 60,
        "SL100 规则分析事实摘要",
        "=" * 60,
        f"风险等级: {facts.get('risk_level', 'unknown')}",
        f"总结: {facts.get('summary', '')}",
        "",
        "服务概览:",
    ]
    for service, info in facts.get("services", {}).items():
        levels = info.get("level_counts", {})
        lines.append(
            f"- {service}: errors={info.get('error_count', 0)}, "
            f"warnings={info.get('warning_count', 0)}, levels={levels}"
        )

    incidents = facts.get("incidents", [])
    lines.append("")
    lines.append("命中的规则:")
    if not incidents:
        lines.append("- 无")
    for incident in incidents:
        lines.append(
            f"- {incident.get('type')} [{incident.get('risk_level')}], "
            f"services={incident.get('related_services', [])}"
        )

    keywords = facts.get("error_keywords", [])
    if keywords:
        lines.append("")
        lines.append("错误关键词:")
        lines.extend(f"- {item['keyword']}: {item['count']}" for item in keywords[:8])

    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    missing = [path for path in args.logs if not Path(path).expanduser().exists()]
    if missing:
        print(f"日志文件不存在: {missing}", file=sys.stderr)
        return 2

    facts = extract_rule_facts_from_paths(args.logs, tail_lines=args.tail_lines)

    if args.output:
        Path(args.output).write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"规则 facts JSON 已保存到: {args.output}")

    if args.summary:
        print(render_summary(facts))
    else:
        print(json.dumps(facts, ensure_ascii=False, indent=2))

    return 1 if args.fail_on_high and facts.get("risk_level") == "high" else 0


if __name__ == "__main__":
    raise SystemExit(main())
