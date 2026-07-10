# SL100 团队工作台部署

团队版运行在 `log.hassecurity.cn`，使用 Caddy 自动签发 HTTPS 证书。应用从 Git 仓库拉取固定 revision，运行时密钥只保存在服务器，不进入 Git 或 Docker 镜像。

## 一次性服务器准备

```bash
git clone git@github.com:CodeJF/code-reviewer.git /opt/sl100-diagnosis
cd /opt/sl100-diagnosis
mkdir -p secrets/ssh
cp deploy/.env.example deploy/.env
chmod 600 deploy/.env
```

编辑 `deploy/.env`，填写 PostgreSQL 密码、会话密钥、公司 OIDC Discovery URL、Client ID、Client Secret、OIDC 群组映射和飞书机器人 Webhook。OIDC 回调地址固定为：

```text
https://log.hassecurity.cn/api/auth/callback
```

把团队诊断用的 `sl100.local.json` 放入 `/opt/sl100-diagnosis/secrets/`。首版可使用 root 的只读日志凭据；后续必须替换为受限的 `sl100-diagnosis` 账号。SSH 文件需可由容器内 UID `10001` 读取：

```bash
chown -R 10001:10001 /opt/sl100-diagnosis/secrets/ssh
chmod 700 /opt/sl100-diagnosis/secrets/ssh
chmod 600 /opt/sl100-diagnosis/secrets/ssh/*
```

## Git 版本部署

只部署已经通过本地验证的 Git revision：

```bash
cd /opt/sl100-diagnosis
./scripts/deploy_team.sh origin/main
```

脚本会 `git fetch`、切换到指定 commit/branch 的当前 revision、构建镜像并启动 Caddy、API、worker、PostgreSQL、Redis 和每日留存清理任务。上线后检查：

```bash
curl -fsS https://log.hassecurity.cn/api/health
docker compose --env-file deploy/.env -f deploy/docker-compose.yml ps
```

## 运行边界

- 不保存原始 ES、SSH 或用户日志正文；报告和评论写库前再次脱敏。
- 脱敏报告与评论保留 90 天，事件元数据与审计记录保留 1 年。
- 只有团队服务容器持有日志源凭据；浏览器只能调用受控诊断 API。
