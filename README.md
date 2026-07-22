# IoT Ops Agent

IoT Ops Agent 是一个受控、可评测、可回退的 IoT 运维诊断平台。它把自然语言报障转换成只读查询计划，从 Elasticsearch、白名单日志文件和内部文档中提取脱敏证据，最终生成可供人工确认的 Incident Report。

项目包含团队工作台、确定性诊断、受控 Tool Use Agent、MCP Server、离线评测，以及 PostgreSQL、Redis/RQ 和 Alembic 支撑的完整运行链路。

## 快速开始

### 1. 准备环境

本地运行只需要：

- macOS 或 Linux
- Docker Desktop，或 Docker Engine + Docker Compose v2

不需要预先安装 Python，也不需要复制 `.env`。

### 2. 启动完整环境

```bash
./bin/iotops up
```

该命令会自动完成镜像构建、数据库迁移，并启动以下服务：

| 服务 | 用途 |
| --- | --- |
| PostgreSQL | 团队数据与审计记录 |
| Redis / RQ | 异步任务队列 |
| API | FastAPI 团队工作台 |
| Worker | 执行诊断任务 |
| Reconciler | 恢复异常中断的队列任务 |

命令只有在迁移成功、服务正常运行且 API 就绪后才会返回成功。

### 3. 打开工作台

访问 [http://127.0.0.1:8780](http://127.0.0.1:8780)。开发环境使用本地认证和匿名示例数据源，所有端口默认仅绑定本机回环地址。

## 日常操作

| 命令 | 作用 |
| --- | --- |
| `./bin/iotops status` | 查看迁移、容器和 API 健康状态 |
| `./bin/iotops logs` | 跟踪全部服务日志 |
| `./bin/iotops logs api` | 只跟踪指定服务日志 |
| `./bin/iotops restart` | 重启完整环境 |
| `./bin/iotops down` | 停止服务并保留数据库数据 |
| `./bin/iotops test` | 在开发镜像中执行完整验证 |
| `./bin/iotops clean --dry-run` | 预览可安全清理的生成物 |
| `./bin/iotops reset --yes` | 删除本地容器和数据卷 |

需要在宿主机断点调试或热更新时，运行：

```bash
./bin/iotops debug
```

该模式额外要求本机安装 Python 3.13 和 `uv`。

## 一次诊断如何完成

```text
自然语言报障 → 查询计划 → 人工批准 → 只读工具执行 → 证据校验 → Incident Report
```

系统不会收到问题后立即扩大范围查询。每次诊断都会先确定服务、时间窗口、数据源、关键词和执行预算，再由批准者选择确定性规则模式或 AI 辅助模式。

AI 辅助模式只能调用计划允许的只读工具。模型输出的根因必须引用真实 `evidence_id`；如果工具越界、预算耗尽、模型失败或证据引用无效，系统会自动回退到确定性报告。

## 使用命令行工具

需要直接开发或调用诊断能力时，先安装项目依赖：

```bash
uv sync --frozen --all-groups
```

统一入口为 `iot-ops`：

```bash
# 生成查询计划，不访问数据源
uv run iot-ops diagnose "今天上午 9 点设备连接服务异常" --dry-run

# 分析本地日志
uv run iot-ops logs analyze samples/sl100_logs/device_login_failed.log --local-only

# 查询数据源和内部文档
uv run iot-ops es health
uv run iot-ops remote list
uv run iot-ops docs ask "MQTT 设备无法连接时先检查什么" --local-only

# 执行评测或启动 MCP Server
uv run iot-ops eval logs
uv run iot-ops eval docs
uv run iot-ops eval product
uv run iot-ops mcp serve
```

原有能力仍提供 kebab-case 兼容命令，例如 `sl100-diagnose`、`sl100-agent`、`sl100-es-logs` 和 `sl100-mcp-server`。完整映射见[命令迁移说明](docs/command-migration.md)。

## 安全边界

- 规则诊断不依赖大模型；AI 辅助默认关闭，每次执行都需要显式同意。
- Agent 受到服务白名单、时间窗口、调用次数、运行时间和 token 预算限制。
- 原始日志不会写入团队数据库，诊断证据在进入 Agent 前完成脱敏。
- 项目不提供自动修复、任意 SSH、任意路径读取或生产写入工具。
- 权限变更、计划批准、AI 调用、事件升级和用户反馈均保留审计记录。

HTTP API、数据库迁移历史、MCP 工具名称和诊断报告格式在目录重组后保持兼容。

## 项目结构

```text
src/iot_ops_agent/               产品 Python 包
  diagnosis/                     规则、证据、事件与查询计划
  integrations/                  Elasticsearch、SSH 日志与文档检索
  agent/                         Tool Use Runtime 与 MCP Server
  web/                           团队工作台、Worker 与 Reconciler
  cli/                           统一命令入口
  evaluation/                    合成与真实案例评测
bin/iotops                       本地环境生命周期命令
deploy/                          Compose、监控与生产部署配置
tools/go-log-tools/              Go 日志分析工具
examples/code-reviewer-tutorial/ 早期代码审查 Agent 教程归档
evals/                           可提交的评测数据集
tests/                           单元测试与集成测试
```

评测结果、trace 和临时报告统一写入 Git 忽略的 `artifacts/`。

## 验证项目

推荐直接在与开发环境一致的容器中执行完整检查：

```bash
./bin/iotops test
```

该命令会运行 Ruff、pytest 与覆盖率、三类合成评测、MCP smoke test 和 Go 测试。PostgreSQL/Redis 集成验证及本地栈 smoke test 也包含在 CI 中。

真实案例评测使用私有、脱敏的本地数据集。数据不足或质量门槛未达到时会如实失败，不会用合成案例替代真实结论。

## 深入文档

- [开发、调试与部署](docs/sl100-team-deployment.md)
- [CLI 命令迁移表](docs/command-migration.md)
- [Elasticsearch 与远程日志](docs/sl100-es-logs.md)
- [产品演示指南](docs/sl100-ops-agent-demo.md)
- [项目架构与工程案例](docs/sl100-ops-agent-case-study.md)
- [旧代码审查 Agent 教程](examples/code-reviewer-tutorial/README.md)

仓库中的公开材料只使用匿名 IoT 场景。真实系统映射、生产地址、密钥、私人笔记和私有评测语料不会进入版本控制。
