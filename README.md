# PortDB: a container terminal operating system on PostgreSQL

PortDB runs a three-berth container terminal end to end, in both directions:

- **Import:** ships sail in and quay cranes discharge them onto AGVs, which carry the boxes to the import yard. From there a box leaves by road (lorry through the in-gate and out-gate) or by rail (AGV to the rail terminal, rail-mounted gantry to a wagon).
- **Export:** lorries drop export boxes at the export yard, and inbound trains are unloaded there too. When a ship has finished discharging, its cranes load exports, and the database will not let a heavier box sit on a lighter one, or a later port of discharge sit on an earlier one.
- **Transhipment:** a box discharged from one ship waits in the export yard for another.

Boxes under a customs hold must pass a drive-through X-ray portal before they leave.

Time and queueing are modelled with a SimPy discrete-event simulation. **Every physical move is a PostgreSQL transaction**, and the schema, not the Python, decides whether a move is legal. That includes **where every vehicle may be at every moment**: AGVs and lorries book their routes as rectangles in (road cell, time), and an exclusion constraint guarantees no two ever overlap. Cranes on a shared rail hold disjoint `numrange` envelopes, so they cannot pass each other. The isometric diorama in the browser is only a view of that database.

```
import    ship ─QC─▶ AGV ─RTG─▶ import yard ─┬─RTG─▶ lorry ─(X-ray if held)─▶ out-gate
                                              └─RTG─▶ AGV ─(X-ray if held)─RMG─▶ wagon ─▶ train
export    lorry ─RTG─▶ export yard           train ─RMG─▶ AGV ─RTG─▶ export yard
          export yard ─RTG─▶ AGV ─▶ staging park ─▶ QC ─▶ ship ─▶ sails
tranship  ship ─QC─▶ AGV ─RTG─▶ export yard ─▶ … onto a later ship
```

![Diorama: three berths, six yard blocks, rail terminal, gates and customs](docs/diorama.png)

```bash
docker compose up --build        # → http://localhost:8080      (Postgres is on :5432, user/pass port/port)
```

---

## 1. Architecture

```
browser ── Vite + React Three Fiber ──── WS /ws: motion tracks + 0.5 s stats ─┐
                                          REST /api: layout, snapshot, clearance, sim controls, debug
api ─────── FastAPI ── one asyncio loop ── SimPy Environment, stepped every 20 ms
                            │
                            │ psycopg 3 async pool: up to 16 transactions in flight at once
db ──────── PostgreSQL 16 ─ ENUMs · composite keys · CHECKs · deferred constraint triggers ·
                            gist exclusion constraints · partial indexes
```

SimPy processes are synchronous generators, and the driver is async. A process that needs the database yields `self.db(fn, ...)`. That call starts `fn` as an asyncio task in **its own pooled transaction** and resumes the process when the transaction commits. If PostgreSQL raises, the exception is thrown back into the process. Deadlock victims are retried, which is safe because each step is a single transaction.

6 QCs, 16 AGVs, 6 RTGs, 2 RMGs, 2 scanners and up to 10 lorries have transactions open at the same time on separate connections, so the locking below is real contention. See [`sim_engine.py`](backend/portdb/sim_engine.py).

## 2. Relational model

```mermaid
erDiagram
    terminals ||--o{ yard_blocks : has
    yard_blocks ||--o{ yard_slots : "bay × row × tier"
    yard_slots |o--o| containers : "holds (UNIQUE)"
    berths ||--o| vessels : "one live vessel"
    vessels ||--o{ containers : "cells (UNIQUE)"
    trains ||--o{ containers : "wagon slots (UNIQUE)"
    ports ||--o{ containers : "port of discharge"
    equipment |o--o{ containers : "carries (AGV)"
    equipment ||--o{ equipment_tasks : performs
    containers ||--o{ equipment_tasks : "moved by"
    containers ||--o{ gate_transactions : "via gate"

    containers {
        char11 id PK "ISO 6346, check digit verified"
        container_flow flow "IMPORT | EXPORT | TRANSHIP"
        container_state state "ENUM, per-flow transitions"
        outbound_mode mode "TRUCK | RAIL | VESSEL"
        char5 pod FK "port of discharge"
        char block_code FK
        int vessel_id FK
        int train_id FK
        text carrier_id FK
    }
    road_bookings {
        bigserial id PK
        text vehicle
        smallint x
        smallint z
        float8 t0 "sim time"
        float8 t1 "1e9 = parked"
    }
    equipment {
        text id PK
        equipment_kind kind
        numrange rail_span "QC / RMG envelope"
    }
```

