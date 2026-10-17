# BACKBONE: PortDB Terminal Operating System

Engineering blueprint for PortDB: a container-terminal operating system (TOS) driven by a discrete-event simulation. It covers the schema, the concurrency model, the simulation loop, the wire protocol and the build plan. `README.md` is the public summary; this file is the working spec.

**Decisions locked (Step 1):** Vite + React + React Three Fiber · uv + Python 3.12 · database is authoritative for every move · Compose with `db` + `api` + `web`.

---

## 1. System shape

```
 browser ── Vite/R3F diorama ──────────────┐
    ▲  WS /ws  (events + 0.5 s stats)       │ REST /api/*  (layout, snapshot, clearance, controls)
    │                                       ▼
 api ── FastAPI ── asyncio loop ── SimPy Environment (stepped by a 50 Hz driver task)
                        │
                        │ psycopg 3 async pool (8 connections, real concurrent transactions)
                        ▼
 db  ── PostgreSQL 16 ── ENUMs · composite keys · CHECKs · constraint triggers · partial indexes
```

One process and one event loop. SimPy owns *time and queueing*. PostgreSQL owns *truth*: every state change is a committed transaction, and an illegal move is rejected by the database, not by Python.

### Why SimPy runs inside asyncio

SimPy generators are synchronous, and the DB driver is async. The bridge has two parts:

1. **Driver task.** Every 20 ms it does `env.run(until=wall_elapsed * speed)`. Pause means it stops advancing. A speed change rebases the wall-clock origin.
2. **DB step as a SimPy event.** A process that needs the DB does `result = yield self.db(fn, *args)`. `db()` creates a `simpy.Event`, schedules an asyncio task that runs `fn` in its own transaction, and calls `event.succeed(result)` (or `.fail(exc)`) when it finishes. The next driver tick resumes the process.

Several processes (2 QCs, 5 AGVs, 3 RTGs, up to 4 lorries) can have transactions in flight at once on separate pooled connections. That makes `FOR UPDATE SKIP LOCKED` and the constraint triggers carry real load instead of being decorative.

> DB latency (about 1–5 ms) is not charged to simulated time. At these cycle times (seconds) the error is noise.

---

## 2. Relational core

### 2.1 Topology

```
terminals 1──* yard_blocks 1──* yard_slots (block_code, bay, row, tier)   ← composite natural PK
                                     ▲ 0..1
vessels 1──* containers ─────────────┘  FK (block_code,bay,row,tier) + UNIQUE  ⇒ 1 box per slot
                 │  └── carrier_id ──► equipment (the AGV currently carrying it)
                 │
equipment 1──* equipment_tasks  (append-only ledger; partial index on live work)
gate_transactions *──1 containers
```

### 2.2 Types

```sql
CREATE TYPE container_state AS ENUM
  ('STOWED_ON_BOARD','DISCHARGING','IN_TRANSIT','STACKED_IN_YARD','RETRIEVING','GATE_OUT');
CREATE TYPE equipment_kind  AS ENUM ('QC','AGV','RTG');
CREATE TYPE equipment_state AS ENUM ('IDLE','BUSY');
CREATE TYPE task_kind       AS ENUM ('DISCHARGE','TRANSPORT','STACK','RESHUFFLE','RETRIEVE');
CREATE TYPE task_status     AS ENUM ('PENDING','IN_PROGRESS','DONE','FAILED');
CREATE TYPE vessel_status   AS ENUM ('INBOUND','BERTHED','DEPARTED');
CREATE TYPE lorry_status    AS ENUM ('QUEUED','AT_LANE','LOADED','DEPARTED');
```

### 2.3 Tables (abridged DDL; the migration holds the full text)

