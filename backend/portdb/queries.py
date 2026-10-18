"""Every SQL statement the terminal runs. Each function takes an open psycopg AsyncConnection
(dict rows); the caller owns the transaction boundary."""

import json
from decimal import Decimal

from psycopg import AsyncConnection, errors

from . import config

PARKED = config.PARKED

# --- yard ------------------------------------------------------------------------------------

CLEARANCE_SQL = """
WITH RECURSIVE stack AS (
    SELECT c.id, c.block_code, c.bay, c.row, c.tier, c.half, 0 AS depth
    FROM containers c
    WHERE c.id = %(target)s AND c.state IN ('STACKED_IN_YARD', 'RETRIEVING')
  UNION ALL
    SELECT above.id, above.block_code, above.bay, above.row, above.tier, above.half, s.depth + 1
    FROM stack s
    JOIN containers above
      ON (above.block_code, above.bay, above.row, above.half, above.tier)
       = (s.block_code, s.bay, s.row, s.half, s.tier + 1)
)
SELECT id, block_code, bay, row, tier, half, bay_code(bay, half) AS bay_code, depth, depth = 0 AS is_target,
       count(*) FILTER (WHERE depth > 0) OVER () AS shifter_moves
FROM stack
ORDER BY tier DESC
"""


async def clearance(conn: AsyncConnection, box: str) -> list[dict]:
    """Blockers top-down, then the target. Empty if the box is not in the yard."""
    return await (await conn.execute(CLEARANCE_SQL, {"target": box})).fetchall()


async def explain_clearance(conn: AsyncConnection, box: str) -> str:
    rows = await (await conn.execute("EXPLAIN (ANALYZE, BUFFERS, COSTS OFF) " + CLEARANCE_SQL, {"target": box})).fetchall()
    return "\n".join(r["QUERY PLAN"] for r in rows)


async def choose_block(conn: AsyncConnection, hazmat: bool, purpose: str) -> str | None:
    """Least loaded eligible block of the right purpose, counting boxes already on their way."""
    row = await (await conn.execute(
        """
        SELECT b.code
        FROM yard_blocks b
        LEFT JOIN LATERAL (SELECT coalesce(sum(CASE WHEN is_forty(c.iso_type) THEN 1 ELSE 0.5 END), 0) AS n
                           FROM containers c WHERE c.block_code = b.code) occ ON true
        LEFT JOIN LATERAL (SELECT count(*) AS n FROM equipment_tasks t
                           WHERE t.status IN ('PENDING','IN_PROGRESS') AND t.kind = 'TRANSPORT'
                             AND t.target->>'block' = b.code) inflight ON true
        WHERE b.purpose = %(purpose)s
          AND (b.allows_hazmat OR NOT %(hazmat)s)
          AND occ.n + inflight.n < b.bays * b.rows * b.tiers - %(reserve)s
        ORDER BY (%(hazmat)s AND b.allows_hazmat) DESC, occ.n + inflight.n, b.code
        LIMIT 1
        """,
        {"hazmat": hazmat, "purpose": purpose, "reserve": config.BLOCK_RESHUFFLE_RESERVE},
    )).fetchone()
    return row and row["code"]


async def allocate_slot(conn: AsyncConnection, block: str, forty: bool,
                        avoid: tuple[int, int] | None = None, box: str | None = None) -> dict | None:
    """First free supported position for a box of this length: a 40 fills a slot (half 0), 20s pair up
    fore and aft (halves 1, 2). Only stacks that are empty or already hold this length qualify.
    Stacks are built to tier 3 before tiers 4-5 are used. `avoid` keeps a re-handled box off its own stack.

    Given the box: hazardous cargo keeps its IMDG distance from incompatible classes, and under the
    SMART strategy a box prefers to sit on one that will leave after it (exports: earlier port, or same
    port and lighter, which is the order the load planner picks; imports: same onward mode), and the
    yard is filled tier by tier."""
    xbay, xrow = avoid or (None, None)
    if box:   # hazmat placements in a block take turns, so this one sees the last one's commit
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('imdg:' || %s)) FROM containers "
                           "WHERE id = %s AND imdg_class IS NOT NULL", (block, box))
    return await (await conn.execute(
        """
        WITH me AS (SELECT c.id, c.imdg_class, c.weight_kg, c.mode, coalesce(p.rotation, 0) AS rotation
                    FROM containers c LEFT JOIN ports p ON p.code = c.pod WHERE c.id = %(box)s)
        SELECT s.block_code, s.bay, s.row, s.tier, h.half
        FROM yard_slots s CROSS JOIN unnest(%(halves)s::smallint[]) AS h(half)
        WHERE s.block_code = %(block)s
          AND (s.bay, s.row) IS DISTINCT FROM (%(xbay)s::smallint, %(xrow)s::smallint)
          AND NOT EXISTS (SELECT 1 FROM containers c
                          WHERE (c.block_code, c.bay, c.row) = (s.block_code, s.bay, s.row)
                            AND is_forty(c.iso_type) <> %(forty)s)
          AND NOT EXISTS (SELECT 1 FROM containers c
                          WHERE (c.block_code, c.bay, c.row, c.tier, c.half) = (s.block_code, s.bay, s.row, s.tier, h.half))
          AND (s.tier = 1 OR EXISTS (SELECT 1 FROM containers c
                          WHERE (c.block_code, c.bay, c.row, c.tier, c.half) = (s.block_code, s.bay, s.row, s.tier - 1, h.half)))
          AND NOT EXISTS (SELECT 1 FROM me JOIN containers o ON o.block_code = s.block_code AND o.id <> me.id
                          JOIN imdg_segregation g ON (g.class_a, g.class_b)
                                                   = (least(o.imdg_class, me.imdg_class), greatest(o.imdg_class, me.imdg_class))
                          WHERE greatest(abs(o.bay - s.bay), abs(o.row - s.row)) <= g.code)
        ORDER BY CASE WHEN %(smart)s THEN coalesce((
                   SELECT CASE WHEN b.flow = 'IMPORT' THEN b.mode <> me.mode
                               ELSE NOT (me.rotation > coalesce(pb.rotation, 0)
                                         OR (me.rotation = coalesce(pb.rotation, 0) AND me.weight_kg >= b.weight_kg)) END
                   FROM me, containers b LEFT JOIN ports pb ON pb.code = b.pod
                   WHERE (b.block_code, b.bay, b.row, b.half, b.tier) = (s.block_code, s.bay, s.row, h.half, s.tier - 1)),
                 false) END,
                 -- SMART also spreads low: road pick-ups come in random order, so every tier saved
                 -- is a re-handle saved; BASIC builds each stack to tier 3 before starting the next
                 CASE WHEN %(smart)s THEN s.tier END,
                 s.tier > 3, s.bay, s.row, s.tier, h.half
        LIMIT 1
        FOR UPDATE OF s SKIP LOCKED
        """,
        {"block": block, "xbay": xbay, "xrow": xrow, "forty": forty, "halves": [0] if forty else [1, 2], "box": box,
         "smart": config.YARD_STRATEGY == "SMART"},
    )).fetchone()


