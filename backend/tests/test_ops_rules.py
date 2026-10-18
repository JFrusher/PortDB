"""Migration 0005: time travel, change feed, and the operating rules the database enforces itself."""

import asyncio
import json
import random

import psycopg
import pytest

from portdb import queries as q
from portdb.seed_vessel import cargo, insert_boxes

from .conftest import connect, run


def rejects(fragment: str):
    return pytest.raises(psycopg.errors.Error, match=fragment)


async def at(conn, t: float) -> None:
    """Stamp the open transaction with a simulated second, as the engine does."""
    await conn.execute("SELECT set_config('portdb.sim_t', %s, true)", (str(t),))


async def stack(conn, vessel: int, t: float, block="A", bay=1, row=1) -> str:
    """Discharge the next box into a yard slot through the real lifecycle, all at simulated second t."""
    async with conn.transaction():
        await at(conn, t)
        box = (await q.claim_next_box(conn, vessel, 1, 6))["id"]
        await q.load_on_agv(conn, box, "AGV-1")
    async with conn.transaction():
        await at(conn, t)
        await q.move_to_slot(conn, box, {"block_code": block, "bay": bay, "row": row, "tier": 1, "half": 0},
                             "STACKED_IN_YARD")
    return box


def test_history_answers_where_was_it_at_t(db):
    url, vessel = db

    async def go():
        async with await connect(url) as conn:
            box = await stack(conn, vessel, 10)
            async with conn.transaction():
                await at(conn, 20)
                await conn.execute("UPDATE containers SET state = 'RETRIEVING' WHERE id = %s", (box,))
            then = {b["id"]: b for b in await q.boxes_as_of(conn, 15)}
            now = {b["id"]: b for b in await q.boxes_as_of(conn, 25)}
            assert then[box]["state"] == "STACKED_IN_YARD" and then[box]["block_code"] == "A"
            assert now[box]["state"] == "RETRIEVING"
            assert then[box]["yard_in_t"] == 10
            # one closed version per change, the newest still open
            rows = await (await conn.execute(
                "SELECT state, lower(valid)::float AS t0, upper(valid)::float AS t1 FROM container_history "
                "WHERE container_id = %s ORDER BY lower(valid)", (box,))).fetchall()
            # claim, AGV and stack all at t=10 collapse into one version
            assert [(r["state"], r["t0"], r["t1"]) for r in rows] == [
                ("STOWED_ON_BOARD", 0, 10), ("STACKED_IN_YARD", 10, 20), ("RETRIEVING", 20, None)]
    run(go())


def test_change_feed_publishes_once_per_commit(db):
    url, vessel = db

    async def go():
        async with await connect(url) as listener, await connect(url) as conn:
            await listener.set_autocommit(True)
            await listener.execute("LISTEN container_feed")
            box = await stack(conn, vessel, 5)
            seen = []
            gen = listener.notifies(timeout=1.0)
            async for n in gen:
                seen.append(json.loads(n.payload))
            mine = [s["box"]["state"] for s in seen if s["box"]["id"] == box]
            # claim + AGV hand-over in one commit, then the stack: the feed carries each commit's end state
            assert mine == ["IN_TRANSIT", "STACKED_IN_YARD"]
    run(go())


def test_no_crane_lifts_above_its_wind_limit(db):
    url, vessel = db

    async def go():
        async with await connect(url) as conn:
            await q.add_weather(conn, 0, 23.0, 30.0, 0.5, 0.0)       # QC limit 20, RTG 22, RMG 24
            await conn.commit()
            box = await stack(conn, vessel, 1)
            for unit in ("QC-1", "RTG-A"):
                with rejects("wind_limit"):
                    async with conn.transaction():
                        await at(conn, 5)
                        await q.open_task(conn, unit, box, "STACK", {}, {}, 5)
            async with conn.transaction():                          # an RMG is rated higher; AGVs do not lift
                await at(conn, 5)
                await q.open_task(conn, "RMG-1", box, "RAIL_LOAD", {}, {}, 5)
                await q.open_task(conn, "AGV-2", box, "TRANSPORT", {}, {}, 5)
            await q.add_weather(conn, 6, 12.0, 15.0, 0.0, 0.0)       # the wind drops
            await conn.commit()
            async with conn.transaction():
                await at(conn, 7)
                await q.open_task(conn, "QC-1", box, "STACK", {}, {}, 7)
    run(go())


def test_equipment_that_is_down_takes_no_work_and_no_rail(db):
    url, vessel = db

    async def go():
        async with await connect(url) as conn:
            box = await stack(conn, vessel, 1)
            await q.set_equipment_state(conn, "QC-3", "DOWN")
            await conn.commit()
            with rejects("equipment_down"):
                async with conn.transaction():
                    await q.open_task(conn, "QC-3", box, "DISCHARGE", {}, {}, 2)
            span = await (await conn.execute(
                "SELECT lower(rail_span)::float AS lo, upper(rail_span)::float AS hi FROM equipment WHERE id = 'QC-3'"
            )).fetchone()
            assert await q.claim_rail(conn, "QC-3", span["lo"] - 1, span["hi"]) == "equipment_down"
            assert await q.claim_rail(conn, "QC-3", span["lo"] + 0.1, span["hi"] - 0.1) is None   # shrinking is fine
    run(go())