```sql
CREATE TABLE terminals (id smallserial PRIMARY KEY, code text UNIQUE NOT NULL, name text NOT NULL);

CREATE TABLE yard_blocks (
  code          char(1) PRIMARY KEY,                       -- 'A','B','C'
  terminal_id   smallint NOT NULL REFERENCES terminals,
  bays smallint NOT NULL CHECK (bays  > 0),
  rows smallint NOT NULL CHECK (rows  > 0),
  tiers smallint NOT NULL CHECK (tiers BETWEEN 1 AND 6),
  allows_hazmat boolean NOT NULL DEFAULT false            -- IMDG segregation: hazmat only in C
);

CREATE TABLE yard_slots (                                   -- 3 × 4 × 4 × 5 = 240 rows, pre-generated
  block_code char(1)  NOT NULL REFERENCES yard_blocks,
  bay smallint NOT NULL, row smallint NOT NULL, tier smallint NOT NULL,
  PRIMARY KEY (block_code, bay, row, tier)
);
-- bounds check against the parent block happens in the slot-generation INSERT … SELECT generate_series

CREATE TABLE vessels (
  id serial PRIMARY KEY, name text NOT NULL, imo char(7) UNIQUE NOT NULL,
  bays smallint NOT NULL, rows smallint NOT NULL, tiers smallint NOT NULL,
  status vessel_status NOT NULL DEFAULT 'INBOUND', berthed_at timestamptz, departed_at timestamptz
);

CREATE TABLE equipment (
  id text PRIMARY KEY,                                      -- 'QC-1','AGV-3','RTG-B'
  kind equipment_kind NOT NULL, state equipment_state NOT NULL DEFAULT 'IDLE',
  home_block char(1) REFERENCES yard_blocks,               -- RTGs only
  idle_since timestamptz NOT NULL DEFAULT now(),
  CHECK ((kind = 'RTG') = (home_block IS NOT NULL))
);

CREATE TABLE containers (
  id          char(11) PRIMARY KEY CHECK (iso6346_valid(id)),   -- e.g. MSCU1234565, check digit verified
  iso_type    char(4)  NOT NULL CHECK (iso_type IN ('22G1','45G1','22R1','45R1')),
  weight_kg   integer  NOT NULL CHECK (weight_kg BETWEEN 2000 AND 32500),
  hazmat      boolean  NOT NULL DEFAULT false,
  state       container_state NOT NULL DEFAULT 'STOWED_ON_BOARD',
  vessel_id   integer REFERENCES vessels,
  vessel_bay smallint, vessel_row smallint, vessel_tier smallint,
  block_code char(1), bay smallint, row smallint, tier smallint,
  carrier_id  text REFERENCES equipment,
  FOREIGN KEY (block_code, bay, row, tier) REFERENCES yard_slots,
  UNIQUE (block_code, bay, row, tier),                     -- ONE CONTAINER PER YARD SLOT
  UNIQUE (vessel_id, vessel_bay, vessel_row, vessel_tier), -- one per ship cell
  -- location is all-or-nothing, and present exactly when the state says it must be
  CHECK (num_nulls(block_code, bay, row, tier) IN (0,4)),
  CHECK (num_nulls(vessel_bay, vessel_row, vessel_tier) IN (0,3)),
  CHECK ((state IN ('STACKED_IN_YARD','RETRIEVING')) = (block_code IS NOT NULL)),
  CHECK ((state IN ('STOWED_ON_BOARD','DISCHARGING')) = (vessel_bay IS NOT NULL)),
  CHECK ((state = 'IN_TRANSIT') = (carrier_id IS NOT NULL))
);

CREATE TABLE equipment_tasks (                              -- ledger: rows are only ever appended or advanced
  id bigserial PRIMARY KEY,
  equipment_id text NOT NULL REFERENCES equipment,
  container_id char(11) NOT NULL REFERENCES containers,
  kind task_kind NOT NULL, status task_status NOT NULL DEFAULT 'PENDING',
  origin jsonb NOT NULL, target jsonb NOT NULL,
  sim_t0 double precision, sim_t1 double precision,
  created_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz
);

CREATE TABLE gate_transactions (
  id bigserial PRIMARY KEY, plate text NOT NULL,
  container_id char(11) NOT NULL REFERENCES containers, lane smallint CHECK (lane BETWEEN 1 AND 4),
  status lorry_status NOT NULL DEFAULT 'QUEUED',
  shifter_moves smallint NOT NULL DEFAULT 0,                -- re-handles the RTG needed to dig it out
  arrived_at timestamptz NOT NULL DEFAULT now(), departed_at timestamptz
);
-- at most one live pickup per container
CREATE UNIQUE INDEX gate_one_live_pickup ON gate_transactions (container_id) WHERE status <> 'DEPARTED';
```

A null in any yard-location column makes both the `UNIQUE` and the composite FK skip the row. That is the intended behaviour: a box on the ship or on an AGV has no slot. The `num_nulls` CHECKs stop half-filled coordinates from slipping past.

