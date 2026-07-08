# AI Code Reviewer + SL100 Ops Diagnosis Agent

基于 Claude API 的 AI 应用学习项目。前半部分是代码审查 Agent，后半部分是贴近真实业务的 **SL100 IoT 运维诊断 Agent**。

## 功能

- **结构化审查** (`main.py`) — 读取任意 Python 文件，输出评分 + 问题分类 + 修改建议（JSON 格式）
- **Agent 模式** (`agent.py`) — 给一句自然语言指令，AI 自主决定扫描哪些文件、按什么顺序审查
- **自动化评测** (`eval.py`) — 5 个 golden set 用例，验证 AI 审查质量（通过率 / 召回率）
- **SL100 日志诊断** (`sl100_log_agent.py`) — 读取 SL100 日志，脱敏后输出结构化诊断
- **SL100 规则分析器** (`sl100_rules.py`) — 不调用 AI，先提取错误级别、时间线、message_id、uuid 和 incident facts
- **SL100 Tool Use Agent** (`sl100_agent.py`) — Claude 自主调用日志和文档工具定位问题
- **SL100 Golden Log Evals** (`eval_sl100_logs.py`) — 10 条脱敏日志用例，验证诊断覆盖率
- **Go 日志分析服务** (`go-log-tools/`) — Go HTTP 服务解析日志，Python Agent 可调用
- **MCP Server** (`sl100_mcp_server.py`) — 将 SL100 诊断能力暴露给支持 MCP 的客户端

## 快速开始

```bash
# 1. 安装依赖
uv sync

# 2. 设置 API Key
echo 'ANTHROPIC_API_KEY=your_api_key_here' > .env

# 3. 审查单个文件
uv run main.py test_sample.py

# 4. Agent 模式（AI 自主决策）
uv run agent.py "项目里有哪些 Python 文件？挑最有问题的审查一下"

# 5. 跑评测
uv run eval.py
```

## SL100 真实业务工具

### 本地日志诊断

默认会调用 Claude API：

```bash
uv run sl100_log_agent.py /Users/jianfengxu/Desktop/SL100_Service/.local-run/gateway.log
```

不想消耗 API credit 时，用本地规则诊断：

```bash
uv run sl100_log_agent.py samples/sl100_logs/device_login_failed.log --local-only
```

只看确定性 facts：

```bash
uv run sl100_log_agent.py samples/sl100_logs/device_login_failed.log --facts
```

### 规则工具 + AI 组合诊断

M3 的核心是先让确定性规则提取 facts，再把 facts 交给 Claude 解释。

只运行规则层，不调用 Claude：

```bash
uv run sl100_rules.py samples/sl100_logs/device_login_failed.log --summary
uv run sl100_rules.py samples/sl100_logs/device_login_failed.log --output sl100_facts_device_login.json
```

组合模式：先保存规则 facts，再输出诊断报告：

```bash
uv run sl100_log_agent.py samples/sl100_logs/device_login_failed.log \
  --facts-output sl100_facts_device_login.json \
  --local-only
```

如果 `.env` 里已经配置 `ANTHROPIC_API_KEY`，去掉 `--local-only` 就会把规则 facts 交给 Claude 生成诊断 JSON：

```bash
uv run sl100_log_agent.py samples/sl100_logs/device_login_failed.log \
  --facts-output sl100_facts_device_login.json
```

### SL100 日志评测

默认不调用 Claude：

```bash
uv run eval_sl100_logs.py
```

需要评估 Claude 输出时：

```bash
uv run eval_sl100_logs.py --use-ai
```

### SL100 Tool Use Agent

先查看 Agent 可以调用哪些工具，不消耗 API credit：

```bash
uv run sl100_agent.py --list-tools
```

运行真正的 Tool Use Agent，会调用 Claude API：

```bash
uv run sl100_agent.py \
  "分析 samples/sl100_logs/device_login_failed.log 里的设备登录问题" \
  --trace-output sl100_trace_device_login.json
```

`sl100_trace_device_login.json` 会保存 Claude 调用过哪些工具、传入什么参数、工具返回什么结果。

### SL100 文档问答

M6 的目标是让 Agent 检索 SL100 架构、MQTT/WebSocket、服务职责文档，再基于检索片段回答。

只看本地检索结果，不调用 Claude：

```bash
uv run sl100_docs_qa.py "设备 MQTT 连不上，应该看哪些服务和日志？" --local-only
```

保存检索 chunks，方便检查 RAG 输入：

```bash
uv run sl100_docs_qa.py "设备 MQTT 连不上，应该看哪些服务和日志？" \
  --local-only \
  --output sl100_docs_mqtt_query.json
```

评测文档检索质量，不调用 Claude：

```bash
uv run eval_sl100_docs.py
```

如果 `.env` 里已经配置 `ANTHROPIC_API_KEY`，去掉 `--local-only` 会让 Claude 基于检索片段回答：

