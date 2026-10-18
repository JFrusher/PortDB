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

Every transaction carries the simulated second it acts at (portdb.sim_t), so the database can judge
time-dependent rules itself: no crane lifts above its wind limit, nothing that is down takes work, no
lorry checks in during a gate outage. Box state reaches the browser from the database's change feed
(pg_notify at commit), not from here: this module only streams motion and calls.
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
HOMES = {u["id"]: u["home"] for u in L.units()}
HELD_BY = ("wind_limit", "equipment_down")


def constraint_of(e: Exception) -> str:
    """Which rule refused a transaction: the constraint's name, or the tag our triggers lead with."""
    diag = getattr(e, "diag", None)
    return (diag and diag.constraint_name) or str(e).split(":", 1)[0].strip()


def loc(r: dict) -> dict:
    return {k: r[k] for k in ("block_code", "bay", "row", "tier", "half")}


def slot_point(r: dict):
    return L.slot(r["block_code"], r["bay"], r["row"], r["tier"], r["half"])


def is_forty(box: dict) -> bool:
    return box["iso_type"].startswith("4")


def trapezoid(t: float, T: float, ramp: float) -> float:
    """Fraction of a vehicle track covered after t of T seconds: the browser's speed profile
    (world.ts trapezoid), so a position computed here matches the one drawn there."""
    if T <= 0 or t >= T:
        return 1.0
    if t <= 0:
        return 0.0
    r = min(ramp, T / 2)
    v = 1 / (T - r)
    if t < r:
        return v * t * t / (2 * r)
    if t > T - r:
        return 1 - v * (T - t) ** 2 / (2 * r)
    return v * (t - r / 2)


def leg_shares(path, floor: float = 0.6) -> list[float]:
    """Share of a crane move's time per leg, by length: a long trolley run takes longer than a short
    hoist, so the spreader moves at a believable, even pace. `floor` keeps short legs (locking on to a
    box) from being instant."""
    w = [max(math.dist(a, b), floor) for a, b in zip(path, path[1:])]
    total = sum(w)
    return [x / total for x in w]


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


async def tx_stack(conn, box, forty, block, agv, task, t0, t1):
    """None if no slot is free for this box yet (full, or IMDG distance rules them all out)."""
    s = await q.allocate_slot(conn, block, forty, box=box)
    if s is None:
        return None
    await q.move_to_slot(conn, box, s, "STACKED_IN_YARD")
    await q.close_task(conn, task, t0)
    await q.log_task(conn, f"RTG-{block}", box, "STACK", {"agv": agv}, s, t0, t1)
    return s


async def tx_reshuffle(conn, blocker, t0, t1):
    s = await q.allocate_slot(conn, blocker["block_code"], blocker["half"] == 0, avoid=(blocker["bay"], blocker["row"]),
                              box=blocker["id"])
    if s is None:
        raise RuntimeError(f"no_slot: nowhere to put blocker {blocker['id']}")
    await q.move_to_slot(conn, blocker["id"], s)
    await q.log_task(conn, f"RTG-{blocker['block_code']}", blocker["id"], "RESHUFFLE", loc(blocker), s, t0, t1)
    return s


async def tx_load_truck(conn, target, gate, lorry, moves, t0, t1):
    await q.load_on_truck(conn, target["id"])
    await q.log_task(conn, f"RTG-{target['block_code']}", target["id"], "RETRIEVE", loc(target), {"lorry": lorry}, t0, t1)
    await q.gate_update(conn, gate, status="LOADED", shifter_moves=moves)


async def tx_drop_from_truck(conn, box, forty, block, gate, lorry, t0, t1):
    s = await q.allocate_slot(conn, block, forty, box=box)
    if s is None:
        return None
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


async def tx_load_vessel(conn, qc, box, vessel, bay, row, tier, half, agv, t0):
    await q.load_on_vessel(conn, box, vessel, bay, row, tier, half)
    cell = {"vessel": vessel, "bay": bay, "row": row, "tier": tier, "half": half}
    return await q.open_task(conn, qc, box, "LOAD", {"agv": agv}, cell, t0)


async def tx_stow(conn, box, task, t1):
    await q.stow(conn, box)
    await q.close_task(conn, task, t1)


async def tx_gate_out(conn, box, gate):
    if box:
        await q.gate_out(conn, box)
    await q.gate_depart(conn, gate)


