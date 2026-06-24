"""
v3: 读取本地文件做代码审查
跑法: uv run main.py <文件路径>
示例: uv run main.py ~/some_project/app.py
"""
import os
import sys
import json
from dotenv import load_dotenv
load_dotenv(override=True)
os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)  # 见 v1 复盘
import anthropic

client = anthropic.Anthropic()

# ============================================================
# 1. 用 system prompt 锁死输出格式（最朴素的"结构化"做法）
# ============================================================
SYSTEM_PROMPT = """你是代码审查专家。

严格按以下 JSON schema 返回结果，不要输出任何额外文字、不要 markdown 代码块包裹：

{
  "summary": "一句话总结这段代码的整体质量",
  "score": 0到100的整数,
  "issues": [
    {
      "severity": "high" | "medium" | "low",
      "category": "bug" | "style" | "performance" | "security" | "docs",
      "line": 行号(整数) 或 null,
      "message": "问题描述",
      "suggestion": "修改建议"
    }
  ]
}

如果代码无问题，issues 返回空数组 []。"""


def review_code(code: str, max_retries: int = 2) -> dict:
    """让 Claude 审代码并返回结构化 dict。解析失败会重试。"""
    last_err = None
    for attempt in range(max_retries + 1):
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": f"审查这段 Python 代码：\n```python\n{code}\n```"}
            ],
        )
        text = msg.content[0].text.strip()

        # 防御：模型偶尔会用 ```json ... ``` 包裹，剥掉
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
            text = text.strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            last_err = e
            print(f"[尝试 {attempt + 1}/{max_retries + 1}] JSON 解析失败: {e}")
            print(f"原始返回前 200 字符: {text[:200]!r}")

    raise RuntimeError(f"重试 {max_retries} 次仍解析失败，最后错误: {last_err}")


def print_result(result: dict, file_path: str):
    """把审查结果格式化打印。从 v2 搬过来的，抽成函数方便复用。"""
    print(f"\n{'=' * 50}")
    print(f"文件: {file_path}")
    print(f"评分: {result['score']}/100")
    print(f"总结: {result['summary']}")
    print(f"问题数: {len(result['issues'])}")
    print("=" * 50)

    by_severity = {"high": [], "medium": [], "low": []}
    for issue in result["issues"]:
        by_severity.setdefault(issue["severity"], []).append(issue)

    for sev in ["high", "medium", "low"]:
        items = by_severity.get(sev, [])
        if not items:
            continue
        print(f"\n[{sev.upper()}] {len(items)} 个问题:")
        for it in items:
            line_info = f"行 {it['line']}" if it.get("line") else "全文"
            print(f"  · {line_info} ({it['category']}): {it['message']}")
            print(f"    → {it['suggestion']}")

    high_count = len(by_severity.get("high", []))
    if high_count > 0:
        print(f"\n❌ 发现 {high_count} 个高危问题")
    else:
        print("\n✅ 无高危问题")
    return high_count


# ============================================================
# v3 新增：从命令行参数读文件路径
# ============================================================
if __name__ == "__main__":
    # --- 1. 解析命令行参数 ---
    # sys.argv 是一个列表: ["main.py", "第1个参数", "第2个参数", ...]
    # Go 类比: os.Args = []string{"main.py", "第1个参数", ...}
    if len(sys.argv) < 2:
        print("用法: uv run main.py <文件路径>")
        print("示例: uv run main.py ~/some_project/app.py")
        sys.exit(1)

    file_path = sys.argv[1]

    # --- 2. 读文件 ---
    # Go 类比: data, err := os.ReadFile(filePath)
    if not os.path.exists(file_path):
        print(f"错误: 文件不存在 → {file_path}")
        sys.exit(1)

    with open(file_path, encoding="utf-8") as f:
        code = f.read()

    if not code.strip():
        print(f"错误: 文件是空的 → {file_path}")
        sys.exit(1)

    print(f"读取文件: {file_path} ({len(code)} 字符)")

    # --- 3. 调 API 审查 ---
    result = review_code(code)

    # --- 4. 打印结果 ---
    high_count = print_result(result, file_path)

    # --- 5. 可选：把 JSON 结果存到文件 ---
    output_path = file_path + ".review.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n详细 JSON 已保存到: {output_path}")

    sys.exit(1 if high_count > 0 else 0)