### 2.4 Triggers and functions

| Object | Kind | Rule enforced |
|---|---|---|
| `iso6346_valid(text)` | `IMMUTABLE` SQL function, used in a CHECK | Owner code + `U` + 6 digits + valid ISO 6346 check digit |
| `container_state_transitions` | lookup table + `BEFORE UPDATE OF state` trigger | Only legal lifecycle edges: `STOWED→DISCHARGING→IN_TRANSIT→STACKED_IN_YARD→RETRIEVING→GATE_OUT` (plus `STACKED→STACKED` for reshuffles) |
| `trg_hazmat_segregation` | `BEFORE INSERT OR UPDATE OF block_code` | Hazmat box can only land in a block with `allows_hazmat` |
| `trg_stack_gravity` | `CONSTRAINT TRIGGER … DEFERRABLE INITIALLY DEFERRED`, `AFTER INSERT OR UPDATE OR DELETE` | No floating boxes: tier *t* > 1 needs an occupant at *t*−1, and a slot cannot be vacated while *t*+1 is occupied. Checked at **commit**, so a multi-row reshuffle is judged on its final state. Locks the stack's 5 `yard_slots` rows `FOR UPDATE` first, which serialises concurrent writers per stack and closes the READ COMMITTED gap |
| `trg_vessel_gravity` | same pattern on ship cells | Discharge must go top-down |

### 2.5 Indexes

```sql
-- live work queue only; DONE/FAILED history (the bulk of the table) is not indexed
CREATE INDEX equipment_tasks_live ON equipment_tasks (equipment_id, status)
  WHERE status IN ('PENDING','IN_PROGRESS');

-- the UNIQUE (block_code, bay, row, tier) constraint IS the composite spatial index:
-- point lookups, stack scans (block,bay,row prefix) and the recursive CTE all use it.

-- QC "next box to discharge": top tier first within its bay range
CREATE INDEX containers_on_board ON containers (vessel_id, vessel_bay, vessel_tier DESC)
  WHERE state = 'STOWED_ON_BOARD';

-- lorry target pick
CREATE INDEX containers_in_yard ON containers (block_code) WHERE state = 'STACKED_IN_YARD';
```

---

## 3. Key queries

### 3.1 Stack clearance: recursive CTE

A lorry asks for a box. This walks *up* the stack one tier at a time from the target. The gravity trigger guarantees a contiguous stack, so the recursion stops at the first empty tier. The result is the RTG's work order: blockers from the top down, then the target.

```sql
WITH RECURSIVE stack AS (
    SELECT c.id, c.block_code, c.bay, c.row, c.tier, 0 AS depth
    FROM containers c
    WHERE c.id = %(target)s AND c.state IN ('STACKED_IN_YARD','RETRIEVING')
  UNION ALL
    SELECT above.id, above.block_code, above.bay, above.row, above.tier, s.depth + 1
    FROM stack s
    JOIN containers above
      ON (above.block_code, above.bay, above.row, above.tier)
       = (s.block_code,     s.bay,     s.row,     s.tier + 1)   -- one tier per step
)
SELECT id, tier, depth, depth = 0 AS is_target,
       count(*) FILTER (WHERE depth > 0) OVER () AS shifter_moves
FROM stack
ORDER BY tier DESC;
```

`GET /api/containers/{id}/clearance` returns this result as-is, along with its `EXPLAIN` plan, for inspection. At 240 slots the planner prefers a hash join for the recursive step. The `containers_one_per_slot` unique index is what makes each step a point lookup once the table grows (or under `SET enable_hashjoin = off`).

### 3.2 QC claims the next box with `SKIP LOCKED`

Both QCs run this at the same moment. Neither blocks the other, and they never pick the same box.

```sql
UPDATE containers SET state = 'DISCHARGING'
WHERE id = (
  SELECT id FROM containers
  WHERE vessel_id = %(v)s AND state = 'STOWED_ON_BOARD'
    AND vessel_bay BETWEEN %(bay_lo)s AND %(bay_hi)s
  ORDER BY vessel_tier DESC, vessel_bay, vessel_row
  LIMIT 1
  FOR UPDATE SKIP LOCKED)
RETURNING id, vessel_bay, vessel_row, vessel_tier, hazmat, weight_kg;
```

### 3.3 AGV claim: QC vs QC race

