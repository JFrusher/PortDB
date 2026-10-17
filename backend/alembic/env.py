from alembic import context
from sqlalchemy import create_engine

from portdb.db import sqlalchemy_url

url = context.config.get_main_option("sqlalchemy.url") or sqlalchemy_url()
with create_engine(url).connect() as conn:
    context.configure(connection=conn)
    with context.begin_transaction():
        context.run_migrations()
