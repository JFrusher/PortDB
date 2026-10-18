"""HTTP + WebSocket face of the terminal.

    python -m portdb.api        # resets the terminal on boot: in-flight SimPy state cannot be resumed

Two streams reach the browser over one socket: motion and calls from the engine, and box changes
from the database itself (LISTEN container_feed: a trigger notifies at commit). Both are appended to
event_log in batches, which with container_history is what time travel replays.
"""

import asyncio
import contextlib
import json
import logging
import random
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Path, Query, WebSocket, WebSocketDisconnect
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from . import config, layout, queries as q
from .db import DATABASE_URL, make_pool
from .seed_vessel import reset, seed_yard
from .sim_engine import Terminal

log = logging.getLogger("portdb.api")
STATS_EVERY_S = 0.5
FLUSH_EVERY_S = 0.5
CLIENT_BACKLOG = 2000
FEED_DELAY_S = 0.06          # a box change lands just after the crane track that animates it
NOT_LOGGED = {"stats", "box"}                     # box versions live in container_history
ROLES = {"customs": "portdb_customs", "gate": "portdb_gate", "line": "portdb_line"}

clients: set[asyncio.Queue] = set()
pending_log: list[tuple] = []


def broadcast(ev: dict) -> None:
    if ev["type"] not in NOT_LOGGED:
        unit = ev.get("id") if ev["type"] in ("track", "lorry") else None
        pending_log.append((ev["t"] if "t" in ev else ev["t0"], ev["type"], unit, Jsonb(ev)))
    for queue in list(clients):
        try:
            queue.put_nowait(ev)
        except asyncio.QueueFull:          # a stalled browser is dropped, it never slows the sim
            clients.discard(queue)


async def stats_frame() -> dict:
    async with pool.connection() as conn:
        db_stats = await q.stats(conn)
    return {"type": "stats", "t": round(sim.env.now, 3), "speed": sim.speed, "paused": sim.paused,
            "vessels": [sim.vessel_view(v) for v in sim.vessels.values()], "train": sim.train,
            "scanning": len(sim.scans), "weather": sim.weather_now, "limits": limits,
            "strategy": config.YARD_STRATEGY, "dual": sim.dual, "chaos_auto": sim.chaos_auto,
            "history_from": max(0.0, sim.env.now - config.RETENTION_S),
            "queues": sim.queues(), "rejects": sim.rejects, **db_stats}


async def stats_loop() -> None:
    while True:
        await asyncio.sleep(STATS_EVERY_S)
        if clients:
            broadcast(await stats_frame())


async def flush_loop() -> None:
    """The event stream goes to the database in batches (one COPY per half second)."""
    while True:
        await asyncio.sleep(FLUSH_EVERY_S)
        if not pending_log:
            continue
        rows = pending_log[:]
        del pending_log[:len(rows)]
        try:
            async with pool.connection() as conn:
                await q.append_events(conn, rows)
        except Exception:
            log.exception("event log flush failed (%d events dropped)", len(rows))


async def maintain_loop() -> None:
    """Partitions ahead of the clock, retention behind it."""
    last = -config.MAINTAIN_EVERY_S
    while True:
        await asyncio.sleep(2)
        if sim.env.now - last >= config.MAINTAIN_EVERY_S:
            last = sim.env.now
            async with pool.connection() as conn:
                done = await q.maintain(conn, sim.env.now)
            if done["dropped"]:
                log.info("history retention: dropped %d partitions", done["dropped"])


async def feed_loop() -> None:
    """LISTEN on the database's change feed and pass each committed box change to the browsers."""
    loop = asyncio.get_running_loop()
    while True:
        try:
            async with await AsyncConnection.connect(DATABASE_URL, autocommit=True) as conn:
                await conn.execute("LISTEN container_feed")
                async for n in conn.notifies():
                    ev = json.loads(n.payload)
                    loop.call_later(FEED_DELAY_S, broadcast, {"type": "box", "t": ev["t"], **ev["box"]})
        except Exception:
            log.exception("change feed lost, reconnecting")
            await asyncio.sleep(1)


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    global pool, sim, limits
    pool = make_pool()
    await pool.open(wait=True)
    async with pool.connection() as conn:
        await reset(conn)
        await seed_yard(conn, random.Random())
        limits = await q.wind_limits(conn)
    with contextlib.suppress(Exception):          # only where the server preloads it (Compose does)
        async with pool.connection() as conn:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
    sim = Terminal(pool, broadcast)
    tasks = [asyncio.create_task(f) for f in (sim.run(), stats_loop(), flush_loop(), maintain_loop(), feed_loop())]
    for t in tasks:
        t.add_done_callback(lambda t: t.cancelled() or t.exception() and log.error("task died", exc_info=t.exception()))
    yield
    for t in tasks:
        t.cancel()
    await pool.close()


