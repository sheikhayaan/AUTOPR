"""Alembic migration environment.

Design choices (defensible under questioning):

* **Single source of truth for the URL.** We do not read ``sqlalchemy.url``
  from alembic.ini; we pull it from ``app.config.settings`` so migrations run
  against the same database the app uses (SQLite locally, Postgres in Compose /
  cloud). One knob — ``AUTOPR_DATABASE_URL`` — governs both.

* **Model metadata is the autogenerate target.** Importing ``app.models``
  registers every table on ``Base.metadata``; ``--autogenerate`` diffs that
  against the live database. Keep this import even though it looks unused.

* **Batch mode on SQLite.** SQLite cannot ``ALTER TABLE`` in place for most
  operations; ``render_as_batch`` makes Alembic emit the copy-and-swap dance so
  future migrations apply on SQLite as well as Postgres.
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Import settings + metadata from the application. This import has the side
# effect of registering all ORM tables on Base.metadata — required for
# autogenerate to see the schema.
from app.config import settings
from app.models import Base

# Alembic Config object (values from alembic.ini).
config = context.config

# Wire Python logging from the ini, if present.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The metadata autogenerate compares against.
target_metadata = Base.metadata


def _database_url() -> str:
    """The URL to migrate — always the app's configured database."""
    return settings.database_url


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live connection (``alembic upgrade --sql``)."""
    url = _database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=_is_sqlite(url),
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection."""
    url = _database_url()
    # Feed the URL into the config Alembic uses to build the engine, overriding
    # any placeholder in alembic.ini.
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = url
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=_is_sqlite(url),
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
