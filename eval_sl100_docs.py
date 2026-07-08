"""
M6: Retrieval evals for SL100 docs Q&A.

This eval checks the local retrieval layer only. It does not call Claude.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from sl100_log_core import search_docs


def contains_text(haystack: str, needle: str) -> bool:
    return needle.lower() in haystack.lower()


def run_checks(case: dict, chunks: list[dict[str, str]]) -> list[dict]:
    expected = case["expected"]
    combined_sources = "\n".join(chunk.get("source", "") for chunk in chunks)
    combined_text = "\n".join(
        f"{chunk.get('source', '')}\n{chunk.get('title', '')}\n{chunk.get('text', '')}"
        for chunk in chunks
    )
    checks = []

    source_matches = [
        source
        for source in expected.get("sources", [])
        if contains_text(combined_sources, source)
    ]
    checks.append({
        "name": "has_expected_source",
        "passed": bool(source_matches),
        "reason": f"命中 sources={source_matches}, 实际 sources={sorted({c.get('source', '') for c in chunks})}",
    })

    for term in expected.get("terms", []):
        checks.append({
            "name": f"has_term:{term}",
            "passed": contains_text(combined_text, term),
            "reason": f"检索内容中{'包含' if contains_text(combined_text, term) else '缺少'} {term}",
        })

    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate SL100 docs retrieval quality.")
    parser.add_argument("--cases", default="evals/sl100_doc_cases.json")
    parser.add_argument("--output", default="sl100_doc_eval_results.json")
    parser.add_argument("--max-chunks", type=int, default=5)
    args = parser.parse_args()

    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    results = []
    passed_cases = 0
    passed_checks = 0
    total_checks = 0

    print(f"开始 SL100 文档检索评测，共 {len(cases)} 个用例\n")

    for index, case in enumerate(cases, start=1):
        chunks = search_docs(case["query"], max_chunks=args.max_chunks)
        checks = run_checks(case, chunks)
        case_passed = all(check["passed"] for check in checks)
        total_checks += len(checks)
        passed_checks += sum(1 for check in checks if check["passed"])
        if case_passed:
            passed_cases += 1

        print(f"[{index}/{len(cases)}] {case['query']}")
        for check in checks:
            status = "PASS" if check["passed"] else "FAIL"
            print(f"  [{status}] {check['name']} - {check['reason']}")
        print()

        results.append({
            "id": case["id"],
            "query": case["query"],
            "status": "pass" if case_passed else "fail",
            "checks": checks,
            "top_sources": [
                {
                    "source": chunk.get("source"),
                    "chunk": chunk.get("chunk"),
                    "title": chunk.get("title"),
                    "score": chunk.get("score"),
                }
                for chunk in chunks
            ],
        })

    print("=" * 60)
    print(f"用例通过率: {passed_cases}/{len(cases)} ({passed_cases / len(cases) * 100:.0f}%)")
    print(f"检查通过率: {passed_checks}/{total_checks} ({passed_checks / total_checks * 100:.0f}%)")
    print("=" * 60)

    Path(args.output).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n详细结果已保存到: {args.output}")

    return 0 if passed_cases == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
