"""The database, not Python, refuses illegal terminal states."""

import asyncio
import re
import random

import psycopg
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from portdb import queries as q
from portdb.db import sqlalchemy_url
from portdb.models import Base
from portdb.seed_vessel import cargo, container_id, insert_boxes, reset, seed_yard

from .conftest import connect, run


async def discharge_to(conn, vessel: int, slot: dict) -> str:
    """Walk the next box through the real lifecycle into `slot`, one commit per step."""
    async with conn.transaction():
        box = (await q.claim_next_box(conn, vessel, 1, 6))["id"]
    async with conn.transaction():
        await q.load_on_agv(conn, box, "AGV-1")
    async with conn.transaction():
        await q.move_to_slot(conn, box, slot, "STACKED_IN_YARD")
    return box


def slot(block="A", bay=1, row=1, tier=1, half=0):
    return {"block_code": block, "bay": bay, "row": row, "tier": tier, "half": half}


def rejects(fragment: str):
    return pytest.raises(psycopg.errors.Error, match=fragment)


def test_models_match_migration(db):
    url, _ = db
    with create_engine(sqlalchemy_url(url)).connect() as c:
        # partitions are created at run time; the parents are compared
        ctx = MigrationContext.configure(c, opts={"include_name": lambda name, kind, _: not (
            kind == "table" and re.search(r"_(p\d+|default)$", name or ""))})
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == []


def test_iso6346_sql_matches_python(db):
    url, _ = db
    ids = ["CSQU3054383"] + [container_id(o, s) for o in ("MSC", "ZZZ", "ABC") for s in (0, 1, 123456, 999999)]

    async def go():
        async with await connect(url) as conn:
            for i in ids:
                ok = (await (await conn.execute("SELECT iso6346_valid(%s) AS ok", (i,))).fetchone())["ok"]
                bad = i[:10] + str((int(i[10]) + 1) % 10)
                nok = (await (await conn.execute("SELECT iso6346_valid(%s) AS ok", (bad,))).fetchone())["ok"]
                assert (ok, nok) == (True, False), i
    run(go())


def test_rejects_bad_check_digit(db):
    url, vessel = db

    async def go():
        async with await connect(url) as conn:
            with rejects("iso6346_valid|containers_id_check"):
                await conn.execute(
                    """INSERT INTO containers (id, iso_type, weight_kg, vessel_id, vessel_bay, vessel_row, vessel_tier, vessel_half)
                       VALUES ('CSQU3054384', '22G1', 5000, %s, 1, 1, 11, 1)""", (vessel,))
    run(go())


def test_one_container_per_slot(db):
    url, vessel = db

    async def go():
        async with await connect(url) as conn:
            await discharge_to(conn, vessel, slot())
            with rejects("containers_one_per_slot"):
                await discharge_to(conn, vessel, slot())
    run(go())


def test_no_floating_boxes(db):
    url, vessel = db

    async def go():
        async with await connect(url) as conn:
            with rejects("stack_gravity"):
                await discharge_to(conn, vessel, slot(tier=2))
    run(go())


def test_cannot_pull_box_from_under_stack(db):
    url, vessel = db

    async def go():
        async with await connect(url) as conn:
            bottom = await discharge_to(conn, vessel, slot(tier=1))
            await discharge_to(conn, vessel, slot(tier=2))
            async with conn.transaction():
                await conn.execute("UPDATE containers SET state = 'RETRIEVING' WHERE id = %s", (bottom,))
            with rejects("stack_gravity"):
                async with conn.transaction():
                    await q.load_on_truck(conn, bottom)
    run(go())


def test_illegal_transition(db):
    url, vessel = db

    async def go():
        async with await connect(url) as conn:
            with rejects("illegal_transition"):
                await conn.execute(
                    """UPDATE containers SET state = 'STACKED_IN_YARD', vessel_bay = NULL, vessel_row = NULL,
                              vessel_tier = NULL, block_code = 'A', bay = 1, row = 1, tier = 1
                       WHERE id = (SELECT id FROM containers ORDER BY vessel_tier DESC LIMIT 1)""")
    run(go())


