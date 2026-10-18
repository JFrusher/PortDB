"""Headless A/B runs: yard placement strategy and dual cycling, each on a fresh database.

    python -m portdb.bench --seconds 1200 --runs 2

Every run is the full simulation against a throwaway PostgreSQL (pgserver), storms and all. Re-handles
are counted from the task ledger, the same rows the live KPI panel reads.
"""

import argparse
import asyncio
import random
import tempfile
from pathlib import Path

from . import config, queries as q
from .db import make_pool, sqlalchemy_url
from .seed_vessel import reset, seed_yard
from .sim_engine import Terminal

VARIANTS = {
    "basic placement": {"YARD_STRATEGY": "BASIC", "DUAL_CYCLE": True},
    "smart placement": {"YARD_STRATEGY": "SMART", "DUAL_CYCLE": True},
    "single cycling":  {"YARD_STRATEGY": "SMART", "DUAL_CYCLE": False},
}


async def one(url: str, seconds: float, seed: int, knobs: dict) -> dict:
    for k, v in knobs.items():
        setattr(config, k, v)
    from psycopg import AsyncConnection
    from psycopg.rows import dict_row
    async with await AsyncConnection.connect(url, row_factory=dict_row) as conn:
        await reset(conn)
        await seed_yard(conn, random.Random(seed))
        await conn.commit()
    pool = make_pool(url)
    await pool.open()
    sim = Terminal(pool, lambda ev: None, seed=seed)
    sim.dual = knobs["DUAL_CYCLE"]
    sim.speed = 16
    await sim.run(until=seconds)
    async with pool.connection() as conn:
        rows = await q.kpi(conn, 0, seconds)
    await pool.close()
    rehandles = sum(r["rehandles"] for r in rows)
    retrievals = sum(r["retrievals"] for r in rows)
    qc = sum(r["qc_moves"] for r in rows)
    return {"rehandles_per_100": 100 * rehandles / max(retrievals, 1), "qc_moves_per_hour": qc * 3600 / seconds,
            "dual_cycles": sim.dual_cycles, "wind_holds": sim.refusals.get("wind_limit", 0)}


def main() -> None:
    import pgserver
    from alembic import command
    from alembic.config import Config

    p = argparse.ArgumentParser()
    p.add_argument("--seconds", type=float, default=1200)
    p.add_argument("--runs", type=int, default=2)
    a = p.parse_args()
    server = pgserver.get_server(tempfile.mkdtemp(), cleanup_mode="delete")
    url = server.get_uri()
    root = Path(__file__).parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", sqlalchemy_url(url))
    command.upgrade(cfg, "head")
    print(f"{'variant':18} {'re-handles/100 retrievals':>26} {'QC moves/h':>11} {'dual cycles':>12}")
    for name, knobs in VARIANTS.items():
        runs = [asyncio.run(one(url, a.seconds, seed, knobs), loop_factory=asyncio.SelectorEventLoop)
                for seed in range(1, a.runs + 1)]
        avg = {k: sum(r[k] for r in runs) / len(runs) for k in runs[0]}
        print(f"{name:18} {avg['rehandles_per_100']:26.1f} {avg['qc_moves_per_hour']:11.0f} {avg['dual_cycles']:12.0f}",
              flush=True)
    server.cleanup()


if __name__ == "__main__":
    main()
