"""World geometry shared by the engine and (via GET /api/layout) the diorama.

Grid units: x runs along the quay, z runs inland (water is z < 0), y is up.

    z  1     dead-end spurs: QC hand-over under each ship bay, and AGV parks between berths
    z  2,3   quay lanes (AGV through traffic, under the QC portals)
    z  4     dead-end spurs: RTG hand-over in front of every yard bay
    z  5..8  yard rows (RTG legs at z 3.6 and 10.6)
    z 10     lorry spots under the RTGs (dead ends off the through lane)
    z 11     lorry through lane: queue + in-gate west, customs portal + out-gate east
    z 13     rail lane (AGV through traffic, under the RMGs)
    z 14     dead-end spurs: RMG hand-over under each wagon slot
    z 15     rail siding

AGVs and lorries only ever wait on a dead-end spur (or the in-gate booth), never on a through lane,
so a vehicle holding an open-ended booking can delay others but never deadlock them.
"""

from collections import deque

from . import config

BOX_H = 0.5
SHIP_DECK = 0.3
BERTH_X0 = {1: 4, 2: 17, 3: 30}                  # ship bay b sits at x = x0 + b
BLOCK_X0 = {"A": 2, "B": 11, "C": 20, "D": 29, "E": 38, "F": 47}
BLOCK_Z0 = 4                                     # yard row r sits at z = 4 + r
QC_SPUR_Z, BLOCK_SPUR_Z = 1, 4
LANE_ZS = (2, 3)
LOAD_Z, THRU_Z = 10, 11
RAIL_LANE_Z, RAIL_SPUR_Z, RAIL_Z = 13, 14, 15
RAIL_X0 = 7                                      # stopped train: wagon w centred at x = RAIL_X0 + 2(w-1) + 1
TEU = 0.5                                        # a 20 ft box is half a 40 ft position
TRAIN_FROM_X, TRAIN_TO_X = -70, 110
LORRY_SPAWN_X, QUEUE_HEAD_X, GATE_IN_X = -34, -9, -7
SCAN_ROAD_X, GATE_OUT_X, LORRY_EXIT_X = 58, 64, 86
SCAN_RAIL = (28, 7)                              # drive-through portal on the AGV road between C and D
CUSTOMS = (61.5, 6.5)                            # inspection shed footprint centre
VERTICALS = (1, 10, 19, 28, 37, 46, 55)
# quay-side parks between the berths, then yard-side waiting bays beside the cross roads (z = 12)
PARKS = [(x, QC_SPUR_Z) for x in (0, 1, 2, 13, 14, 15, 26, 27, 28, 44, 45, 46)]     + [(x + dx, 12) for x in VERTICALS[1:-1] for dx in (-1, 1)]

RTG_LEGS_Z = (3.6, 10.6)
RMG_LEGS_Z = (12.4, 15.8)
QC_LEGS_Z = (0.2, 2.7)
QC_HALF_WIDTH = 0.9                              # rail envelope half-width (frame + bogies)
RMG_HALF_WIDTH = 0.8
RTG_CLEAR_Y = config.BLOCK_TIERS * BOX_H + 0.6
QC_CLEAR_Y = SHIP_DECK + 10 * BOX_H + 0.8
RMG_CLEAR_Y = 2.2
AGV_BED_Y, LORRY_BED_Y, WAGON_BED_Y = 0.35, 0.42, 0.5
VESSEL_SAIL_FROM_X, VESSEL_SAIL_TO_X = -60, 90

Node = tuple[int, int]
Point = tuple[float, float, float]


def half_dx(half: int) -> float:
    """A 40 ft position holds one 40 (half 0) or two 20s, fore (1) and aft (2)."""
    return (0.0, -TEU / 2, TEU / 2)[half]


def ship_cell(berth: int, bay: int, row: int, tier: int, half: int = 0) -> Point:
    return (BERTH_X0[berth] + bay + half_dx(half), SHIP_DECK + (tier - 1) * BOX_H, -row - 0.5)


def slot(block: str, bay: int, row: int, tier: int, half: int = 0) -> Point:
    return (BLOCK_X0[block] + bay + half_dx(half), (tier - 1) * BOX_H, BLOCK_Z0 + row)


def wagon_x(wagon: int) -> int:
    return RAIL_X0 + 2 * (wagon - 1) + 1


def wagon_slot_point(wagon: int, wagon_slot: int, forty: bool) -> Point:
    """Positions 1..3 are 20 ft each along the wagon; a 40 centres on its two."""
    return (wagon_x(wagon) + (wagon_slot - (1.5 if forty else 2)) * TEU, WAGON_BED_Y, RAIL_Z)


