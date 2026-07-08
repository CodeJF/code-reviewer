"""
M6: Lightweight SL100 docs Q&A.

This is a dependency-free RAG-style entrypoint: retrieve relevant local doc
chunks by keyword, then optionally ask Claude to answer with citations.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from sl100_log_core import docs_context_for_query, init_anthropic_client, search_docs


def answer_with_claude(question: str, context: str) -> str:
    client = init_anthropic_client()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=(
            "你是 SL100 项目文档问答助手。只能基于给定 context 回答；"
            "信息不足时直接说明缺少资料。回答必须引用来源，格式如 [SL100服务架构与部署说明.md#3]。"
        ),
        messages=[{
            "role": "user",
            "content": f"问题：{question}\n\ncontext:\n{context}",
        }],
    )
    return response.content[0].text


def render_sources(chunks: list[dict[str, str]]) -> str:
    lines = [
        "=" * 60,
        "SL100 文档检索结果",
        "=" * 60,
    ]
    if not chunks:
        lines.append("未检索到相关文档片段。")
        return "\n".join(lines)

    for index, chunk in enumerate(chunks, start=1):
        lines.append(
            f"{index}. [{chunk['source']}#{chunk['chunk']}] "
            f"{chunk['title']} score={chunk['score']}"
        )
        preview = " ".join(chunk["text"].split())[:220]
        lines.append(f"   {preview}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Ask questions over local SL100 docs.")
    parser.add_argument("question")
    parser.add_argument("--local-only", action="store_true", help="Only print retrieved chunks; do not call Claude.")
    parser.add_argument("--max-chunks", type=int, default=5, help="Maximum retrieved chunks.")
    parser.add_argument("--json", action="store_true", help="Print retrieved chunks as JSON.")
    parser.add_argument("--output", help="Optional path to save retrieved chunks JSON.")
    args = parser.parse_args()

    chunks = search_docs(args.question, max_chunks=args.max_chunks)
    if args.output:
        payload = {"question": args.question, "chunks": chunks}
        Path(args.output).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"文档检索 JSON 已保存到: {args.output}")

    if args.json:
        print(json.dumps({"question": args.question, "chunks": chunks}, ensure_ascii=False, indent=2))
        return 0

    if args.local_only:
        print(render_sources(chunks))
        return 0

    context = docs_context_for_query(args.question, max_chunks=args.max_chunks)
    print(render_sources(chunks))
    print()
    print("=" * 60)
    print("Claude 回答")
    print("=" * 60)
    print(answer_with_claude(args.question, context))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
