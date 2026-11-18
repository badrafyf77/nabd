"""Alembic entry point. Migration credentials come from NABD settings."""
from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from nabd.settings import Settings

url = context.config.attributes.get("database_url")
if url is None:
    url = Settings().sqlalchemy_url

if context.is_offline_mode():
    context.configure(url=url, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    with create_engine(url, poolclass=NullPool).connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()
