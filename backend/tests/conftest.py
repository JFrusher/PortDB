"""Tests run against a real PostgreSQL: $TEST_DATABASE_URL if set, else a throwaway pgserver instance."""

import asyncio
import os
import random
import tempfile
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from portdb.db import sqlalchemy_url
from portdb.seed_vessel import reset, seed_vessel

BACKEND = Path(__file__).parent.parent


def run(coro):
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


async def connect(url: str) -> AsyncConnection:
    return await AsyncConnection.connect(url, row_factory=dict_row)


@pytest.fixture(scope="session")
def pg_url():
    url = os.environ.get("TEST_DATABASE_URL")
    server = None
    if not url:
        import pgserver
        server = pgserver.get_server(tempfile.mkdtemp(), cleanup_mode="delete")
        url = server.get_uri()
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", sqlalchemy_url(url))
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield url
    if server:
        server.cleanup()


@pytest.fixture
def db(pg_url, monkeypatch):
    """Fresh terminal with one berthed vessel (fixed seed), every box 40 ft unless a test says otherwise.
    Returns (url, vessel_id)."""
    from portdb import config
    monkeypatch.setattr(config, "SHARE_40FT", 1.0)

    async def setup():
        async with await connect(pg_url) as conn:
            await reset(conn)
            vessel = await seed_vessel(conn, random.Random(7))
            # neutral cargo: each test sets the flag it is about
            await conn.execute("UPDATE containers SET hazmat = false, imdg_class = NULL, customs_hold = false, "
                               "flow = 'IMPORT', mode = 'TRUCK', pod = NULL")
            return vessel
    return pg_url, run(setup())
