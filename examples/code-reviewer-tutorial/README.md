# Code Reviewer Agent 教程归档

这里保留了项目早期从“单次模型调用”演进到 Tool Use Agent 的完整学习实现。它不是当前 IoT Ops Agent 的运行依赖，但仍可作为个人项目演进记录和面试讲解材料。

## 内容

- `main.py`：读取单个 Python 文件并输出结构化代码审查结果。
- `eval.py`：早期代码审查评测。
- `agent.py`：让模型自行选择文件、代码分析和 Go 工具的 Tool Use Agent。
- `go-tools/`：为教程 Agent 提供静态代码分析的 Go HTTP 服务。
- `test_sample.py`：用于演示审查结果的样例代码。
- `historical-artifacts/`：迁移前保留的历史运行结果。

## 运行

在仓库根目录安装依赖后进入本目录：

```bash
uv sync
cd examples/code-reviewer-tutorial

uv run main.py test_sample.py
uv run eval.py
uv run agent.py "帮我审查 test_sample.py"
```

需要 Go 静态分析工具时，另开终端：

```bash
cd examples/code-reviewer-tutorial/go-tools
go run .
```

教程会调用外部模型，运行前需要配置对应的 API 凭据。历史代码按原貌保留，不参与当前产品 CI 和生产镜像。