```bash
uv run sl100_docs_qa.py "设备 MQTT 连不上，应该看哪些服务和日志？"
```

### Go 日志分析服务

M5 的目标是把日志解析能力做成 Go 工具服务，再让 Python Agent 调用。

先单独运行 Go CLI，不启动 HTTP 服务：

```bash
cd go-log-tools
go run . -file ../samples/sl100_logs/device_login_failed.log
go test ./...
```

再启动 HTTP 服务：

```bash
cd go-log-tools
go run .

# 另一个终端
cd ..
uv run python -c 'import json; from sl100_agent import go_analyze_log; print(json.dumps(go_analyze_log("samples/sl100_logs/device_login_failed.log"), ensure_ascii=False, indent=2))'
```

如果要让 Claude 自己决定调用 Go 工具，保持 Go 服务运行，再执行：

```bash
uv run sl100_agent.py \
  "用 go_analyze_log 分析 samples/sl100_logs/device_login_failed.log，并解释设备登录失败原因" \
  --trace-output sl100_trace_go_device_login.json
```

### MCP Server

```bash
uv run sl100_mcp_server.py
```

MCP tools:

- `analyze_logs`
- `search_sl100_docs`
- `summarize_incident`
- `find_service_errors`

## Go 工具服务

Agent 可调用 Go 编写的静态分析服务，实现跨语言工具链：

```bash
# 启动 Go 分析服务
cd go-tools && go run main.go

# Agent 自动调用 Go 服务分析代码
cd .. && uv run agent.py "用 go_analyze 工具分析 test_sample.py"
```

Go 服务提供的分析能力：函数提取（名称/行数/参数/docstring）、import 分析、行数统计、安全问题检测、风格检查、复杂度评估。

## 项目结构

```
code-reviewer/
├── main.py          # v1-v3: API 调用 → 结构化输出 → CLI 文件审查
├── agent.py         # v4: Tool Use Agent（AI 自主编排工具调用）
├── eval.py          # v5: 自动化评测框架
├── sl100_log_core.py     # SL100 日志脱敏、规则分析、Claude 诊断共享核心
├── sl100_log_agent.py    # SL100 日志诊断 CLI
├── sl100_rules.py        # SL100 规则 facts 提取 CLI
├── sl100_agent.py        # SL100 Tool Use Agent
├── eval_sl100_logs.py    # SL100 golden log evals
├── eval_sl100_docs.py    # SL100 docs retrieval evals
├── sl100_docs_qa.py      # 轻量 RAG 文档问答
├── sl100_mcp_server.py   # MCP stdio server
├── go-tools/        # Go 静态分析服务（跨语言工具链）
│   ├── main.go
│   └── go.mod
├── go-log-tools/    # Go 日志分析服务
├── test_sample.py   # 测试用代码样本
├── samples/sl100_logs/
├── evals/sl100_log_cases.json
├── evals/sl100_doc_cases.json
├── pyproject.toml   # 项目配置
└── .env             # API Key（不提交到 Git）
```

## 技术栈

- **LLM**: Claude API (claude-sonnet-4-6)
- **语言**: Python 3.13 + Go
- **包管理**: uv
- **SDK**: anthropic
- **跨语言通信**: HTTP (Go service ↔ Python agent)

## 版本演进

| 版本 | 文件 | 核心能力 |
|------|------|---------|
| v1 | main.py | API 调用，文本输出 |
| v2 | main.py | 结构化 JSON 输出 + prompt schema 约束 + 失败重试 |
| v3 | main.py | CLI 读文件 + 结果持久化 |
| v4 | agent.py | Tool Use Agent 循环（AI 自主决定调用顺序） |
| v5 | eval.py | Golden set 评测，13 项检查 100% 通过 |
| v6 | go-tools/ + agent.py | Go 静态分析服务，跨语言 Agent 工具链 |
| M1 | sl100_log_agent.py | SL100 本地日志诊断 + 脱敏 |
| M2 | eval_sl100_logs.py | 10 条 SL100 golden log evals |
| M3 | sl100_rules.py + sl100_log_agent.py | 规则工具 + AI 组合诊断 |
| M4 | sl100_agent.py | SL100 Tool Use Agent |
| M5 | go-log-tools/ | Go 日志分析服务 |
| M6 | sl100_docs_qa.py + eval_sl100_docs.py | 轻量 RAG 文档问答 + 检索评测 |
| M7 | sl100_mcp_server.py | MCP Server |

## 安全约束

- 不提交真实 SL100 日志。
- 不提交 `.env`、API key、token、手机号、邮箱、设备 SN、真实公网 IP。
- `samples/sl100_logs/` 和 `evals/sl100_log_cases.json` 只放脱敏或 synthetic 样本。
- 默认先分析本地日志，不直接连接线上服务器。