A SimPy `Container(level = idle AGVs)` gates entry, so a process only queries when a unit exists. The DB still decides *which* unit. If both QCs pass the gate together, `SKIP LOCKED` hands them different AGVs. Without the lock, both would read the same `IDLE` row and double-book it.

```sql
UPDATE equipment SET state = 'BUSY'
WHERE id = (SELECT id FROM equipment
            WHERE kind = 'AGV' AND state = 'IDLE'
            ORDER BY idle_since
            LIMIT 1 FOR UPDATE SKIP LOCKED)
RETURNING id;
```

The same transaction moves the box to `IN_TRANSIT` with `carrier_id` set, and inserts the `TRANSPORT` task row.

### 3.4 Slot allocation: stack to tier 3, then higher

```sql
SELECT s.bay, s.row, s.tier
FROM yard_slots s
WHERE s.block_code = %(block)s
  AND %(exclude_stack)s IS DISTINCT FROM (s.bay, s.row)              -- reshuffle: not back onto its own stack
  AND NOT EXISTS (SELECT 1 FROM containers c
                  WHERE (c.block_code,c.bay,c.row,c.tier) = (s.block_code,s.bay,s.row,s.tier))
  AND (s.tier = 1 OR EXISTS (SELECT 1 FROM containers c
                  WHERE (c.block_code,c.bay,c.row,c.tier) = (s.block_code,s.bay,s.row,s.tier-1)))
ORDER BY s.tier > 3, s.bay, s.row, s.tier      -- build stacks to tier 3 first, tiers 4-5 only when needed
LIMIT 1
FOR UPDATE OF s SKIP LOCKED;
```

### 3.5 Block routing

Hazmat goes to C. Everything else goes to the block with the lowest `occupied + in-flight`. In-flight is counted from live `TRANSPORT` tasks, which uses the partial index.

---

## 4. Simulation engine (`sim_engine.py`)

### 4.1 Feeds and speeds (`config.py`, all tunable)

| Parameter | Value | Note |
|---|---|---|
| QC cycle | `uniform(3.0, 4.5)` s | spreader down → AGV |
| AGV speed | 2 road nodes/s | path from the road graph (BFS) |
| RTG move | 2.5 s per container handled | stack, reshuffle hop, or load to lorry |
| Lorry arrivals | `expovariate(1/10.0)` | Poisson, λ = 10 s |
| Gate lanes | `simpy.Resource(capacity=4)` | |
| Vessel load | 200 boxes in 240 cells | bottom-up per stack, heavier boxes lower |
| Mix | 60 % 20 ft / 40 % 40 ft, 10 % reefer, 6 % hazmat | |
| Next vessel | berths when the previous one departs **and** yard occupancy < 50 % | keeps the demo running indefinitely |

**Flow balance.** QCs deliver about 0.53 boxes/s. AGVs, at roughly 12 s per round trip × 5 units, carry about 0.42/s. AGVs are therefore the bottleneck, so QCs occasionally wait with a box hanging, which looks realistic. RTGs can handle 1.2/s per three blocks, enough headroom for stacking, lorry loads and reshuffles. The yard peaks at around 70 %, which keeps buried boxes, and therefore reshuffles, common. A homing AGV can be recalled mid-route, so it does not have to finish driving to its park spot first. Measured at 1x: about 0.38 boxes/s, 109 TEU off and 22 re-handles in the first 3m46s, 0 DB rejects.

> One TEU cell holds one box regardless of length (20 ft and 40 ft are drawn differently but slot identically). `ponytail:` real stowage pairs 20 ft cells; upgrade path is a `length` column on slots plus a paired-cell CHECK.

### 4.2 Processes

```
vessel_arrival()          seed → BERTHED → spawn qc_worker ×2 → wait both done → DEPARTED → wait yard<50% → loop
qc_worker(qc, bays)       loop: claim box (3.2) → cycle timeout → wait idle-AGV gate → claim AGV (3.3)
                                → spawn agv_trip(agv, box)
agv_trip(agv, box)        route block (3.5) → drive path → request rtg[block] → RTG allocates slot (3.4)
                                + STACK (state STACKED_IN_YARD) → drive home → release AGV
lorry_source()            loop: timeout expovariate → pick random STACKED box → spawn lorry(box)
lorry(box)                request gate lane → box RETRIEVING → request rtg[block]
                                → run clearance CTE (3.1) → for each blocker: allocate (3.4) + RESHUFFLE
                                → RETRIEVE → GATE_OUT → release lane
```