app = FastAPI(title="PortDB TOS", lifespan=lifespan)
BoxId = Annotated[str, Path(pattern=r"^[A-Z]{3}U[0-9]{7}$")]


@app.get("/api/layout")
def get_layout():
    return layout.payload()


@app.get("/api/snapshot")
async def snapshot():
    async with pool.connection() as conn:
        boxes = await q.live_containers(conn)
    return {"sim": {"t": round(sim.env.now, 3), "speed": sim.speed, "paused": sim.paused},
            "vessels": [sim.vessel_view(v) for v in sim.vessels.values()], "train": sim.train,
            "boxes": boxes, "tracks": list(sim.tracks.values()), "weather": sim.weather_now,
            "reshuffling": list(sim.reshuffling.values()), "scans": list(sim.scans.values())}


@app.get("/api/stats")
async def stats():
    return await stats_frame()


@app.get("/api/debug")
def debug():
    """Crane allocation and who is waiting on whom, for diagnosing stalls."""
    return {"t": sim.env.now, "assign": sim.assign, "crane_x": sim.crane_x, "blocked": sim.blocked,
            "stuck": sim.stuck, "plans": {k: len(v) for k, v in sim.plans.items()}, "loading": sim.loading,
            "realloc_pending": sim.realloc_pending, "idle_x": sim.idle_x, "held": sim.held, "down": sorted(sim.down),
            "vessels": {b: {k: v[k] for k in ("phase", "left", "loaded", "exhausted")} for b, v in sim.vessels.items()},
            "agv_free": sim.agv_gate.level, "agv_at": sim.at}


@app.get("/api/containers/{box}")
async def container(box: BoxId):
    async with pool.connection() as conn:
        row = await q.get_container(conn, box)
    if not row:
        raise HTTPException(404, f"{box} has never been in the terminal")
    return row


@app.get("/api/containers/{box}/clearance")
async def clearance(box: BoxId):
    async with pool.connection() as conn:
        rows = await q.clearance(conn, box)
        if not rows:
            raise HTTPException(404, f"{box} is not stacked in the yard")
        return {"rows": rows, "plan": await q.explain_clearance(conn, box)}


@app.get("/api/containers/{box}/journey")
async def journey(box: BoxId):
    async with pool.connection() as conn:
        return await q.journey(conn, box)


# --- time travel -----------------------------------------------------------------------------

@app.get("/api/replay/seek")
async def seek(t: Annotated[float, Query(ge=0)]):
    """The terminal as it stood at simulated second t: boxes from the temporal history, everything
    else from the last logged event per unit."""
    t = min(t, sim.env.now)
    async with pool.connection() as conn:
        boxes = await q.boxes_as_of(conn, t)
        latest = await q.latest_events(conn, t, ["track", "vessel", "train", "scan", "reshuffle", "weather", "remove"],
                                       config.RETENTION_S)
        removes = await (await conn.execute(
            "SELECT t, body->'ids' AS ids FROM event_log WHERE type = 'remove' AND t <= %s AND t > %s",
            (t, t - config.RETENTION_S))).fetchall()
    gone: dict[str, float] = {}
    for r in removes:
        for i in r["ids"]:
            gone[i] = max(gone.get(i, 0.0), r["t"])
    by = lambda kind: [e for e in latest if e["type"] == kind]  # noqa: E731
    tracks = [e["body"] for e in by("track") if gone.get(e["unit"], -1) < e["t"]]
    vessels = [e["body"] for e in by("vessel") if e["body"]["status"] != "DEPARTED"]
    trains = [e["body"] for e in by("train") if e["body"]["status"] != "DEPARTED"]
    trains = [tr for tr in trains if gone.get(tr["unit"], -1) < tr["t"]]
    weather = by("weather")
    return {"sim": {"t": t, "speed": sim.speed, "paused": False},
            "boxes": [b for b in boxes if b["id"] not in gone or b["state"] != "GATE_OUT"],
            "tracks": tracks, "vessels": vessels, "train": max(trains, key=lambda x: x["t"]) if trains else None,
            "scans": [e["body"] for e in by("scan") if e["body"]["t1"] > t],
            "reshuffling": [e["body"] for e in by("reshuffle") if e["body"]["active"]],
            "weather": weather[0]["body"] if weather else sim.weather_now}


