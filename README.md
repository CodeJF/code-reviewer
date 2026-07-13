# AI Code Reviewer + SL100 Ops Diagnosis Agent

基于 Claude API 的 AI 应用学习项目。前半部分是代码审查 Agent，后半部分是贴近真实业务的 **SL100 IoT 运维诊断 Agent**。

## 功能

- **结构化审查** (`main.py`) — 读取任意 Python 文件，输出评分 + 问题分类 + 修改建议（JSON 格式）
- **Agent 模式** (`agent.py`) — 给一句自然语言指令，AI 自主决定扫描哪些文件、按什么顺序审查
- **自动化评测** (`eval.py`) — 5 个 golden set 用例，验证 AI 审查质量（通过率 / 召回率）
- **SL100 日志诊断** (`sl100_log_agent.py`) — 读取 SL100 日志，脱敏后输出结构化诊断
- **SL100 规则分析器** (`sl100_rules.py`) — 不调用 AI，先提取错误级别、时间线、message_id、uuid 和 incident facts
- **SL100 ES 日志查询** (`sl100_es_logs.py`) — 通过 `sl100-93` 只读查询 Elasticsearch 真实日志
- **SL100 远程文件日志查询** (`sl100_remote_logs.py`) — 通过 SSH 只读读取白名单服务日志，作为 ES 补充数据源
- **SL100 产品化排障 CLI** (`sl100_diagnose.py`) — 一句话报障 → 查询计划 → ES/remote 证据 → 统一排障报告
- **SL100 Tool Use Agent** (`sl100_agent.py`) — Claude 自主调用日志和文档工具定位问题
- **SL100 Golden Log Evals** (`eval_sl100_logs.py`) — 10 条脱敏日志用例，验证诊断覆盖率
- **Go 日志分析服务** (`go-log-tools/`) — Go HTTP 服务解析日志，Python Agent 可调用
- **MCP Server** (`sl100_mcp_server.py`) — 将 SL100 诊断能力暴露给支持 MCP 的客户端

## 求职展示入口

SL100 运维诊断 Agent 的求职包装材料：

- [项目说明](docs/sl100-ops-agent-case-study.md)：真实问题、架构设计、关键取舍和安全策略。
- [Demo 指南](docs/sl100-ops-agent-demo.md)：本地可复现命令、预期输出和演示顺序。
- [面试讲稿](docs/sl100-ops-agent-interview.md)：3 分钟介绍、简历描述、常见追问和 STAR 版本。
- [ES 日志接入](docs/sl100-es-logs.md)：真实日志系统接入、查询命令、时间窗口和安全边界。

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

### 产品化一键排障

这是个人排障的主入口。它会先生成确定性查询计划，再按 `Elasticsearch -> 远程文件日志 fallback` 查询，并输出统一 Incident Report。没有给时间时，先查最近 2 小时；没有明确证据才扩展到今天全天。

先看查询计划，不访问服务器：

```bash
uv run sl100_diagnose.py \
  "测试说 2026-07-09 上午 9 点多 deviceShadow websocket 异常" \
  --dry-run
```

执行真实排障并输出中文报告：

```bash
uv run sl100_diagnose.py \
  "测试说 2026-07-09 上午 9 点多 deviceShadow websocket 异常"
```

保存结构化报告：

```bash
uv run sl100_diagnose.py \
  "测试说 2026-07-09 上午 9 点多 deviceShadow websocket 异常" \
  --json \
  --output sl100_incident_deviceShadow_websocket.json
```

统一报告字段：

```text
incident_id / query / time_window / services / data_sources / evidence
timeline / root_cause / result_status / confidence / risk_level / next_actions
redaction_status / query_attempts
```

`result_status` 的含义：`actionable`（找到异常证据）、`no_evidence`（当前范围没有证据）、`data_unavailable`（日志数据不可用）、`safety_blocked`（无法安全展示命中内容）。后面三种状态都不能解释成“业务没有问题”。

### 配置

默认配置在代码里，示例文件是：

```bash
cat configs/sl100.example.json
```

如需覆盖 host、索引或远程日志路径，复制为本地私有配置：

```bash
cp configs/sl100.example.json configs/sl100.local.json
```

`configs/sl100.local.json` 已被 `.gitignore` 忽略。也可以通过环境变量指定：

```bash
SL100_CONFIG=/path/to/sl100.local.json uv run sl100_diagnose.py "今天 gateway 有没有 error"
```

### 真实日志系统查询

`sl100-93` 上运行 Elasticsearch + Kibana。真实排障优先查 ES，再按需回到服务器文件日志。

检查 ES 连接：

```bash
uv run sl100_es_logs.py health
```

查看某天 SL100 日志索引：

```bash
uv run sl100_es_logs.py indices --date 2026-07-09
```

按服务、关键词、时间窗口搜索真实日志：

```bash
uv run sl100_es_logs.py search \
  --service deviceShadow \
  --keyword websocket \
  --from "2026-07-09 09:00" \
  --to "2026-07-09 10:00" \
  --size 20
```

不调用 Claude，直接用规则层诊断 ES 日志：

