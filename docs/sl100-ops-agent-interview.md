# SL100 运维诊断 Agent 面试讲稿

## 3 分钟项目介绍

我做了一个 SL100 IoT 运维诊断 Agent，用来辅助排查设备登录、MQTT 连接、WebSocket 推送、OTA、推送队列和数据库连接等问题。

这个项目不是普通聊天机器人，而是一个真实业务工具。核心思路是先用确定性规则从日志里提取 facts，比如服务名、错误数、request_id、uuid、incident 类型和时间线，再把这些 facts 交给 Claude 做归因和排查建议。这样能减少 token 成本，也能避免 LLM 直接读原始日志时产生误判。

我把项目分成几层：

- M1 是本地日志诊断 CLI，支持脱敏和结构化诊断。
- M2 是 golden log evals，用 10 条日志用例验证诊断准确率和误报。
- M3 是规则工具层，把日志事实提取成可复用 facts JSON。
- M4 是 Tool Use Agent，让 Claude 自己选择调用日志工具、文档工具和分析工具。
- M5 是 Go 日志分析服务，Python 负责编排，Go 负责稳定高性能解析。
- M6 是轻量 RAG 文档问答，把 SL100 架构和 MQTT/WebSocket 文档接入诊断上下文。
- M7 是 MCP Server，让 Claude Desktop / Claude Code 可以标准化调用这些本地工具。

当前自动化验证结果是：日志 eval 10/10，通过 41/41 个检查；文档检索 eval 4/4，通过 16/16 个检查；Go 工具服务有单元测试；MCP Server 有 smoke test。

这个项目体现了我对 AI Agent 应用开发的理解：LLM 不是替代所有代码，而是和确定性工具组合。规则层负责可靠事实，LLM 负责解释和决策，evals 负责防回归，MCP 负责把工具标准化接入 AI 客户端。

## 简历项目描述

**SL100 IoT 运维诊断 Agent**  
基于 Claude API / Python / Go 构建真实业务日志诊断 Agent，支持 SL100 多服务日志脱敏分析、设备链路故障定位、规则工具调用、RAG 文档检索和 MCP 工具暴露。设计 10 条 golden log evals 覆盖设备登录、MQTT、WebSocket、OTA、推送、数据库连接等场景，通过率 10/10；实现 Go HTTP 日志分析服务并由 Python Agent 编排调用；提供 MCP Server 使 Claude Desktop / Claude Code 可直接调用本地诊断工具。

技术栈：`Python`、`Go`、`Claude API`、`Tool Use`、`MCP`、`uv`、`Golden Evals`、`RAG`

## 面试官可能追问

### 为什么不直接把日志给 Claude？

直接给 Claude 有三类问题：

- 安全：原始日志可能有 token、IP、手机号、设备标识。
- 成本：日志很长，token 成本高。
- 准确性：LLM 容易被噪声干扰，且难以稳定复现。

所以我先做脱敏和规则 facts 提取，再让 Claude 基于 facts 做解释。

### M2 eval 的价值是什么？

eval 不是为了证明 demo 能跑，而是为了防止规则和 prompt 改动后引入误报或漏报。

例如我不只检查 `device_login_failed` 有没有命中，也检查不应该出现的 incident 有没有误报。这样能把“感觉能用”变成“有自动化质量门槛”。

### M3 和 M4 的区别是什么？

M3 是工具能力：我手动调用规则分析器，得到 facts JSON。

M4 是 Agent 编排：Claude 根据用户目标决定调用哪个工具、传什么参数、是否继续查文档或日志。

一句话：M3 是工具，M4 是会用工具的 Agent。

### Go 服务的价值是什么？

Go 服务不是为了炫技，而是发挥我的后端背景：

- 日志扫描、规则匹配和聚合适合用 Go 做高性能稳定工具。
- Python 更适合 Agent 编排和快速集成 Claude SDK。
- HTTP/JSON 边界清晰，后续可以把 Go 工具独立部署或给其他系统调用。

### RAG 在这个项目里解决什么？

日志只能说明“发生了什么”，文档能说明“这个服务负责什么、链路怎么走、应该查哪里”。

比如 MQTT 连不上，RAG 会检索到 `gateway`、`deviceShadow`、MQTT broker、WebSocket 等相关文档片段，再让 Claude 基于这些上下文回答排查路径。

### MCP 有什么意义？

MCP 把本地工具标准化。原来这些能力只能通过我自己的 CLI 或 Python Agent 调用；有了 MCP，Claude Desktop / Claude Code 这类客户端可以直接调用：

- `analyze_logs`
- `search_sl100_docs`
- `summarize_incident`
- `find_service_errors`

这让工具从“项目内部功能”变成“可接入 AI 客户端的能力”。

## 30 秒版本

我做了一个 SL100 IoT 运维诊断 Agent。它不是直接把日志丢给 LLM，而是先做日志脱敏和规则 facts 提取，再让 Claude 基于 facts 和 RAG 文档做诊断。项目包含 Tool Use Agent、Go 日志分析服务、MCP Server 和自动化 evals。当前日志 eval 10/10，文档检索 eval 4/4。这个项目证明我能把 AI Agent 技术落到真实业务工具里，而不是只会跑 API demo。

## STAR 版本

**Situation**：SL100 是多服务 IoT 后端，设备登录、MQTT、WebSocket、推送等问题排查需要跨日志和文档，人工定位成本高。

**Task**：构建一个可本地运行、可脱敏、可评测、可接入 AI 客户端的诊断 Agent。

**Action**：实现日志诊断 CLI、规则 facts 层、golden evals、Claude Tool Use Agent、Go 日志分析服务、RAG 文档问答和 MCP Server。

**Result**：形成完整求职项目证据链：10 条日志 eval 全通过，4 条文档检索 eval 全通过，Go 工具测试通过，MCP smoke test 通过，可用 README 和 Demo 命令复现。
