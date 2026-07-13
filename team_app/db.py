"""Database setup for the single-node team deployment."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from team_app.config import TeamSettings
from team_app.models import Base


def make_engine(settings: TeamSettings):
    kwargs = {"pool_pre_ping": True}
    if settings.database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if settings.database_url.startswith("sqlite:///"):
            path = settings.database_url.removeprefix("sqlite:///")
            if path and path != ":memory:":
                Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    return create_engine(settings.database_url, **kwargs)


def make_session_factory(settings: TeamSettings) -> sessionmaker[Session]:
    return sessionmaker(bind=make_engine(settings), autoflush=False, autocommit=False, expire_on_commit=False)


def initialize_database(session_factory: sessionmaker[Session]) -> None:
    Base.metadata.create_all(session_factory.kw["bind"])


def migration_is_current(settings: TeamSettings) -> bool:
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    config = Config("alembic.ini")
    script = ScriptDirectory.from_config(config)
    engine = make_engine(settings)
    try:
        with engine.connect() as connection:
            current = MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()
    return current == script.get_current_head()
