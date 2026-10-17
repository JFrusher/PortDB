import os

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://port:port@localhost:5432/port")


def sqlalchemy_url(url: str = DATABASE_URL) -> str:
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


def make_pool(url: str = DATABASE_URL, size: int = 16) -> AsyncConnectionPool:
    return AsyncConnectionPool(url, min_size=2, max_size=size, open=False, kwargs={"row_factory": dict_row})
