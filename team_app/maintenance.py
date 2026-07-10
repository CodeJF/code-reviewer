"""Retention command run by the single-node maintenance container."""
from __future__ import annotations

import json

from team_app.config import TeamSettings
from team_app.db import make_session_factory
from team_app.services import purge_expired_data


def main() -> int:
    with make_session_factory(TeamSettings.from_env())() as session:
        print(json.dumps(purge_expired_data(session), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
