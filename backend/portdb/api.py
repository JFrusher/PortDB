"""HTTP + WebSocket face of the terminal.

    python -m portdb.api        # resets the terminal on boot: in-flight SimPy state cannot be resumed
"""

import asyncio
import contextlib
import logging
import random
from typing import Annotated

from fastapi import FastAPI, HTTPException, Path, Query, WebSocket, WebSocketDisconnect

from . import layout, queries as q
from .db import make_pool
from .seed_vessel import reset, seed_yard
from .sim_engine import Terminal

log = logging.getLogger("portdb.api")
STATS_EVERY_S = 0.5
CLIENT_BACKLOG = 2000

clients: set[asyncio.Queue] = set()


def broadcast(ev: dict) -> None:
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
            "scanning": len(sim.scans),
            "queues": sim.queues(), "rejects": sim.rejects, **db_stats}


async def stats_loop() -> None:
    while True:
        await asyncio.sleep(STATS_EVERY_S)
        if clients:
            broadcast(await stats_frame())


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    global pool, sim
    pool = make_pool()
    await pool.open(wait=True)
    async with pool.connection() as conn:
        await reset(conn)
        await seed_yard(conn, random.Random())
    sim = Terminal(pool, broadcast)
    tasks = [asyncio.create_task(sim.run()), asyncio.create_task(stats_loop())]
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
    for b in boxes:                        # lorries are not equipment rows: the sim knows who carries what
        if b["id"] in sim.riding:
            b["carrier_id"] = sim.riding[b["id"]]
    return {"sim": {"t": round(sim.env.now, 3), "speed": sim.speed, "paused": sim.paused},
            "vessels": [sim.vessel_view(v) for v in sim.vessels.values()], "train": sim.train,
            "boxes": boxes, "tracks": list(sim.tracks.values()),
            "reshuffling": list(sim.reshuffling.values()), "scans": list(sim.scans.values())}


@app.get("/api/stats")
async def stats():
    return await stats_frame()


@app.get("/api/debug")
def debug():
    """Crane allocation and who is waiting on whom, for diagnosing stalls."""
    return {"t": sim.env.now, "assign": sim.assign, "crane_x": sim.crane_x, "blocked": sim.blocked,
            "stuck": sim.stuck, "plans": {k: len(v) for k, v in sim.plans.items()}, "loading": sim.loading,
            "realloc_pending": sim.realloc_pending, "idle_x": sim.idle_x,
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