The DDL is in three hand-written migrations:
- [`0001_schema.py`](backend/alembic/versions/0001_schema.py): the yard core.
- [`0002_landside.py`](backend/alembic/versions/0002_landside.py): trains and customs.
- [`0003_export_berths_bookings.py`](backend/alembic/versions/0003_export_berths_bookings.py): export, berths, stowage and collisions.

New ENUM labels are added in an autocommit block, because a label cannot be used in the transaction that created it. The SQLAlchemy models in [`models.py`](backend/portdb/models.py) mirror the schema, and a test fails if the two drift apart.

### What the database refuses

| Rule | Mechanism |
|---|---|
| One box per yard slot / ship cell / wagon slot | Composite FKs + `UNIQUE` constraints, coordinates all-or-nothing via `num_nulls()` |
| Legal lifecycle, per direction | `BEFORE UPDATE` trigger looks up `container_state_transitions (from, to, flows[])`. An import can never be `LOADING` onto a ship, and an export never goes `RETRIEVING → ON_TRUCK` |
| Leaves by its booked mode | `containers_exit_by_booked_mode`: `GATE_OUT` needs `TRUCK`, `RAIL_OUT` needs `RAIL`, `LOADING`/`SAILED` need `VESSEL` |
| No floating boxes, no pulling from under a stack (yard **and** ship) | Deferred constraint trigger: every touched stack must satisfy `count(*) = max(tier)`, judged **at commit** |
| **Stowage**: heavy under light, later port under earlier | Deferred constraint trigger `trg_stowage` compares the touched cell with the cells directly below and above |
| Import/export yard segregation, IMDG blocks | `trg_yard_segregation`: imports only in A–C, exports and tranships only in D–F, hazmat only in C or F |
| One ship per berth, and it must fit | Partial unique index `vessels (berth_id) WHERE status <> 'DEPARTED'` + `trg_berth_fit` |
| Customs release | `CHECK (NOT customs_hold OR inspected_at IS NOT NULL OR state NOT IN ('GATE_OUT','ON_RAIL','RAIL_OUT'))` |
| **No two vehicles in one place at one time** | `road_bookings_no_overlap EXCLUDE USING gist (box(point(cell, t0), point(cell + 0.5, t1)) WITH &&)` (section 4) |
| **Cranes cannot pass each other** | `EXCLUDE USING gist (rail_span WITH &&) WHERE (kind = 'QC')`, and the same for `RMG` |
| Valid container numbers | `CHECK (iso6346_valid(id))`: ISO 6346 check digit in PL/pgSQL |

## 3. Stack clearance: the recursive CTE

A lorry or an AGV asks for a box. The RTG needs the boxes above it, in order. The query anchors on the target and climbs exactly one tier per recursive step. The gravity rule guarantees stacks have no gaps, so the recursion ends at the first empty tier.

```sql
WITH RECURSIVE stack AS (
    SELECT c.id, c.block_code, c.bay, c.row, c.tier, 0 AS depth
    FROM containers c
    WHERE c.id = %(target)s AND c.state IN ('STACKED_IN_YARD', 'RETRIEVING')
  UNION ALL
    SELECT above.id, above.block_code, above.bay, above.row, above.tier, s.depth + 1
    FROM stack s
    JOIN containers above
      ON (above.block_code, above.bay, above.row, above.tier)
       = (s.block_code, s.bay, s.row, s.tier + 1)
)
SELECT id, block_code, bay, row, tier, depth, depth = 0 AS is_target,
       count(*) FILTER (WHERE depth > 0) OVER () AS shifter_moves
FROM stack
ORDER BY tier DESC;
```

The rows come back top-down: that is the RTG's work order. Click any stacked box, or type its number into the inspector, to run it live with its `EXPLAIN (ANALYZE, BUFFERS)` plan.

## 4. Collision control in the database

**Roads.** A booking is a rectangle in (road cell, sim time). Two rectangles may not overlap:

```sql
CREATE FUNCTION cell_key(x integer, z integer) RETURNS double precision
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$ SELECT ((x + 128) * 64 + z)::double precision $$;

CREATE TABLE road_bookings (
  id bigserial PRIMARY KEY, vehicle text NOT NULL,
  x smallint NOT NULL, z smallint NOT NULL, t0 double precision NOT NULL, t1 double precision NOT NULL,
  CHECK (t1 > t0),
  CONSTRAINT road_bookings_no_overlap
    EXCLUDE USING gist (box(point(cell_key(x, z), t0), point(cell_key(x, z) + 0.5, t1)) WITH &&)
);
```

