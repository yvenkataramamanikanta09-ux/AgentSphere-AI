"""Alembic owns schema changes; application startup never calls create_all()."""

from alembic import context

from agentsphere.config import Settings
from agentsphere.database import build_engine
from agentsphere.models import Base

config = context.config
target_metadata = Base.metadata


def run_migrations() -> None:
    # Tests can supply their own connection without changing DATABASE_URL.
    supplied_connection = config.attributes.get("connection")
    if supplied_connection is not None:
        context.configure(connection=supplied_connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
        return

    secret = Settings().database_url
    if secret is None:
        raise RuntimeError("Set DATABASE_URL before running Alembic.")
    url = secret.get_secret_value()
    if context.is_offline_mode():
        context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
        with context.begin_transaction():
            context.run_migrations()
        return

    engine = build_engine(url)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


run_migrations()