def qc_spur(berth: int, bay: int) -> Node:
    return (BERTH_X0[berth] + bay, QC_SPUR_Z)


def block_spurs(block: str) -> list[Node]:
    return [(BLOCK_X0[block] + b, BLOCK_SPUR_Z) for b in range(1, config.BLOCK_BAYS + 1)]


def rail_spur(wagon: int) -> Node:
    return (wagon_x(wagon), RAIL_SPUR_Z)


def lorry_spot(block: str, bay: int) -> Node:
    return (BLOCK_X0[block] + bay, LOAD_Z)


def point(n: Node, y: float = 0) -> Point:
    return (n[0], y, n[1])


def lifted(p: Point, y: float) -> Point:
    return (p[0], y, p[2])


# --- AGV road graph: explicit edges, so spurs are dead ends ---------------------------------
QC_SPURS = {qc_spur(b, bay) for b, (_, bays, *_rest) in config.BERTHS.items() for bay in range(1, bays + 1)}
BLOCK_SPURS = {n for b in BLOCK_X0 for n in block_spurs(b)}
RAIL_SPURS = {rail_spur(w) for w in range(1, config.TRAIN_WAGONS + 1)}
SPURS = QC_SPURS | BLOCK_SPURS | RAIL_SPURS | set(PARKS)

EDGES: dict[Node, set[Node]] = {}


def _link(a: Node, b: Node) -> None:
    EDGES.setdefault(a, set()).add(b)
    EDGES.setdefault(b, set()).add(a)


for x in range(-1, 58):
    _link((x, 2), (x, 3))
    for z in LANE_ZS:
        _link((x, z), (x + 1, z))
for x in range(1, 55):
    _link((x, RAIL_LANE_Z), (x + 1, RAIL_LANE_Z))
for x in VERTICALS:
    for z in range(3, RAIL_LANE_Z):
        _link((x, z), (x, z + 1))
for sx, sz in QC_SPURS | {p for p in PARKS if p[1] == QC_SPUR_Z}:
    _link((sx, sz), (sx, 2))
for sx, sz in (p for p in PARKS if p[1] == 12):
    _link((sx, sz), (min(VERTICALS, key=lambda v: abs(v - sx)), 12))
for sx, sz in BLOCK_SPURS:
    _link((sx, sz), (sx, 3))
for sx, sz in RAIL_SPURS:
    _link((sx, sz), (sx, RAIL_LANE_Z))
ROADS = set(EDGES)


def road_path(start: Node, goal: Node, avoid: frozenset[Node] = frozenset()) -> list[Node] | None:
    """Shortest path; `avoid` nodes are skipped (but never the start or the goal)."""
    prev = {start: start}
    todo = deque([start])
    while todo:
        n = todo.popleft()
        if n == goal:
            path = [goal]
            while path[-1] != start:
                path.append(prev[path[-1]])
            return path[::-1]
        for m in EDGES[n]:
            if m not in prev and (m == goal or m not in avoid) and (m == goal or m not in SPURS):
                prev[m] = n
                todo.append(m)
    return None


# --- lorry lanes: fixed polylines, expanded into unit cells for booking --------------------
def cells_along(points: list[Node]) -> list[Node]:
    """Axis-aligned waypoints -> every unit cell visited, in order (first point included)."""
    out = [points[0]]
    for (x0, z0), (x1, z1) in zip(points, points[1:]):
        dx, dz = (x1 > x0) - (x1 < x0), (z1 > z0) - (z1 < z0)
        x, z = x0, z0
        while (x, z) != (x1, z1):
            x, z = x + dx, z + dz
            out.append((x, z))
    return out


def units() -> list[dict]:
    """Every crane, AGV and scanner with the spot it is drawn at before its first move."""
    qcs = [{"id": f"QC-{i}", "kind": "QC", "home": (x, QC_CLEAR_Y, -2.0)} for i, x in enumerate(qc_homes(), start=1)]
    rtgs = [{"id": f"RTG-{b}", "kind": "RTG",
             "home": (x0 + (config.BLOCK_BAYS + 1) / 2, RTG_CLEAR_Y, BLOCK_Z0 + (config.BLOCK_ROWS + 1) / 2)}
            for b, x0 in BLOCK_X0.items()]
    agvs = [{"id": f"AGV-{i}", "kind": "AGV", "home": point(PARKS[i - 1])} for i in range(1, config.AGV_COUNT + 1)]
    rmgs = [{"id": f"RMG-{i}", "kind": "RMG", "home": (x, RMG_CLEAR_Y, RAIL_Z - 1)} for i, x in enumerate(rmg_homes(), start=1)]
    return qcs + rtgs + agvs + rmgs