async def move_to_slot(conn: AsyncConnection, box: str, slot: dict, state: str | None = None) -> None:
    """state=None keeps the current one: a reshuffled blocker may itself be RETRIEVING for another lorry."""
    await conn.execute(
        """UPDATE containers SET state = coalesce(%(state)s::container_state, state), carrier_id = NULL,
                  block_code = %(block_code)s, bay = %(bay)s, row = %(row)s, tier = %(tier)s, half = %(half)s
           WHERE id = %(box)s""",
        {**slot, "box": box, "state": state},
    )


async def yard_fill(conn: AsyncConnection, purpose: str) -> float:
    row = await (await conn.execute(
        """SELECT coalesce(sum(CASE WHEN is_forty(c.iso_type) THEN 1 ELSE 0.5 END), 0)::float
                  / (SELECT count(*) FROM yard_slots s JOIN yard_blocks b ON b.code = s.block_code
                     WHERE b.purpose = %(p)s) AS f
           FROM containers c JOIN yard_blocks b ON b.code = c.block_code WHERE b.purpose = %(p)s""",
        {"p": purpose})).fetchone()
    return row["f"]


# --- quay ------------------------------------------------------------------------------------

async def claim_next_box(conn: AsyncConnection, vessel_id: int, bay_lo: int, bay_hi: int) -> dict | None:
    """Top-most box in the QC's bay range: nothing above it in any state (another crane may still be
    lifting the box that was on top). SKIP LOCKED: two QCs never wait on or share a box."""
    return await (await conn.execute(
        """
        UPDATE containers SET state = 'DISCHARGING'
        WHERE id = (
          SELECT c.id FROM containers c
          WHERE c.vessel_id = %(v)s AND c.state = 'STOWED_ON_BOARD' AND c.vessel_bay BETWEEN %(lo)s AND %(hi)s
            AND NOT EXISTS (SELECT 1 FROM containers a
                            WHERE (a.vessel_id, a.vessel_bay, a.vessel_row, a.vessel_half)
                                = (c.vessel_id, c.vessel_bay, c.vessel_row, c.vessel_half)
                              AND a.vessel_tier > c.vessel_tier)
          ORDER BY c.vessel_tier DESC, c.vessel_bay, c.vessel_row, c.vessel_half
          LIMIT 1
          FOR UPDATE OF c SKIP LOCKED)
        RETURNING id, iso_type, hazmat, weight_kg, flow, vessel_bay, vessel_row, vessel_tier, vessel_half
        """,
        {"v": vessel_id, "lo": bay_lo, "hi": bay_hi},
    )).fetchone()


async def peek_next_bay(conn: AsyncConnection, vessel_id: int, bays: list[int]) -> int | None:
    """Bay of the box claim_next_box would take, without claiming it: the QC moves first, then claims.
    Only bays still holding inbound cargo: under dual cycling, emptied bays fill with exports."""
    row = await (await conn.execute(
        """SELECT c.vessel_bay FROM containers c
           WHERE c.vessel_id = %(v)s AND c.state = 'STOWED_ON_BOARD' AND c.vessel_bay = ANY(%(bays)s)
             AND NOT EXISTS (SELECT 1 FROM containers a
                             WHERE (a.vessel_id, a.vessel_bay, a.vessel_row, a.vessel_half)
                                 = (c.vessel_id, c.vessel_bay, c.vessel_row, c.vessel_half)
                               AND a.vessel_tier > c.vessel_tier)
           ORDER BY c.vessel_tier DESC, c.vessel_bay, c.vessel_row, c.vessel_half
           LIMIT 1""",
        {"v": vessel_id, "bays": bays})).fetchone()
    return row and row["vessel_bay"]