Every arrow that changes a box or a unit is **one transaction**: state change, location change, and an `equipment_tasks` ledger row. A trigger violation raises an exception into the SimPy process (`event.fail`). That marks the task `FAILED`, logs it, and returns the process to its loop. The database refuses and the simulation adapts.

### 4.3 Boot

`seed_vessel.py --reset` truncates operational tables and reseeds the terminal, blocks, slots, equipment and the first vessel. In-flight SimPy state cannot be resumed after a restart, so **every boot starts with a fresh terminal**.

---

## 5. API and WebSocket interface

### REST

| Method | Path | Returns |
|---|---|---|
| GET | `/api/layout` | world geometry: quay, ship cells, road nodes/edges, block origins, lanes, handover points |
| GET | `/api/snapshot` | all live containers and equipment with positions, plus sim clock |
| GET | `/api/containers/{id}/clearance` | rows from 3.1 + `EXPLAIN (ANALYZE, BUFFERS)` text |
| GET | `/api/stats` | the same payload as the `stats` frame |
| POST | `/api/sim/pause` · `/resume` · `/speed?x=4` | sim clock control |

### WebSocket `/ws` (server → client, JSON)

The client never polls positions. Every motion is sent once as a **track**: a polyline plus a start and end in sim time. The client interpolates each one against its own copy of the sim clock, with equal time per segment. A box is drawn riding its carrier (AGV or lorry), following its own crane track, or resting at its DB location.

```jsonc
{"type":"hello","layout":{…},"sim":{"t":12.4,"speed":1,"paused":false},
 "vessel":{…},"boxes":[…live containers…],"tracks":[…latest per unit/box…],"reshuffling":[…]}

{"type":"track","id":"AGV-3","kind":"AGV","t0":40.1,"t1":46.6,"path":[[4,0,1],[5,0,1],…]}
{"type":"track","id":"QC-1","kind":"QC","t0":36.6,"t1":40.1,"path":[above cell, cell, above cell, above AGV, AGV bed]}
{"type":"box","id":"MSCU1234565","state":"IN_TRANSIT","carrier_id":"AGV-3"}
{"type":"box","id":"MSCU1234565","state":"STACKED_IN_YARD","block_code":"A","bay":2,"row":3,"tier":2}
{"type":"boxes","boxes":[…new manifest…]}            {"type":"remove","ids":["MSCU1234565","LRY-18"]}
{"type":"vessel","status":"BERTHED|DEPARTED","id":2,"name":"Mersey Wren","imo":"9074729"}
{"type":"reshuffle","active":true,"block":"A","target":"…","moves":2}   // drives the amber badge
{"type":"lorry","id":"LRY-18","plate":"KK78 ZMO","box":"…","block":"B","lane":4}
{"type":"reject","error":"stack_gravity: yard stack A-1-1 has 2 boxes but top tier 3"}
{"type":"stats","t":50.0,"speed":1,"paused":false,"yard":{"A":41,"B":38,"C":22},"capacity":240,
 "on_board":131,"teu_discharged":103,"gated_out":12,"reshuffles":7,"rejects":0,
 "queues":{"agv_wait":1,"rtg":{"A":2,"B":0,"C":1},"gate":0}}            // every 0.5 s
```

Broadcast uses one bounded `asyncio.Queue` per client. A client that falls 2,000 events behind is dropped rather than slowing the sim. Each client subscribes *before* its snapshot is read, so the two may overlap, but nothing is lost in between. Overlaps are harmless because every update is an upsert.

---

## 6. Diorama (frontend)

- **Camera:** R3F `OrthographicCamera` at a true isometric angle (`rotation = [-atan(1/√2), π/4, 0]`).
- **Tilt-shift:** `@react-three/postprocessing` `TiltShift2` plus soft `N8AO` ambient occlusion and warm directional "late afternoon" light.
- **Geometry:** boxes only, no model files. A container is a `boxGeometry` (20 ft = 1 cell, 40 ft = 1 cell drawn 1.9× long and squeezed). All containers share one `InstancedMesh` (≤ 240 + vessel), with colour per instance by type (dry: sage/slate · reefer: ice blue · hazmat: terracotta).
- **Motion:** each unit stores its latest `move`. Each frame it lerps along the path polyline at `(simNow − t0)/(t1 − t0)`.
- **HUD:** solid ivory cards, monospace telemetry, a live log of `[INFO] MSCU1234565 stacked A-02-03-4` lines, and an amber **RE-RECYCLE IN PROGRESS** tag floating above a block while `reshuffle.active`. The styling follows `scratch/style_sheet.md`: Pastel Harbor palette, no gradients, glass, sparkles or neon.

