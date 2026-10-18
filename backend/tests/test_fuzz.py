"""Property test: whatever sequence of moves is thrown at it, the database never commits an invalid yard,
ship or wagon. Hypothesis generates the moves (most of them illegal); each runs in its own transaction
and a refusal is fine. After every sequence, independent SQL checks look for a broken invariant."""

import random

import psycopg
from hypothesis import HealthCheck, given, settings, strategies as st

from portdb.seed_vessel import cargo, insert_boxes, reset

from .conftest import connect, run

N_BOXES = 10
POS = st.tuples(st.integers(1, 2), st.integers(1, 2), st.sampled_from([1, 1, 1, 2, 2, 3]), st.integers(0, 2))  # bay row tier half
OPS = st.one_of(
    st.tuples(st.just("yard"), st.integers(0, N_BOXES - 1), st.sampled_from("ACDF"), POS),
    st.tuples(st.just("ship"), st.integers(0, N_BOXES - 1), POS),
    st.tuples(st.just("wagon"), st.integers(0, N_BOXES - 1), st.integers(1, 2), st.integers(1, 3)),
    st.tuples(st.just("lift"), st.integers(0, N_BOXES - 1)),
)

# each must come back empty
INVARIANTS = {
    "gravity": """
        SELECT 1 FROM containers WHERE block_code IS NOT NULL GROUP BY block_code, bay, row, half
        HAVING count(*) <> max(tier)
        UNION ALL
        SELECT 1 FROM containers WHERE vessel_bay IS NOT NULL GROUP BY vessel_id, vessel_bay, vessel_row, vessel_half
        HAVING count(*) <> max(vessel_tier)""",
    "one length per stack": """
        SELECT 1 FROM containers WHERE block_code IS NOT NULL GROUP BY block_code, bay, row
        HAVING count(DISTINCT left(iso_type, 1)) > 1
        UNION ALL
        SELECT 1 FROM containers WHERE vessel_bay IS NOT NULL GROUP BY vessel_id, vessel_bay, vessel_row
        HAVING count(DISTINCT left(iso_type, 1)) > 1""",
    "length matches position": """
        SELECT 1 FROM containers
        WHERE (half IS NOT NULL AND (half = 0) <> (left(iso_type, 1) = '4'))
           OR (vessel_half IS NOT NULL AND (vessel_half = 0) <> (left(iso_type, 1) = '4'))""",
    "stowage": """
        SELECT 1 FROM containers lo JOIN containers hi
          ON (hi.vessel_id, hi.vessel_bay, hi.vessel_row, hi.vessel_half, hi.vessel_tier)
           = (lo.vessel_id, lo.vessel_bay, lo.vessel_row, lo.vessel_half, lo.vessel_tier + 1)
        JOIN ports pl ON pl.code = lo.pod JOIN ports ph ON ph.code = hi.pod
        WHERE hi.weight_kg > lo.weight_kg OR ph.rotation > pl.rotation""",
    "imdg": """
        SELECT 1 FROM containers a JOIN containers b ON a.block_code = b.block_code AND a.id < b.id
        JOIN imdg_segregation s ON (s.class_a, s.class_b) = (least(a.imdg_class, b.imdg_class), greatest(a.imdg_class, b.imdg_class))
        WHERE greatest(abs(a.bay - b.bay), abs(a.row - b.row)) <= s.code""",
    "hazmat block": """
        SELECT 1 FROM containers c JOIN yard_blocks b ON b.code = c.block_code WHERE c.hazmat AND NOT b.allows_hazmat""",
    "wagon overlap": """
        SELECT 1 FROM containers a JOIN containers b ON (a.train_id, a.wagon) = (b.train_id, b.wagon) AND a.id < b.id
        WHERE int4range(a.wagon_slot, a.wagon_slot + CASE WHEN left(a.iso_type, 1) = '4' THEN 2 ELSE 1 END)
           && int4range(b.wagon_slot, b.wagon_slot + CASE WHEN left(b.iso_type, 1) = '4' THEN 2 ELSE 1 END)""",
    "history has one open version per box": """
        SELECT 1 FROM container_history WHERE upper_inf(valid) GROUP BY container_id HAVING count(*) <> 1
        UNION ALL
        SELECT 1 FROM containers c WHERE NOT EXISTS (SELECT 1 FROM container_history h
                                                     WHERE h.container_id = c.id AND upper_inf(valid))""",
}