def test_hazmat_only_in_block_c(db):
    url, vessel = db

    async def go():
        async with await connect(url) as conn:
            await conn.execute("UPDATE containers SET hazmat = true, imdg_class = '9'")
            await conn.commit()
            with rejects("hazmat_segregation"):
                await discharge_to(conn, vessel, slot("A"))
            await conn.rollback()
            await discharge_to(conn, vessel, slot("C"))
    run(go())


def test_clearance_cte_lists_blockers_top_down(db):
    url, vessel = db

    async def go():
        async with await connect(url) as conn:
            boxes = [await discharge_to(conn, vessel, slot(tier=t)) for t in (1, 2, 3, 4)]
            rows = await q.clearance(conn, boxes[1])
            assert [r["id"] for r in rows] == [boxes[3], boxes[2], boxes[1]]
            assert [r["is_target"] for r in rows] == [False, False, True]
            assert {r["shifter_moves"] for r in rows} == {2}
            assert await q.clearance(conn, boxes[3]) == [
                {"id": boxes[3], "block_code": "A", "bay": 1, "row": 1, "tier": 4, "half": 0, "bay_code": "02",
                 "depth": 0, "is_target": True, "shifter_moves": 0}]
    run(go())


def test_allocation_builds_to_tier_3_and_avoids_own_stack(db, monkeypatch):
    url, vessel = db
    from portdb import config
    monkeypatch.setattr(config, "YARD_STRATEGY", "BASIC")

    async def go():
        async with await connect(url) as conn:
            await discharge_to(conn, vessel, slot())
            async with conn.transaction():
                assert await q.allocate_slot(conn, "A", True) == slot(tier=2)
                assert await q.allocate_slot(conn, "A", True, avoid=(1, 1)) == slot(row=2, tier=1)
                # a 20 may not join a 40 stack: it starts its own, fore half first
                assert await q.allocate_slot(conn, "A", False) == slot(row=2, tier=1, half=1)
                # SMART spreads the yard: ground slots first
                monkeypatch.setattr(config, "YARD_STRATEGY", "SMART")
                assert await q.allocate_slot(conn, "A", True) == slot(row=2, tier=1)
    run(go())


def test_concurrent_claims_never_collide(db):
    """Two QCs and two AGV claims at the same instant on separate connections."""
    url, vessel = db

    async def claim():
        async with await connect(url) as conn:
            async with conn.transaction():
                box = await q.claim_next_box(conn, vessel, 1, 6)
                agv = await q.claim_agv(conn, 0, 1)
                await asyncio.sleep(0.2)          # hold the row locks while the rival runs
                return box["id"], agv

    async def go():
        (b1, a1), (b2, a2) = await asyncio.gather(claim(), claim())
        assert b1 != b2 and a1 != a2
    run(go())


async def retrieve(conn, box: str) -> None:
    async with conn.transaction():
        await conn.execute("UPDATE containers SET state = 'RETRIEVING' WHERE id = %s", (box,))


def test_customs_hold_blocks_release_until_scanned(db):
    url, vessel = db

    async def go():
        async with await connect(url) as conn:
            box = await discharge_to(conn, vessel, slot())
            await conn.execute("UPDATE containers SET customs_hold = true WHERE id = %s", (box,))
            await conn.commit()
            await retrieve(conn, box)
            async with conn.transaction():
                await q.load_on_truck(conn, box)
            with rejects("containers_customs_release"):
                async with conn.transaction():
                    await q.gate_out(conn, box)
            async with conn.transaction():
                await q.mark_inspected(conn, box)
                await q.gate_out(conn, box)
    run(go())