---

## 7. Repository layout (as built)

```
docker-compose.yml          db (postgres:16, healthcheck) · api · web (nginx, :8080)
BACKBONE.md  README.md
backend/
  pyproject.toml  uv.lock  Dockerfile  alembic.ini
  alembic/versions/0001_schema.py   hand-written: ENUMs, ISO 6346 function, CHECKs, triggers, indexes
  alembic/versions/0002_landside.py trains, outbound mode, customs holds, new lifecycle edges (§9)
  portdb/
    config.py        feeds, speeds, fleet and block dimensions (every number a calibration knob)
    layout.py        world geometry, AGV road graph + BFS, unit homes (self-check: python -m portdb.layout)
    db.py            psycopg async pool
    models.py        SQLAlchemy 2 declarative mirror (test asserts zero drift from the migration)
    queries.py       every SQL statement, incl. the clearance CTE and SKIP LOCKED claims
    sim_engine.py    asyncio driver, DB-step bridge, QC / AGV / RTG / lorry / train / scanner processes
    api.py           FastAPI routes + /ws broadcaster (python -m portdb.api)
    seed_vessel.py   terminal grid + fleet + manifest generator (ISO 6346 ids, weight-sorted tiers)
  tests/test_db_rules.py   the DB must reject 2 boxes/slot, floating or undermined stacks, illegal
                           transitions, hazmat outside C, bad check digits; CTE, allocation, SKIP LOCKED race
  tests/test_sim.py        5 sim-minutes headless at 10x: zero rejects, boxes conserved, re-handles,
                           a train railed out, a box X-rayed
frontend/
  package.json  vite.config.ts  index.html  Dockerfile  nginx.conf
  src/ main.tsx  world.ts (WS mirror, sim clock, easing)  scene.tsx (terrain, scenery, camera, containers)
       models.tsx (cranes, vehicles, ship, train, portals, gates)  hud.tsx (flow strip, rail, customs)  styles.css
```

---

## 8. Phased plan

| Phase | Deliverable | Done when |
|---|---|---|
| **1 · DB and migrations** | `pyproject.toml`, `0001_schema.py`, `models.py`, `queries.py`, `test_db_rules.py` | `alembic upgrade head` on empty PG; every rule test raises the expected DB error; CTE test passes |
| **2 · SimPy engine** | `config.py`, `sim_engine.py`, `seed_vessel.py` | headless 60 s run: boxes conserved, no unexpected `FAILED` tasks, reshuffles occur |
| **3 · FastAPI and WebSockets** | `api.py` | `/ws` streams `hello` → `move/qc/rtg/stats`; clearance endpoint returns rows and plan |
| **4 · Diorama UI** | `frontend/` | ship empties visually, AGVs lerp smoothly, amber badge appears during reshuffles |
| **5 · Docker and seeders** | `docker-compose.yml`, two Dockerfiles, nginx config | `docker compose up` → http://localhost:8080 shows a live port |
| **6 · Docs** | `README.md` whitepaper | ER diagram, CTE walkthrough, indexing rationale, one-command launch |

**Out of scope until asked:** export (gate-in) flow, auth, multiple berths, persistence of sim state across restarts, 20/40 ft paired cells.

---

## 9. Landside extension (migration 0002)

The second build made the yard a junction instead of a dead end.

```
ship ─QC─▶ AGV ─RTG─▶ yard stack ─┬─RTG─▶ lorry ──(SCN-ROAD if held)──▶ out-gate         ON_TRUCK → GATE_OUT
                                   └─RTG─▶ AGV ──(SCN-RAIL if held)──RMG─▶ wagon ─▶ train  IN_TRANSIT → ON_RAIL → RAIL_OUT
```

