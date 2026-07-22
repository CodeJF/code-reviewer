"""Unified command dispatcher that preserves the focused legacy CLIs."""
from __future__ import annotations

import sys
from collections.abc import Callable

from iot_ops_agent.agent import mcp_server, tools
from iot_ops_agent.cli import docs_qa, es_logs, log_agent, remote_logs, rules
from iot_ops_agent.diagnosis import diagnose
from iot_ops_agent.evaluation import docs, logs, product, real_cases, review_cases


HELP = """IoT Ops Agent command line

Usage:
  iot-ops diagnose <query> [options]
  iot-ops agent <request> [options]
  iot-ops logs analyze <paths...> [options]
  iot-ops logs facts <paths...> [options]
  iot-ops es <health|indices|search|count|analyze> [options]
  iot-ops remote <list|tail|search|analyze> [options]
  iot-ops docs ask <question> [options]
  iot-ops cases <collect|review|snapshot> [options]
  iot-ops eval <logs|docs|product|real> [options]
  iot-ops mcp serve

Run `iot-ops <command> --help` or a nested command with `--help` for details.
"""


def _run(entrypoint: Callable[[], int | None], args: list[str], label: str) -> int:
    original = sys.argv
    sys.argv = [label, *args]
    try:
        return int(entrypoint() or 0)
    finally:
        sys.argv = original


def _require_nested(args: list[str], usage: str) -> tuple[str, list[str]]:
    if not args or args[0] in {"-h", "--help"}:
        print(usage)
        raise SystemExit(0 if args else 2)
    return args[0], args[1:]


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in {"-h", "--help"}:
        print(HELP)
        return 0 if args else 2

    command, rest = args[0], args[1:]
    direct = {
        "diagnose": diagnose.main,
        "agent": tools.main,
        "es": es_logs.main,
        "remote": remote_logs.main,
        "cases": review_cases.main,
    }
    if command in direct:
        return _run(direct[command], rest, f"iot-ops {command}")

    if command == "logs":
        nested, nested_args = _require_nested(rest, "Usage: iot-ops logs <analyze|facts> [options]")
        entries = {"analyze": log_agent.main, "facts": rules.main}
        if nested not in entries:
            print(f"Unknown logs command: {nested}", file=sys.stderr)
            return 2
        return _run(entries[nested], nested_args, f"iot-ops logs {nested}")

    if command == "docs":
        nested, nested_args = _require_nested(rest, "Usage: iot-ops docs ask <question> [options]")
        if nested != "ask":
            print(f"Unknown docs command: {nested}", file=sys.stderr)
            return 2
        return _run(docs_qa.main, nested_args, "iot-ops docs ask")

    if command == "eval":
        nested, nested_args = _require_nested(rest, "Usage: iot-ops eval <logs|docs|product|real> [options]")
        entries = {"logs": logs.main, "docs": docs.main, "product": product.main, "real": real_cases.main}
        if nested not in entries:
            print(f"Unknown eval command: {nested}", file=sys.stderr)
            return 2
        return _run(entries[nested], nested_args, f"iot-ops eval {nested}")

    if command == "mcp":
        nested, nested_args = _require_nested(rest, "Usage: iot-ops mcp serve")
        if nested != "serve" or nested_args:
            print("Usage: iot-ops mcp serve", file=sys.stderr)
            return 2
        return mcp_server.main()

    print(f"Unknown command: {command}\n", file=sys.stderr)
    print(HELP, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