def test_box_leaves_by_its_booked_mode(db):
    url, vessel = db

    async def go():
        async with await connect(url) as conn:
            box = await discharge_to(conn, vessel, slot())
            await conn.execute("UPDATE containers SET mode = 'RAIL' WHERE id = %s", (box,))
            await conn.commit()
            await retrieve(conn, box)
            with rejects("containers_exit_by_booked_mode"):
                async with conn.transaction():
                    await q.load_on_truck(conn, box)
    run(go())


def test_one_box_per_wagon_slot(db):
    url, vessel = db

    async def go():
        async with await connect(url) as conn:
            boxes = [await discharge_to(conn, vessel, slot(row=r)) for r in (1, 2)]
            await conn.execute("UPDATE containers SET mode = 'RAIL'")
            train = await q.arrive_train(conn, "RS-T", 8)
            await conn.commit()
            for i, box in enumerate(boxes):
                await retrieve(conn, box)
                async with conn.transaction():
                    await q.load_on_agv(conn, box, f"AGV-{i + 1}")
            async with conn.transaction():
                await q.load_on_wagon(conn, boxes[0], train, 1, 1)
            with rejects("containers_one_per_wagon_slot"):
                async with conn.transaction():
                    await q.load_on_wagon(conn, boxes[1], train, 1, 1)
            async with conn.transaction():
                assert await q.depart_train(conn, train) == [boxes[0]]
    run(go())


def test_rail_planner_picks_least_buried_box(db):
    url, vessel = db

    async def go():
        async with await connect(url) as conn:
            buried = [await discharge_to(conn, vessel, slot(tier=t)) for t in (1, 2, 3)]
            await conn.execute("UPDATE containers SET mode = 'RAIL' WHERE id = ANY(%s)", (buried[:2],))
            await conn.commit()
            async with conn.transaction():
                assert (await q.pick_rail_box(conn))["id"] == buried[1]      # one box above it, beats two
    run(go())


def test_shift_start_yard_obeys_every_rule(pg_url):
    """The pre-stocked yard goes through the same constraints: gravity, hazmat, one per slot."""
    async def go():
        async with await connect(pg_url) as conn:
            await reset(conn)
            n = await seed_yard(conn, random.Random(3))
            await conn.commit()
            rows = await (await conn.execute(
                "SELECT count(*) AS n, count(*) FILTER (WHERE hazmat AND block_code NOT IN ('C', 'F')) AS bad FROM containers")).fetchone()
            assert (rows["n"], rows["bad"]) == (n, 0)
    run(go())


# --- export, berths, collisions (migration 0003) -----------------------------------------------

async def export_in_yard(conn, rng, n, **over):
    """n export boxes on their own tier-1 slots in block D, committed."""
    boxes = [cargo(rng, "EXPORT") | {"state": "STACKED_IN_YARD", "block_code": "D", "bay": 1, "row": r, "tier": 1,
                                     "half": 0, "hazmat": False, "imdg_class": None} | over for r in range(1, n + 1)]
    await insert_boxes(conn, boxes)
    await conn.commit()
    return boxes


async def empty_vessel(conn, berth=2, bays=6):
    row = await (await conn.execute(
        "INSERT INTO vessels (name, imo, bays, rows, tiers, status, berth_id) "
        "VALUES ('Test Tern', '9074729', %s, 4, 10, 'BERTHED', %s) RETURNING id", (bays, berth))).fetchone()
    await conn.commit()
    return row["id"]


async def load(conn, box, vessel, tier):
    await retrieve(conn, box)
    async with conn.transaction():
        await q.load_on_agv(conn, box, "AGV-1")
    async with conn.transaction():
        await q.load_on_vessel(conn, box, vessel, 1, 1, tier, 0)
    async with conn.transaction():
        await q.stow(conn, box)