**Schema.**
- `containers` gains `mode outbound_mode`, `customs_hold`, `inspected_at`, and `(train_id, wagon, wagon_slot)`.
- The new `trains` table has a `train_status` ENUM.
- New `CHECK`s:
  - a held box cannot reach `GATE_OUT`, `ON_RAIL` or `RAIL_OUT` without `inspected_at`
  - a box leaves only by its booked mode
  - one box per wagon slot, with coordinates all-or-nothing and present exactly when the box is on rail
- The `RETRIEVING → GATE_OUT` edge is replaced by `RETRIEVING → ON_TRUCK → GATE_OUT` and `RETRIEVING → IN_TRANSIT → ON_RAIL → RAIL_OUT`.
- The ENUM labels are added inside `autocommit_block()`, because PostgreSQL will not use a label in the transaction that created it.

**Engine.**
- `lorry()` follows the in-gate, loading spot, optional X-ray and out-gate polyline on the lorry lanes.
- `trains()` runs the cycle: roll in, a loading window, two `rail_planner` workers, roll out.
- `rail_planner` takes the least-buried rail box (see the README §4 query), uses an AGV, digs it out with the RTG, sends held boxes through `SCN-RAIL` and lifts with the RMG. The wagon slot is assigned in memory and guarded by the `UNIQUE` constraint.
- `dig_out()` and `inspect()` are shared by both exits.

**Boot.** `seed_yard()` pre-stocks 36 boxes per block, inserted directly as `STACKED_IN_YARD` through the same constraints, so trucks and trains have work from second one.

**World.** The world is now about 92 × 21 units:
- quay
- three blocks
- a lorry loading lane and through lane inside and outside the RTG legs
- in-gate and out-gate with animated barriers
- a customs compound with two portals
- a rail siding with an RMG runway
- scenery

AGV roads gained cross links up to a rail lane at z = 14.

**Rendering.**
- Vehicles move by arc length with a trapezoid speed profile; crane legs ease in and out.
- The ship sails in and out.
- React re-renders are batched to 10 Hz, and scene components subscribe through narrow selectors.
- Static props are instanced.
- `PerformanceMonitor` drops N8AO and tilt-shift on weak GPUs. On the dev machine's Intel UHD this took the frame rate from 6 fps to about 42 fps with effects off, or about 26 fps with them forced on.

---

## 10. Both directions, three berths, collisions (migration 0003)

### Flows

```
import    ship ─QC─▶ AGV ─RTG─▶ import blocks A–C ─┬─RTG─▶ lorry ─(X-ray)─▶ out-gate
                                                   └─RTG─▶ AGV ─(X-ray)─RMG─▶ wagon ─▶ train
export    lorry ─RTG─▶ export blocks D–F           train ─RMG─▶ AGV ─RTG─▶ export blocks D–F
          export yard ─RTG─▶ AGV ─▶ park (staging) ─▶ QC ─▶ ship ─▶ sails
tranship  ship ─QC─▶ AGV ─RTG─▶ export blocks ─▶ … loaded onto a later ship like an export
```

A vessel call is: sail in, then **discharge** (imports and tranships), then **load** (exports, with a load window), then sail. Trains run the same pattern: unload inbound export boxes, then load rail-booked imports, then depart.

### Schema added

| Object | Rule |
|---|---|
| `container_flow` ENUM + `containers.flow`, `containers.pod → ports` | `flow IN (EXPORT, TRANSHIP)` ⇔ a port of discharge is set ⇔ `mode = 'VESSEL'` |
| `container_state_transitions.flows container_flow[]` | Lifecycle edges are now per flow. An import can never be `LOADING` onto a ship, and an export can never go `RETRIEVING → ON_TRUCK` |
| `containers_exit_by_booked_mode` | `GATE_OUT` needs `mode = 'TRUCK'`, `RAIL_OUT` needs `RAIL`, `LOADING`/`SAILED` need `VESSEL`. `ON_TRUCK`/`ON_RAIL` are also allowed for an export *arriving* by that mode |
| `trg_yard_segregation` (replaces the hazmat trigger) | Imports only in `IMPORT` blocks, exports and tranships only in `EXPORT` blocks, hazmat only where `allows_hazmat` (C and F) |
| `trg_stowage` (deferred constraint trigger) | On board, a box may not out-weigh the box beneath it, nor be discharged later (higher port rotation) than it. Checked at commit against the cells directly below and above |
| `berths`, `vessels.berth_id`, `vessels_one_per_berth` (partial unique), `trg_berth_fit` | One live vessel per berth; a vessel must fit its berth's bays and rows |
| `road_bookings` + `road_bookings_no_overlap EXCLUDE USING gist (box(point(cell_key(x,z), t0), point(cell_key(x,z)+0.5, t1)) WITH &&)` | A booking is a rectangle in (road cell, sim time). No two rectangles may overlap, so no two vehicles share a cell at the same moment. Built on native gist over `box`, with no `btree_gist` extension needed |
| `equipment.rail_span numrange` + `EXCLUDE USING gist (rail_span WITH &&) WHERE (kind = 'QC')` (and one for `RMG`) | Cranes on one rail hold disjoint envelopes. To pass a neighbour, a crane would have to claim an envelope overlapping it, which is refused |