async def tx_export_arrival(conn, rng, plate, lorry):
    """A lorry turns up with an export box. Turned away (None) if the export yard is full."""
    box = cargo(rng, "EXPORT")
    block = await q.choose_block(conn, box["hazmat"], "EXPORT")
    if block is None:
        return None
    await insert_boxes(conn, [box | {"state": "ON_TRUCK"}])
    gate = await (await conn.execute(
        "INSERT INTO gate_transactions (plate, vehicle, container_id) VALUES (%s, %s, %s) RETURNING id",
        (plate, lorry, box["id"]))).fetchone()
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
        self.planning: set[str] = set()           # cranes waiting on the load planner's query
        self.train: dict | None = None
        self.reshuffling: dict[str, dict] = {}
        self.scans: dict[str, dict] = {}
        self.gate_queue: list[str] = []
        self.spots_taken: set = set()
        self.idle_on_spur: set[str] = set()
        self.stuck: dict[str, tuple] = {}              # vehicle -> what it is waiting for (diagnostics)
        self.blocked: dict[str, tuple] = {}            # crane -> (at, wants) while a neighbour is in the way
        self.waits = Counter()                    # how often vehicles / cranes had to yield to someone
        self.refusals = Counter()                 # database refusals by rule: the engine asked, the DB said no
        self.held: dict[str, str] = {}            # crane -> rule holding it right now
        self.down: set[str] = set()               # equipment out of service
        self.chaos_auto = False
        self.force_storm = False
        self.weather_now = {"wind": 6.0, "gust": 7.5, "rain": 0.0, "fog": 0.0}
        self.dual = config.DUAL_CYCLE
        self.dual_cycles = 0
        self.strategy_log = [(0.0, config.YARD_STRATEGY)]
        self.rejects = 0
        self._jobs: set[asyncio.Task] = set()

    # bridge ------------------------------------------------------------------------------------
    def db(self, fn, *args, **kwargs) -> simpy.Event:
        ev = self.env.event()
        t = self.env.now

        async def job():
            for attempt in range(4):
                try:
                    async with self.pool.connection() as conn:
                        # the simulated moment this transaction acts at, for time-dependent rules and history
                        await conn.execute("SELECT set_config('portdb.sim_t', %s, true)", (repr(t),))
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

    def refused(self, rule: str, at, detail: str = "") -> None:
        """The database said no. Counted per rule, and flashed where it happened in the X-ray view."""
        self.refusals[rule] += 1
        self.send("refusal", rule=rule, at=[round(c, 2) for c in at], detail=detail[:160])

    def unit_point(self, unit: str):
        tr = self.tracks.get(unit)
        return tuple(tr["path"][-1]) if tr else tuple(HOMES.get(unit, (0, 0, 0)))

    def lift(self, unit: str, step):
        """A crane step, `step(t)` -> db event. The database refuses lifts above the wind limit and work
        for equipment that is down; the crane holds and asks again. Other refusals propagate."""
        while True:
            try:
                result = yield step(self.env.now)
            except errors.CheckViolation as e:
                rule = constraint_of(e)
                if rule not in HELD_BY:
                    raise
                self.held[unit] = rule
                self.refused(rule, self.unit_point(unit), str(e).splitlines()[0])
                yield self.env.timeout(config.LIFT_RETRY_S)
                continue
            self.held.pop(unit, None)
            return result

    async def run(self, until: float | None = None) -> None:
        async with self.pool.connection() as conn:
            for agv, (x, z) in self.at.items():
                await q.park(conn, agv, x, z, 0.0)
            w = self.weather_now
            await q.add_weather(conn, 0.0, w["wind"], w["gust"], w["rain"], w["fog"])
        self.spawn(self.weather())
        self.spawn(self.chaos_loop())
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

    def track(self, unit: str, kind: str, path, duration: float, t0: float | None = None, legs=None) -> None:
        """`legs`: each leg's share of the duration (crane moves); vehicles move by arc length instead."""
        t0 = self.env.now if t0 is None else t0
        ev = {"type": "track", "id": unit, "kind": kind, "t0": round(t0, 3), "t1": round(t0 + duration, 3),
              "path": [[round(c, 3) for c in p] for p in path]}
        if legs:
            ev["legs"] = [round(w, 4) for w in legs]
        self.tracks[unit] = ev
        self.emit(ev)

    def remove(self, *ids: str) -> None:
        for i in ids:
            self.tracks.pop(i, None)
        self.send("remove", ids=list(ids))

    def vessel_view(self, v: dict) -> dict:
        keys = ("id", "name", "imo", "berth", "bays", "rows", "tiers", "status", "phase", "t0", "t1",
                "manifest", "left", "loaded")
        return {k: v[k] for k in keys}

    def queues(self) -> dict:
        return {"agv_wait": len(self.agv_gate.get_queue),
                "rtg": {b: len(r.queue) for b, r in self.rtg.items()},
                "gate": len(self.gate_queue),
                "waits": dict(self.waits),
                "refusals": dict(self.refusals),
                "held": self.held, "down": sorted(self.down), "dual_cycles": self.dual_cycles}

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
                self.refused("road_bookings_no_overlap", L.point(route[min(1, len(route) - 1)]),
                             f"{unit}: another vehicle booked the same cell first")
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
        """From wherever the spreader is: hoist clear, travel over src, lower, lift the box, travel,
        lower to dst. The box's track has the same legs and sits at src until it is lifted, so the
        two stay together (and swing together)."""
        up_src, up_dst = L.lifted(src, clear_y), L.lifted(dst, clear_y)
        cur = self.unit_point(unit)
        approach = []
        for p in (cur, L.lifted(cur, clear_y), up_src):
            if not approach or math.dist(approach[-1], p) > 1e-3:
                approach.append(tuple(p))
        path = approach + [src, up_src, up_dst, dst]
        legs = leg_shares(path)
        self.track(unit, kind, path, duration, legs=legs)
        self.track(box, "BOX", [src] * len(approach) + [src, up_src, up_dst, dst], duration, legs=legs)

    def gantry(self, crane: str, x: float, job="any"):
        """Travel along the rail. The DB refuses an envelope that overlaps a neighbour, so cranes wait.
        Given the job the move is for, gives up (returns False) if the crane is reassigned meanwhile."""
        cur = self.crane_x[crane]
        if abs(cur - x) < 1e-3:
            return True
        half = L.QC_HALF_WIDTH if crane.startswith("QC") else L.RMG_HALF_WIDTH
        while rule := (yield self.db(q.claim_rail, crane, min(cur, x) - half, max(cur, x) + half)):
            self.waits["crane"] += 1
            self.refused(rule, self.unit_point(crane), f"{crane} wants x={x:.1f}")
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
        path = [p, (p[0], clear, p[2]), (x, clear, p[2])]
        self.track(crane, kind, path, duration, legs=leg_shares(path))
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

    def agv_to_stack(self, agv: str, box: str, forty: bool, block: str, task: int):
        try:
            yield from self.travel(agv, "AGV", goals=near(L.block_spurs(block), self.at[agv][0]))
            spur = self.at[agv]
            rtg = f"RTG-{block}"
            while True:
                with self.rtg[block].request() as turn:
                    yield turn
                    s = yield from self.lift(rtg, lambda t: self.db(tx_stack, box, forty, block, agv, task, t,
                                                                    t + config.RTG_MOVE_S))
                    if s:
                        self.crane(rtg, "RTG", box, L.point(spur, L.AGV_BED_Y), slot_point(s), L.RTG_CLEAR_Y,
                                   config.RTG_MOVE_S)
                        yield self.env.timeout(config.RTG_MOVE_S)
                        break
                self.waits["no_slot"] += 1                # nothing free for this box yet: let the RTG work on
                yield self.env.timeout(2.0)
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
            yield from self.lift(f"RTG-{block}", lambda t: self.db(tx_load_agv_from_yard, target, agv, t,
                                                                   t + config.RTG_MOVE_S))
            self.crane(f"RTG-{block}", "RTG", box, slot_point(target), L.point(spur, L.AGV_BED_Y),
                       L.RTG_CLEAR_Y, config.RTG_MOVE_S)
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
            s = yield from self.lift(f"RTG-{block}", lambda t: self.db(tx_reshuffle, b, t, t + config.RTG_MOVE_S))
            self.crane(f"RTG-{block}", "RTG", b["id"], slot_point(b), slot_point(s), L.RTG_CLEAR_Y, config.RTG_MOVE_S)
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
        if any(self.plans.values()) or any(self.loading.values()) or self.planning:
            # boxes are already on their way to some cranes (or about to be): let those pipelines drain first
            self.realloc_pending = True
            return
        self.realloc_pending = False
        self.assign = new
        fired, self.reassigned = self.reassigned, self.env.event()
        fired.succeed()

    def make_way(self, qc: str):
        """A crane with nothing to do steps aside when a neighbour is held up wanting rail it stands on.
        If this crane is boxed in too, its own neighbour sees it blocked and steps aside in turn."""
        me = self.crane_x[qc]
        reach = 2 * L.QC_HALF_WIDTH + 0.1
        for other, (cur, want) in list(self.blocked.items()):
            if other == qc or not other.startswith("QC") or not min(cur, want) - reach < me < max(cur, want) + reach:
                continue
            away = max(me, want + reach) if want > cur else min(me, want - reach)
            if abs(away - me) > 1e-3:
                yield from self.gantry(qc, max(-4.0, min(50.0, away)), job=self.assign[qc])
                return True
        return False

    def idle(self, qc: str, job):
        """Wait up to 5 s for a new assignment, making way for neighbours meanwhile."""
        until = self.env.now + 5
        while self.assign[qc] == job and self.env.now < until:
            if not (yield from self.make_way(qc)):
                yield self.reassigned | self.env.timeout(0.5)

    def plan(self, qc: str, v: dict, bays: list[int]):
        """plan_step, with the crane marked as planning: reassigning it mid-query would leave the step
        it returns pointing at the old berth, possibly through its neighbours."""
        self.planning.add(qc)
        try:
            return (yield from self.plan_step(qc, v, bays))
        finally:
            self.planning.discard(qc)

    def qc_worker(self, qc: str):
        while True:
            if self.realloc_pending and not (any(self.plans.values()) or any(self.loading.values()) or self.planning):
                self.reallocate()                         # the last pipeline drained: apply the deferred plan
            job = self.assign[qc]
            v = self.vessels.get(job[0]) if job else None
            if not v or v["phase"] not in ("DISCHARGE", "LOAD"):
                if job is None and qc in self.idle_x:
                    yield from self.gantry(qc, self.idle_x[qc], job=None)
                yield from self.idle(qc, job)
                continue
            try:
                if v["phase"] == "DISCHARGE":
                    did = yield from (self.qc_dual if self.dual else self.qc_discharge)(qc, v, job)
                else:
                    did = yield from self.qc_load(qc, v, job)
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
            yield from self.idle(qc, job)

    # quay: discharge ---------------------------------------------------------------------------
    def qc_discharge(self, qc: str, v: dict, job: tuple):
        bays = [b for b in job[1] if v["bay_left"][b] > 0]
        bay = (yield self.db(q.peek_next_bay, v["id"], bays)) if bays else None
        if bay is None:
            return False
        spur = L.qc_spur(v["berth"], bay)
        if self.assign[qc] != job or not (yield from self.gantry(qc, spur[0], job=job)):
            return True                                   # reassigned while waiting for the rail: start over
        box = yield from self.lift(qc, lambda t: self.db(tx_claim, qc, v["id"], bay, bay, t))
        if not box:
            return True
        v["bay_left"][bay] -= 1
        v["bay_busy"][bay] += 1
        agv = yield from self.claim_agv(spur)
        arrived = self.env.process(self.travel(agv, "AGV", goals=[spur]))
        cycle = self.rng.uniform(*config.QC_CYCLE)
        src = L.ship_cell(v["berth"], bay, box["vessel_row"], box["vessel_tier"], box["vessel_half"])
        self.crane(qc, "QC", box["id"], src, L.point(spur, L.AGV_BED_Y), L.QC_CLEAR_Y, cycle)
        try:
            yield self.env.timeout(cycle) & arrived
        except Exception as e:
            self.spawn(self.rescue(agv, e))
            raise
        while not (handed := (yield self.db(tx_handover, qc, box, agv, self.env.now))):
            yield self.env.timeout(1)                     # every eligible block at capacity: hold the box
        block, task = handed
        v["bay_busy"][bay] -= 1
        self.spawn(self.agv_to_stack(agv, box["id"], is_forty(box), block, task))
        v["left"] -= 1
        if v["left"] == 0 and not v["phase_done"].triggered:
            v["phase_done"].succeed()
        return True

    def qc_dual(self, qc: str, v: dict, job: tuple):
        """Dual cycling: while a vessel still has imports aboard, a QC also loads exports into bays it has
        already emptied, so the trolley carries a box both ways instead of running back empty. One
        export at a time is kept on its way, and it goes straight to the spur."""
        plan = self.plans[qc]
        emptied = [b for b in job[1] if v["bay_left"][b] == 0 and v["bay_busy"][b] == 0]
        if emptied and not plan and not self.realloc_pending:
            step = yield from self.plan(qc, v, emptied)
            if step:
                step["turn"].succeed()
                plan.append(step)
                self.spawn(self.fetch_export(v, step))
        if plan and plan[0]["arrived"].triggered:
            yield from self.dual_load(qc, v, plan)
        did = yield from self.qc_discharge(qc, v, job)
        if not did and plan:                              # our bays are empty but an export is on its way
            yield from self.dual_load(qc, v, plan)
            return True
        return did

    def dual_load(self, qc: str, v: dict, plan: deque):
        step = plan.popleft()
        self.loading[qc] = True
        try:
            yield from self.load_step(qc, v, step, plan, dual=True)
            self.dual_cycles += 1
        finally:
            self.loading[qc] = False

    # quay: load --------------------------------------------------------------------------------
    def columns(self, v: dict, stack: tuple) -> list[int]:
        """Open columns of a ship stack: the whole 40 ft column (0), or fore and aft 20 ft columns (1, 2).
        An empty stack takes whatever length is loaded into it first."""
        kind = v["kind"].get(stack)
        cols = [0] if kind == "40" else [1, 2] if kind == "20" else [None]
        return [h for h in cols if (stack, h) not in v["closed"] and v["height"].get((stack, h), 0) < v["tiers"]]

    def plan_step(self, qc: str, v: dict, bays: list[int]):
        """Next cell in the QC's bays and an export box that may sit there: same length as the stack,
        no heavier and no later-port than the box beneath it in the same column. A column closes when
        nothing in the yard fits. A stack with boxes still on their way for one crane is left alone by
        the others, so tiers always go on in order."""
        for bay in bays:
            for row in range(1, v["rows"] + 1):
                stack = (bay, row)
                owner, pending = v["owner"].get(stack, (qc, 0))
                cols = self.columns(v, stack)
                if not cols or (pending and owner != qc):
                    continue
                half = min(cols, key=lambda h: v["height"].get((stack, h), 0))
                kind = v["kind"].get(stack)
                below = v["top"].get((stack, half))
                box = yield self.db(q.pick_export, *(below or (None, None)), None if kind is None else kind == "40")
                if box is None:
                    if kind is None:
                        return None                       # no exports in the yard at all right now
                    v["closed"].add((stack, half))
                    continue
                if kind is None:
                    v["kind"][stack] = "40" if is_forty(box) else "20"
                    half = 0 if is_forty(box) else 1
                tier = v["height"].get((stack, half), 0) + 1
                v["height"][(stack, half)] = tier
                v["top"][(stack, half)] = (box["weight_kg"], box["rotation"])
                v["owner"][stack] = (qc, pending + 1)
                return {"box": box, "cell": (bay, row, tier, half), "spur": L.qc_spur(v["berth"], bay),
                        "turn": self.env.event(), "arrived": self.env.event()}
        return None

    def bays_full(self, v: dict, bays: list[int]) -> bool:
        return not any(self.columns(v, (b, r)) for b in bays for r in range(1, v["rows"] + 1))

    def qc_load(self, qc: str, v: dict, job: tuple):
        bays = job[1]
        plan = self.plans[qc]
        while len(plan) <= config.LOAD_PREFETCH and self.env.now < v["deadline"] and not self.realloc_pending:
            step = yield from self.plan(qc, v, bays)
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

    def load_step(self, qc: str, v: dict, step: dict, plan: deque, dual: bool = False):
        # this AGV may come forward; so may the next one, unless it is bound for the same spur
        for s in (step, *[n for n in list(plan)[:1] if n["spur"] != step["spur"]]):
            if not s["turn"].triggered:
                s["turn"].succeed()
        agv = yield step["arrived"]
        bay, row, tier, half = step["cell"]
        box = step["box"]["id"]
        yield from self.gantry(qc, step["spur"][0])
        task = yield from self.lift(qc, lambda t: self.db(tx_load_vessel, qc, box, v["id"], bay, row, tier, half, agv, t))
        cycle = self.rng.uniform(*config.QC_CYCLE) * (config.DUAL_CYCLE_FACTOR if dual else 1.0)
        self.crane(qc, "QC", box, L.point(step["spur"], L.AGV_BED_Y), L.ship_cell(v["berth"], bay, row, tier, half),
                   L.QC_CLEAR_Y, cycle)
        self.spawn(self.free_agv(agv))
        yield self.env.timeout(cycle)
        yield self.db(tx_stow, box, task, self.env.now)
        owner, pending = v["owner"][(bay, row)]
        v["owner"][(bay, row)] = (owner, pending - 1)
        v["loaded"] += 1
        return True

    def fetch_export(self, v: dict, step: dict):
        """Yard -> AGV -> park nearest the QC (staging) -> QC spur, in the QC's order."""
        box, block = step["box"]["id"], step["box"]["block_code"]
        agv = None
        try:
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
        span = config.BERTH_WINDOW_S[b]
        first = config.FIRST_CALL_S[b]
        wid = yield self.db(q.plan_window, b, "next call", first, first + span)
        yield self.env.timeout(first)
        while True:
            while (yield self.db(q.yard_fill, "IMPORT")) > config.NEXT_VESSEL_YARD_MAX:
                yield self.env.timeout(3)
            row = yield self.db(tx_berth, self.rng, b)
            now = self.env.now
            yield self.db(q.window_alongside, wid, row["id"], row["name"], now)
            nxt_t0 = now + span + config.VESSEL_SAIL_S + config.NEXT_VESSEL_GAP_S
            nxt = yield self.db(q.plan_window, b, "next call", nxt_t0, nxt_t0 + span)
            v = {"id": row["id"], "name": row["name"], "imo": row["imo"], "berth": b,
                 "bays": row["bays"], "rows": row["rows"], "tiers": row["tiers"],
                 "status": "ARRIVING", "phase": None, "t0": now, "t1": now + config.VESSEL_SAIL_S,
                 "manifest": len(row["boxes"]), "left": len(row["boxes"]), "loaded": 0,
                 "bay_left": Counter(x["vessel_bay"] for x in row["boxes"]), "bay_busy": Counter(),
                 "height": {}, "top": {}, "kind": {}, "owner": {}, "closed": set(), "exhausted": set(),
                 "deadline": 0.0, "phase_done": self.env.event(), "window": (wid, now + span)}
            self.vessels[b] = v
            self.send("vessel", **self.vessel_view(v))
            self.spawn(self.keep_window(v, nxt))
            yield self.env.timeout(config.VESSEL_SAIL_S)

            # under dual cycling exports start going aboard during discharge: no load deadline yet
            v.update(status="BERTHED", phase="DISCHARGE", deadline=math.inf)
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
            yield self.db(q.window_done, v["window"][0], now + config.VESSEL_SAIL_S)
            wid = nxt
            self.send("vessel", **self.vessel_view(v))
            yield self.env.timeout(config.VESSEL_SAIL_S)
            self.send("vessel", **{**self.vessel_view(v), "status": "DEPARTED"})
            self.remove(*sailed)
            del self.vessels[b]
            yield self.env.timeout(config.NEXT_VESSEL_GAP_S)

    def keep_window(self, v: dict, nxt: int):
        """The berth plan follows reality: a call that runs over its window extends it. If that would
        overlap the next call's window the database refuses, and the next call is pushed back first."""
        step = config.WINDOW_STEP_S
        while v["status"] not in ("DEPARTING", "DEPARTED"):
            yield self.env.timeout(5)
            wid, end = v["window"]
            if v["status"] in ("DEPARTING", "DEPARTED") or self.env.now < end - 10:
                continue
            if not (yield self.db(q.move_window, wid, 0, step)):
                x0, x1 = q.quay(v["berth"])
                self.refused("berth_windows_no_overlap", ((x0 + x1) / 2, 0.5, -3.0),
                             f"{v['name']} overruns into the next call at berth {v['berth']}")
                yield self.db(q.move_window, nxt, step, step)
                if not (yield self.db(q.move_window, wid, 0, step)):
                    continue
            v["window"] = (wid, end + step)

    # road ----------------------------------------------------------------------------------------
    def lorry_source(self, flow: str):
        for n in itertools.count(1):
            yield self.env.timeout(self.rng.expovariate(1 / config.LORRY_MEAN_GAP_S[flow]))
            self.spawn(self.lorry(f"LRY-{flow[0]}{n}", flow))

    def queue_shift(self) -> None:
        """Close up the gate queue. A lorry still rolling in carries on from where it is now (not from
        where it was heading), at road speed."""
        now = self.env.now
        for k, lorry in enumerate(self.gate_queue):
            tr = self.tracks.get(lorry)
            x = L.QUEUE_HEAD_X - 2.2 * k
            if not tr or abs(tr["path"][-1][0] - x) <= 0.01:
                continue
            (x0, y, z), x1 = tr["path"][0], tr["path"][-1][0]
            at = x0 + (x1 - x0) * trapezoid(now - tr["t0"], tr["t1"] - tr["t0"], 0.7)
            self.track(lorry, "LORRY", [(at, y, z), (x, 0, L.THRU_Z)], max(0.6, abs(x - at) / config.LORRY_SPEED))

    def lorry(self, lorry: str, flow: str):
        plate = "".join(self.rng.choices(string.ascii_uppercase, k=2)) + f"{self.rng.randint(10, 99)} " + \
                "".join(self.rng.choices(string.ascii_uppercase, k=3))
        if flow == "IMPORT":
            pick = yield self.db(q.pick_pickup, plate, lorry)
            if not pick:
                return                                    # nothing booked for road in the yard
            box, block, gate, hold = pick["box"], pick["block_code"], pick["gate_id"], pick["customs_hold"]
            forty = None
        else:
            arrival = yield self.db(tx_export_arrival, self.rng, plate, lorry)
            if not arrival:
                return                                    # export yard full: turned away at the gate
            box, block, gate, hold = arrival["id"], arrival["dest_block"], arrival["gate_id"], False
            forty = is_forty(arrival)
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
        # check in at the booth: the database refuses while an outage covers this moment
        while True:
            try:
                yield self.db(q.gate_checkin, gate)
                break
            except errors.CheckViolation as e:
                if constraint_of(e) != "gate_closed":
                    raise
                self.refused("gate_closed", (L.GATE_IN_X, 0.5, L.THRU_Z), str(e).splitlines()[0])
                yield self.env.timeout(2.0)

        bays = [b for b in range(1, config.BLOCK_BAYS + 1) if L.lorry_spot(block, b) not in self.spots_taken]
        spot = L.lorry_spot(block, bays[0] if bays else 1)
        self.spots_taken.add(spot)
        route = L.cells_along([thru(L.QUEUE_HEAD_X), thru(L.GATE_IN_X), thru(spot[0]), spot])
        gate_i = route.index(thru(L.GATE_IN_X))
        self.gate_queue.remove(lorry)
        self.queue_shift()
        yield from self.travel(lorry, "LORRY", cells=route, dwell={gate_i: (config.GATE_CHECKIN_S, None)})

        bed = (spot[0], L.LORRY_BED_Y, spot[1])
        rtg = f"RTG-{block}"
        while True:
            with self.rtg[block].request() as turn:
                yield turn
                if flow == "IMPORT":
                    target, moves = yield from self.dig_out(block, box)
                    yield from self.lift(rtg, lambda t: self.db(tx_load_truck, target, gate, lorry, moves, t,
                                                                t + config.RTG_MOVE_S))
                    self.crane(rtg, "RTG", box, slot_point(target), bed, L.RTG_CLEAR_Y, config.RTG_MOVE_S)
                    yield self.env.timeout(config.RTG_MOVE_S)
                    break
                s = yield from self.lift(rtg, lambda t: self.db(tx_drop_from_truck, box, forty, block, gate, lorry, t,
                                                                t + config.RTG_MOVE_S))
                if s:
                    self.crane(rtg, "RTG", box, bed, slot_point(s), L.RTG_CLEAR_Y, config.RTG_MOVE_S)
                    yield self.env.timeout(config.RTG_MOVE_S)
                    break
            self.waits["no_slot"] += 1
            yield self.env.timeout(2.0)

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

    # rail ----------------------------------------------------------------------------------------
    def trains(self):
        yield self.env.timeout(config.FIRST_TRAIN_S)
        for n in itertools.count(1):
            name = f"RS-{n:03d}"
            train, inbound = yield self.db(tx_train_arrives, self.rng, name)
            unit = f"TRAIN-{train}"
            capacity = config.TRAIN_WAGONS * 3          # TEU: 60 ft wagons
            self.train = {"id": train, "unit": unit, "name": name, "status": "ARRIVING", "loaded": 0,
                          "unloaded": 0, "inbound": len(inbound), "capacity": capacity}
            self.send("train", **self.train)
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

    @staticmethod
    def wagon_place(free: dict, forty: bool):
        """Where a box goes on 60 ft wagons: a 40 needs two adjacent free 20 ft positions; a 20 goes
        to the fullest wagon that still has room, so wagons fill up rather than all half-fill."""
        if forty:
            for w, pos in free.items():
                for p in (1, 2):
                    if {p, p + 1} <= pos:
                        return w, p
            return None
        part = sorted((w for w, pos in free.items() if pos), key=lambda w: len(free[w]))
        return (part[0], min(free[part[0]])) if part else None

    def rmg_worker(self, rmg: str, train: int, wagons: list[int], on_wagon: dict, deadline: float):
        for w in wagons:
            for p in (1, 2, 3):
                if (w, p) in on_wagon:
                    try:
                        yield from self.rail_unload(rmg, train, w, p, on_wagon[(w, p)])
                    except Exception as e:
                        self.reject(e)
        if self.train["status"] == "UNLOADING":
            yield self.db(q.train_status, train, "LOADING")
            self.train["status"] = "LOADING"
            self.send("train", **self.train)
        free = {w: {1, 2, 3} for w in wagons}
        while any(free.values()) and self.env.now < deadline:
            # only ask for a 40 while a wagon can still take one
            pick = yield self.db(q.pick_rail_box, None if self.wagon_place(free, True) else False)
            if not pick:
                yield self.env.timeout(3)                 # nothing that fits is booked for rail yet
                continue
            forty = is_forty(pick)
            w, p = self.wagon_place(free, forty)
            free[w] -= {p, p + 1} if forty else {p}
            try:
                yield from self.rail_load(rmg, train, pick, w, p)
            except Exception as e:
                self.reject(e)

    def rail_unload(self, rmg: str, train: int, wagon: int, wagon_slot: int, box: dict):
        spur = L.rail_spur(wagon)
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
        task = yield from self.lift(rmg, lambda t: self.db(tx_unload_wagon, rmg, box, agv, block, train, wagon, wagon_slot,
                                                           t, t + config.RMG_MOVE_S))
        self.crane(rmg, "RMG", box["id"], L.wagon_slot_point(wagon, wagon_slot, is_forty(box)), L.point(spur, L.AGV_BED_Y),
                   L.RMG_CLEAR_Y, config.RMG_MOVE_S)
        yield self.env.timeout(config.RMG_MOVE_S)
        self.train["unloaded"] += 1
        self.send("train", **self.train)
        self.spawn(self.agv_to_stack(agv, box["id"], is_forty(box), block, task))

    def rail_load(self, rmg: str, train: int, pick: dict, wagon: int, wagon_slot: int):
        box, block = pick["id"], pick["block_code"]
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
        spur = L.rail_spur(wagon)
        if pick["customs_hold"]:
            yield from self.travel(agv, "AGV", goals=[spur], via=L.SCAN_RAIL,
                                   dwell={"via": (config.INSPECT_S + 0.6, lambda: self.inspect("SCN-RAIL", box))})
        else:
            yield from self.travel(agv, "AGV", goals=[spur])
        yield from self.gantry(rmg, spur[0])
        yield from self.lift(rmg, lambda t: self.db(tx_load_wagon, rmg, box, agv, train, wagon, wagon_slot,
                                                    t, t + config.RMG_MOVE_S))
        self.crane(rmg, "RMG", box, L.point(spur, L.AGV_BED_Y), L.wagon_slot_point(wagon, wagon_slot, is_forty(pick)),
                   L.RMG_CLEAR_Y, config.RMG_MOVE_S)
        yield self.env.timeout(config.RMG_MOVE_S)
        self.train["loaded"] += 2 if is_forty(pick) else 1
        self.send("train", **self.train)

    def pruner(self):
        while True:
            yield self.env.timeout(config.BOOK_PRUNE_EVERY_S)
            yield self.db(q.prune_bookings, self.env.now - 5)

    # weather + chaos -----------------------------------------------------------------------------
    def weather(self):
        """Wind that wanders, a storm front every few minutes, fog around dawn. Each reading is a row:
        the lift trigger reads the latest one, so the database decides when cranes stop."""
        wind, storm = self.weather_now["wind"], None
        next_storm = config.FIRST_STORM_S
        while True:
            now = self.env.now
            if self.force_storm or (storm is None and now >= next_storm):
                self.force_storm = False
                length = self.rng.uniform(80, 120)
                storm = (now, now + length, self.rng.uniform(24, 29))
                next_storm = now + length + self.rng.uniform(*config.STORM_GAP_S)
                self.send("chaos", what="storm", detail=f"storm front: wind building to {storm[2]:.0f} m/s")
            gale = 0.0
            if storm:
                t0, t1, _ = storm
                gale = math.sin(math.pi * (now - t0) / (t1 - t0)) ** 0.7 if now < t1 else 0.0
                if now >= t1:
                    storm = None
            base = 6.5 + 2.5 * math.sin(now / 97.0)
            target = base + (storm[2] - base) * gale if storm else base
            wind = max(0.5, wind + (target - wind) * 0.45 + self.rng.gauss(0, 0.5))
            hour = (config.DAY_START_H + 24 * now / config.DAY_S) % 24
            fog = 0.25 * gale if storm else max(0.0, 0.65 - abs(hour - 6.5) * 0.4)
            w = {"wind": round(wind, 1), "gust": round(wind * self.rng.uniform(1.15, 1.4), 1),
                 "rain": round(min(1.0, gale * 1.3), 2), "fog": round(min(1.0, fog), 2)}
            yield self.db(q.add_weather, now, w["wind"], w["gust"], w["rain"], w["fog"])
            self.weather_now = w
            self.send("weather", **w)
            yield self.env.timeout(config.WEATHER_EVERY_S)

    def chaos_loop(self):
        while True:
            yield self.env.timeout(self.rng.uniform(*config.CHAOS_GAP_S))
            if self.chaos_auto:
                self.spawn(self.chaos(self.rng.choice(["crane", "agv", "gate"])))

    def chaos(self, kind: str):
        """Something breaks. The database is told; its rules then keep work away until it is fixed."""
        now = self.env.now
        if kind == "storm":
            self.force_storm = True
        elif kind == "gate":
            if (yield self.db(q.gate_outage, now, now + config.GATE_OUTAGE_S, "barrier fault")):
                self.send("chaos", what="gate", detail=f"in-gate barrier fault, closed for {config.GATE_OUTAGE_S:.0f} s")
        elif kind == "crane":
            up = [qc for qc in QCS if qc not in self.down]
            unit = self.rng.choice([qc for qc in up if self.assign.get(qc)] or up)
            yield self.db(q.set_equipment_state, unit, "DOWN")
            self.down.add(unit)
            self.send("chaos", what="crane", unit=unit, detail=f"{unit} hoist fault, out of service")
            yield self.env.timeout(config.REPAIR_S["crane"])
            yield self.db(q.set_equipment_state, unit, "IDLE")
            self.down.discard(unit)
            self.send("chaos", what="repaired", unit=unit, detail=f"{unit} repaired")
        elif kind == "agv":
            yield self.agv_gate.get(1)
            unit = yield self.db(q.agv_fault)
            if not unit:
                yield self.agv_gate.put(1)
                return
            self.down.add(unit)
            self.send("chaos", what="agv", unit=unit, detail=f"{unit} drive fault, out of service")
            yield self.env.timeout(config.REPAIR_S["agv"])
            yield self.db(q.release_equipment, unit)
            self.down.discard(unit)
            self.send("chaos", what="repaired", unit=unit, detail=f"{unit} repaired")
            yield self.agv_gate.put(1)
