# SL100 运维诊断 Agent Demo 指南

## 环境准备

```bash
cd /Users/jianfengxu/code-reviewer
uv sync
```

如果要调用 Claude API：

```bash
echo 'ANTHROPIC_API_KEY=your_api_key_here' > .env
```

不配置 API key 也可以运行本地规则、eval、Go 工具和 MCP smoke test。

## Demo 1：本地日志诊断

```bash
uv run sl100_log_agent.py samples/sl100_logs/device_login_failed.log --local-only
```

预期能看到：

- 风险等级：`high`
- incident：`device_login_failed`
- 服务：`gateway`
- 证据：`/v1/device/login`、`UuidInvalid`
- 建议：检查设备登录参数、uuid、Redis MQTT 用户写入

## Demo 2：查看规则 facts

```bash
uv run sl100_rules.py samples/sl100_logs/device_login_failed.log --summary
```

这个命令展示 M3 的核心：先用确定性规则提取事实，再让 AI 解释。

## Demo 3：日志 eval

```bash
uv run eval_sl100_logs.py
```

当前结果：

```text
用例通过率: 10/10 (100%)
检查通过率: 41/41 (100%)
```

## Demo 4：Tool Use Agent

先查看工具清单，不调用 Claude：

```bash
uv run sl100_agent.py --list-tools
```

如果 `.env` 已配置 API key：

```bash
uv run sl100_agent.py \
  "分析 samples/sl100_logs/device_login_failed.log 里的设备登录问题" \
  --trace-output sl100_trace_device_login.json \
  --max-turns 4
```

`sl100_trace_device_login.json` 会记录 Claude 调用了哪些工具、传了哪些参数、工具返回了什么。

## Demo 5：Go 日志分析服务

CLI 模式：

```bash
cd /Users/jianfengxu/code-reviewer/go-log-tools
go run . -file ../samples/sl100_logs/device_login_failed.log
go test ./...
```

HTTP 服务模式：

```bash
cd /Users/jianfengxu/code-reviewer/go-log-tools
go run .
```

另一个终端：

```bash
cd /Users/jianfengxu/code-reviewer
uv run python -c 'import json; from sl100_agent import go_analyze_log; print(json.dumps(go_analyze_log("samples/sl100_logs/device_login_failed.log"), ensure_ascii=False, indent=2))'
```

## Demo 6：RAG 文档问答

本地检索，不调用 Claude：

```bash
uv run sl100_docs_qa.py "设备 MQTT 连不上，应该看哪些服务和日志？" --local-only
```

检索 eval：

```bash
uv run eval_sl100_docs.py
```

当前结果：

```text
用例通过率: 4/4 (100%)
检查通过率: 16/16 (100%)
```

## Demo 7：MCP Server

本地 smoke test：

```bash
uv run scripts/test_sl100_mcp.py
```

手动启动：

```bash
uv run sl100_mcp_server.py
```

Claude Desktop 配置示例：

```bash
cat mcp/sl100-mcp-config.example.json
```

## 面试演示顺序

建议 5 分钟演示：

1. 跑 `sl100_log_agent.py --local-only` 展示能诊断日志。
2. 跑 `sl100_rules.py --summary` 展示规则 facts。
3. 跑 `eval_sl100_logs.py` 展示质量验证。
4. 跑 `sl100_agent.py --list-tools` 展示 Tool Use 工具设计。
5. 跑 `eval_sl100_docs.py` 展示 RAG 检索评测。
6. 讲 Go 服务和 MCP 扩展，不一定现场启动。

## 注意事项

- 不演示真实生产日志。
- 不展示 `.env`。
- 不展示真实服务器 IP、token、设备 SN。
- 面试时优先讲工程设计和验证结果，不要只讲“调用了 Claude”。
