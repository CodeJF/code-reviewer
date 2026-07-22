"""Interactive administrative commands that keep passwords out of shell history."""
from __future__ import annotations

import argparse
import getpass

from iot_ops_agent.web.accounts import bootstrap_admin
from iot_ops_agent.web.config import TeamSettings
from iot_ops_agent.web.db import make_session_factory


def bootstrap() -> None:
    settings = TeamSettings.from_env()
    username = input("管理员用户名（小写字母/数字/._-）：").strip()
    display_name = input("显示名称：").strip()
    password = getpass.getpass("密码（12～128 个字符）：")
    confirmation = getpass.getpass("再次输入密码：")
    if password != confirmation:
        raise SystemExit("两次输入的密码不一致")
    with make_session_factory(settings)() as session:
        try:
            user = bootstrap_admin(
                session,
                username=username,
                display_name=display_name,
                password=password,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    print(f"首位管理员已创建：{user.username}（{user.display_name}）")


def main() -> None:
    parser = argparse.ArgumentParser(description="SL100 团队账号管理")
    parser.add_argument("command", choices=["bootstrap"])
    args = parser.parse_args()
    if args.command == "bootstrap":
        bootstrap()


if __name__ == "__main__":
    main()
