"""
M1: SL100 local log diagnosis CLI.

Examples:
  uv run iot-ops logs analyze samples/sl100_logs/device_login_failed.log --local-only
  uv run iot-ops logs analyze /path/to/gateway.log
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import json

from iot_ops_agent.diagnosis.log_core import (
    analyze_paths,
    diagnose_with_claude,
    docs_context_for_query,
    extract_rule_facts_from_paths,
    local_diagnosis,
    render_report,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze SL100 service logs.")
    parser.add_argument("logs", nargs="+", help="One or more log file paths.")
    parser.add_argument("--tail-lines", type=int, default=800, help="Read only the last N lines per file.")
    parser.add_argument("--local-only", action="store_true", help="Do not call Claude; use deterministic local diagnosis.")
    parser.add_argument("--json", action="store_true", help="Print raw JSON result.")
    parser.add_argument("--facts", action="store_true", help="Print deterministic extracted facts and exit.")
    parser.add_argument("--facts-output", help="Optional path to save deterministic facts JSON before diagnosis.")
    parser.add_argument("--docs-query", default="", help="Optional docs retrieval query for Claude diagnosis.")
    parser.add_argument("--output", help="Optional path to save diagnosis JSON.")
    parser.add_argument("--fail-on-high", action="store_true", help="Exit 1 when the final risk level is high.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    missing = [path for path in args.logs if not Path(path).expanduser().exists()]
    if missing:
        print(f"日志文件不存在: {missing}", file=sys.stderr)
        return 2

    if args.facts:
        result = extract_rule_facts_from_paths(args.logs, tail_lines=args.tail_lines)
    else:
        if args.facts_output:
            facts = extract_rule_facts_from_paths(args.logs, tail_lines=args.tail_lines)
            Path(args.facts_output).write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"规则 facts JSON 已保存到: {args.facts_output}")
            if args.local_only:
                result = local_diagnosis(facts)
            else:
                docs_context = docs_context_for_query(args.docs_query or "SL100 gateway deviceShadow pushService MQTT 设备登录 日志")
                result = diagnose_with_claude(facts, docs_context=docs_context)
        else:
            result = analyze_paths(
                args.logs,
                tail_lines=args.tail_lines,
                use_ai=not args.local_only,
                docs_query=args.docs_query,
            )

    if args.output:
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"诊断 JSON 已保存到: {args.output}")

    if args.json or args.facts:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_report(result))

    return 1 if args.fail_on_high and result.get("risk_level") == "high" else 0


if __name__ == "__main__":
    raise SystemExit(main())
