"""SimPy terminal model stepped from asyncio. SimPy owns time and queueing; PostgreSQL owns truth.

Bridge: a process that needs the database yields `self.db(fn, ...)`. That schedules `fn(conn, ...)`
as an asyncio task in its own pooled transaction and resumes the process when it commits (or raises
into it when the database refuses). Many such transactions are in flight at once.

Container flows:
    import    ship ─QC─▶ AGV ─RTG─▶ import yard ─┬─RTG─▶ lorry ─(X-ray)─▶ out-gate
                                                   └─RTG─▶ AGV ─(X-ray)─RMG─▶ wagon ─▶ train
    export    lorry ─RTG─▶ export yard           train ─RMG─▶ AGV ─RTG─▶ export yard
              export yard ─RTG─▶ AGV ─▶ park (staging) ─▶ QC ─▶ ship ─▶ sails
    tranship  ship ─QC─▶ AGV ─RTG─▶ export yard ─▶ ... loaded like an export onto a later ship

Collision avoidance: AGVs and lorries book (road cell, sim-time) rectangles before they move
(road_bookings, EXCLUDE USING gist). QCs and RMGs claim rail envelopes (numrange exclusion).
"""

import asyncio
import itertools
import logging
import math
import random
import string
from collections import Counter, deque

import simpy
from psycopg import errors

from . import config, layout as L, queries as q
from .seed_vessel import cargo, insert_boxes, new_inbound_rail, seed_vessel

log = logging.getLogger("portdb.sim")
TICK_S = 0.02
SPEED = {"AGV": config.AGV_NODES_PER_S, "LORRY": config.LORRY_SPEED}
QCS = [f"QC-{i}" for i in range(1, config.QC_COUNT + 1)]
RMGS = list(L.rmg_wagons())


def loc(r: dict) -> dict:
    return {k: r[k] for k in ("block_code", "bay", "row", "tier")}


def slot_point(r: dict):
    return L.slot(r["block_code"], r["bay"], r["row"], r["tier"])


def near(cells, to) -> list:
    """Cells nearest first: by Manhattan distance to a node, or by x alone when given a number."""
    if isinstance(to, tuple):
        return sorted(cells, key=lambda c: abs(c[0] - to[0]) + abs(c[1] - to[1]))
    return sorted(cells, key=lambda c: abs(c[0] - to))


# --- transactions: one function = one commit -------------------------------------------------

async def tx_berth(conn, rng, berth):
    v = await q.berthed_vessel(conn, berth)
    if not v:
        await seed_vessel(conn, rng, berth)
        v = await q.berthed_vessel(conn, berth)
    boxes = [b for b in await q.live_containers(conn) if b["vessel_id"] == v["id"]]
    return {**v, "boxes": boxes}


async def tx_claim(conn, qc, vessel, lo, hi, t):
    box = await q.claim_next_box(conn, vessel, lo, hi)
    if box:
        cell = {"vessel": vessel, "bay": box["vessel_bay"], "row": box["vessel_row"], "tier": box["vessel_tier"]}
        box["task"] = await q.open_task(conn, qc, box["id"], "DISCHARGE", cell, {"quay": qc}, t)
    return box


async def tx_handover(conn, qc, box, agv, t):
    block = await q.choose_block(conn, box["hazmat"], "IMPORT" if box["flow"] == "IMPORT" else "EXPORT")
    if block is None:
        return None
    await q.load_on_agv(conn, box["id"], agv)
    await q.close_task(conn, box["task"], t)
    return block, await q.open_task(conn, agv, box["id"], "TRANSPORT", {"quay": qc}, {"block": block}, t)


async def tx_stack(conn, box, block, agv, task, t0, t1):
    s = await q.allocate_slot(conn, block)
    if s is None:
        raise RuntimeError(f"no_slot: block {block} full")
    await q.move_to_slot(conn, box, s, "STACKED_IN_YARD")
    await q.close_task(conn, task, t0)
    await q.log_task(conn, f"RTG-{block}", box, "STACK", {"agv": agv}, s, t0, t1)
    return s


async def tx_reshuffle(conn, blocker, t0, t1):
    s = await q.allocate_slot(conn, blocker["block_code"], avoid=(blocker["bay"], blocker["row"]))
    if s is None:
        raise RuntimeError(f"no_slot: nowhere to put blocker {blocker['id']}")
    await q.move_to_slot(conn, blocker["id"], s)
    await q.log_task(conn, f"RTG-{blocker['block_code']}", blocker["id"], "RESHUFFLE", loc(blocker), s, t0, t1)
    return s


async def tx_load_truck(conn, target, gate, lorry, moves, t0, t1):
    await q.load_on_truck(conn, target["id"])
    await q.log_task(conn, f"RTG-{target['block_code']}", target["id"], "RETRIEVE", loc(target), {"lorry": lorry}, t0, t1)
    await q.gate_update(conn, gate, status="LOADED", shifter_moves=moves)


async def tx_drop_from_truck(conn, box, block, gate, lorry, t0, t1):
    s = await q.allocate_slot(conn, block)
    if s is None:
        raise RuntimeError(f"no_slot: export block {block} full")
    await q.move_to_slot(conn, box, s, "STACKED_IN_YARD")
    await q.log_task(conn, f"RTG-{block}", box, "DROP", {"lorry": lorry}, s, t0, t1)
    await q.gate_update(conn, gate, status="LOADED")
    return s


async def tx_load_agv_from_yard(conn, target, agv, t0, t1):
    await q.load_on_agv(conn, target["id"], agv)
    await q.log_task(conn, f"RTG-{target['block_code']}", target["id"], "RETRIEVE", loc(target), {"agv": agv}, t0, t1)


async def tx_inspect(conn, scanner, box, t0, t1):
    await q.mark_inspected(conn, box)
    await q.log_task(conn, scanner, box, "INSPECT", {"scanner": scanner}, {"result": "CLEAR"}, t0, t1)


async def tx_load_wagon(conn, rmg, box, agv, train, wagon, wagon_slot, t0, t1):
    await q.load_on_wagon(conn, box, train, wagon, wagon_slot)
    await q.log_task(conn, rmg, box, "RAIL_LOAD", {"agv": agv}, {"train": train, "wagon": wagon, "slot": wagon_slot}, t0, t1)


