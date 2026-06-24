"""
v4: Tool Use — 让 Claude 自己决定调哪个函数
跑法: uv run agent.py "帮我审查 test_sample.py"
      uv run agent.py "项目里有哪些 Python 文件？"
      uv run agent.py "审查项目里所有 Python 文件，给出总结"

v1-v3 是你指挥 AI 做事（"审查这段代码"）
v4 是 AI 自己决定做什么（你只说目标，它决定调哪些工具、按什么顺序）
这就是 Agent 的雏形。
"""
import os
import sys
import json
import glob
from dotenv import load_dotenv
load_dotenv(override=True)
os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
import anthropic

client = anthropic.Anthropic()


# ============================================================
# 第一部分：定义"工具"（就是普通的 Python 函数）
# ============================================================

def list_py_files(directory: str) -> list[str]:
    """列出目录下所有 .py 文件"""
    pattern = os.path.join(directory, "**", "*.py")
    return sorted(glob.glob(pattern, recursive=True))


def read_file(path: str) -> str:
    """读取文件内容"""
    with open(path, encoding="utf-8") as f:
        return f.read()


def count_lines(path: str) -> dict:
    """统计文件行数"""
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    total = len(lines)
    blank = sum(1 for l in lines if not l.strip())
    return {"total": total, "blank": blank, "code": total - blank}


# ============================================================
# 第二部分：告诉 Claude 有哪些工具可用（JSON Schema 描述）
#
# Go 类比：定义 gRPC 的 .proto 文件 / OpenAPI spec
# 不是给 Python 看的，是给 Claude 看的"说明书"
# ============================================================

TOOLS = [
    {
        "name": "list_py_files",
        "description": "列出指定目录下所有 Python 文件的路径（递归搜索子目录）",
        "input_schema": {
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "要搜索的目录路径，例如 '.' 表示当前目录"
                }
            },
            "required": ["directory"]
        }
    },
    {
        "name": "read_file",
        "description": "读取指定路径文件的完整内容",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径"
                }
            },
            "required": ["path"]
        }
    },
    {
        "name": "count_lines",
        "description": "统计文件的总行数、空行数、代码行数",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径"
                }
            },
            "required": ["path"]
        }
    },
]

# 工具名 → 函数的映射表，方便后面根据名字找到函数执行
# Go 类比: handlers := map[string]func(){...}
TOOL_FUNCTIONS = {
    "list_py_files": list_py_files,
    "read_file": read_file,
    "count_lines": count_lines,
}


# ============================================================
# 第三部分：Agent 循环（核心）
#
# v1-v3 的流程: 你发消息 → Claude 回文本 → 结束
# v4 的流程:    你发消息 → Claude 可能回文本，也可能要求调工具
#               → 你执行工具，把结果发回去 → Claude 继续思考
#               → 可能又要调工具... → 直到 Claude 觉得够了，回文本
# ============================================================

def run_agent(user_message: str):
    """Agent 主循环：发消息 → 工具调用循环 → 最终回复"""
    print(f"\n你: {user_message}\n")

    messages = [{"role": "user", "content": user_message}]

    # 循环：Claude 可能连续调用多个工具
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system="你是一个代码审查助手。你可以使用工具来查找和阅读代码文件，然后给出审查意见。用中文回复。",
            tools=TOOLS,
            messages=messages,
        )

        # ---------- 判断 Claude 想干什么 ----------

        # 情况 1: stop_reason == "end_turn" → Claude 说完了，退出循环
        if response.stop_reason == "end_turn":
            for block in response.content:
                if block.type == "text":
                    print(f"Claude: {block.text}")
            break

        # 情况 2: stop_reason == "tool_use" → Claude 想调工具
        if response.stop_reason == "tool_use":
            # 先把 Claude 的回复（可能包含文字+工具调用）追加到对话
            messages.append({"role": "assistant", "content": response.content})

            # 收集所有工具调用的结果
            tool_results = []
            for block in response.content:
                if block.type == "text" and block.text:
                    print(f"Claude(思考中): {block.text}")

                if block.type == "tool_use":
                    tool_name = block.name
                    tool_input = block.input
                    tool_id = block.id
                    print(f"  [调用工具] {tool_name}({json.dumps(tool_input, ensure_ascii=False)})")

                    # 在 TOOL_FUNCTIONS 里找到对应函数并执行
                    func = TOOL_FUNCTIONS.get(tool_name)
                    if func:
                        try:
                            result = func(**tool_input)
                            result_str = json.dumps(result, ensure_ascii=False, indent=2) if not isinstance(result, str) else result
                        except Exception as e:
                            result_str = f"工具执行出错: {e}"
                    else:
                        result_str = f"未知工具: {tool_name}"

                    print(f"  [工具返回] {result_str[:200]}{'...' if len(result_str) > 200 else ''}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": result_str,
                    })

            # 把所有工具结果一次性发回给 Claude
            messages.append({"role": "user", "content": tool_results})
            # 继续循环 → Claude 拿到工具结果后再决定下一步


# ============================================================
# 第四部分：程序入口
# ============================================================
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: uv run agent.py <你的指令>")
        print('示例: uv run agent.py "帮我审查 test_sample.py"')
        print('      uv run agent.py "项目里有哪些 Python 文件？挑最大的审查一下"')
        sys.exit(1)

    run_agent(sys.argv[1])