def test_no_lorry_checks_in_during_a_gate_outage(db):
    url, vessel = db

    async def go():
        async with await connect(url) as conn:
            box = await stack(conn, vessel, 1)
            await conn.execute("UPDATE containers SET state = 'RETRIEVING' WHERE id = %s", (box,))
            gate = (await (await conn.execute(
                "INSERT INTO gate_transactions (plate, vehicle, container_id) VALUES ('AB12 CDE', 'LRY-I1', %s) "
                "RETURNING id", (box,))).fetchone())["id"]
            assert await q.gate_outage(conn, 10, 50, "barrier fault")
            assert not await q.gate_outage(conn, 40, 60, "second fault")    # outages never overlap
            await conn.commit()
            with rejects("gate_closed"):
                async with conn.transaction():
                    await at(conn, 30)
                    await q.gate_checkin(conn, gate)
            async with conn.transaction():
                await at(conn, 55)
                await q.gate_checkin(conn, gate)
    run(go())


def hazbox(rng, cls: str, bay: int, row: int) -> dict:
    return cargo(rng, "IMPORT") | {"hazmat": True, "imdg_class": cls, "state": "STACKED_IN_YARD",
                                   "block_code": "C", "bay": bay, "row": row, "tier": 1,
                                   "half": 0, "iso_type": "45G1"}


def test_imdg_segregation_by_class_distance(db):
    url, _ = db
    rng = random.Random(3)

    async def go():
        async with await connect(url) as conn:
            await insert_boxes(conn, [hazbox(rng, "3", 1, 1)])
            await conn.commit()
            with rejects("imdg_segregation"):                         # flammable liquid vs oxidiser: separated from
                await insert_boxes(conn, [hazbox(rng, "5.1", 3, 1)])
                await conn.commit()
            await conn.rollback()
            await insert_boxes(conn, [hazbox(rng, "5.1", 4, 1), hazbox(rng, "9", 1, 2)])  # 3 stacks clear; class 9 any
            await conn.commit()
            # the allocator keeps its distance too: a new class 8 must stay 3 stacks from the oxidiser
            new = cargo(rng, "IMPORT", forty=True) | {"hazmat": True, "imdg_class": "8", "state": "ON_TRUCK",
                                                       "mode": "TRUCK"}
            await insert_boxes(conn, [new])
            s = await q.allocate_slot(conn, "C", True, box=new["id"])
            assert max(abs(s["bay"] - 4), abs(s["row"] - 1)) > 2
    run(go())


def test_berth_windows_never_overlap_on_the_quay(db):
    url, _ = db

    async def go():
        async with await connect(url) as conn:
            a = await q.plan_window(conn, 1, "Little Puffin", 0, 300)
            b = await q.plan_window(conn, 1, "Mersey Wren", 320, 600)
            await q.plan_window(conn, 2, "Solent Otter", 100, 400)        # another berth: other quay metres
            await conn.commit()
            assert not await q.move_window(conn, a, 0, 30)                  # would run into the next call
            assert await q.move_window(conn, b, 30, 30)                     # push the next call back first
            assert await q.move_window(conn, a, 0, 30)
            with rejects("berth_windows_no_overlap"):
                await q.plan_window(conn, 1, "Humber Gull", 500, 700)
    run(go())


def test_row_level_security_shapes_who_sees_what(db):
    url, _ = db

    async def go():
        async with await connect(url) as conn:
            await conn.execute("UPDATE containers SET customs_hold = true WHERE id IN "
                               "(SELECT id FROM containers ORDER BY id LIMIT 5)")
            await conn.commit()
            everyone = {r["id"] for r in await q.live_containers(conn)}
            customs = set(await q.visible_to(conn, "portdb_customs", None))
            line = set(await q.visible_to(conn, "portdb_line", "MSC"))
            gate = set(await q.visible_to(conn, "portdb_gate", None))
            assert len(customs) == 5 and customs < everyone
            assert line and all(i.startswith("MSC") for i in line)
            assert gate == {i for i in everyone}                          # the fixture books every box by road
            with rejects("permission denied"):
                async with conn.transaction():
                    await conn.execute("SET LOCAL ROLE portdb_customs")
                    await conn.execute("UPDATE containers SET customs_hold = false")
    run(go())


def test_retention_drops_partitions_but_keeps_every_box(db):
    url, vessel = db

    async def go():
        async with await connect(url) as conn:
            box = await stack(conn, vessel, 10)
            await conn.execute("SELECT ensure_partitions(0, 1500)")
            await conn.commit()
            assert box in {b["id"] for b in await q.boxes_as_of(conn, 1000)}
            dropped = (await (await conn.execute("SELECT drop_history_before(900) AS n")).fetchone())["n"]
            await conn.commit()
            assert dropped >= 4                                             # three history + three log partitions
            assert box in {b["id"] for b in await q.boxes_as_of(conn, 1000)}   # carried forward to t=900
            assert not await q.boxes_as_of(conn, 100)                       # older history is gone
    run(go())


def test_concurrent_hazmat_placements_cannot_both_win(db):
    """Two transactions place incompatible classes next to each other at once: one must lose."""
    url, _ = db
    rng = random.Random(9)

    async def place(cls: str, bay: int):
        async with await connect(url) as conn:
            await insert_boxes(conn, [hazbox(rng, cls, bay, 1)])
            await asyncio.sleep(0.2)
            await conn.commit()

    async def go():
        results = await asyncio.gather(place("3", 2), place("5.1", 3), return_exceptions=True)
        assert sum(isinstance(r, psycopg.errors.Error) for r in results) == 1
    run(go())