async def pick_export(conn: AsyncConnection, below_kg: int | None, below_rotation: int | None,
                      forty: bool | None = None) -> dict | None:
    """Load planner: the next box for a cell must not out-weigh, or out-travel, the box beneath it.
    Greedy: latest port first, then heaviest, then fewest boxes to dig through. Keeps the most
    options open for the cells above."""
    return await (await conn.execute(
        """
        UPDATE containers SET state = 'RETRIEVING'
        WHERE id = (
          SELECT c.id FROM containers c JOIN ports p ON p.code = c.pod
          WHERE c.state = 'STACKED_IN_YARD' AND c.mode = 'VESSEL'
            AND (%(kg)s::int IS NULL OR (c.weight_kg <= %(kg)s AND p.rotation <= %(rot)s))
            AND (%(forty)s::boolean IS NULL OR is_forty(c.iso_type) = %(forty)s)
          ORDER BY p.rotation DESC, c.weight_kg DESC,
                   (SELECT count(*) FROM containers a
                    WHERE (a.block_code, a.bay, a.row, a.half) = (c.block_code, c.bay, c.row, c.half) AND a.tier > c.tier),
                   c.id
          LIMIT 1
          FOR UPDATE OF c SKIP LOCKED)
        RETURNING id, block_code, weight_kg, pod, iso_type, hazmat,
                  (SELECT rotation FROM ports WHERE code = pod) AS rotation
        """,
        {"kg": below_kg, "rot": below_rotation, "forty": forty},
    )).fetchone()


async def load_on_vessel(conn: AsyncConnection, box: str, vessel: int, bay: int, row: int, tier: int,
                         half: int) -> None:
    """Spreader has the box over its cell: stowage, gravity and length rules judge it at commit."""
    await conn.execute(
        """UPDATE containers SET state = 'LOADING', carrier_id = NULL,
                  vessel_id = %s, vessel_bay = %s, vessel_row = %s, vessel_tier = %s, vessel_half = %s
           WHERE id = %s""",
        (vessel, bay, row, tier, half, box),
    )


async def stow(conn: AsyncConnection, box: str) -> None:
    await conn.execute("UPDATE containers SET state = 'STOWED_ON_BOARD' WHERE id = %s", (box,))


async def berthed_vessel(conn: AsyncConnection, berth: int) -> dict | None:
    return await (await conn.execute(
        "SELECT id, name, imo, bays, rows, tiers FROM vessels WHERE berth_id = %s AND status <> 'DEPARTED'",
        (berth,))).fetchone()


async def vessel_status(conn: AsyncConnection, vessel: int, status: str) -> None:
    await conn.execute("UPDATE vessels SET status = %s WHERE id = %s", (status, vessel))


async def sail(conn: AsyncConnection, vessel: int) -> list[str]:
    await conn.execute(
        "UPDATE vessels SET status = 'DEPARTED', departed_at = clock_timestamp() WHERE id = %s", (vessel,))
    rows = await (await conn.execute(
        "UPDATE containers SET state = 'SAILED' WHERE vessel_id = %s AND state = 'STOWED_ON_BOARD' RETURNING id",
        (vessel,))).fetchall()
    return [r["id"] for r in rows]


# --- equipment -------------------------------------------------------------------------------

async def claim_agv(conn: AsyncConnection, x: int, z: int) -> str | None:
    """Nearest idle AGV (Manhattan), longest-idle on ties. The row lock stops two claimants booking one AGV."""
    row = await (await conn.execute(
        """
        UPDATE equipment SET state = 'BUSY'
        WHERE id = (SELECT id FROM equipment
                    WHERE kind = 'AGV' AND state = 'IDLE'
                    ORDER BY abs(pos_x - %s) + abs(pos_z - %s), idle_since, id
                    LIMIT 1 FOR UPDATE SKIP LOCKED)
        RETURNING id
        """,
        (x, z),
    )).fetchone()
    return row and row["id"]


async def claim_this_agv(conn: AsyncConnection, agv: str) -> bool:
    row = await (await conn.execute(
        "UPDATE equipment SET state = 'BUSY' WHERE id = %s AND state = 'IDLE' RETURNING id", (agv,))).fetchone()
    return row is not None


async def release_equipment(conn: AsyncConnection, unit: str, x: int | None = None, z: int | None = None) -> None:
    await conn.execute(
        """UPDATE equipment SET state = 'IDLE', idle_since = clock_timestamp(),
                  pos_x = coalesce(%s, pos_x), pos_z = coalesce(%s, pos_z) WHERE id = %s""",
        (x, z, unit))


async def claim_rail(conn: AsyncConnection, crane: str, lo: float, hi: float) -> str | None:
    """Claim [lo, hi] of the crane's rail. Returns the refusing rule's name (None when granted): a
    neighbour's envelope overlaps, or the crane is out of service.
    Claims on one rail take a transaction-scoped advisory lock first: two neighbours widening toward
    each other at the same instant would otherwise each wait on the other's uncommitted row."""
    try:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(left(%s, 2)))", (crane,))
            await conn.execute("UPDATE equipment SET rail_span = numrange(%s, %s) WHERE id = %s",
                               (Decimal(str(round(lo, 3))), Decimal(str(round(hi, 3))), crane))
        return None
    except errors.ExclusionViolation as e:
        return e.diag.constraint_name or "rail_overlap"
    except errors.DeadlockDetected:
        return "deadlock_detected"
    except errors.CheckViolation as e:
        return str(e).split(":", 1)[0]


