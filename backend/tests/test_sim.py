"""Headless run: thirteen simulated minutes at 12x, every move committed through the real schema."""

import random

from portdb import queries as q
from portdb.db import make_pool
from portdb.seed_vessel import reset, seed_yard
from portdb.sim_engine import Terminal

from .conftest import connect, run


def test_terminal_runs_clean(pg_url):
    events = []

    async def go():
        async with await connect(pg_url) as conn:
            await reset(conn)
            await seed_yard(conn, random.Random(5))
        pool = make_pool(pg_url)
        await pool.open()
        sim = Terminal(pool, events.append, seed=1)
        sim.speed = 12
        await sim.run(until=780)
        async with pool.connection() as conn:
            s = await q.stats(conn)
            live = await (await conn.execute(
                "SELECT count(*) AS n FROM equipment_tasks WHERE status IN ('PENDING','IN_PROGRESS')")).fetchone()
        await pool.close()
        return sim, s, live["n"]

    sim, stats, live = run(go())
    kinds = {e["type"] for e in events}
    print({k: stats[k] for k in ("discharged", "loaded", "sailed", "gated_out", "railed_out", "reshuffles",
                                 "inspected", "bookings")}, stats["states"], sim.queues())
    assert sim.rejects == 0, [e for e in events if e["type"] == "reject"][:5]
    assert stats["discharged"] > 50 and stats["loaded"] > 0           # both directions over the quay
    assert stats["gated_out"] > 0 and stats["railed_out"] > 0 and stats["reshuffles"] > 0
    assert stats["states"].get("STACKED_IN_YARD", 0) > 0
    assert sum(sim.waits.values()) > 0                                 # bookings made someone yield
    assert live <= 30                                                  # only work actually in flight is open
    assert {"vessel", "track", "lorry", "train", "remove", "weather"} <= kinds