def test_stowage_refuses_heavy_on_light(db):
    url, _ = db

    async def go():
        async with await connect(url) as conn:
            light, heavy = await export_in_yard(conn, random.Random(1), 2, pod="NLRTM")
            await conn.execute("UPDATE containers SET weight_kg = 5000 WHERE id = %s", (light["id"],))
            await conn.execute("UPDATE containers SET weight_kg = 20000 WHERE id = %s", (heavy["id"],))
            vessel = await empty_vessel(conn)
            await load(conn, light["id"], vessel, 1)
            with rejects("stowage_weight"):
                await load(conn, heavy["id"], vessel, 2)
    run(go())


def test_stowage_refuses_later_port_on_earlier(db):
    url, _ = db

    async def go():
        async with await connect(url) as conn:
            first, later = await export_in_yard(conn, random.Random(2), 2, weight_kg=9000)
            await conn.execute("UPDATE containers SET pod = 'NLRTM' WHERE id = %s", (first["id"],))
            await conn.execute("UPDATE containers SET pod = 'PLGDN', weight_kg = 8000 WHERE id = %s", (later["id"],))
            await conn.commit()
            vessel = await empty_vessel(conn)
            await load(conn, first["id"], vessel, 1)
            with rejects("stowage_port"):
                await load(conn, later["id"], vessel, 2)
    run(go())


def test_imports_never_load_and_exports_never_truck_out(db):
    url, vessel = db

    async def go():
        async with await connect(url) as conn:
            box = await discharge_to(conn, vessel, slot())
            await retrieve(conn, box)
            async with conn.transaction():
                await q.load_on_agv(conn, box, "AGV-1")
            other = await empty_vessel(conn)
            with rejects("illegal_transition"):
                async with conn.transaction():
                    await q.load_on_vessel(conn, box, other, 1, 1, 1, 0)
            (export,) = await export_in_yard(conn, random.Random(3), 1)
            await retrieve(conn, export["id"])
            with rejects("illegal_transition"):
                async with conn.transaction():
                    await q.load_on_truck(conn, export["id"])
    run(go())


def test_import_and_export_stacks_are_segregated(db):
    url, _ = db

    async def go():
        async with await connect(url) as conn:
            with rejects("yard_segregation"):
                await insert_boxes(conn, [cargo(random.Random(4), "EXPORT") | {
                    "state": "STACKED_IN_YARD", "block_code": "A", "bay": 1, "row": 1, "tier": 1}])
    run(go())


def test_berths_take_one_fitting_vessel(db):
    url, _ = db

    async def go():
        async with await connect(url) as conn:
            with rejects("berth_fit"):
                await empty_vessel(conn, berth=2, bays=10)
            await conn.rollback()
            with rejects("vessels_one_per_berth"):
                await empty_vessel(conn, berth=1)                 # the fixture's vessel is at berth 1
    run(go())


def test_road_bookings_never_overlap_in_space_time(db):
    url, _ = db

    async def go():
        async with await connect(url) as conn:
            await conn.execute("INSERT INTO road_bookings (vehicle, x, z, t0, t1) VALUES ('AGV-1', 5, 2, 0, 2)")
            await conn.commit()
            # same cell, later: fine. neighbouring cell, same time: fine
            await conn.execute("INSERT INTO road_bookings (vehicle, x, z, t0, t1) VALUES ('AGV-2', 5, 2, 2.5, 3)")
            await conn.execute("INSERT INTO road_bookings (vehicle, x, z, t0, t1) VALUES ('AGV-3', 6, 2, 0, 2)")
            await conn.commit()
            with rejects("road_bookings_no_overlap"):
                await conn.execute("INSERT INTO road_bookings (vehicle, x, z, t0, t1) VALUES ('AGV-4', 5, 2, 1, 1.5)")
            await conn.rollback()
            legs = [(4, 2, 1.0, 1.6), (5, 2, 1.3, 1.9)]
            assert [(c["x"], c["z"]) for c in await q.route_clashes(conn, "AGV-4", legs)] == [(5, 2)]
            assert await q.book_route(conn, "AGV-4", legs, 1.0) is False      # the constraint, not the pre-check
            assert await q.book_route(conn, "AGV-4", [(4, 2, 3.0, 3.6), (5, 2, 3.3, 3.9)], 3.0) is True
    run(go())


