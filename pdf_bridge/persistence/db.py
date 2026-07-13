"""Synchronous SQLAlchemy setup used by the API and scheduled-job clients."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from pdf_bridge.core.config import get_settings

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base using deterministic cross-database constraint names."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def build_engine(database_url: str, *, echo: bool = False) -> Engine:
    """Create an engine with safe SQLite defaults and PostgreSQL portability."""

    options: dict[str, object] = {"pool_pre_ping": True, "echo": echo}
    if database_url.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False, "timeout": 30}
        if ":memory:" in database_url:
            options["poolclass"] = StaticPool

    engine = create_engine(database_url, **options)

    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def configure_sqlite(dbapi_connection: object, _connection_record: object) -> None:
            """Enable integrity, contention, and durability settings per connection."""

            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=30000")
                if ":memory:" not in database_url:
                    cursor.execute("PRAGMA journal_mode=WAL")
            finally:
                cursor.close()

    return engine


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Build a session factory with explicit transaction boundaries."""

    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return the cached engine for the active application settings."""

    return build_engine(get_settings().database_url)


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    """Return the cached session factory for the active engine."""

    return build_session_factory(get_engine())


def get_db() -> Iterator[Session]:
    """Request dependency; commit and rollback are deliberately manager-owned."""

    with get_session_factory()() as session:
        yield session


@contextmanager
def session_scope(
    factory: sessionmaker[Session] | None = None,
) -> Iterator[Session]:
    """Run command-line work in a commit-or-rollback transaction."""

    session_factory = factory or get_session_factory()
    with session_factory() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def create_schema(engine: Engine | None = None) -> None:
    """Create tables for tests only; deployed instances should run Alembic."""

    # Importing registers all mapped tables with Base.metadata.
    from . import models  # noqa: F401

    Base.metadata.create_all(engine or get_engine())


def clear_database_caches() -> None:
    """Dispose and clear cached database objects, primarily for tests."""

    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_session_factory.cache_clear()
    get_engine.cache_clear()