### Collision avoidance (engine)

- **Road graph:** explicit edges. Every place a vehicle may *wait* is a dead-end spur: QC hand-overs, RTG hand-overs, RMG hand-overs and parks. Lanes only carry finite bookings, so a parked vehicle can delay others but never deadlock them.
- **`travel()`:** plans a BFS route and times it into legs, `[t_enter − margin, t_leave + margin]` per cell, with an open-ended booking on the destination spur. It then runs:
  1. `route_clashes` (an index-backed overlap query).
  2. On a clash, it re-routes around the clashing cells, otherwise it departs later.
  3. `book_route` inserts the legs **in cell order**.

  Inserting in cell order means two bookers can never wait on each other in a cycle. The pre-check is an optimisation; the exclusion constraint is the guarantee, and a lost race just re-plans.
- **Lorries:** book fixed polylines through cells: in-gate booth (check-in dwell), loading spot, X-ray portal dwell, out-gate dwell. AGV cross-roads share cells with the lorry lanes, so crossings are arbitrated by the same table.
- **Cranes:** `gantry()` claims `[min(cur, x) − w, max(cur, x) + w]`, travels, then shrinks back to `[x − w, x + w]`. Claims on one rail take a `pg_advisory_xact_lock` first, so two neighbours never wait on each other's uncommitted rows.

### Quay crane allocation

`reallocate()` shares the six QCs between berths with work: at least one each, at most one per two bays, in proportion to open bays. It keeps the cranes' west-to-east order, and spare cranes park at the rail ends.

Three rules keep the order-preserving plan from ever asking a crane to pass another:
1. **Discharge peeks, moves, then claims.** A crane never holds a box while travelling, and abandons a move if its assignment changes while it waits for the rail.
2. **Load commitments are never abandoned.** If any crane still has export boxes on their way (planned or in its hands), reallocation is deferred and those pipelines drain first.
3. **A stack with pending boxes for one crane is left alone by the others**, so tiers always go on in order.

### Load planning

`plan_step()` walks the crane's stacks bottom-up and asks `pick_export(below_kg, below_rotation)` for the best fitting box. The greedy order is latest port first, then heaviest, then fewest boxes to dig. If nothing fits a stack, that stack closes. Picked boxes are fetched by AGV (RTG dig if needed) to the park nearest the crane. They come forward to the hand-over spur one at a time, and early only if they are bound for a different spur. The stowage trigger re-checks every placement at commit.

### Throughput levers (all in `config.py`)

16 AGVs (22 parks), AGVs at 4 nodes/s, job chaining (a freed AGV is handed straight to a waiting job, and a 2 s sweeper sends it home otherwise), a load prefetch of 3, and a 360 s load window. The database retries deadlock and serialization victims: every step is a single transaction, so a retry is safe.

### Diorama

- **Draw calls:** every model is a single merged, vertex-coloured geometry (`parts.ts`), so the 6 QCs, 6 RTGs, 2 RMGs and 16 AGVs cost about one draw call each.
- **Close-up detail:** at camera zoom ≥ 40, the scene mounts a second "detail" geometry per model (ladders, walkways, railings, spreader twistlocks and flippers, wheel hubs, lifeboats, mooring lines, signage) and instanced people (lashers under the QCs, yard checkers, gate clerks, customs officers, rail crew).
- **Quality tiers:** set by `PerformanceMonitor` (or pinned with `?q=0|1|2`): flat → shadows → shadows + AO/tilt-shift.
- **Export colours:** exports are coloured by port of discharge, so the stowage rule is visible on deck.