# --- road bookings ---------------------------------------------------------------------------

async def route_clashes(conn: AsyncConnection, vehicle: str, legs: list[tuple]) -> list[dict]:
    """Other vehicles' bookings that overlap any proposed (x, z, t0, t1) leg. Uses the exclusion index."""
    xs, zs, t0s, t1s = (list(c) for c in zip(*legs))
    return await (await conn.execute(
        """
        SELECT DISTINCT b.x, b.z, b.t1 >= %(parked)s AS parked
        FROM unnest(%(xs)s::int[], %(zs)s::int[], %(t0s)s::float8[], %(t1s)s::float8[]) AS p(x, z, t0, t1)
        JOIN road_bookings b
          ON box(point(cell_key(b.x, b.z), b.t0), point(cell_key(b.x, b.z) + 0.5, b.t1))
          && box(point(cell_key(p.x, p.z), p.t0), point(cell_key(p.x, p.z) + 0.5, p.t1))
        WHERE b.vehicle <> %(v)s
        """,
        {"xs": xs, "zs": zs, "t0s": t0s, "t1s": t1s, "v": vehicle, "parked": PARKED},
    )).fetchall()


async def book_route(conn: AsyncConnection, vehicle: str, legs: list[tuple], depart: float) -> bool:
    """Swap the vehicle's bookings from `depart` on for the new legs. False if someone got there first:
    the pre-check in route_clashes is an optimisation, the exclusion constraint is the guarantee.

    Legs go in in cell order. An exclusion check waits for a conflicting uncommitted row, so two
    bookers inserting the same cells in opposite orders would deadlock; a fixed order cannot cycle."""
    cut = depart - 0.001
    xs, zs, t0s, t1s = (list(c) for c in zip(*legs))
    try:
        async with conn.transaction():
            await conn.execute("DELETE FROM road_bookings WHERE vehicle = %s AND t0 >= %s", (vehicle, cut))
            await conn.execute("UPDATE road_bookings SET t1 = %s WHERE vehicle = %s AND t1 > %s", (cut, vehicle, cut))
            await conn.execute(
                """INSERT INTO road_bookings (vehicle, x, z, t0, t1)
                   SELECT %s, x, z, t0, t1
                   FROM unnest(%s::int[], %s::int[], %s::float8[], %s::float8[]) AS l(x, z, t0, t1)
                   ORDER BY cell_key(x, z)""",
                (vehicle, xs, zs, t0s, t1s))
        return True
    except (errors.ExclusionViolation, errors.DeadlockDetected):
        return False


async def park(conn: AsyncConnection, vehicle: str, x: int, z: int, t: float) -> None:
    await conn.execute("INSERT INTO road_bookings (vehicle, x, z, t0, t1) VALUES (%s, %s, %s, %s, %s)",
                       (vehicle, x, z, t, PARKED))


async def free_cells(conn: AsyncConnection, cells: list[tuple[int, int]], vehicle: str = "") -> list[tuple[int, int]]:
    """Of these dead-end cells, the ones nobody else is parked on or heading to park on."""
    rows = await (await conn.execute(
        """SELECT p.x, p.z FROM unnest(%s::int[], %s::int[]) WITH ORDINALITY AS p(x, z, i)
           WHERE NOT EXISTS (SELECT 1 FROM road_bookings b
                             WHERE b.x = p.x AND b.z = p.z AND b.t1 >= %s AND b.vehicle <> %s)
           ORDER BY p.i""",
        ([x for x, _ in cells], [z for _, z in cells], PARKED, vehicle))).fetchall()
    return [(r["x"], r["z"]) for r in rows]


async def leave(conn: AsyncConnection, vehicle: str) -> None:
    await conn.execute("DELETE FROM road_bookings WHERE vehicle = %s", (vehicle,))


async def prune_bookings(conn: AsyncConnection, before: float) -> int:
    cur = await conn.execute("DELETE FROM road_bookings WHERE t1 < %s", (before,))
    return cur.rowcount


async def live_bookings(conn: AsyncConnection, vehicle: str, now: float) -> list[dict]:
    return await (await conn.execute(
        "SELECT x, z, t0, t1 FROM road_bookings WHERE vehicle = %s AND t1 >= %s ORDER BY t0", (vehicle, now))).fetchall()


# --- box moves -------------------------------------------------------------------------------

async def load_on_agv(conn: AsyncConnection, box: str, agv: str) -> None:
    """From a ship cell (discharge), a yard slot (rail / vessel transfer) or a wagon (rail-in) onto an AGV."""
    await conn.execute(
        """UPDATE containers SET state = 'IN_TRANSIT', carrier_id = %s,
                  vessel_bay = NULL, vessel_row = NULL, vessel_tier = NULL, vessel_half = NULL,
                  block_code = NULL, bay = NULL, row = NULL, tier = NULL, half = NULL,
                  train_id = NULL, wagon = NULL, wagon_slot = NULL
           WHERE id = %s""",
        (agv, box),
    )


async def load_on_truck(conn: AsyncConnection, box: str) -> None:
    await conn.execute(
        "UPDATE containers SET state = 'ON_TRUCK', block_code = NULL, bay = NULL, row = NULL, tier = NULL, half = NULL "
        "WHERE id = %s",
        (box,),
    )


