"""Database engine, session factory, and a portable idempotent-insert helper.

The engine is created from ``settings.database_url``. It works with both
Postgres (production / Compose) and SQLite (local + tests). We deliberately
keep everything synchronous in Phase 1: the webhook handler does one INSERT
and one XADD, both sub-millisecond, so async buys nothing here and sync is
far easier to test. Async is noted as an alternative in the decisions log.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings


def _make_engine(url: str):
    connect_args = {}
    if url.startswith("sqlite"):
        # Needed so the same in-memory / file DB is usable across threads,
        # which the concurrency test exercises.
        connect_args = {"check_same_thread": False}
    eng = create_engine(url, future=True, pool_pre_ping=True, connect_args=connect_args)
    if url.startswith("sqlite"):
        _install_sqlite_pragmas(eng)
    return eng


def _install_sqlite_pragmas(eng) -> None:
    """Apply connection PRAGMAs on every new SQLite connection.

    SQLite's defaults suit a single embedded caller; a concurrent web app +
    worker sharing one file DB want:
      * journal_mode=WAL — readers never block the writer and vice versa, so
        the API and the worker can touch the same file without "database is
        locked" errors. On an in-memory DB this is a harmless no-op (the mode
        stays "memory"), so the test suite is unaffected.
      * busy_timeout=5000 — on a briefly-locked DB, wait up to 5s for the lock
        rather than failing immediately.
      * synchronous=NORMAL — the recommended durability/throughput trade-off
        under WAL: safe against application crashes without a full fsync per
        commit.
    PRAGMAs are per-connection, so this runs on every connect, not once.
    """

    @event.listens_for(eng, "connect")
    def _set_sqlite_pragma(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.execute("PRAGMA synchronous=NORMAL")
        finally:
            cur.close()


engine = _make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session context manager."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def insert_ignore_duplicates(table, values: dict, index_elements: list[str]):
    """Return a dialect-appropriate ``INSERT ... ON CONFLICT DO NOTHING``.

    This is the load-bearing primitive for idempotency: a duplicate webhook
    (same dedup_key) hits the unique constraint and the row is silently not
    inserted, rather than raising or creating a duplicate. Both Postgres and
    modern SQLite support the ON CONFLICT clause with the same semantics for
    our purposes; we branch only because the SQLAlchemy construct differs.
    """
    if settings.is_sqlite:
        sqlite_stmt = sqlite_insert(table).values(**values)
        return sqlite_stmt.on_conflict_do_nothing(index_elements=index_elements)
    pg_stmt = pg_insert(table).values(**values)
    return pg_stmt.on_conflict_do_nothing(index_elements=index_elements)
