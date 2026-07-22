# IoT Ops Agent Demo 指南

所有命令默认在仓库根目录执行。公开演示只使用匿名配置和合成日志，不展示 `.env`、真实日志、主机、设备标识或私有评测 corpus。

## 1. 安装与自动化验收

```bash
uv sync --frozen --all-groups
uv run ruff check .
uv run pytest --cov=iot_ops_agent --cov-report=term-missing
uv run iot-ops eval logs
uv run iot-ops eval docs
uv run iot-ops eval product
uv run scripts/test_sl100_mcp.py
(cd tools/go-log-tools && go test ./...)
```

预期核心结果：Python 核心覆盖率不低于 80%，日志评测 15/15，文档检索 4/4，产品行为 5/5。

## 2. 运行团队工作台

```bash
docker compose -p iot-ops-demo -f deploy/docker-compose.dev.yml up -d --wait

export APP_ENV=development
export APP_URL=http://127.0.0.1:8780
export AUTH_MODE=dev
export DATABASE_URL=postgresql+psycopg://sl100:sl100-local-development@127.0.0.1:55432/sl100_team_dev
export REDIS_URL=redis://127.0.0.1:56379/0

uv run alembic upgrade head
uv run uvicorn iot_ops_agent.web.api:app --host 127.0.0.1 --port 8780
```

另开两个终端：

```bash
uv run python -m iot_ops_agent.web.worker
uv run python -m iot_ops_agent.web.reconciler
```

打开 `http://127.0.0.1:8780`。开发模式使用模拟管理员，不需要创建真实账号。

## 3. 演示受控诊断

输入：

```text
今天上午 9 点 deviceShadow websocket 异常
```

先点击“生成只读计划”，讲清以下信息：

- 创建计划时尚未读取日志；
- 计划固定目标服务、时间窗口、数据源、关键词和预算；
- 规则模式不调用模型；
- AI 模式需要本次显式同意，且仍受同一计划约束。

公开 Demo 建议选择“按规则执行”。等待 Worker 完成后查看：

- 结构化 Incident Report；
- `evidence_id` 和脱敏证据；
- `result_status`、置信度、风险、下一步；
- 工具轨迹、耗时和 token（规则模式为 0）；
- 结果反馈入口。

如需演示 AI 模式，只在私有测试环境设置模型密钥和 `AI_ASSISTED_ENABLED=true`。不要把密钥写入演示脚本、录屏或仓库。

## 4. 演示人工事件协作

完成诊断后点击“升级为 Incident”，展示：

- 事件不是 Agent 自动创建，而是人工确认；
- 值班成员可以认领、推进排查中/已缓解/已解决状态；
- 评论再次脱敏后保存；
- 操作进入审计记录；
- 通知未配置时明确显示 skipped，不影响主流程。

## 5. 演示质量与可观测性

管理员进入“质量面板”，查看完成率、平均耗时、AI 运行数、反馈覆盖和有用率。

内部指标：

```bash
curl http://127.0.0.1:8780/internal/metrics
```

生产配置中该路径由 Caddy 对公网屏蔽，只允许 Compose 内的 Prometheus 抓取；Grafana 只绑定宿主机 `127.0.0.1:53000`。

## 6. 演示原有能力没有被废弃

```bash
# 本地确定性诊断
uv run iot-ops logs analyze samples/sl100_logs/device_login_failed.log --local-only

# 查看规则 facts
uv run iot-ops logs facts samples/sl100_logs/device_login_failed.log --summary

# 只生成自然语言查询计划
uv run iot-ops diagnose "今天上午 9 点 deviceShadow websocket 异常" --dry-run

# MCP 工具服务
uv run iot-ops mcp serve
```

说明团队版是在既有规则、CLI、MCP 和数据源之上增加产品控制面，不是重新做一个互不兼容的 Demo。

## 7. 真实评测的正确讲法

```bash
uv run iot-ops eval real
```

当前私有回放的 precision 为 100%、recall 为 78%、证据命中率为 72%，正常样本只有 4 条，因此上线门槛尚未全部通过。演示时应主动说明这是数据采集和历史证据过期问题，下一步是补采正常样本和失败快照，而不是修改阈值让结果变绿。

## 8 分钟推荐演示顺序

1. 1 分钟：业务问题和“不是聊天机器人”的定位；
2. 2 分钟：生成计划、解释 Human Gate 和预算；
3. 2 分钟：执行规则诊断、查看证据和报告；
4. 1 分钟：升级 Incident 和审计；
5. 1 分钟：质量面板、Prometheus/Grafana；
6. 1 分钟：自动化测试与真实评测未过门槛的原因。