async def gate_out(conn: AsyncConnection, box: str) -> None:
    await conn.execute("UPDATE containers SET state = 'GATE_OUT' WHERE id = %s", (box,))


async def mark_inspected(conn: AsyncConnection, box: str) -> None:
    await conn.execute("UPDATE containers SET inspected_at = clock_timestamp() WHERE id = %s", (box,))


async def load_on_wagon(conn: AsyncConnection, box: str, train: int, wagon: int, slot: int) -> None:
    await conn.execute(
        """UPDATE containers SET state = 'ON_RAIL', carrier_id = NULL,
                  train_id = %s, wagon = %s, wagon_slot = %s
           WHERE id = %s""",
        (train, wagon, slot, box),
    )


async def arrive_train(conn: AsyncConnection, name: str, wagons: int) -> int:
    row = await (await conn.execute(
        "INSERT INTO trains (name, wagons, status) VALUES (%s, %s, 'UNLOADING') RETURNING id", (name, wagons))).fetchone()
    return row["id"]


async def train_status(conn: AsyncConnection, train: int, status: str) -> None:
    await conn.execute("UPDATE trains SET status = %s WHERE id = %s", (status, train))


async def depart_train(conn: AsyncConnection, train: int) -> list[str]:
    await conn.execute(
        "UPDATE trains SET status = 'DEPARTED', departed_at = clock_timestamp() WHERE id = %s", (train,))
    rows = await (await conn.execute(
        "UPDATE containers SET state = 'RAIL_OUT' WHERE train_id = %s AND state = 'ON_RAIL' RETURNING id",
        (train,))).fetchall()
    return [r["id"] for r in rows]


async def pick_rail_box(conn: AsyncConnection, forty: bool | None = None) -> dict | None:
    """Rail planner: the rail-booked box with the fewest boxes on top of it, so trains rarely wait on digs."""
    return await (await conn.execute(
        """
        UPDATE containers SET state = 'RETRIEVING'
        WHERE id = (
          SELECT c.id FROM containers c
          WHERE c.state = 'STACKED_IN_YARD' AND c.mode = 'RAIL'
            AND (%(forty)s::boolean IS NULL OR is_forty(c.iso_type) = %(forty)s)
          ORDER BY (SELECT count(*) FROM containers a
                    WHERE (a.block_code, a.bay, a.row, a.half) = (c.block_code, c.bay, c.row, c.half) AND a.tier > c.tier),
                   c.id
          LIMIT 1
          FOR UPDATE OF c SKIP LOCKED)
        RETURNING id, block_code, customs_hold, iso_type
        """, {"forty": forty}
    )).fetchone()


async def pick_pickup(conn: AsyncConnection, plate: str, vehicle: str) -> dict | None:
    """Random truck-booked stacked box, marked RETRIEVING with its gate transaction opened."""
    return await (await conn.execute(
        """
        WITH pick AS (
          UPDATE containers SET state = 'RETRIEVING'
          WHERE id = (SELECT id FROM containers WHERE state = 'STACKED_IN_YARD' AND mode = 'TRUCK'
                      ORDER BY random() LIMIT 1 FOR UPDATE SKIP LOCKED)
          RETURNING id, block_code, customs_hold)
        INSERT INTO gate_transactions (plate, vehicle, container_id)
        SELECT %s, %s, id FROM pick
        RETURNING id AS gate_id, container_id AS box,
                  (SELECT block_code FROM pick) AS block_code,
                  (SELECT customs_hold FROM pick) AS customs_hold
        """,
        (plate, vehicle),
    )).fetchone()


async def gate_update(conn: AsyncConnection, gate_id: int, **fields) -> None:
    sets = ", ".join(f"{k} = %({k})s" for k in fields)
    await conn.execute(f"UPDATE gate_transactions SET {sets} WHERE id = %(id)s", {**fields, "id": gate_id})


async def gate_depart(conn: AsyncConnection, gate_id: int) -> None:
    await conn.execute(
        "UPDATE gate_transactions SET status = 'DEPARTED', departed_at = clock_timestamp() WHERE id = %s",
        (gate_id,))


# --- ledger ----------------------------------------------------------------------------------

async def gate_checkin(conn: AsyncConnection, gate_id: int) -> None:
    """The lorry is at the in-gate booth. Refused (gate_closed) while an outage covers this moment."""
    await conn.execute("UPDATE gate_transactions SET status = 'AT_LANE' WHERE id = %s", (gate_id,))


async def open_task(conn: AsyncConnection, unit: str, box: str, kind: str, origin: dict, target: dict, t0: float) -> int:
    row = await (await conn.execute(
        """INSERT INTO equipment_tasks (equipment_id, container_id, kind, status, origin, target, sim_t0)
           VALUES (%s, %s, %s, 'IN_PROGRESS', %s, %s, %s) RETURNING id""",
        (unit, box, kind, json.dumps(origin), json.dumps(target), t0),
    )).fetchone()
    return row["id"]


async def close_task(conn: AsyncConnection, task: int, t1: float, error: str | None = None) -> None:
    await conn.execute(
        """UPDATE equipment_tasks SET status = %s, sim_t1 = %s, error = %s, finished_at = clock_timestamp()
           WHERE id = %s""",
        ("FAILED" if error else "DONE", t1, error, task),
    )


