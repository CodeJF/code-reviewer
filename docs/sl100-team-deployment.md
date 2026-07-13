# SL100 团队版开发、测试与部署

团队版使用“本地账号 + 管理员邀请”，不依赖 OIDC，也不使用飞书登录。飞书仅是可选通知渠道；未配置 Webhook 时，工作台会明确显示“通知未启用”。

生产地址为 `https://log.hassecurity.cn`。生产数据库、Redis、会话和备份都由 Docker Compose 管理，PostgreSQL 与 Redis 不暴露宿主机端口。

## 1. 本地断点调试

本地 Compose 默认只启动 PostgreSQL 和 Redis，API、worker 在宿主机运行，便于 VS Code 断点调试：

```bash
docker compose -p sl100-dev -f deploy/docker-compose.dev.yml up -d

export APP_ENV=development
export APP_URL=http://127.0.0.1:8780
export AUTH_MODE=dev
export DATABASE_URL=postgresql+psycopg://sl100:sl100-local-development@127.0.0.1:55432/sl100_team_dev
export REDIS_URL=redis://127.0.0.1:56379/0

uv run alembic upgrade head
uv run uvicorn team_app.api:app --reload --host 127.0.0.1 --port 8780
```

另开终端运行后台进程：

```bash
uv run python -m team_app.worker
uv run python -m team_app.reconciler
```

VS Code 已提供四个入口：调试 API、调试 worker、同时调试 API 与 worker、调试真实案例采集。开发模式使用请求头模拟角色，浏览器默认是本地管理员；生产环境固定使用 `AUTH_MODE=local`。

停止本地基础设施：

```bash
docker compose -p sl100-dev -f deploy/docker-compose.dev.yml down
```

除非明确要清空本地测试数据，不要加 `-v`。

## 2. 本地账号流程测试

需要验证真实 Cookie、Redis Session 和 CSRF 时，把本地环境切换为：

```bash
export AUTH_MODE=local
export SESSION_SECRET='local-test-secret-at-least-32-characters'
uv run python -m team_app.admin bootstrap
uv run uvicorn team_app.api:app --reload --host 127.0.0.1 --port 8780
```

首位管理员命令会交互式读取用户名、显示名称和密码，密码不会进入命令行参数、环境变量或 shell 历史。

管理员登录后，在“成员管理”生成一次性邀请链接。邀请链接有效期 24 小时，只显示一次；成员打开链接设置密码并激活。密码重置链接有效期 30 分钟，使用一次后立即失效。

## 3. 自动化验证

基础验证：

```bash
uv sync --frozen
uv run python -m unittest discover -s tests -v
uv run eval_sl100_logs.py
uv run eval_sl100_product.py
uv run eval_sl100_real_cases.py --enforce-gates
```

团队版测试覆盖：

- Argon2id 密码、邀请过期与重复使用；
- 登录失败锁定、用户名/IP 限流、统一错误提示；
- Redis 服务端 Session、CSRF、改密/重置/禁用后的 Session 失效；
- 最后一名有效管理员保护和三种角色越权校验；
- 每人并发诊断上限、失败诊断关联重试；
- 报告、错误、评论和通知错误的入库脱敏；
- 通知重试、任务幂等、reconciler 恢复和留存清理。

生产 Compose 静态检查：

```bash
docker compose --env-file deploy/.env -f deploy/docker-compose.yml config --quiet
docker compose --env-file deploy/.env -f deploy/docker-compose.yml build
```

`sl100-dev` 与 `sl100-prod` 是不同 Compose 项目，因此网络、命名卷、PostgreSQL 和 Redis 都相互隔离。可用下面命令核对资源名：

```bash
docker compose -p sl100-dev -f deploy/docker-compose.dev.yml config --volumes
docker compose -p sl100-prod --env-file deploy/.env -f deploy/docker-compose.yml config --volumes
docker volume ls --filter name=sl100-dev --filter name=sl100-prod
docker network ls --filter name=sl100-dev --filter name=sl100-prod
```

## 4. 服务器首次准备

服务器使用 root 部署，仓库目录固定为 `/opt/sl100-diagnosis`：