async def tx_unload_wagon(conn, rmg, box, agv, block, train, wagon, wagon_slot, t0, t1):
    await q.load_on_agv(conn, box["id"], agv)
    await q.log_task(conn, rmg, box["id"], "RAIL_UNLOAD", {"train": train, "wagon": wagon, "slot": wagon_slot},
                     {"agv": agv}, t0, t1)
    return await q.open_task(conn, agv, box["id"], "TRANSPORT", {"rmg": rmg}, {"block": block}, t0)


async def tx_load_vessel(conn, box, vessel, bay, row, tier):
    await q.load_on_vessel(conn, box, vessel, bay, row, tier)


async def tx_stow(conn, qc, box, agv, cell, t0, t1):
    await q.stow(conn, box)
    await q.log_task(conn, qc, box, "LOAD", {"agv": agv}, cell, t0, t1)


async def tx_gate_out(conn, box, gate):
    if box:
        await q.gate_out(conn, box)
    await q.gate_depart(conn, gate)


async def tx_export_arrival(conn, rng, plate):
    """A lorry turns up with an export box. Turned away (None) if the export yard is full."""
    box = cargo(rng, "EXPORT")
    block = await q.choose_block(conn, box["hazmat"], "EXPORT")
    if block is None:
        return None
    await insert_boxes(conn, [box | {"state": "ON_TRUCK"}])
    gate = await (await conn.execute(
        "INSERT INTO gate_transactions (plate, container_id) VALUES (%s, %s) RETURNING id", (plate, box["id"]))).fetchone()
    return box | {"state": "ON_TRUCK", "dest_block": block, "gate_id": gate["id"]}


async def tx_train_arrives(conn, rng, name):
    train = await q.arrive_train(conn, name, config.TRAIN_WAGONS)
    return train, await new_inbound_rail(conn, rng, train)


# --- the terminal ----------------------------------------------------------------------------