async def log_task(conn: AsyncConnection, unit: str, box: str, kind: str, origin: dict, target: dict,
                   t0: float, t1: float) -> None:
    await close_task(conn, await open_task(conn, unit, box, kind, origin, target, t0), t1)


# --- read models -----------------------------------------------------------------------------

async def stats(conn: AsyncConnection) -> dict:
    row = await (await conn.execute(
        """
        SELECT
          (SELECT coalesce(jsonb_object_agg(b.code, (SELECT count(*) FROM containers c WHERE c.block_code = b.code)), '{}')
             FROM yard_blocks b) AS yard,
          (SELECT count(*) FROM yard_slots) AS capacity,
          (SELECT coalesce(jsonb_object_agg(state, n), '{}')
             FROM (SELECT state, count(*) AS n FROM containers GROUP BY state) s) AS states,
          (SELECT coalesce(jsonb_object_agg(flow, n), '{}')
             FROM (SELECT flow, count(*) AS n FROM containers
                   WHERE state NOT IN ('GATE_OUT','RAIL_OUT','SAILED') GROUP BY flow) f) AS flows,
          (SELECT count(*) FROM equipment_tasks WHERE kind = 'DISCHARGE' AND status = 'DONE') AS discharged,
          (SELECT count(*) FROM equipment_tasks WHERE kind = 'LOAD' AND status = 'DONE') AS loaded,
          (SELECT count(*) FROM equipment_tasks WHERE kind = 'RESHUFFLE' AND status = 'DONE') AS reshuffles,
          (SELECT count(*) FROM containers WHERE state = 'GATE_OUT') AS gated_out,
          (SELECT count(*) FROM containers WHERE state = 'RAIL_OUT') AS railed_out,
          (SELECT count(*) FROM containers WHERE state = 'SAILED') AS sailed,
          (SELECT count(*) FROM containers WHERE customs_hold AND inspected_at IS NULL
                                              AND state NOT IN ('GATE_OUT','RAIL_OUT')) AS customs_pending,
          (SELECT count(*) FROM containers WHERE inspected_at IS NOT NULL) AS inspected,
          (SELECT count(*) FROM road_bookings) AS bookings
        """
    )).fetchone()
    return dict(row)


async def get_container(conn: AsyncConnection, box: str) -> dict | None:
    return await (await conn.execute(
        """SELECT f.*, v.name AS vessel_name, t.name AS train_name
           FROM container_feed f
           LEFT JOIN vessels v ON v.id = f.vessel_id
           LEFT JOIN trains t ON t.id = f.train_id
           WHERE f.id = %s""", (box,))).fetchone()


async def live_containers(conn: AsyncConnection) -> list[dict]:
    return await (await conn.execute(
        "SELECT * FROM container_feed WHERE state NOT IN ('GATE_OUT', 'RAIL_OUT', 'SAILED') ORDER BY id")).fetchall()


# --- weather, breakdowns, outages ------------------------------------------------------------

async def add_weather(conn: AsyncConnection, t: float, wind: float, gust: float, rain: float, fog: float) -> None:
    await conn.execute("INSERT INTO weather (t, wind_ms, gust_ms, rain, fog) VALUES (%s, %s, %s, %s, %s) "
                       "ON CONFLICT (t) DO NOTHING", (t, wind, gust, rain, fog))


async def wind_limits(conn: AsyncConnection) -> dict:
    rows = await (await conn.execute("SELECT kind::text AS kind, max_wind_ms FROM crane_wind_limits")).fetchall()
    return {r["kind"]: r["max_wind_ms"] for r in rows}


async def set_equipment_state(conn: AsyncConnection, unit: str, state: str) -> None:
    await conn.execute("UPDATE equipment SET state = %s WHERE id = %s", (state, unit))


async def agv_fault(conn: AsyncConnection) -> str | None:
    """Take one idle AGV out of service (the caller already holds its pool token)."""
    row = await (await conn.execute(
        """UPDATE equipment SET state = 'DOWN'
           WHERE id = (SELECT id FROM equipment WHERE kind = 'AGV' AND state = 'IDLE'
                       ORDER BY random() LIMIT 1 FOR UPDATE SKIP LOCKED)
           RETURNING id""")).fetchone()
    return row and row["id"]


async def gate_outage(conn: AsyncConnection, t0: float, t1: float, reason: str) -> bool:
    try:
        async with conn.transaction():
            await conn.execute("INSERT INTO gate_outages (during, reason) VALUES (numrange(%s::numeric, %s::numeric), %s)",
                               (round(t0, 3), round(t1, 3), reason))
        return True
    except errors.ExclusionViolation:          # already down: outages never overlap
        return False


# --- berth plan ------------------------------------------------------------------------------

def quay(berth: int) -> tuple[int, int]:
    """Quay metres (grid x) a ship at this berth takes, hull and fenders."""
    from .layout import BERTH_X0
    return BERTH_X0[berth] - 2, BERTH_X0[berth] + config.BERTHS[berth][1] + 4


async def plan_window(conn: AsyncConnection, berth: int, name: str, t0: float, t1: float) -> int:
    lo, hi = quay(berth)
    row = await (await conn.execute(
        """INSERT INTO berth_windows (berth_id, vessel_name, quay, during)
           VALUES (%s, %s, int4range(%s, %s), numrange(%s::numeric, %s::numeric)) RETURNING id""",
        (berth, name, lo, hi, round(t0, 3), round(t1, 3)))).fetchone()
    return row["id"]


