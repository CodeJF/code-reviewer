# CLI 命令迁移

产品代码已从根目录脚本迁入 `src/iot_ops_agent/`。功能、参数和输出结构保持不变，推荐统一使用 `iot-ops`。

| 原入口 | 新入口 | 兼容命令 |
|---|---|---|
| `sl100_diagnose.py` | `iot-ops diagnose` | `sl100-diagnose` |
| `sl100_agent.py` | `iot-ops agent` | `sl100-agent` |
| `sl100_log_agent.py` | `iot-ops logs analyze` | `sl100-log-agent` |
| `sl100_rules.py` | `iot-ops logs facts` | `sl100-rules` |
| `sl100_es_logs.py` | `iot-ops es` | `sl100-es-logs` |
| `sl100_remote_logs.py` | `iot-ops remote` | `sl100-remote-logs` |
| `sl100_docs_qa.py` | `iot-ops docs ask` | `sl100-docs-qa` |
| `sl100_review_cases.py` | `iot-ops cases` | `sl100-review-cases` |
| `sl100_mcp_server.py` | `iot-ops mcp serve` | `sl100-mcp-server` |
| `eval_sl100_logs.py` | `iot-ops eval logs` | `sl100-eval-logs` |
| `eval_sl100_docs.py` | `iot-ops eval docs` | `sl100-eval-docs` |
| `eval_sl100_product.py` | `iot-ops eval product` | `sl100-eval-product` |
| `eval_sl100_real_cases.py` | `iot-ops eval real` | `sl100-eval-real-cases` |

例如：

```bash
# 旧方式
uv run sl100_diagnose.py "deviceShadow websocket 异常" --dry-run

# 推荐方式
uv run iot-ops diagnose "deviceShadow websocket 异常" --dry-run

# 功能兼容入口
uv run sl100-diagnose "deviceShadow websocket 异常" --dry-run
```

Python 内部导入已统一改为 `iot_ops_agent.*`。根目录脚本路径不再作为公共接口，HTTP API、MCP JSON-RPC、数据库和诊断 JSON 不受影响。
