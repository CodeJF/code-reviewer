"""RQ worker entrypoint used by Docker Compose."""
from __future__ import annotations

from redis import Redis
from rq import Worker

from team_app.config import TeamSettings


def main() -> None:
    settings = TeamSettings.from_env()
    Worker(["diagnosis", "notifications"], connection=Redis.from_url(settings.redis_url)).work(with_scheduler=True)


if __name__ == "__main__":
    main()