```bash
uv run sl100_es_logs.py analyze \
  --service deviceShadow \
  --keyword websocket \
  --from "2026-07-09 09:00" \
  --to "2026-07-09 10:00" \
  --output sl100_incident_es_deviceShadow.json
```

真实场景的 Agent 入口：

```bash
uv run sl100_agent.py "测试说今天上午 9 点多 deviceShadow websocket 异常，帮我查日志"
uv run sl100_agent.py "查 gateway 最近一次 error，分析可能原因"
```

当前 ES 服务映射：

```text
gateway      -> api-gateway-YYYY-MM-DD
deviceShadow -> api-device-shadow-YYYY-MM-DD
pushService  -> api-push-service-YYYY-MM-DD
access       -> api-access-YYYY-MM-DD
```

时间参数默认按 `Asia/Shanghai` 理解，查询 ES 时自动转成 UTC `@timestamp`。

ES 保持原始日志，不需要修改 Filebeat 或索引。诊断工具只在本机内存读取原始响应；终端输出、JSON 报告、Agent trace 和发给模型的内容均经过脱敏，不保存原始 ES 日志。

### 远程文件日志 fallback

当 ES 没有某个服务、采集有延迟，或需要看 `std_err.log` / `srd_out.log` 时，用远程文件日志作为补充。

列出白名单日志：

```bash
uv run sl100_remote_logs.py list
```

读取某个服务文件日志尾部：

```bash
uv run sl100_remote_logs.py tail --service gateway --log error --tail-lines 100
uv run sl100_remote_logs.py tail --service pushService --log stderr --tail-lines 100
```

不调用 Claude，直接分析远程文件日志：

```bash
uv run sl100_remote_logs.py analyze --service AdminService --logs error,stderr --tail-lines 300
```

远程文件日志只允许访问代码里的白名单路径，不接受任意服务器路径。

### 真实案例质量评测

没有历史工单时，用真实 ES 日志建立自己的评测集：

```bash
uv run sl100_review_cases.py collect --since-days 30 --per-service 10
uv run sl100_review_cases.py review
uv run eval_sl100_real_cases.py
uv run eval_sl100_real_cases.py --enforce-gates
```

`review` 一次展示一条脱敏日志和工具结论，你只需要选择“真实故障 / 正常行为 / 证据不足”。候选和标签保存在 Git 忽略的 `.sl100/` 与 `evals/sl100_real_cases.local.jsonl`，只保存查询信息、ES 文档 ID、证据指纹、同类异常签名和你的判断，不保存原始日志正文；同一服务内仅保留一个同类异常候选，避免重复日志扭曲评测。

### 团队工作台

团队版把现有受控诊断能力封装成 Web 工作台：本地账号与管理员邀请、管理员/值班/只读角色、人工确认建事件、审计记录和可选飞书通知。部署前先按 [团队部署说明](docs/sl100-team-deployment.md) 配置 HTTPS、首位管理员与仅部署服务器可读的日志凭据。

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

M7 的目标是把 SL100 工具标准化成 MCP Server，让 Claude Desktop / Claude Code 等 MCP 客户端直接调用。

本地 smoke test，不调用 Claude：

```bash
uv run scripts/test_sl100_mcp.py
```

手动启动 MCP stdio server：

```bash
uv run sl100_mcp_server.py
```

MCP tools:

- `analyze_logs`
- `search_sl100_docs`
- `summarize_incident`
- `find_service_errors`
- `search_es_logs`
- `analyze_es_logs`
- `summarize_es_incident`
- `list_remote_log_files`
- `analyze_remote_service_log`

Claude Desktop 配置示例：

```bash
cat mcp/sl100-mcp-config.example.json
```

配置后，MCP 客户端可以调用本地工具分析日志、检索 SL100 文档和总结 incident。

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
├── sl100_es.py           # sl100-93 Elasticsearch 只读查询核心
├── sl100_es_logs.py      # Elasticsearch 日志查询 CLI
├── sl100_remote.py       # 远程服务器文件日志只读 fallback
├── sl100_remote_logs.py  # 远程文件日志查询 CLI
├── sl100_planner.py      # 自然语言报障 -> 确定性查询计划
├── sl100_incident.py     # 统一 Incident Report 和渲染
├── sl100_diagnose.py     # 产品化一键排障 CLI
├── sl100_agent.py        # SL100 Tool Use Agent
├── eval_sl100_logs.py    # SL100 golden log evals
├── eval_sl100_docs.py    # SL100 docs retrieval evals
├── sl100_docs_qa.py      # 轻量 RAG 文档问答
├── sl100_mcp_server.py   # MCP stdio server
├── scripts/test_sl100_mcp.py # MCP server smoke test
├── mcp/sl100-mcp-config.example.json
├── docs/sl100-ops-agent-*.md # 求职展示材料
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
| M8 | docs/sl100-ops-agent-*.md | 求职包装、Demo 指南、面试讲稿 |

## 安全约束

- 不提交真实 SL100 日志。
- 不提交 `.env`、API key、token、手机号、邮箱、设备 SN、真实公网 IP。
- `samples/sl100_logs/` 和 `evals/sl100_log_cases.json` 只放脱敏或 synthetic 样本。
- 默认先分析本地日志，不直接连接线上服务器。
