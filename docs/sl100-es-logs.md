# SL100 Elasticsearch 日志接入说明

## 为什么接日志系统

直接读服务机器上的 `*.log` 文件有两个问题：

- 业务代码写文件日志不完整，有些真实请求链路看不到。
- 测试同事通常只会给“某个时间、某个功能报错”，不会给具体日志文件路径。

`sl100-93` 上已有 Elasticsearch + Kibana 日志栈，更适合做真实排障入口。Agent 应该先查日志系统，再在必要时回到服务器文件日志做补充验证。

## 已验证环境

只读探测结果：

```text
Host: sl100-93
Elasticsearch: 7.17.21
Kibana: 5601
ES endpoint: 127.0.0.1:9200 on sl100-93
```

本地工具不会直接访问公网 ES，也不会开放端口。查询链路是：

```text
Mac -> ssh sl100-93 -> curl http://127.0.0.1:9200
```

## 当前索引映射

```text
gateway      -> api-gateway-YYYY-MM-DD
deviceShadow -> api-device-shadow-YYYY-MM-DD
pushService  -> api-push-service-YYYY-MM-DD
access       -> api-access-YYYY-MM-DD
```

核心字段：

```text
@timestamp
message
fields.log_type
host.hostname
host.ip
```

注意：`@timestamp` 是 UTC 时间，用户输入默认按 `Asia/Shanghai` 解释。

例如：

```text
用户说：2026-07-09 09:20 左右
ES 查：2026-07-09T01:10:00Z 到 2026-07-09T01:30:00Z
```

## 手动排障命令

## 产品化一键排障入口

优先使用 `sl100_diagnose.py`。它接受一句自然语言报障，输出统一 Incident Report。

只看查询计划：

```bash
uv run sl100_diagnose.py \
  "测试说 2026-07-09 上午 9 点多 deviceShadow websocket 异常" \
  --dry-run
```

执行排障：

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

报告统一字段：

```text
incident_id
query
time_window
services
data_sources
evidence
timeline
root_cause
confidence
risk_level
next_actions
redaction_status
```

报告还会提供：

```text
result_status  # actionable / no_evidence / data_unavailable / safety_blocked
query_attempts # 未给时间时展示“最近 2 小时 -> 今天全天”的查询过程
```

`data_unavailable` 表示日志系统或索引不可用，绝不等价于“没有异常”；`safety_blocked` 表示命中内容无法安全脱敏，工具不会输出原始内容。

## 底层手动排障命令

检查 ES：

```bash
uv run sl100_es_logs.py health
```

看某天有哪些 SL100 日志索引：

```bash
uv run sl100_es_logs.py indices --date 2026-07-09
```

按服务、关键词、时间窗口查询：

```bash
uv run sl100_es_logs.py search \
  --service deviceShadow \
  --keyword websocket \
  --from "2026-07-09 09:00" \
  --to "2026-07-09 10:00" \
  --size 20
```

按“某个时间点左右”查询：

```bash
uv run sl100_es_logs.py search \
  --service gateway \
  --keyword error \
  --around "2026-07-09 09:20" \
  --around-minutes 10
```

不调用 Claude，直接用本地规则诊断：

```bash
uv run sl100_es_logs.py analyze \
  --service deviceShadow \
  --keyword websocket \
  --from "2026-07-09 09:00" \
  --to "2026-07-09 10:00"
```

## 远程文件日志 fallback

ES 是第一入口，但不是唯一入口。下面几种情况需要回到服务机器文件日志：

- ES 没有对应服务索引。
- ES 采集有延迟。
- 需要查 `std_err.log`、`srd_out.log`、`sql.log`。
- 需要确认服务本机日志和 ES 是否一致。

命令：

```bash
uv run sl100_remote_logs.py list
uv run sl100_remote_logs.py tail --service gateway --log error --tail-lines 100
uv run sl100_remote_logs.py search --service deviceShadow --log stderr --keyword panic
uv run sl100_remote_logs.py analyze --service pushService --logs error,stderr
```

这些工具只允许读取代码白名单里的固定路径，不接受任意远程路径。

## Agent 排障入口

真实场景使用 `sl100_agent.py`：

```bash
uv run sl100_agent.py "测试说今天上午 9 点多 deviceShadow websocket 异常，帮我查日志"
uv run sl100_agent.py "查 gateway 最近一次 error，分析可能原因"
uv run sl100_agent.py "查 pushService 最近一小时有没有推送失败"
```

Agent 流程：

```text
理解测试反馈
-> 判断服务和时间窗口
-> 查询 ES 索引
-> 拉取命中日志
-> 脱敏
-> 规则聚类
-> 输出中文排障结论
```

## 安全边界

- 只读查询 ES。
- 不修改 Elasticsearch、Kibana、索引和服务器文件。
- 不保存原始 ES 日志。
- 输出前先脱敏 IP、token、secret、手机号、邮箱、uuid 等敏感内容。
- 默认限制返回条数，避免一次拉大量日志。
- ES 可以继续保存原始日志，不需要修改 Filebeat；脱敏发生在本机 Agent 输出和模型调用之前。

## 后续优化

- 补充 AdminService、cloudStorage 的 ES 索引映射。
- 支持跨服务链路查询：gateway -> deviceShadow -> pushService。
- 从 message_id、uuid 聚合完整时间线。
- 将飞书机器人或 Web API 作为第二阶段交付入口。
