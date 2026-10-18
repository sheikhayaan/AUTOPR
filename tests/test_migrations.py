"""Schema-ownership tests: Alembic owns the schema, models must agree with it.

`docs/decisions/phase-8.md` made migrations the single source of truth and
demoted `Base.metadata.create_all` to the test harness. That decision has one
standing risk: a model can be edited without a migration, so a fresh
`alembic upgrade head` no longer matches what the code expects. Nothing catches
that by accident — the app would fail only on a real deploy.

So we assert it here, offline:

1. `upgrade head` on a throwaway SQLite file, then ask Alembic itself to
   autogenerate a diff between the live schema and `Base.metadata`, and demand
   the diff is empty. Any model change without a migration fails this test.
2. `assert_schema_ready` (the boot guard in `app/main.py` / `app/worker.py`)
   refuses an un-migrated database and accepts a migrated one.
"""

from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from alembic import command
from app.config import settings
from app.db import assert_schema_ready
from app.models import Base

PROJECT_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


@pytest.fixture()
def migrated_engine(tmp_path, monkeypatch):
    """A temp SQLite DB upgraded to head, with settings pointed at it.

    `alembic/env.py` deliberately reads the URL from `app.config.settings` (one
    knob for app and migrations), so we monkeypatch the setting rather than
    passing an override the app would never use.
    """
    db_path = tmp_path / "migrated.db"
    url = f"sqlite+pysqlite:///{db_path.as_posix()}"
    monkeypatch.setattr(settings, "database_url", url, raising=False)

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    command.upgrade(cfg, "head")

    eng = create_engine(url, future=True)
    try:
        yield eng
    finally:
        eng.dispose()


def test_models_and_migrations_do_not_drift(migrated_engine):
    """No model change may exist without a migration (autogenerate diff == [])."""
    with migrated_engine.connect() as conn:
        ctx = MigrationContext.configure(
            conn, opts={"compare_type": True, "compare_server_default": True}
        )
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == [], (
        f"models drift from migrations; run `alembic revision --autogenerate`: {diff}"
    )


def test_migrated_schema_contains_every_model_table(migrated_engine):
    tables = set(inspect(migrated_engine).get_table_names())
    assert set(Base.metadata.tables) <= tables
    # The version stamp is what makes the DB self-describing across deploys.
    assert "alembic_version" in tables


def test_boot_guard_rejects_unmigrated_database(tmp_path):
    empty = create_engine(f"sqlite+pysqlite:///{(tmp_path / 'empty.db').as_posix()}", future=True)
    with pytest.raises(RuntimeError, match="alembic upgrade head"):
        assert_schema_ready(empty)
    empty.dispose()


def test_boot_guard_accepts_migrated_database(migrated_engine):
    assert_schema_ready(migrated_engine)  # must not raise


def test_migrations_are_reversible_to_base(migrated_engine, tmp_path, monkeypatch):
    """`downgrade base` must work: a baseline you cannot roll back isn't one."""
    url = str(migrated_engine.url)
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    command.downgrade(cfg, "base")
    with migrated_engine.connect() as conn:
        remaining = set(inspect(conn).get_table_names()) - {"alembic_version"}
    assert remaining == set(), f"downgrade left tables behind: {remaining} (url={url})"
    # And back up again, so the fixture's engine ends in the expected state.
    command.upgrade(cfg, "head")
    with migrated_engine.connect() as conn:
        assert conn.execute(text("select 1")).scalar() == 1
