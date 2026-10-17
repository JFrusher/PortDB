"""Terminal bootstrap and cargo generators: vessel manifests, export drop-offs, inbound rail.

    python -m portdb.seed_vessel --reset    # wipe operational data, rebuild grid + fleet, stock the yard
"""

import argparse
import asyncio
import itertools
import random
from decimal import Decimal

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.range import Range

from . import config, layout
from .db import DATABASE_URL

OWNERS = ["MSC", "MAE", "CMA", "HLX", "ONE", "EGL", "COS", "OOL"]
SHIP_NAMES = ["Little Puffin", "Mersey Wren", "Solent Otter", "Humber Gull", "Tamar Heron", "Clyde Kittiwake",
              "Severn Tern", "Medway Plover", "Forth Gannet", "Tyne Curlew"]
PORT_RANK = {code: rotation for code, _, rotation in config.PORTS}
_serials = itertools.count(1)      # unique per process; every boot truncates the containers table


def iso6346_check_digit(prefix10: str) -> int:
    """ISO 6346: letters A=10..Z=38 skipping multiples of 11, position i weighted 2^i, sum mod 11 mod 10.
    Mirrors iso6346_valid() in the migration."""
    total = 0
    for i, ch in enumerate(prefix10):
        n = int(ch) if ch.isdigit() else (ord(ch) - 55) + (ord(ch) - 56) // 10
        total += n << i
    return total % 11 % 10


def container_id(owner: str, serial: int) -> str:
    prefix = f"{owner}U{serial:06d}"
    return prefix + str(iso6346_check_digit(prefix))


def new_id(rng: random.Random) -> str:
    # 7919 is coprime to 10^6, so the serials stay unique for the first million boxes
    return container_id(rng.choice(OWNERS), next(_serials) * 7919 % 1_000_000)


def imo_number(rng: random.Random) -> str:
    digits = [rng.randint(1, 9)] + [rng.randint(0, 9) for _ in range(5)]
    check = sum(d * (7 - i) for i, d in enumerate(digits)) % 10
    return "".join(map(str, digits)) + str(check)


def rail_span(x: float, half: float) -> Range:
    return Range(Decimal(str(round(x - half, 3))), Decimal(str(round(x + half, 3))))


def cargo(rng: random.Random, flow: str, hazmat_ok: bool = True) -> dict:
    """One box's commercial details. Imports carry an onward mode; exports and tranships a port of discharge."""
    forty = rng.random() < config.SHARE_40FT
    reefer = rng.random() < config.SHARE_REEFER
    box = {
        "id": new_id(rng),
        "iso_type": ("45" if forty else "22") + ("R1" if reefer else "G1"),
        "weight_kg": int(rng.triangular(3800, 30480, 14000) if forty else rng.triangular(2300, 24000, 11000)),
        "hazmat": hazmat_ok and rng.random() < config.SHARE_HAZMAT,
        "flow": flow,
    }
    if flow == "IMPORT":
        box |= {"mode": "RAIL" if rng.random() < config.SHARE_RAIL else "TRUCK",
                "customs_hold": rng.random() < config.SHARE_CUSTOMS_HOLD, "pod": None}
    else:
        box |= {"mode": "VESSEL", "customs_hold": False, "pod": rng.choice(config.PORTS)[0]}
    return box


COLS = ["id", "iso_type", "weight_kg", "hazmat", "flow", "mode", "customs_hold", "pod", "state",
        "vessel_id", "vessel_bay", "vessel_row", "vessel_tier", "block_code", "bay", "row", "tier",
        "train_id", "wagon", "wagon_slot"]


async def insert_boxes(conn: AsyncConnection, boxes: list[dict]) -> None:
    async with conn.cursor() as cur:
        await cur.executemany(
            f"INSERT INTO containers ({', '.join(COLS)}) VALUES ({', '.join(f'%({c})s' for c in COLS)})",
            [{c: b.get(c) for c in COLS} for b in boxes],
        )


def manifest(vessel_id: int, berth: int, rng: random.Random) -> list[dict]:
    """Imports + a share of tranships, stowed bottom-up, heaviest lowest; tranship ports latest-lowest."""
    _, bays, rows, tiers, load = config.BERTHS[berth]
    stacks = [(b, r) for b in range(1, bays + 1) for r in range(1, rows + 1)]
    heights = dict.fromkeys(stacks, 0)
    for _ in range(load):
        heights[rng.choice([s for s in stacks if heights[s] < tiers])] += 1

    boxes = []
    for (bay, row), height in heights.items():
        column = sorted((cargo(rng, "TRANSHIP" if rng.random() < config.SHARE_TRANSHIP else "IMPORT")
                         for _ in range(height)), key=lambda c: -c["weight_kg"])
        # stowage rule: a later port never sits on an earlier one, so hand the ports out latest-first
        pods = sorted((c["pod"] for c in column if c["pod"]), key=lambda p: -PORT_RANK[p])
        for tier, box in enumerate(column, start=1):
            if box["pod"]:
                box["pod"] = pods.pop(0)
            box |= {"state": "STOWED_ON_BOARD", "vessel_id": vessel_id,
                    "vessel_bay": bay, "vessel_row": row, "vessel_tier": tier}
        boxes += column
    return boxes


