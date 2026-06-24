# AI Code Reviewer

基于 Claude API 的智能代码审查工具。支持结构化 JSON 输出、Tool Use Agent 自主审查、自动化评测。

## 功能

- **结构化审查** (`main.py`) — 读取任意 Python 文件，输出评分 + 问题分类 + 修改建议（JSON 格式）
- **Agent 模式** (`agent.py`) — 给一句自然语言指令，AI 自主决定扫描哪些文件、按什么顺序审查
- **自动化评测** (`eval.py`) — 5 个 golden set 用例，验证 AI 审查质量（通过率 / 召回率）

## 快速开始

```bash
# 1. 安装依赖
uv sync

# 2. 设置 API Key
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env

# 3. 审查单个文件
uv run main.py test_sample.py

# 4. Agent 模式（AI 自主决策）
uv run agent.py "项目里有哪些 Python 文件？挑最有问题的审查一下"

# 5. 跑评测
uv run eval.py
```

## 项目结构

```
code-reviewer/
├── main.py          # v1-v3: API 调用 → 结构化输出 → CLI 文件审查
├── agent.py         # v4: Tool Use Agent（AI 自主编排工具调用）
├── eval.py          # v5: 自动化评测框架
├── test_sample.py   # 测试用代码样本
├── pyproject.toml   # 项目配置
└── .env             # API Key（不提交到 Git）
```

## 技术栈

- **LLM**: Claude API (claude-sonnet-4-6)
- **语言**: Python 3.13
- **包管理**: uv
- **SDK**: anthropic

## 版本演进

| 版本 | 文件 | 核心能力 |
|------|------|---------|
| v1 | main.py | API 调用，文本输出 |
| v2 | main.py | 结构化 JSON 输出 + prompt schema 约束 + 失败重试 |
| v3 | main.py | CLI 读文件 + 结果持久化 |
| v4 | agent.py | Tool Use Agent 循环（AI 自主决定调用顺序） |
| v5 | eval.py | Golden set 评测，13 项检查 100% 通过 |