It uses native gist on `box`, so it needs no `btree_gist` and runs on stock Postgres. Before an AGV moves, `travel()` does three things:
1. Runs a BFS over the road graph and times the route into legs, `[t_enter − margin, t_leave + margin]` per cell, with an open-ended booking (`t1 = 1e9`) on the destination.
2. Asks `route_clashes` (an index-backed `&&` join) whether anyone else holds those rectangles. On a clash it re-routes around the clashing cells, otherwise it departs later.
3. Calls `book_route`, which truncates the vehicle's own bookings at departure and inserts the new legs.

The pre-check is an optimisation; the constraint is the guarantee, and a lost race simply re-plans.

Two details keep the whole thing deadlock-free:
- **Spurs.** Every place a vehicle may *wait* (QC, RTG and RMG hand-overs, parks, lorry spots, the in-gate booth) is a dead-end spur. Lanes only ever carry finite bookings, so a parked vehicle can delay others but never trap them.
- **Cell-ordered inserts.** An exclusion check waits for a conflicting *uncommitted* row. If two bookers inserted overlapping cells in opposite orders they would deadlock; `INSERT … SELECT unnest(…) ORDER BY cell_key(x, z)` gives every booker the same order, so no cycle can form.

Lorries book fixed polylines the same way: in-gate dwell, spot, X-ray dwell and out-gate dwell. AGV cross-roads share cells with the lorry lanes, so level crossings are arbitrated by the same table.

**Rails.** Each QC and RMG holds `rail_span = [x − w, x + w]`. To travel, a crane first claims `[min(cur, x) − w, max(cur, x) + w]`; overlapping a neighbour is refused, so it waits. Claims on one rail take `pg_advisory_xact_lock` first, so two neighbours widening toward each other never wait on each other's uncommitted rows.

**Crane allocation.** QCs are shared across the berths in proportion to open bays, keeping their west-to-east order. Three rules ensure the order-preserving plan never asks a crane to pass another:
1. Discharge peeks, moves, then claims the box.
2. Load commitments drain before cranes are reassigned.
3. Stacks with boxes in flight for one crane are left alone by the others.

## 5. Concurrency and indexing

| Race | Resolution |
|---|---|
| QCs reach for the next box | `UPDATE … WHERE id = (SELECT … AND nothing above it … LIMIT 1 FOR UPDATE SKIP LOCKED)` |
| Two jobs claim an AGV | A SimPy `Container` counts idle AGVs, and the DB picks the *nearest* idle one (`ORDER BY abs(pos_x - x) + abs(pos_z - z)`) with `FOR UPDATE SKIP LOCKED` |
| Lorry, rail planner and load planner want boxes from the same stacks | Each claims with `FOR UPDATE SKIP LOCKED` inside `UPDATE … SET state = 'RETRIEVING'` |
| A re-handle moves a box someone else is waiting for | Relocation keeps the state; the transition trigger enforces it |

The load planner asks for the best box that may sit on what is already in the stack:

```sql
UPDATE containers SET state = 'RETRIEVING'
WHERE id = (
  SELECT c.id FROM containers c JOIN ports p ON p.code = c.pod
  WHERE c.state = 'STACKED_IN_YARD' AND c.mode = 'VESSEL'
    AND (%(kg)s::int IS NULL OR (c.weight_kg <= %(kg)s AND p.rotation <= %(rot)s))
  ORDER BY p.rotation DESC, c.weight_kg DESC,
           (SELECT count(*) FROM containers a                      -- fewest boxes to dig through
            WHERE (a.block_code, a.bay, a.row) = (c.block_code, c.bay, c.row) AND a.tier > c.tier)
  LIMIT 1
  FOR UPDATE OF c SKIP LOCKED)
RETURNING …;
```

Partial indexes cover only live work:
- `equipment_tasks_live`: work still pending or in progress.
- `containers_on_board`: the QC claim.
- `containers_in_yard (mode, block_code)`: lorry and rail picks.
- `containers_export_ready (pod, weight_kg DESC)`: the load planner.
- `containers_customs_pending`.

The `UNIQUE (block_code, bay, row, tier)` constraint doubles as the composite spatial index.

## 6. Simulation parameters