async def move_window(conn: AsyncConnection, wid: int, start_by: float = 0.0, end_by: float = 0.0) -> bool:
    """Shift a window's start and end. False if it would overlap another window on the same quay."""
    try:
        async with conn.transaction():
            await conn.execute(
                "UPDATE berth_windows SET during = numrange(lower(during) + %s::numeric, upper(during) + %s::numeric) "
                "WHERE id = %s AND status <> 'DONE'",   # a call that has sailed keeps its window
                (round(start_by, 3), round(end_by, 3), wid))
        return True
    except errors.ExclusionViolation:
        return False


async def window_alongside(conn: AsyncConnection, wid: int, vessel: int, name: str, t: float) -> None:
    """The ship is here: the window slides to start now, keeping its planned length."""
    await conn.execute(
        """UPDATE berth_windows SET status = 'ALONGSIDE', vessel_id = %(v)s, vessel_name = %(n)s,
                  during = numrange(%(t)s::numeric, %(t)s::numeric + (upper(during) - lower(during)))
           WHERE id = %(w)s""", {"v": vessel, "n": name, "t": round(t, 3), "w": wid})


async def window_done(conn: AsyncConnection, wid: int, t: float) -> None:
    await conn.execute(
        "UPDATE berth_windows SET status = 'DONE', during = numrange(lower(during), greatest(%s::numeric, lower(during) + 1)) "
        "WHERE id = %s", (round(t, 3), wid))


async def berth_plan(conn: AsyncConnection, since: float) -> list[dict]:
    return await (await conn.execute(
        """SELECT id, berth_id AS berth, vessel_name AS name, status,
                  lower(during)::float AS t0, upper(during)::float AS t1
           FROM berth_windows WHERE upper(during) > %s ORDER BY berth_id, lower(during)""", (since,))).fetchall()


# --- time travel ---------------------------------------------------------------------------------

async def maintain(conn: AsyncConnection, now: float) -> dict:
    """Partitions for the next ten minutes; whole partitions past retention dropped."""
    made = (await (await conn.execute("SELECT ensure_partitions(%s, %s) AS n", (now, now + 600))).fetchone())["n"]
    dropped = (await (await conn.execute("SELECT drop_history_before(%s) AS n",
                                         (now - config.RETENTION_S,))).fetchone())["n"]
    return {"made": made, "dropped": dropped}


async def append_events(conn: AsyncConnection, rows: list[tuple]) -> None:
    """(t, type, unit, body-json) rows in one COPY: the stream is written in batches, never per event."""
    async with conn.cursor() as cur:
        async with cur.copy("COPY event_log (t, type, unit, body) FROM STDIN") as copy:
            for r in rows:
                await copy.write_row(r)


async def boxes_as_of(conn: AsyncConnection, t: float) -> list[dict]:
    """Every box as it stood at simulated second t: one GiST probe on the validity range."""
    rows = await (await conn.execute(
        """SELECT snapshot FROM container_history
           WHERE valid @> %(t)s::numeric AND lower(valid) <= %(t)s
             AND state NOT IN ('SAILED', 'RAIL_OUT')""", {"t": t})).fetchall()
    return [r["snapshot"] for r in rows]


async def latest_events(conn: AsyncConnection, t: float, types: list[str], window: float) -> list[dict]:
    """The last event per unit (or per body id) of these types at or before t."""
    return await (await conn.execute(
        """SELECT DISTINCT ON (type, coalesce(unit, body->>'id', body->>'block')) t, type, unit, body
           FROM event_log WHERE t <= %s AND t > %s AND type = ANY(%s)
           ORDER BY type, coalesce(unit, body->>'id', body->>'block'), t DESC""", (t, t - window, types))).fetchall()


async def events_between(conn: AsyncConnection, t0: float, t1: float) -> list[dict]:
    """Everything that happened in (t0, t1]: box versions from the history, the rest from the log."""
    return await (await conn.execute(
        """SELECT t, type, body FROM (
             SELECT lower(valid)::float AS t, 'box' AS type, snapshot AS body
             FROM container_history WHERE lower(valid) > %(a)s AND lower(valid) <= %(b)s
             UNION ALL
             SELECT t, type, body FROM event_log WHERE t > %(a)s AND t <= %(b)s) e
           ORDER BY t""", {"a": t0, "b": t1})).fetchall()


async def journey(conn: AsyncConnection, box: str) -> dict:
    """Every version of a box, and every crane and vehicle movement that carried it."""
    stops = await (await conn.execute(
        """SELECT lower(valid)::float AS t0, upper(valid)::float AS t1, state, snapshot
           FROM container_history WHERE container_id = %s ORDER BY lower(valid)""", (box,))).fetchall()
    moves = await (await conn.execute(
        """WITH carried AS (
             SELECT snapshot->>'carrier_id' AS unit, lower(valid)::float AS t0,
                    coalesce(upper(valid)::float, 'Infinity') AS t1
             FROM container_history WHERE container_id = %(b)s AND snapshot->>'carrier_id' IS NOT NULL)
           SELECT e.t, e.unit, e.body->'path' AS path, (e.body->>'t1')::float AS t1
           FROM event_log e
           WHERE e.type = 'track' AND (e.unit = %(b)s
                 OR EXISTS (SELECT 1 FROM carried c WHERE c.unit = e.unit AND e.t >= c.t0 AND e.t < c.t1))
           ORDER BY e.t""", {"b": box})).fetchall()
    return {"stops": stops, "moves": moves}