def qc_homes() -> list[float]:
    """Two cranes per berth, spread over its bays, west to east."""
    out = []
    for b, (_, bays, *_rest) in config.BERTHS.items():
        out += [BERTH_X0[b] + bays * 0.25 + 0.5, BERTH_X0[b] + bays * 0.75 + 0.5]
    return out


def rmg_wagons() -> dict[str, list[int]]:
    half = config.TRAIN_WAGONS // 2
    return {"RMG-1": list(range(1, half + 1)), "RMG-2": list(range(half + 1, config.TRAIN_WAGONS + 1))}


def rmg_homes() -> list[float]:
    return [wagon_x(ws[len(ws) // 2]) for ws in rmg_wagons().values()]


def payload() -> dict:
    return {
        "box_h": BOX_H, "ship_deck": SHIP_DECK, "teu": TEU,
        "day": {"length_s": config.DAY_S, "start_h": config.DAY_START_H},
        "agv_bed_y": AGV_BED_Y, "lorry_bed_y": LORRY_BED_Y, "wagon_bed_y": WAGON_BED_Y,
        "berths": [{"id": b, "name": name, "x0": BERTH_X0[b], "bays": bays, "rows": rows, "tiers": tiers}
                   for b, (name, bays, rows, tiers, _) in config.BERTHS.items()],
        "sail": {"from_x": VESSEL_SAIL_FROM_X, "to_x": VESSEL_SAIL_TO_X},
        "blocks": [{"code": b, "x0": BLOCK_X0[b], "z0": BLOCK_Z0, "bays": config.BLOCK_BAYS,
                    "rows": config.BLOCK_ROWS, "tiers": config.BLOCK_TIERS, "purpose": purpose, "hazmat": hz}
                   for b, (purpose, hz) in config.BLOCKS.items()],
        "roads": sorted(n for n in ROADS if n not in SPURS),
        "spurs": sorted(SPURS - set(PARKS)),
        "parks": PARKS,
        "lanes": {"load_z": LOAD_Z, "thru_z": THRU_Z, "spawn_x": LORRY_SPAWN_X, "gate_in_x": GATE_IN_X,
                  "gate_out_x": GATE_OUT_X, "exit_x": LORRY_EXIT_X},
        "rail": {"lane_z": RAIL_LANE_Z, "spur_z": RAIL_SPUR_Z, "z": RAIL_Z, "x0": RAIL_X0,
                 "wagons": config.TRAIN_WAGONS, "from_x": TRAIN_FROM_X, "to_x": TRAIN_TO_X},
        "scanners": [{"id": "SCN-ROAD", "pos": (SCAN_ROAD_X, 0, THRU_Z), "across": "z"},
                     {"id": "SCN-RAIL", "pos": point(SCAN_RAIL), "across": "x"}],
        "customs": CUSTOMS,
        "legs": {"qc": QC_LEGS_Z, "rtg": RTG_LEGS_Z, "rmg": RMG_LEGS_Z},
        "rtg_clear_y": RTG_CLEAR_Y, "qc_clear_y": QC_CLEAR_Y, "rmg_clear_y": RMG_CLEAR_Y,
        "units": units(),
    }


if __name__ == "__main__":
    # every operational stop must be reachable from every park, and back, without crossing a spur
    stops = sorted(QC_SPURS | BLOCK_SPURS | RAIL_SPURS) + [SCAN_RAIL]
    for p in PARKS:
        for s in stops:
            for a, b in ((p, s), (s, p)):
                path = road_path(a, b)
                assert path and path[0] == a and path[-1] == b, (a, b)
                assert not any(n in SPURS for n in path[1:-1]), (a, b, path)
                assert all(m in EDGES[n] for n, m in zip(path, path[1:]))
    assert len(PARKS) >= config.AGV_COUNT
    assert not set(VERTICALS) & {x for b in BLOCK_X0 for x, _ in block_spurs(b)}, "vertical road through a yard bay"
    assert RTG_LEGS_Z[0] < BLOCK_SPUR_Z and LOAD_Z < RTG_LEGS_Z[1] < THRU_Z
    assert RMG_LEGS_Z[0] < RAIL_LANE_Z and RAIL_Z < RMG_LEGS_Z[1]
    longest = max(len(road_path(p, s)) for p in PARKS for s in stops)
    print(f"layout ok: {len(ROADS)} road nodes, {len(SPURS)} spurs, longest park->stop trip {longest} nodes")
