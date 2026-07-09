"""
CLI for querying the SL100 Elasticsearch logging stack on sl100-93.

Examples:
  uv run sl100_es_logs.py health
  uv run sl100_es_logs.py indices --date 2026-07-09
  uv run sl100_es_logs.py search --service gateway --keyword error --date 2026-07-09
  uv run sl100_es_logs.py analyze --service deviceShadow --keyword websocket --from "2026-07-09 09:00" --to "2026-07-09 10:00"
"""
from __future__ import annotations

import argparse
import json
import sys

from sl100_es import (
    DEFAULT_ERROR_QUERY,
    analyze_logs,
    count_logs,
    es_health,
    list_indices,
    search_logs,
)
from sl100_incident import build_incident_report, render_incident_report


def _add_time_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--date", dest="date_text", default="", help="Local date in Asia/Shanghai, e.g. 2026-07-09.")
    parser.add_argument("--from", dest="from_text", default="", help="Local start time, e.g. '2026-07-09 09:00'.")
    parser.add_argument("--to", dest="to_text", default="", help="Local end time, e.g. '2026-07-09 10:00'.")
    parser.add_argument("--around", dest="around_text", default="", help="Local center time, e.g. '2026-07-09 09:20'.")
    parser.add_argument("--around-minutes", type=int, default=10, help="Minutes before/after --around.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Query SL100 Elasticsearch logs through sl100-93 SSH.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("health", help="Check Elasticsearch cluster info.")

    indices = subparsers.add_parser("indices", help="List SL100 api-* indices.")
    indices.add_argument("--date", dest="date_text", default="", help="Filter index date, e.g. 2026-07-09.")
    indices.add_argument("--service", default="", help="Optional service: gateway, deviceShadow, pushService, access.")

    count = subparsers.add_parser("count", help="Count logs matching a service/time/keyword query.")
    count.add_argument("--service", required=True, help="Service: gateway, deviceShadow, pushService, access.")
    count.add_argument("--keyword", default=DEFAULT_ERROR_QUERY, help="Keyword or simple query string.")
    _add_time_args(count)

    search = subparsers.add_parser("search", help="Search redacted ES logs.")
    search.add_argument("--service", required=True, help="Service: gateway, deviceShadow, pushService, access.")
    search.add_argument("--keyword", default="", help="Keyword or simple query string.")
    search.add_argument("--size", type=int, default=20, help="Max hits to return, capped at 200.")
    _add_time_args(search)

    analyze = subparsers.add_parser("analyze", help="Analyze ES logs with local rules by default.")
    analyze.add_argument("--service", required=True, help="Service: gateway, deviceShadow, pushService, access.")
    analyze.add_argument("--keyword", default=DEFAULT_ERROR_QUERY, help="Keyword or simple query string.")
    analyze.add_argument("--size", type=int, default=80, help="Max hits to analyze, capped at 200.")
    analyze.add_argument("--use-ai", action="store_true", help="Call Claude after deterministic facts are extracted.")
    analyze.add_argument("--json", action="store_true", help="Print raw JSON instead of a report.")
    analyze.add_argument("--output", help="Optional path to save unified incident report JSON.")
    _add_time_args(analyze)

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.command == "health":
        print(json.dumps(es_health(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "indices":
        print(json.dumps(list_indices(date_text=args.date_text, service=args.service), ensure_ascii=False, indent=2))
        return 0

    if args.command == "count":
        result = count_logs(
            service=args.service,
            keyword=args.keyword,
            date_text=args.date_text,
            from_text=args.from_text,
            to_text=args.to_text,
            around_text=args.around_text,
            around_minutes=args.around_minutes,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "search":
        result = search_logs(
            service=args.service,
            keyword=args.keyword,
            date_text=args.date_text,
            from_text=args.from_text,
            to_text=args.to_text,
            around_text=args.around_text,
            around_minutes=args.around_minutes,
            size=args.size,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "analyze":
        result = analyze_logs(
            service=args.service,
            keyword=args.keyword,
            date_text=args.date_text,
            from_text=args.from_text,
            to_text=args.to_text,
            around_text=args.around_text,
            around_minutes=args.around_minutes,
            size=args.size,
            use_ai=args.use_ai,
        )
        report = build_incident_report(
            query=f"{args.service} {args.keyword}",
            analysis=result,
            plan={
                "services": [args.service],
                "date": args.date_text,
                "from_time": args.from_text,
                "to_time": args.to_text,
                "around": args.around_text,
            },
        )
        if args.output:
            from pathlib import Path
            Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"排障报告已保存到: {args.output}", file=sys.stderr)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(render_incident_report(report))
        return 0

    raise AssertionError(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