# --- x-ray, dashboards ---------------------------------------------------------------------------

async def xray(conn: AsyncConnection, now: float, ahead: float) -> dict:
    bookings = await (await conn.execute(
        """SELECT vehicle, x, z, t0, least(t1, %(far)s) AS t1, t1 >= %(parked)s AS parked
           FROM road_bookings WHERE t1 >= %(now)s AND t0 <= %(far)s""",
        {"now": now, "far": now + ahead, "parked": PARKED})).fetchall()
    rails = await (await conn.execute(
        """SELECT id, kind::text AS kind, lower(rail_span)::float AS lo, upper(rail_span)::float AS hi, state::text AS state
           FROM equipment WHERE rail_span IS NOT NULL ORDER BY id""")).fetchall()
    outage = await (await conn.execute(
        "SELECT reason, upper(during)::float AS until FROM gate_outages WHERE during @> %s::numeric",
        (now,))).fetchone()
    return {"bookings": bookings, "rails": rails, "gate_outage": outage}


async def visible_to(conn: AsyncConnection, role: str, line: str | None) -> list[str]:
    """The same SELECT run as another role: row-level security decides which boxes come back."""
    async with conn.transaction():
        await conn.execute(f"SET LOCAL ROLE {role}")       # role comes from a fixed allow-list in the API
        if line:
            await conn.execute("SELECT set_config('portdb.line', %s, true)", (line,))
        rows = await (await conn.execute(
            "SELECT id FROM container_feed WHERE state NOT IN ('GATE_OUT', 'RAIL_OUT', 'SAILED')")).fetchall()
    return [r["id"] for r in rows]


async def kpi(conn: AsyncConnection, since: float, bucket: float) -> list[dict]:
    """Re-handles per 100 yard retrievals and quay crane moves, per bucket of simulated time."""
    return await (await conn.execute(
        """SELECT (floor(sim_t0 / %(b)s) * %(b)s)::float AS t,
                  count(*) FILTER (WHERE kind = 'RESHUFFLE') AS rehandles,
                  count(*) FILTER (WHERE kind = 'RETRIEVE') AS retrievals,
                  count(*) FILTER (WHERE kind IN ('DISCHARGE', 'LOAD')) AS qc_moves
           FROM equipment_tasks WHERE status = 'DONE' AND sim_t0 >= %(s)s
           GROUP BY 1 ORDER BY 1""", {"b": bucket, "s": since})).fetchall()


async def heat(conn: AsyncConnection) -> dict:
    stacks = await (await conn.execute(
        """SELECT origin->>'block_code' AS block, (origin->>'bay')::int AS bay, (origin->>'row')::int AS row, count(*) AS n
           FROM equipment_tasks WHERE kind = 'RESHUFFLE' AND status = 'DONE' GROUP BY 1, 2, 3""")).fetchall()
    reefer = await (await conn.execute(
        """SELECT b.code AS block, count(c.id) FILTER (WHERE c.iso_type LIKE '%%R1') AS reefers
           FROM yard_blocks b LEFT JOIN containers c ON c.block_code = b.code GROUP BY b.code ORDER BY b.code""")).fetchall()
    return {"rehandles": stacks, "reefers": reefer}


async def db_stats(conn: AsyncConnection) -> dict:
    """What the database itself reports: commits, rollbacks, deadlocks, lock waits, table sizes, and the
    heaviest statements when pg_stat_statements is installed."""
    db = await (await conn.execute(
        """SELECT xact_commit, xact_rollback, deadlocks, conflicts, tup_inserted, tup_updated, tup_deleted,
                  round(100.0 * blks_hit / greatest(blks_hit + blks_read, 1), 2)::float AS cache_hit_pct
           FROM pg_stat_database WHERE datname = current_database()""")).fetchone()
    locks = await (await conn.execute(
        "SELECT count(*) FILTER (WHERE NOT granted) AS waiting, count(*) AS held FROM pg_locks")).fetchone()
    sizes = await (await conn.execute(
        """SELECT (SELECT count(*) FROM container_history) AS history_rows,
                  (SELECT count(*) FROM event_log) AS event_rows,
                  (SELECT count(*) FROM pg_inherits i JOIN pg_class p ON p.oid = i.inhparent
                   WHERE p.relname IN ('container_history', 'event_log')) AS partitions,
                  (SELECT coalesce(sum(pg_total_relation_size(i.inhrelid)), 0) FROM pg_inherits i
                   JOIN pg_class p ON p.oid = i.inhparent
                   WHERE p.relname IN ('container_history', 'event_log'))::bigint AS history_bytes""")).fetchone()
    top = None
    if await (await conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'")).fetchone():
        try:
            async with conn.transaction():
                top = await (await conn.execute(
                    r"""SELECT left(regexp_replace(query, '\s+', ' ', 'g'), 90) AS query, calls,
                              round(total_exec_time::numeric, 1)::float AS total_ms,
                              round(mean_exec_time::numeric, 3)::float AS mean_ms
                       FROM pg_stat_statements
                       WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
                       ORDER BY total_exec_time DESC LIMIT 8""")).fetchall()
        except errors.Error:                 # installed but not in shared_preload_libraries
            top = None
    return {**db, **locks, **sizes, "top": top}