@app.get("/api/replay/events")
async def replay_events(t0: Annotated[float, Query(ge=0)], t1: Annotated[float, Query(ge=0)]):
    if t1 - t0 > 120:
        raise HTTPException(422, "ask for at most 120 s of events at a time")
    async with pool.connection() as conn:
        rows = await q.events_between(conn, t0, min(t1, sim.env.now))
    return [{**r["body"], "type": r["type"], "t": r["t"]} for r in rows]


# --- views onto the database -----------------------------------------------------------------

@app.get("/api/xray")
async def xray():
    """What the database is holding right now: road bookings ahead, crane rail envelopes, outages."""
    async with pool.connection() as conn:
        return {"t": sim.env.now, **(await q.xray(conn, sim.env.now, 12.0))}


@app.get("/api/visible")
async def visible(as_: Annotated[Literal["customs", "gate", "line"], Query(alias="as")],
                  line: Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")] = None):
    async with pool.connection() as conn:
        return {"role": ROLES[as_], "ids": await q.visible_to(conn, ROLES[as_], line)}


@app.get("/api/kpi")
async def kpi():
    async with pool.connection() as conn:
        rows = await q.kpi(conn, max(0.0, sim.env.now - 1500), 60.0)
    return {"buckets": rows, "strategy": [{"t": t, "name": n} for t, n in sim.strategy_log],
            "dual_cycles": sim.dual_cycles, "now": sim.env.now}


@app.get("/api/heat")
async def heat():
    async with pool.connection() as conn:
        return await q.heat(conn)


@app.get("/api/berth_plan")
async def berth_plan():
    async with pool.connection() as conn:
        return {"now": sim.env.now, "windows": await q.berth_plan(conn, sim.env.now - 300)}


@app.get("/api/dbstats")
async def dbstats():
    async with pool.connection() as conn:
        s = await q.db_stats(conn)
    return {**s, "retries": sim.waits.get("db_retry", 0), "refusals": dict(sim.refusals)}


# --- controls --------------------------------------------------------------------------------

@app.post("/api/sim/pause")
def pause():
    sim.paused = True
    return {"paused": True}


@app.post("/api/sim/resume")
def resume():
    sim.paused = False
    return {"paused": False}


@app.post("/api/sim/speed")
def speed(x: Annotated[float, Query(ge=0.25, le=16)]):
    sim.speed = x
    return {"speed": x}


@app.post("/api/chaos/auto")
def chaos_auto(on: bool):
    sim.chaos_auto = on
    return {"chaos_auto": on}


@app.post("/api/chaos/{kind}")
def chaos(kind: Literal["storm", "crane", "agv", "gate"]):
    sim.spawn(sim.chaos(kind))
    return {"chaos": kind}


@app.post("/api/strategy")
def strategy(name: Literal["BASIC", "SMART"]):
    config.YARD_STRATEGY = name
    sim.strategy_log.append((sim.env.now, name))
    sim.send("strategy", name=name)
    return {"strategy": name}


@app.post("/api/dual")
def dual(on: bool):
    sim.dual = on
    return {"dual": on}


@app.websocket("/ws")
async def stream(ws: WebSocket):
    await ws.accept()
    queue: asyncio.Queue = asyncio.Queue(CLIENT_BACKLOG)
    clients.add(queue)                     # subscribe before the snapshot: overlap is fine, gaps are not
    try:
        await ws.send_json({"type": "hello", "layout": layout.payload(), **(await snapshot())})
        while queue in clients:
            await ws.send_json(await queue.get())
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(queue)


if __name__ == "__main__":
    import os

    import uvicorn

    # psycopg async needs a selector loop (Windows defaults to Proactor)
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000))))
    asyncio.run(server.serve(), loop_factory=asyncio.SelectorEventLoop)