def test_quay_cranes_cannot_pass_each_other(db):
    url, _ = db

    async def go():
        async with await connect(url) as conn:
            spans = {r["id"]: r["rail_span"] for r in await (await conn.execute(
                "SELECT id, rail_span FROM equipment WHERE kind = 'QC' ORDER BY id")).fetchall()}
            qc1, qc2 = float(spans["QC-1"].upper), float(spans["QC-2"].lower)
            assert await q.claim_rail(conn, "QC-1", qc1 - 1.8, qc2 + 0.5) == "equipment_qc_rail_no_crossing"
            assert await q.claim_rail(conn, "QC-1", qc1 - 1.8, qc2 - 0.1) is None    # right up to it is fine
    run(go())


# --- 20 ft / 40 ft (migration 0004) -------------------------------------------------------------

def yard_box(rng, forty, bay, row, tier, half):
    return cargo(rng, "EXPORT", hazmat_ok=False, forty=forty) | {
        "state": "STACKED_IN_YARD", "block_code": "D", "bay": bay, "row": row, "tier": tier, "half": half}


def test_iso_bay_codes(db):
    url, _ = db

    async def go():
        async with await connect(url) as conn:
            row = await (await conn.execute(
                "SELECT bay_code(1, 1) a, bay_code(1, 2) b, bay_code(1, 0) c, bay_code(2, 0) d, bay_code(2, 1) e")).fetchone()
            assert list(row.values()) == ["01", "03", "02", "06", "05"]
    run(go())


def test_two_twenties_share_a_slot_and_each_column_needs_support(db):
    url, _ = db
    rng = random.Random(11)

    async def go():
        async with await connect(url) as conn:
            await insert_boxes(conn, [yard_box(rng, False, 1, 1, 1, 1), yard_box(rng, False, 1, 1, 1, 2),
                                      yard_box(rng, False, 1, 1, 2, 1)])
            await conn.commit()                                       # fore + aft pair, then fore on fore
            with rejects("stack_gravity"):
                await insert_boxes(conn, [yard_box(rng, False, 1, 2, 2, 2)])   # aft column, nothing beneath
                await conn.commit()
    run(go())


def test_forty_never_on_twenty_and_stacks_hold_one_length(db):
    url, _ = db
    rng = random.Random(12)

    async def go():
        async with await connect(url) as conn:
            await insert_boxes(conn, [yard_box(rng, False, 1, 1, 1, 1), yard_box(rng, False, 1, 1, 1, 2)])
            await conn.commit()
            with rejects("stack_mixed_lengths"):
                await insert_boxes(conn, [yard_box(rng, True, 1, 1, 2, 0)])
                await conn.commit()
            await conn.rollback()
            with rejects("containers_half_matches_length"):
                await insert_boxes(conn, [yard_box(rng, True, 1, 2, 1, 1)])   # a 40 cannot claim a 20 ft half
    run(go())


def test_sixty_foot_wagon_positions(db):
    url, _ = db
    rng = random.Random(13)

    async def go():
        async with await connect(url) as conn:
            train = await q.arrive_train(conn, "RS-T", 8)
            await conn.commit()
            rail = lambda forty, w, p: cargo(rng, "EXPORT", forty=forty) | {  # noqa: E731
                "state": "ON_RAIL", "train_id": train, "wagon": w, "wagon_slot": p}
            await insert_boxes(conn, [rail(True, 1, 1), rail(False, 1, 3)])      # 40 + 20 fills a 60 ft wagon
            await conn.commit()
            with rejects("wagon_overlap"):
                await insert_boxes(conn, [rail(False, 2, 2), rail(True, 2, 1)])
                await conn.commit()
            await conn.rollback()
            with rejects("containers_wagon_position"):
                await insert_boxes(conn, [rail(True, 3, 3)])                   # a 40 would hang off the end
    run(go())