async def setup(conn, seed: int) -> list[str]:
    await reset(conn)
    rng = random.Random(seed)
    vessel = (await (await conn.execute(
        "INSERT INTO vessels (name, imo, bays, rows, tiers, status, berth_id) "
        "VALUES ('Fuzz', '9000001', 2, 2, 4, 'LOADING', 1) RETURNING id")).fetchone())["id"]
    train = (await (await conn.execute(
        "INSERT INTO trains (name, wagons, status) VALUES ('FZ', 2, 'LOADING') RETURNING id")).fetchone())["id"]
    boxes = []
    for i in range(N_BOXES):
        # imports booked for rail (wagons) and exports (ships), mixed lengths, some hazardous; all on AGVs
        b = cargo(rng, "IMPORT" if i % 2 else "EXPORT", forty=i % 3 != 0) | {"state": "IN_TRANSIT", "carrier_id": "AGV-1"}
        if b["flow"] == "IMPORT":
            b["mode"] = "RAIL"
        if i % 4 == 1:
            b |= {"hazmat": True, "imdg_class": ["3", "5.1", "8"][i % 3]}
        boxes.append(b)
    await insert_boxes(conn, boxes)
    await conn.commit()
    return [b["id"] for b in boxes], vessel, train


async def apply(conn, op, ids, vessel, train) -> None:
    box = ids[op[1]]
    if op[0] == "yard":
        _, _, block, (bay, row, tier, half) = op
        await conn.execute(
            """UPDATE containers SET state = 'STACKED_IN_YARD', carrier_id = NULL,
                      block_code = %s, bay = %s, row = %s, tier = %s, half = %s WHERE id = %s""",
            (block, bay, row, tier, half, box))
    elif op[0] == "ship":
        _, _, (bay, row, tier, half) = op
        await conn.execute("UPDATE containers SET state = 'RETRIEVING' WHERE id = %s AND state = 'STACKED_IN_YARD'", (box,))
        await conn.execute(
            """UPDATE containers SET state = 'IN_TRANSIT', carrier_id = 'AGV-1', block_code = NULL, bay = NULL,
                      row = NULL, tier = NULL, half = NULL WHERE id = %s AND state = 'RETRIEVING'""", (box,))
        await conn.execute(
            """UPDATE containers SET state = 'LOADING', carrier_id = NULL, vessel_id = %s, vessel_bay = %s,
                      vessel_row = %s, vessel_tier = %s, vessel_half = %s WHERE id = %s""",
            (vessel, bay, row, tier, half, box))
    elif op[0] == "wagon":
        _, _, wagon, pos = op
        await conn.execute(
            """UPDATE containers SET state = 'ON_RAIL', carrier_id = NULL, block_code = NULL, bay = NULL, row = NULL,
                      tier = NULL, half = NULL, train_id = %s, wagon = %s, wagon_slot = %s WHERE id = %s""",
            (train, wagon, pos, box))
    else:
        await conn.execute("UPDATE containers SET state = 'RETRIEVING' WHERE id = %s", (box,))
        await conn.execute(
            """UPDATE containers SET state = 'IN_TRANSIT', carrier_id = 'AGV-2', block_code = NULL, bay = NULL,
                      row = NULL, tier = NULL, half = NULL WHERE id = %s""", (box,))


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(seed=st.integers(0, 10_000), ops=st.lists(OPS, min_size=5, max_size=25))
def test_no_sequence_of_moves_breaks_an_invariant(pg_url, seed, ops):
    async def go():
        async with await connect(pg_url) as conn:
            ids, vessel, train = await setup(conn, seed)
            for t, op in enumerate(ops, start=1):
                try:
                    async with conn.transaction():
                        await conn.execute("SELECT set_config('portdb.sim_t', %s, true)", (str(t),))
                        await apply(conn, op, ids, vessel, train)
                except psycopg.errors.Error:
                    pass                                  # refused: that is the point
            for name, sql in INVARIANTS.items():
                bad = await (await conn.execute(sql)).fetchall()
                assert not bad, f"{name} broken after {ops}"
    run(go())