class Terminal:
    def __init__(self, pool, emit=lambda ev: None, seed: int | None = None):
        self.pool, self.emit = pool, emit
        self.rng = random.Random(seed)
        self.env = simpy.Environment()
        self.speed, self.paused = 1.0, False
        self.agv_gate = simpy.Container(self.env, capacity=config.AGV_COUNT, init=config.AGV_COUNT)
        self.rtg = {b: simpy.Resource(self.env, capacity=1) for b in config.BLOCKS}
        self.lanes = simpy.Store(self.env)
        self.lanes.items.extend(range(1, config.GATE_LANES + 1))
        self.at = {f"AGV-{i}": L.PARKS[i - 1] for i in range(1, config.AGV_COUNT + 1)}
        self.crane_x = dict(zip(QCS, L.qc_homes())) | dict(zip(RMGS, L.rmg_homes()))
        self.tracks: dict[str, dict] = {}         # latest motion per unit/box, replayed to new clients
        self.vessels: dict[int, dict] = {}        # berth -> live call
        self.assign: dict[str, tuple | None] = dict.fromkeys(QCS)
        self.idle_x: dict[str, float] = {}
        self.reassigned = self.env.event()
        self.realloc_pending = False
        self.plans: dict[str, deque] = {qc: deque() for qc in QCS}
        self.loading: dict[str, bool] = dict.fromkeys(QCS, False)   # a popped step still in the crane's hands
        self.train: dict | None = None
        self.reshuffling: dict[str, dict] = {}
        self.scans: dict[str, dict] = {}
        self.riding: dict[str, str] = {}          # box -> lorry (lorries are not equipment rows)
        self.gate_queue: list[str] = []
        self.spots_taken: set = set()
        self.idle_on_spur: set[str] = set()
        self.stuck: dict[str, tuple] = {}              # vehicle -> what it is waiting for (diagnostics)
        self.blocked: dict[str, tuple] = {}            # crane -> (at, wants) while a neighbour is in the way
        self.waits = Counter()                    # how often vehicles / cranes had to yield to someone
        self.rejects = 0
        self._jobs: set[asyncio.Task] = set()

    # bridge ------------------------------------------------------------------------------------
    def db(self, fn, *args, **kwargs) -> simpy.Event:
        ev = self.env.event()

        async def job():
            for attempt in range(4):
                try:
                    async with self.pool.connection() as conn:
                        result = await fn(conn, *args, **kwargs)
                except (errors.DeadlockDetected, errors.SerializationFailure) as e:
                    if attempt < 3:                    # the victim of a lock cycle: its whole step rolled back, retry
                        self.waits["db_retry"] += 1
                        await asyncio.sleep(0.005 * (attempt + 1))
                        continue
                    e.add_note(f"in {fn.__name__}")
                    ev.fail(e)
                except Exception as e:
                    e.add_note(f"in {fn.__name__}")
                    ev.fail(e)
                else:
                    ev.succeed(result)
                return

        task = asyncio.get_running_loop().create_task(job())
        self._jobs.add(task)
        task.add_done_callback(self._jobs.discard)
        return ev

    def spawn(self, gen) -> simpy.Process:
        return self.env.process(self.guard(gen))

    def guard(self, gen):
        try:
            return (yield from gen)
        except Exception as e:
            self.reject(e)

    def reject(self, e: Exception) -> None:
        self.rejects += 1
        log.warning("rejected: %s %s", e, " ".join(getattr(e, "__notes__", [])))
        self.send("reject", error=str(e).splitlines()[0])

    async def run(self, until: float | None = None) -> None:
        async with self.pool.connection() as conn:
            for agv, (x, z) in self.at.items():
                await q.park(conn, agv, x, z, 0.0)
        for b in config.BERTHS:
            self.spawn(self.berth(b))
        for qc in QCS:
            self.spawn(self.qc_worker(qc))
        for flow in config.LORRY_MEAN_GAP_S:
            self.spawn(self.lorry_source(flow))
        self.spawn(self.trains())
        self.spawn(self.pruner())
        loop = asyncio.get_running_loop()
        last = loop.time()
        while until is None or self.env.now < until:
            await asyncio.sleep(TICK_S)
            now = loop.time()
            if not self.paused:
                self.env.run(until=self.env.now + (now - last) * self.speed)
            last = now
        await asyncio.gather(*self._jobs, return_exceptions=True)

    # output ------------------------------------------------------------------------------------
    def send(self, kind: str, **body) -> None:
        self.emit({"type": kind, "t": round(self.env.now, 3), **body})

    def track(self, unit: str, kind: str, path, duration: float, t0: float | None = None) -> None:
        t0 = self.env.now if t0 is None else t0
        ev = {"type": "track", "id": unit, "kind": kind, "t0": round(t0, 3), "t1": round(t0 + duration, 3),
              "path": [[round(c, 3) for c in p] for p in path]}
        self.tracks[unit] = ev
        self.emit(ev)

    def remove(self, *ids: str) -> None:
        for i in ids:
            self.tracks.pop(i, None)
            self.riding.pop(i, None)
        self.send("remove", ids=list(ids))

    def vessel_view(self, v: dict) -> dict:
        keys = ("id", "name", "imo", "berth", "bays", "rows", "tiers", "status", "phase", "t0", "t1",
                "manifest", "left", "loaded")
        return {k: v[k] for k in keys}

    def queues(self) -> dict:
        return {"agv_wait": len(self.agv_gate.get_queue),
                "rtg": {b: len(r.queue) for b, r in self.rtg.items()},
                "gate": len(self.gate_queue),
                "waits": dict(self.waits)}

    # movement: booked routes -------------------------------------------------------------------
    def legs_for(self, kind: str, cells: list, depart: float, dwell: dict, park_end: bool):
        """(x, z, t0, t1) per cell and the arrival time at each. cells[0] is where the vehicle stands."""
        step = 1 / SPEED[kind]
        before, after = config.BOOK_MARGIN[kind]
        legs, times, t = [], [], depart
        for i, (x, z) in enumerate(cells):
            stay = dwell.get(i, (0.0, None))[0]
            last = i == len(cells) - 1
            t0 = depart if i == 0 else t - before
            t1 = config.PARKED if (last and park_end) else t + stay + step + after
            legs.append((x, z, t0, t1))
            times.append(t)
            t += stay + step
        return legs, times

    def travel(self, unit: str, kind: str, goals=None, cells=None, via=None, dwell=None, park_end=True):
        """Book a conflict-free route in (cell, time), then drive it.
        AGVs: BFS to the first free goal (optionally through `via`, dwelling there), re-routing around
        clashes and otherwise departing later. Lorries: fixed `cells`, departing later on a clash.
        `dwell` maps a route index (or "via") to (seconds, action generator factory | None)."""
        dwell = dict(dwell or {})
        delay, avoid, tries = 0.0, set(), 0
        while True:
            if kind == "AGV":
                free = yield self.db(q.free_cells, list(goals), unit)
                if not free:
                    self.waits["destination"] += 1
                    self.stuck[unit] = ("destination", list(goals)[:3])
                    yield self.env.timeout(1.0)
                    continue
                self.stuck.pop(unit, None)
                goal, start = free[0], self.at[unit]
                if goal == start and via is None:
                    return
                legs_a = L.road_path(start, via or goal, frozenset(avoid))
                legs_b = L.road_path(via, goal, frozenset(avoid)) if via else []
                if legs_a is None or (via and legs_b is None):
                    avoid.clear()
                    delay += 0.5
                    continue
                route = legs_a + legs_b[1:]
                plan_dwell = {len(legs_a) - 1: dwell["via"]} if via else {}
            else:
                route, plan_dwell = cells, dwell
            depart = self.env.now + 0.2 + delay
            legs, times = self.legs_for(kind, route, depart, plan_dwell, park_end)
            clashes = yield self.db(q.route_clashes, unit, legs)
            if not clashes:
                if (yield self.db(q.book_route, unit, legs, depart)):
                    break
                self.waits["race"] += 1                  # someone booked between our check and insert
                continue
            tries += 1
            self.waits[kind.lower()] += 1
            if kind == "AGV" and tries % 3:
                avoid |= {(c["x"], c["z"]) for c in clashes} - {route[0], route[-1]}
            else:
                avoid.clear()
                delay += 0.5
            if delay > 20:
                yield self.env.timeout(1.0)
                delay = 0.0
        # drive it: one track per stretch between stops, running the stop's action while parked there
        if depart > self.env.now:
            yield self.env.timeout(depart - self.env.now)
        stops = sorted(plan_dwell) + [len(route) - 1]
        prev = 0
        for i in stops:
            if i > prev:
                leave_at = times[prev] + (plan_dwell[prev][0] if prev in plan_dwell else 0.0)
                duration = times[i] - leave_at
                self.track(unit, kind, [L.point(c) for c in route[prev:i + 1]], duration)
                yield self.env.timeout(duration)
            if i in plan_dwell:
                seconds, action = plan_dwell[i]
                if action:
                    yield from action()
                else:
                    yield self.env.timeout(seconds)
            prev = i
        if kind == "AGV":
            self.at[unit] = route[-1]

    def roll(self, unit: str, kind: str, path, speed: float | None = None, duration: float | None = None):
        """Unbooked motion: trains on their own track, lorries queueing west of the gate."""
        if duration is None:
            duration = sum(math.dist(a, b) for a, b in zip(path, path[1:])) / speed
        self.track(unit, kind, path, duration)
        yield self.env.timeout(duration)

    def crane(self, unit: str, kind: str, box: str, src, dst, clear_y: float, duration: float) -> None:
        """Spreader descends to src, hoists, travels, lowers to dst. The box rides from the second waypoint."""
        up_src, up_dst = L.lifted(src, clear_y), L.lifted(dst, clear_y)
        self.track(unit, kind, [up_src, src, up_src, up_dst, dst], duration)
        self.track(box, "BOX", [src, src, up_src, up_dst, dst], duration)

    def gantry(self, crane: str, x: float, job="any"):
        """Travel along the rail. The DB refuses an envelope that overlaps a neighbour, so cranes wait.
        Given the job the move is for, gives up (returns False) if the crane is reassigned meanwhile."""
        cur = self.crane_x[crane]
        if abs(cur - x) < 1e-3:
            return True
        half = L.QC_HALF_WIDTH if crane.startswith("QC") else L.RMG_HALF_WIDTH
        while not (yield self.db(q.claim_rail, crane, min(cur, x) - half, max(cur, x) + half)):
            self.waits["crane"] += 1
            self.blocked[crane] = (cur, x)
            if job != "any" and self.assign.get(crane) != job:
                self.blocked.pop(crane, None)
                return False
            yield self.env.timeout(0.3 + 0.5 * self.rng.random())
        self.blocked.pop(crane, None)
        kind, clear = ("QC", L.QC_CLEAR_Y) if crane.startswith("QC") else ("RMG", L.RMG_CLEAR_Y)
        last = self.tracks.get(crane)
        p = last["path"][-1] if last else (cur, clear, -2.0 if kind == "QC" else L.RAIL_Z - 1)
        speed = config.QC_GANTRY_SPEED if kind == "QC" else config.RMG_GANTRY_SPEED
        duration = abs(x - cur) / speed + 0.6
        self.track(crane, kind, [p, (p[0], clear, p[2]), (x, clear, p[2])], duration)
        yield self.env.timeout(duration)
        yield self.db(q.claim_rail, crane, x - half, x + half)   # shrink to where we stand: a subset, never clashes
        self.crane_x[crane] = x
        return True

    # AGV pool ----------------------------------------------------------------------------------
    def claim_agv(self, near_node):
        yield self.agv_gate.get(1)
        agv = yield self.db(q.claim_agv, *near_node)
        self.idle_on_spur.discard(agv)
        return agv

    def free_agv(self, agv: str):
        """Hand the AGV straight to the next job if one is waiting (it stands on a dead-end spur, so it
        blocks no one); otherwise, or if nobody takes it within 2 s, it drives to the nearest park."""
        if self.agv_gate.get_queue and self.at[agv] not in L.PARKS:
            yield self.db(q.release_equipment, agv, *self.at[agv])
            self.idle_on_spur.add(agv)
            yield self.agv_gate.put(1)
            self.spawn(self.sweep(agv))
            return
        yield from self.travel(agv, "AGV", goals=near(L.PARKS, self.at[agv]))
        yield self.db(q.release_equipment, agv, *self.at[agv])
        yield self.agv_gate.put(1)

    def rescue(self, agv: str, e: Exception):
        """A job holding an AGV failed: report it, then send the AGV home so the pool never leaks."""
        self.reject(e)
        try:
            yield from self.free_agv(agv)
        except Exception as e2:                           # cannot even drive home: release it where it stands
            self.reject(e2)
            yield self.db(q.release_equipment, agv, *self.at[agv])
            yield self.agv_gate.put(1)

    def sweep(self, agv: str):
        yield self.env.timeout(2.0)
        if agv not in self.idle_on_spur or self.agv_gate.level < 1:
            return
        yield self.agv_gate.get(1)
        if not (yield self.db(q.claim_this_agv, agv)):
            yield self.agv_gate.put(1)                    # somebody claimed it in the meantime
            return
        self.idle_on_spur.discard(agv)
        yield from self.free_agv(agv)

    def agv_to_stack(self, agv: str, box: str, block: str, task: int):
        try:
            yield from self.travel(agv, "AGV", goals=near(L.block_spurs(block), self.at[agv][0]))
            spur = self.at[agv]
            with self.rtg[block].request() as turn:
                yield turn
                t0 = self.env.now
                s = yield self.db(tx_stack, box, block, agv, task, t0, t0 + config.RTG_MOVE_S)
                self.crane(f"RTG-{block}", "RTG", box, L.point(spur, L.AGV_BED_Y), slot_point(s), L.RTG_CLEAR_Y,
                           config.RTG_MOVE_S)
                self.send("box", id=box, state="STACKED_IN_YARD", carrier_id=None, **s)
                yield self.env.timeout(config.RTG_MOVE_S)
        except Exception as e:
            yield from self.rescue(agv, e)
            return
        yield from self.free_agv(agv)

    def yard_to_agv(self, block: str, box: str, agv: str):
        """AGV is on one of the block's spurs: the RTG digs the box out and sets it on the AGV."""
        spur = self.at[agv]
        with self.rtg[block].request() as turn:
            yield turn
            target, _ = yield from self.dig_out(block, box)
            t0 = self.env.now
            yield self.db(tx_load_agv_from_yard, target, agv, t0, t0 + config.RTG_MOVE_S)
            self.crane(f"RTG-{block}", "RTG", box, slot_point(target), L.point(spur, L.AGV_BED_Y),
                       L.RTG_CLEAR_Y, config.RTG_MOVE_S)
            self.send("box", id=box, state="IN_TRANSIT", carrier_id=agv)
            yield self.env.timeout(config.RTG_MOVE_S)

    # shared steps ------------------------------------------------------------------------------
    def dig_out(self, block: str, box: str):
        """Caller holds the block's RTG. Re-handle every box above `box`; return its row and the hop count."""
        rows = yield self.db(q.clearance, box)
        *blockers, target = rows
        if blockers:
            self.reshuffling[block] = {"block": block, "target": box, "moves": len(blockers)}
            self.send("reshuffle", active=True, **self.reshuffling[block])
        for b in blockers:
            t0 = self.env.now
            s = yield self.db(tx_reshuffle, b, t0, t0 + config.RTG_MOVE_S)
            self.crane(f"RTG-{block}", "RTG", b["id"], slot_point(b), slot_point(s), L.RTG_CLEAR_Y, config.RTG_MOVE_S)
            self.send("box", id=b["id"], **s)
            yield self.env.timeout(config.RTG_MOVE_S)
        if blockers:
            self.send("reshuffle", active=False, **self.reshuffling.pop(block))
        return target, len(blockers)

    def inspect(self, scanner: str, box: str):
        """Drive-through X-ray while the vehicle sits in the portal (its booking covers the dwell)."""
        t0 = self.env.now
        self.scans[scanner] = {"id": scanner, "box": box, "t0": t0, "t1": t0 + config.INSPECT_S}
        self.send("scan", **self.scans[scanner])
        yield self.env.timeout(config.INSPECT_S)
        yield self.db(tx_inspect, scanner, box, t0, self.env.now)
        self.scans.pop(scanner, None)
        self.send("box", id=box, inspected=True)

    # quay: crane allocation --------------------------------------------------------------------
    def work_bays(self, v: dict) -> list[int]:
        if v["phase"] == "DISCHARGE":
            return [b for b in range(1, v["bays"] + 1) if v["bay_left"][b]]
        return list(range(1, v["bays"] + 1))

    def reallocate(self) -> None:
        """Share the six QCs between berths with work, keeping their west-to-east order (they cannot
        pass each other). Each berth gets cranes in proportion to its open bays, at least one, at
        most one per QC_MIN_BAYS bays; spare cranes park at the rail ends."""
        active = [b for b in sorted(self.vessels) if self.vessels[b]["phase"] in ("DISCHARGE", "LOAD")]
        bays = {b: self.work_bays(self.vessels[b]) for b in active}
        active = [b for b in active if bays[b]]
        counts = {b: 1 for b in active}
        caps = {b: max(1, len(bays[b]) // config.QC_MIN_BAYS) for b in active}
        spare = len(QCS) - len(active)
        while spare > 0 and any(counts[b] < caps[b] for b in active):
            b = max((b for b in active if counts[b] < caps[b]), key=lambda b: len(bays[b]) / counts[b])
            counts[b] += 1
            spare -= 1
        west = spare // 2
        new = dict.fromkeys(QCS)
        i = west
        for b in active:
            chunks = [bays[b][k * len(bays[b]) // counts[b]:(k + 1) * len(bays[b]) // counts[b]] for k in range(counts[b])]
            for chunk in chunks:
                new[QCS[i]] = (b, chunk)
                i += 1
        for k in range(west):
            self.idle_x[QCS[k]] = -4.0 + 2.0 * k
        for k, qc in enumerate(QCS[i:]):
            self.idle_x[qc] = 47.0 + 2.0 * k
        if new == self.assign:
            self.realloc_pending = False
            return
        if any(self.plans.values()) or any(self.loading.values()):
            # boxes are already on their way to some cranes: let those pipelines drain first
            self.realloc_pending = True
            return
        self.realloc_pending = False
        self.assign = new
        fired, self.reassigned = self.reassigned, self.env.event()
        fired.succeed()

    def qc_worker(self, qc: str):
        while True:
            job = self.assign[qc]
            v = self.vessels.get(job[0]) if job else None
            if not v or v["phase"] not in ("DISCHARGE", "LOAD"):
                if job is None and qc in self.idle_x:
                    yield from self.gantry(qc, self.idle_x[qc], job=None)
                if self.assign[qc] == job:
                    yield self.reassigned | self.env.timeout(5)
                continue
            try:
                did = yield from (self.qc_discharge if v["phase"] == "DISCHARGE" else self.qc_load)(qc, v, job)
            except Exception as e:
                self.reject(e)
                did = True
            if did:
                v["exhausted"].discard(qc)
                continue
            if v["phase"] == "LOAD":
                v["exhausted"].add(qc)
                self.check_load_done(v)
            self.reallocate()
            if self.assign[qc] == job:
                yield self.reassigned | self.env.timeout(5)

    # quay: discharge ---------------------------------------------------------------------------
    def qc_discharge(self, qc: str, v: dict, job: tuple):
        bays = job[1]
        bay = yield self.db(q.peek_next_bay, v["id"], bays[0], bays[-1])
        if bay is None:
            return False
        spur = L.qc_spur(v["berth"], bay)
        if self.assign[qc] != job or not (yield from self.gantry(qc, spur[0], job=job)):
            return True                                   # reassigned while waiting for the rail: start over
        box = yield self.db(tx_claim, qc, v["id"], bay, bay, self.env.now)
        if not box:
            return True
        v["bay_left"][bay] -= 1
        self.send("box", id=box["id"], state="DISCHARGING")
        agv = yield from self.claim_agv(spur)
        arrived = self.env.process(self.travel(agv, "AGV", goals=[spur]))
        cycle = self.rng.uniform(*config.QC_CYCLE)
        src = L.ship_cell(v["berth"], bay, box["vessel_row"], box["vessel_tier"])
        self.crane(qc, "QC", box["id"], src, L.point(spur, L.AGV_BED_Y), L.QC_CLEAR_Y, cycle)
        try:
            yield self.env.timeout(cycle) & arrived
        except Exception as e:
            self.spawn(self.rescue(agv, e))
            raise
        while not (handed := (yield self.db(tx_handover, qc, box, agv, self.env.now))):
            yield self.env.timeout(1)                     # every eligible block at capacity: hold the box
        block, task = handed
        self.send("box", id=box["id"], state="IN_TRANSIT", carrier_id=agv)
        self.spawn(self.agv_to_stack(agv, box["id"], block, task))
        v["left"] -= 1
        if v["left"] == 0 and not v["phase_done"].triggered:
            v["phase_done"].succeed()
        return True

    # quay: load --------------------------------------------------------------------------------
    def plan_step(self, qc: str, v: dict, bays: list[int]):
        """Next cell in the QC's bays (stack by stack, bottom-up) and an export box that may sit there.
        A stack closes when nothing in the yard satisfies the box beneath. A stack with boxes still on
        their way for one crane is left alone by the others, so tiers always go on in order."""
        for bay in bays:
            for row in range(1, v["rows"] + 1):
                stack = (bay, row)
                height = v["height"].get(stack, 0)
                owner, pending = v["owner"].get(stack, (qc, 0))
                if stack in v["closed"] or height >= v["tiers"] or (pending and owner != qc):
                    continue
                below = v["top"].get(stack)
                box = yield self.db(q.pick_export, *(below or (None, None)))
                if box is None:
                    if below is None:
                        return None                       # no exports in the yard at all right now
                    v["closed"].add(stack)
                    continue
                v["height"][stack] = height + 1
                v["top"][stack] = (box["weight_kg"], box["rotation"])
                v["owner"][stack] = (qc, pending + 1)
                return {"box": box, "cell": (bay, row, height + 1), "spur": L.qc_spur(v["berth"], bay),
                        "turn": self.env.event(), "arrived": self.env.event()}
        return None

    def bays_full(self, v: dict, bays: list[int]) -> bool:
        return all((b, r) in v["closed"] or v["height"].get((b, r), 0) >= v["tiers"]
                   for b in bays for r in range(1, v["rows"] + 1))

    def qc_load(self, qc: str, v: dict, job: tuple):
        bays = job[1]
        plan = self.plans[qc]
        while len(plan) <= config.LOAD_PREFETCH and self.env.now < v["deadline"] and not self.realloc_pending:
            step = yield from self.plan_step(qc, v, bays)
            if step is None:
                break
            plan.append(step)
            self.spawn(self.fetch_export(v, step))
        if not plan:
            if self.realloc_pending and not self.loading[qc]:
                self.reallocate()                         # our pipeline is empty: maybe that was the last one
                yield self.reassigned | self.env.timeout(2)
                return True
            if self.env.now >= v["deadline"] or self.bays_full(v, bays):
                return False
            yield self.env.timeout(5)                     # waiting for exports to reach the yard
            return True
        step = plan.popleft()
        self.loading[qc] = True
        try:
            return (yield from self.load_step(qc, v, step, plan))
        finally:
            self.loading[qc] = False

    def load_step(self, qc: str, v: dict, step: dict, plan: deque):
        # this AGV may come forward; so may the next one, unless it is bound for the same spur
        for s in (step, *[n for n in list(plan)[:1] if n["spur"] != step["spur"]]):
            if not s["turn"].triggered:
                s["turn"].succeed()
        agv = yield step["arrived"]
        bay, row, tier = step["cell"]
        box = step["box"]["id"]
        yield from self.gantry(qc, step["spur"][0])
        t0 = self.env.now
        yield self.db(tx_load_vessel, box, v["id"], bay, row, tier)
        cycle = self.rng.uniform(*config.QC_CYCLE)
        self.crane(qc, "QC", box, L.point(step["spur"], L.AGV_BED_Y), L.ship_cell(v["berth"], bay, row, tier),
                   L.QC_CLEAR_Y, cycle)
        self.send("box", id=box, state="LOADING", carrier_id=None, vessel_id=v["id"],
                  vessel_bay=bay, vessel_row=row, vessel_tier=tier)
        self.spawn(self.free_agv(agv))
        yield self.env.timeout(cycle)
        yield self.db(tx_stow, qc, box, agv, {"vessel": v["id"], "bay": bay, "row": row, "tier": tier}, t0, self.env.now)
        self.send("box", id=box, state="STOWED_ON_BOARD")
        owner, pending = v["owner"][(bay, row)]
        v["owner"][(bay, row)] = (owner, pending - 1)
        v["loaded"] += 1
        return True

    def fetch_export(self, v: dict, step: dict):
        """Yard -> AGV -> park nearest the QC (staging) -> QC spur, in the QC's order."""
        box, block = step["box"]["id"], step["box"]["block_code"]
        agv = None
        try:
            self.send("box", id=box, state="RETRIEVING")
            agv = yield from self.claim_agv(L.block_spurs(block)[0])
            yield from self.travel(agv, "AGV", goals=near(L.block_spurs(block), self.at[agv][0]))
            yield from self.yard_to_agv(block, box, agv)
            if not step["turn"].triggered:
                yield from self.travel(agv, "AGV", goals=near(L.PARKS, step["spur"]))
                yield step["turn"]
            yield from self.travel(agv, "AGV", goals=[step["spur"]])
            step["arrived"].succeed(agv)
        except Exception as e:
            step["arrived"].fail(e)
            step["arrived"].defused = True
            if agv:
                yield from self.rescue(agv, e)

    def check_load_done(self, v: dict) -> None:
        mine = {qc for qc, job in self.assign.items() if job and job[0] == v["berth"]}
        if v["phase"] == "LOAD" and mine <= v["exhausted"] and not any(self.plans[qc] or self.loading[qc] for qc in mine) \
                and not v["phase_done"].triggered:
            v["phase_done"].succeed()

    # quay: vessel calls ------------------------------------------------------------------------
    def berth(self, b: int):
        yield self.env.timeout(config.FIRST_CALL_S[b])
        while True:
            while (yield self.db(q.yard_fill, "IMPORT")) > config.NEXT_VESSEL_YARD_MAX:
                yield self.env.timeout(3)
            row = yield self.db(tx_berth, self.rng, b)
            now = self.env.now
            v = {"id": row["id"], "name": row["name"], "imo": row["imo"], "berth": b,
                 "bays": row["bays"], "rows": row["rows"], "tiers": row["tiers"],
                 "status": "ARRIVING", "phase": None, "t0": now, "t1": now + config.VESSEL_SAIL_S,
                 "manifest": len(row["boxes"]), "left": len(row["boxes"]), "loaded": 0,
                 "bay_left": Counter(x["vessel_bay"] for x in row["boxes"]),
                 "height": {}, "top": {}, "owner": {}, "closed": set(), "exhausted": set(),
                 "deadline": 0.0, "phase_done": self.env.event()}
            self.vessels[b] = v
            self.send("vessel", **self.vessel_view(v))
            self.send("boxes", boxes=row["boxes"])
            yield self.env.timeout(config.VESSEL_SAIL_S)

            v.update(status="BERTHED", phase="DISCHARGE")
            self.send("vessel", **self.vessel_view(v))
            self.reallocate()
            if v["left"]:
                yield v["phase_done"]

            yield self.db(q.vessel_status, v["id"], "LOADING")
            v.update(status="LOADING", phase="LOAD", deadline=self.env.now + config.LOAD_WINDOW_S,
                     phase_done=self.env.event())
            self.send("vessel", **self.vessel_view(v))
            self.reallocate()
            self.check_load_done(v)
            yield v["phase_done"]

            v["phase"] = None
            self.reallocate()
            sailed = yield self.db(q.sail, v["id"])
            now = self.env.now
            v.update(status="DEPARTING", t0=now, t1=now + config.VESSEL_SAIL_S)
            self.send("vessel", **self.vessel_view(v))
            yield self.env.timeout(config.VESSEL_SAIL_S)
            self.send("vessel", **{**self.vessel_view(v), "status": "DEPARTED"})
            self.remove(*sailed)
            del self.vessels[b]
            yield self.env.timeout(config.NEXT_VESSEL_GAP_S)

    # road ----------------------------------------------------------------------------------------
    def lorry_source(self, flow: str):
        for n in itertools.count(1):
            yield self.env.timeout(self.rng.expovariate(1 / config.LORRY_MEAN_GAP_S[flow]))
            self.spawn(self.lorry(f"LRY-{flow[0]}{n}", flow))

    def queue_shift(self) -> None:
        for k, lorry in enumerate(self.gate_queue):
            tr = self.tracks.get(lorry)
            x = L.QUEUE_HEAD_X - 2.2 * k
            if tr and abs(tr["path"][-1][0] - x) > 0.01:
                self.track(lorry, "LORRY", [tr["path"][-1], (x, 0, L.THRU_Z)], 1.2)

    def lorry(self, lorry: str, flow: str):
        plate = "".join(self.rng.choices(string.ascii_uppercase, k=2)) + f"{self.rng.randint(10, 99)} " + \
                "".join(self.rng.choices(string.ascii_uppercase, k=3))
        if flow == "IMPORT":
            pick = yield self.db(q.pick_pickup, plate)
            if not pick:
                return                                    # nothing booked for road in the yard
            box, block, gate, hold = pick["box"], pick["block_code"], pick["gate_id"], pick["customs_hold"]
            self.send("box", id=box, state="RETRIEVING")
        else:
            arrival = yield self.db(tx_export_arrival, self.rng, plate)
            if not arrival:
                return                                    # export yard full: turned away at the gate
            box, block, gate, hold = arrival["id"], arrival["dest_block"], arrival["gate_id"], False
            self.riding[box] = lorry
            self.send("boxes", boxes=[{**arrival, "carrier_id": lorry}])
        thru = lambda x: (x, L.THRU_Z)  # noqa: E731

        # queue at the in-gate (west of it nothing else drives, so the queue is not booked)
        self.gate_queue.append(lorry)
        self.send("lorry", id=lorry, plate=plate, box=box, block=block, flow=flow, hold=hold)
        k = len(self.gate_queue) - 1
        yield from self.roll(lorry, "LORRY", [(L.LORRY_SPAWN_X, 0, L.THRU_Z), (L.QUEUE_HEAD_X - 2.2 * k, 0, L.THRU_Z)],
                             config.LORRY_SPEED)
        yield self.lanes.get()
        while self.gate_queue[0] != lorry:                # FIFO by construction; belt and braces
            yield self.env.timeout(0.2)
        tr = self.tracks[lorry]
        if abs(tr["path"][-1][0] - L.QUEUE_HEAD_X) > 0.01:
            yield self.env.timeout(max(0.0, tr["t1"] - self.env.now))

        bays = [b for b in range(1, config.BLOCK_BAYS + 1) if L.lorry_spot(block, b) not in self.spots_taken]
        spot = L.lorry_spot(block, bays[0] if bays else 1)
        self.spots_taken.add(spot)
        route = L.cells_along([thru(L.QUEUE_HEAD_X), thru(L.GATE_IN_X), thru(spot[0]), spot])
        gate_i = route.index(thru(L.GATE_IN_X))
        self.gate_queue.remove(lorry)
        self.queue_shift()
        yield from self.travel(lorry, "LORRY", cells=route, dwell={gate_i: (config.GATE_CHECKIN_S, None)})
        yield self.db(q.gate_update, gate, status="AT_LANE")

        bed = (spot[0], L.LORRY_BED_Y, spot[1])
        with self.rtg[block].request() as turn:
            yield turn
            t0 = self.env.now
            if flow == "IMPORT":
                target, moves = yield from self.dig_out(block, box)
                yield self.db(tx_load_truck, target, gate, lorry, moves, t0, t0 + config.RTG_MOVE_S)
                self.crane(f"RTG-{block}", "RTG", box, slot_point(target), bed, L.RTG_CLEAR_Y, config.RTG_MOVE_S)
                self.riding[box] = lorry
                self.send("box", id=box, state="ON_TRUCK", carrier_id=lorry)
            else:
                s = yield self.db(tx_drop_from_truck, box, block, gate, lorry, t0, t0 + config.RTG_MOVE_S)
                self.crane(f"RTG-{block}", "RTG", box, bed, slot_point(s), L.RTG_CLEAR_Y, config.RTG_MOVE_S)
                self.riding.pop(box, None)
                self.send("box", id=box, state="STACKED_IN_YARD", carrier_id=None, **s)
            yield self.env.timeout(config.RTG_MOVE_S)

        out = L.cells_along([spot, thru(spot[0]), thru(L.SCAN_ROAD_X), thru(L.GATE_OUT_X), thru(L.LORRY_EXIT_X)])
        stops = {out.index(thru(L.GATE_OUT_X)): (config.GATE_CHECKOUT_S + 0.3,
                                                  lambda: self.checkout(gate, box if flow == "IMPORT" else None, lorry))}
        if hold:
            stops[out.index(thru(L.SCAN_ROAD_X))] = (config.INSPECT_S + 0.6, lambda: self.inspect("SCN-ROAD", box))
        self.spots_taken.discard(spot)
        yield from self.travel(lorry, "LORRY", cells=out, dwell=stops, park_end=False)
        yield self.lanes.put(1)
        yield self.db(q.leave, lorry)
        self.remove(*([box, lorry] if flow == "IMPORT" else [lorry]))

    def checkout(self, gate: int, box: str | None, lorry: str):
        yield self.env.timeout(config.GATE_CHECKOUT_S)
        yield self.db(tx_gate_out, box, gate)
        if box:
            self.send("box", id=box, state="GATE_OUT", carrier_id=lorry)

    # rail ----------------------------------------------------------------------------------------
    def trains(self):
        yield self.env.timeout(config.FIRST_TRAIN_S)
        for n in itertools.count(1):
            name = f"RS-{n:03d}"
            train, inbound = yield self.db(tx_train_arrives, self.rng, name)
            unit = f"TRAIN-{train}"
            capacity = config.TRAIN_WAGONS * 2
            self.train = {"id": train, "unit": unit, "name": name, "status": "ARRIVING", "loaded": 0,
                          "unloaded": 0, "inbound": len(inbound), "capacity": capacity}
            self.send("train", **self.train)
            self.send("boxes", boxes=inbound)
            rail = lambda x: (x, 0, L.RAIL_Z)  # noqa: E731
            yield from self.roll(unit, "TRAIN", [rail(L.TRAIN_FROM_X), rail(L.RAIL_X0)], duration=config.TRAIN_RUN_S)

            self.train["status"] = "UNLOADING"
            self.send("train", **self.train)
            on_wagon = {(b["wagon"], b["wagon_slot"]): b for b in inbound}
            deadline = self.env.now + config.TRAIN_WINDOW_S
            yield self.env.all_of([self.spawn(self.rmg_worker(rmg, train, wagons, on_wagon, deadline))
                                   for rmg, wagons in L.rmg_wagons().items()])

            railed = yield self.db(q.depart_train, train)
            self.train["status"] = "DEPARTING"
            self.send("train", **self.train)
            yield from self.roll(unit, "TRAIN", [rail(L.RAIL_X0), rail(L.TRAIN_TO_X)], duration=config.TRAIN_RUN_S)
            self.remove(unit, *railed)
            self.send("train", **{**self.train, "status": "DEPARTED"})
            self.train = None
            yield self.env.timeout(config.TRAIN_GAP_S)

    def rmg_worker(self, rmg: str, train: int, wagons: list[int], on_wagon: dict, deadline: float):
        slots = [(w, s) for w in wagons for s in (1, 2)]
        for slot in slots:
            if slot in on_wagon:
                try:
                    yield from self.rail_unload(rmg, train, *slot, on_wagon[slot])
                except Exception as e:
                    self.reject(e)
        if self.train["status"] == "UNLOADING":
            yield self.db(q.train_status, train, "LOADING")
            self.train["status"] = "LOADING"
            self.send("train", **self.train)
        free = deque(slots)
        while free and self.env.now < deadline:
            pick = yield self.db(q.pick_rail_box)
            if not pick:
                yield self.env.timeout(3)                 # nothing booked for rail in the yard yet
                continue
            try:
                yield from self.rail_load(rmg, train, pick, *free.popleft())
            except Exception as e:
                self.reject(e)

    def rail_unload(self, rmg: str, train: int, wagon: int, wagon_slot: int, box: dict):
        spur = L.rail_spur(wagon, wagon_slot)
        agv = yield from self.claim_agv(spur)
        try:
            yield from self.rail_unload_with(rmg, train, wagon, wagon_slot, box, agv, spur)
        except Exception as e:
            yield from self.rescue(agv, e)

    def rail_unload_with(self, rmg, train, wagon, wagon_slot, box, agv, spur):
        moving = self.env.process(self.travel(agv, "AGV", goals=[spur]))
        yield from self.gantry(rmg, spur[0])
        yield moving
        while not (block := (yield self.db(q.choose_block, box["hazmat"], "EXPORT"))):
            yield self.env.timeout(2)
        t0 = self.env.now
        task = yield self.db(tx_unload_wagon, rmg, box, agv, block, train, wagon, wagon_slot, t0, t0 + config.RMG_MOVE_S)
        self.crane(rmg, "RMG", box["id"], L.wagon_slot_point(wagon, wagon_slot), L.point(spur, L.AGV_BED_Y),
                   L.RMG_CLEAR_Y, config.RMG_MOVE_S)
        self.send("box", id=box["id"], state="IN_TRANSIT", carrier_id=agv)
        yield self.env.timeout(config.RMG_MOVE_S)
        self.train["unloaded"] += 1
        self.send("train", **self.train)
        self.spawn(self.agv_to_stack(agv, box["id"], block, task))

    def rail_load(self, rmg: str, train: int, pick: dict, wagon: int, wagon_slot: int):
        box, block = pick["id"], pick["block_code"]
        self.send("box", id=box, state="RETRIEVING")
        agv = yield from self.claim_agv(L.block_spurs(block)[0])
        try:
            yield from self.rail_load_with(rmg, train, pick, wagon, wagon_slot, box, block, agv)
        except Exception as e:
            yield from self.rescue(agv, e)
            return
        yield from self.free_agv(agv)

    def rail_load_with(self, rmg, train, pick, wagon, wagon_slot, box, block, agv):
        yield from self.travel(agv, "AGV", goals=near(L.block_spurs(block), self.at[agv][0]))
        yield from self.yard_to_agv(block, box, agv)
        spur = L.rail_spur(wagon, wagon_slot)
        if pick["customs_hold"]:
            yield from self.travel(agv, "AGV", goals=[spur], via=L.SCAN_RAIL,
                                   dwell={"via": (config.INSPECT_S + 0.6, lambda: self.inspect("SCN-RAIL", box))})
        else:
            yield from self.travel(agv, "AGV", goals=[spur])
        yield from self.gantry(rmg, spur[0])
        t0 = self.env.now
        yield self.db(tx_load_wagon, rmg, box, agv, train, wagon, wagon_slot, t0, t0 + config.RMG_MOVE_S)
        self.crane(rmg, "RMG", box, L.point(spur, L.AGV_BED_Y), L.wagon_slot_point(wagon, wagon_slot),
                   L.RMG_CLEAR_Y, config.RMG_MOVE_S)
        self.send("box", id=box, state="ON_RAIL", carrier_id=None, train_id=train, wagon=wagon, wagon_slot=wagon_slot)
        yield self.env.timeout(config.RMG_MOVE_S)
        self.train["loaded"] += 1
        self.send("train", **self.train)

    def pruner(self):
        while True:
            yield self.env.timeout(config.BOOK_PRUNE_EVERY_S)
            yield self.db(q.prune_bookings, self.env.now - 5)