```bash
git clone git@github.com:CodeJF/code-reviewer.git /opt/sl100-diagnosis
cd /opt/sl100-diagnosis
cp deploy/.env.example deploy/.env
mkdir -p secrets/ssh
chmod 600 deploy/.env
```

编辑 `deploy/.env`，至少设置：

- `APP_DOMAIN=log.hassecurity.cn`
- `APP_URL=https://log.hassecurity.cn`
- 长随机 `POSTGRES_PASSWORD`
- 不少于 32 个随机字符的 `SESSION_SECRET`
- `AUTH_MODE=local`
- `SL100_SECRET_DIR=/opt/sl100-diagnosis/secrets`

不要把 `deploy/.env`、日志源配置、SSH 私钥或备份提交到 Git。

把团队诊断使用的 `sl100.local.json` 放到 `/opt/sl100-diagnosis/secrets/`。SSH 文件需要允许容器内 UID `10001` 读取。当前可先使用 root 的只读日志凭据，稳定后应替换为权限受限的诊断账号。

```bash
chown -R 10001:10001 /opt/sl100-diagnosis/secrets/ssh
chmod 700 /opt/sl100-diagnosis/secrets/ssh
chmod 600 /opt/sl100-diagnosis/secrets/ssh/*
```

确认阿里云安全组和服务器防火墙只对公网开放 80、443 和必要的 SSH 管理入口。PostgreSQL、Redis 与 API 不直接开放公网端口。

## 5. Git 统一部署

部署脚本只接受干净 Git 工作区，执行目标分支的 fast-forward 拉取：

```bash
cd /opt/sl100-diagnosis
./scripts/deploy_team.sh codex/team-ops-workspace
```

脚本依次执行：

1. 检查 Git 工作区；
2. 切换目标分支并执行 `git pull --ff-only`；
3. 记录新旧 Git SHA；
4. 启动 PostgreSQL、Redis 并执行部署前备份；
5. 构建 `sl100-team:<Git SHA>` 镜像；
6. 执行 `alembic upgrade head`；
7. 启动 API、worker、reconciler、maintenance、backup 和 Caddy；
8. 检查 `/api/ready`、HTTPS 及所有生产服务；
9. 启动失败时恢复上一 Git SHA 的应用镜像。

首次部署成功后创建首位管理员：

```bash
docker compose --env-file deploy/.env -f deploy/docker-compose.yml exec api \
  uv run python -m team_app.admin bootstrap
```

功能稳定并合并到 `main` 后，后续服务器只执行：

```bash
./scripts/deploy_team.sh main
```

## 6. 备份、校验与恢复

`backup` 服务每天生成 PostgreSQL custom-format 备份，创建后立即使用 `pg_restore --list` 校验，服务器本地保留 14 天。手工备份：

```bash
./scripts/backup_team.sh
```

查看备份：

```bash
docker compose --env-file deploy/.env -f deploy/docker-compose.yml \
  run --rm --entrypoint sh backup -c 'ls -lh /backups'
```

恢复会停止业务进程并重建数据库，必须明确传入已校验的文件名：

```bash
./scripts/restore_team.sh sl100_team_YYYYMMDDTHHMMSSZ.dump
```

恢复完成后再次检查：

```bash
curl --fail https://log.hassecurity.cn/api/ready
docker compose --env-file deploy/.env -f deploy/docker-compose.yml ps
```

## 7. 最终人工验收

1. 浏览器访问 `https://log.hassecurity.cn`，确认 HTTPS 证书和安全 Cookie；
2. 创建首位管理员，登录并生成成员邀请；
3. 分别使用管理员、值班、只读账号验证权限；
4. 值班成员完成诊断、失败重试、人工升级事件、负责人指派、评论和状态解决；
5. 确认页面显示成员姓名而不是内部 ID；
6. 未配置 Webhook 时确认“通知未启用”，配置后确认失败重试状态可见；
7. 重启 API、worker、Redis 和 PostgreSQL，确认 Session、任务和业务数据符合预期；
8. 执行一次备份校验和恢复演练；
9. 确认数据库中只存在 Argon2id 密码哈希和邀请/重置 token 的 SHA-256 哈希。