All of these live in [`config.py`](backend/portdb/config.py).

| | |
|---|---|
| Berths | 1 and 2: 6 × 4 × 10 feeders, 55 boxes in; 3: 10 × 6 × 10, 100 in. 15 % of each manifest is transhipment. Discharge, then load (360 s window), then sail |
| Fleet | 6 QCs on one rail, 16 AGVs (4 nodes/s), 6 RTGs, 2 RMGs, 2 X-ray portals, 10 lorries inside the gate at once |
| Yard | A–C import, D–F export, each 6 × 4 × 5; C and F take IMDG. The shift starts with 30 imports / 45 exports per block |
| Exports | lorry drop-offs (Poisson, mean 20 s), 8 per inbound train, plus tranships. Three ports of discharge in rotation: Rotterdam, Hamburg, Gdansk |
| Imports out | lorry pick-ups (Poisson, mean 6.5 s); 35 % booked to rail; 12 % under customs hold |
| Rail | 10 wagons × 2 slots; unload inbound, then load imports; departs when full or after 200 s |
| QC / RTG / RMG | 3.0–4.5 s cycle / 2.5 s per box / 3 s per lift |
| Next ship | a berth takes its next call once the import yard is under 75 % |

## 7. Interfaces

| | |
|---|---|
| `GET /api/layout` | world geometry: berths, road graph and spurs, blocks, lanes, rail, unit homes |
| `GET /api/snapshot` | live containers, latest motion track per unit, vessels, train, sim clock |
| `GET /api/stats` | yard, vessels, flows, discharged/loaded/sailed, collision counters, rejects |
| `GET /api/containers/{id}/clearance` | clearance CTE rows + `EXPLAIN ANALYZE` |
| `GET /api/debug` | crane allocation, rail blocks, vehicles waiting for a destination |
| `POST /api/sim/pause` · `/resume` · `/speed?x=0.25..16` | clock control |
| `WS /ws` | `hello`, then `track` / `box` / `boxes` / `remove` / `vessel` / `train` / `scan` / `reshuffle` / `lorry` / `reject`, and `stats` every 0.5 s |

## 8. The diorama

- **Draw calls:** every model (cranes, AGVs, lorries, the train, ships, gates, portals) is one merged, vertex-coloured geometry ([`parts.ts`](frontend/src/parts.ts)), so the whole fleet costs a few dozen draw calls. Containers are a single instanced mesh. **Exports are coloured by port of discharge**, so the stowage rule is visible on deck.
- **Zoom in:** past zoom 40, each model mounts a detail layer (ladders, walkways, railings, spreader twistlocks and flippers, wheel hubs, lifeboats, mooring lines) and people appear (lashers under the QCs, yard checkers, gate clerks, customs officers, rail crew).
- **Quality tiers:** set by `PerformanceMonitor` (flat → shadows → shadows + AO/tilt-shift); pin one with `?q=0`, `?q=1` or `?q=2`. Post-processing sits behind an error boundary and a context-loss guard, so a GPU hiccup drops a tier instead of the canvas.
- **Motion:** vehicles move by arc length with trapezoid speed profiles, cranes ease between waypoints, ships sail in and out, and gate barriers lift for lorries.

## 9. Development

```bash
# backend (Python 3.12, uv)
cd backend
uv sync
uv run pytest                     # throwaway PostgreSQL via pgserver; or set TEST_DATABASE_URL
DATABASE_URL=postgresql://port:port@localhost:5432/port uv run alembic upgrade head
DATABASE_URL=… uv run python -m portdb.api       # :8000, resets the terminal on boot

# frontend
cd frontend
npm install
npm run dev                       # :5173, proxies /api and /ws to :8000
```

**Tests.** 24 tests run against a real PostgreSQL. The database must reject every illegal state in section 2: two boxes in a slot, floating boxes, illegal per-flow transitions, heavy-on-light and later-port-on-earlier stowage, import/export segregation, ill-fitting or double-berthed ships, overlapping road bookings, and cranes passing. A 13-minute headless run at 12× must discharge, load, gate out and rail out with zero rejects, and must show vehicles yielding to each other.

**Boot semantics.** In-flight SimPy state cannot be resumed after a restart, so the API truncates operational tables on every start and reseeds a pre-stocked yard through the same constraints.

**Not built (yet):** paired 20 ft cells in vessel stowage, failed customs inspections, vessel stability (beyond heavy-under-light), and lorry overtaking on the through lane.