async def reset(conn: AsyncConnection) -> None:
    await conn.execute(
        "TRUNCATE road_bookings, gate_transactions, equipment_tasks, containers, trains, vessels, equipment, "
        "yard_slots, yard_blocks, berths, ports, terminals RESTART IDENTITY"
    )
    await conn.execute("INSERT INTO terminals (code, name) VALUES (%s, %s)",
                       (config.TERMINAL_CODE, config.TERMINAL_NAME))
    async with conn.cursor() as cur:
        await cur.executemany("INSERT INTO ports (code, name, rotation) VALUES (%s, %s, %s)", config.PORTS)
        await cur.executemany(
            "INSERT INTO berths (id, name, x0, max_bays, max_rows) VALUES (%s, %s, %s, %s, %s)",
            [(b, name, layout.BERTH_X0[b], bays, rows) for b, (name, bays, rows, _, _) in config.BERTHS.items()],
        )
        await cur.executemany(
            "INSERT INTO yard_blocks (code, terminal_id, bays, rows, tiers, allows_hazmat, purpose) "
            "VALUES (%s, 1, %s, %s, %s, %s, %s)",
            [(code, config.BLOCK_BAYS, config.BLOCK_ROWS, config.BLOCK_TIERS, hz, purpose)
             for code, (purpose, hz) in config.BLOCKS.items()],
        )
    await conn.execute(
        """INSERT INTO yard_slots (block_code, bay, row, tier)
           SELECT b.code, bay, row, tier FROM yard_blocks b,
                  generate_series(1, b.bays) bay, generate_series(1, b.rows) row, generate_series(1, b.tiers) tier"""
    )
    units = [(f"QC-{i}", "QC", None, None, None, rail_span(x, layout.QC_HALF_WIDTH))
             for i, x in enumerate(layout.qc_homes(), start=1)]
    units += [(f"AGV-{i}", "AGV", None, *layout.PARKS[i - 1], None) for i in range(1, config.AGV_COUNT + 1)]
    units += [(f"RTG-{code}", "RTG", code, None, None, None) for code in config.BLOCKS]
    units += [(f"RMG-{i}", "RMG", None, None, None, rail_span(x, layout.RMG_HALF_WIDTH))
              for i, x in enumerate(layout.rmg_homes(), start=1)]
    units += [(s, "SCANNER", None, None, None, None) for s in ("SCN-ROAD", "SCN-RAIL")]
    async with conn.cursor() as cur:
        await cur.executemany(
            "INSERT INTO equipment (id, kind, home_block, pos_x, pos_z, rail_span) VALUES (%s, %s, %s, %s, %s, %s)",
            units,
        )


async def seed_yard(conn: AsyncConnection, rng: random.Random) -> int:
    """Yard inventory at shift start, stacks built to tier 3 as the allocator would have left them."""
    boxes = []
    for block, (purpose, allows_hazmat) in config.BLOCKS.items():
        stacks = [(bay, row) for bay in range(1, config.BLOCK_BAYS + 1) for row in range(1, config.BLOCK_ROWS + 1)]
        for n in range(config.YARD_PRESTOCK[purpose]):
            bay, row = stacks[n // 3]
            box = cargo(rng, purpose, hazmat_ok=allows_hazmat)
            boxes.append(box | {"state": "STACKED_IN_YARD", "block_code": block, "bay": bay, "row": row,
                                "tier": n % 3 + 1})
    await insert_boxes(conn, boxes)
    return len(boxes)


async def seed_vessel(conn: AsyncConnection, rng: random.Random, berth: int = 1) -> int:
    """Insert a vessel at `berth` with its full manifest. Returns the vessel id."""
    _, bays, rows, tiers, _ = config.BERTHS[berth]
    n = (await (await conn.execute("SELECT count(*) AS n FROM vessels")).fetchone())["n"]
    vessel = await (await conn.execute(
        """INSERT INTO vessels (name, imo, bays, rows, tiers, status, berthed_at, berth_id)
           VALUES (%s, %s, %s, %s, %s, 'BERTHED', clock_timestamp(), %s) RETURNING id""",
        (SHIP_NAMES[n % len(SHIP_NAMES)], imo_number(rng), bays, rows, tiers, berth),
    )).fetchone()
    await insert_boxes(conn, manifest(vessel["id"], berth, rng))
    return vessel["id"]


async def new_export_on_truck(conn: AsyncConnection, rng: random.Random, plate: str) -> dict:
    """A lorry arriving with an export box: the box exists from the moment it reaches the gate."""
    box = cargo(rng, "EXPORT") | {"state": "ON_TRUCK"}
    await insert_boxes(conn, [box])
    gate = await (await conn.execute(
        "INSERT INTO gate_transactions (plate, container_id) VALUES (%s, %s) RETURNING id", (plate, box["id"]))).fetchone()
    return box | {"gate_id": gate["id"]}


async def new_inbound_rail(conn: AsyncConnection, rng: random.Random, train: int) -> list[dict]:
    """Export boxes arriving on a train: first slots of every wagon, then second slots."""
    slots = [(w, s) for s in (1, 2) for w in range(1, config.TRAIN_WAGONS + 1)][:config.TRAIN_INBOUND]
    boxes = [cargo(rng, "EXPORT") | {"state": "ON_RAIL", "train_id": train, "wagon": w, "wagon_slot": s}
             for w, s in slots]
    await insert_boxes(conn, boxes)
    return boxes


async def main(url: str, do_reset: bool, seed: int | None) -> None:
    rng = random.Random(seed)
    async with await AsyncConnection.connect(url, row_factory=dict_row) as conn:
        if do_reset:
            await reset(conn)
            await seed_yard(conn, rng)
        vessel = await seed_vessel(conn, rng)
    print(f"vessel {vessel} berthed")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--reset", action="store_true")
    p.add_argument("--seed", type=int)
    a = p.parse_args()
    asyncio.run(main(DATABASE_URL, a.reset, a.seed), loop_factory=asyncio.SelectorEventLoop)
