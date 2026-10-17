<h1 align="center">PortDB</h1>

<p align="center">
A container terminal where PostgreSQL decides what is allowed to happen.<br>
Three berths, six cranes on one rail, sixteen robot trucks, a rail terminal and a customs X-ray,<br>
all simulated, all committed as transactions, all rendered as a small model port in your browser.
</p>

<p align="center"><img src="docs/media/hero.gif" width="960" alt="The yard at work: gantry cranes stacking, AGVs on the roads, a train on the siding"></p>

```bash
docker compose up --build     # then open http://localhost:8080
```

---

## The idea

Most simulations keep the rules in application code and treat the database as a place to write results. PortDB does the opposite. The SimPy engine proposes moves, and Postgres accepts or refuses them:

- A box cannot be stacked on thin air.
- A heavy box cannot go on top of a light one aboard ship.
- A box booked for Hamburg cannot be loaded above one going to Rotterdam.
- An import cannot leave on a vessel, and a box under customs hold cannot leave at all until it has been scanned.
- Two robot trucks cannot occupy the same piece of road at the same instant.
- Two quay cranes cannot pass each other on their shared rail.

Every one of those rules is a constraint, a trigger or an exclusion index. Every one has a test that tries to break it against a real Postgres. Over long runs the engine produces thousands of contested moves and the database refuses none of them, because the engine learned to ask nicely. When it didn't, the database caught it (see [what broke](#what-broke-and-what-caught-it)).

The diorama is a read-only view. It knows nothing the database doesn't.

## How a box moves

```
import    ship ─QC─▶ AGV ─RTG─▶ import yard ─┬─RTG─▶ lorry ─(X-ray if held)─▶ out-gate
                                              └─RTG─▶ AGV ─(X-ray if held)─RMG─▶ wagon ─▶ train
export    lorry ─RTG─▶ export yard           train ─RMG─▶ AGV ─RTG─▶ export yard
          export yard ─RTG─▶ AGV ─▶ staging park ─▶ QC ─▶ ship ─▶ sails
tranship  ship ─QC─▶ AGV ─RTG─▶ export yard ─▶ onto a later ship
```

A ship sails in, is discharged, is loaded with exports that satisfy the stowage rules, and sails out. A train arrives with export boxes, is unloaded, then takes rail-booked imports. Lorries queue at the gate, collect or drop a box under a yard crane, pass customs if needed and leave.

<p align="center"><img src="docs/media/closeup.gif" width="960" alt="Five quay cranes working a ship, AGVs underneath, lashers on the quay"></p>

## Four things worth reading the code for

### 1. Traffic control is an exclusion constraint

Before an AGV or a lorry moves, it books every road cell on its route for the time it will be there. A booking is a rectangle in (cell, time), and the table refuses overlapping rectangles:

```sql
CREATE TABLE road_bookings (
  id bigserial PRIMARY KEY, vehicle text NOT NULL,
  x smallint NOT NULL, z smallint NOT NULL,
  t0 double precision NOT NULL, t1 double precision NOT NULL,
  CHECK (t1 > t0),
  CONSTRAINT road_bookings_no_overlap
    EXCLUDE USING gist (box(point(cell_key(x, z), t0), point(cell_key(x, z) + 0.5, t1)) WITH &&)
);
```

That runs on stock Postgres with no extension, because it uses the built-in gist operator class for `box`. The engine checks for clashes first, re-routes around them or leaves a little later, then inserts. If two vehicles race for the same gap, the constraint decides and the loser plans again.

Two details make it safe under load:

- **Vehicles only wait on dead ends.** Every place a vehicle can stop (crane hand-overs, parking bays, lorry loading spots, the gate booth) is a spur off the road graph. Through lanes only ever hold short-lived bookings, so a parked truck can slow traffic but never trap it.
- **Bookings are inserted in cell order.** An exclusion check waits for a conflicting row that hasn't committed yet. Two writers inserting the same cells in opposite orders will deadlock. One `INSERT … SELECT … ORDER BY cell_key(x, z)` gives everyone the same order, so the waits can never form a cycle.

### 2. Cranes that can't overtake

Six quay cranes share one rail and are reassigned between berths as work comes and goes. Each crane owns a stretch of rail stored as a `numrange`:

```sql
ALTER TABLE equipment
  ADD CONSTRAINT equipment_qc_rail_no_crossing  EXCLUDE USING gist (rail_span WITH &&) WHERE (kind = 'QC'),
  ADD CONSTRAINT equipment_rmg_rail_no_crossing EXCLUDE USING gist (rail_span WITH &&) WHERE (kind = 'RMG');
```

To travel, a crane claims the whole stretch between where it is and where it's going. If a neighbour is in the way, the claim is refused and the crane waits. To pass its neighbour a crane would have to claim rail the neighbour already holds, so passing is impossible by construction.

### 3. Stowage rules that hold at commit

Loading a ship is a planning problem with two rules that pull against each other. Heavy boxes go low, and boxes for later ports go under boxes for earlier ones. The planner picks greedily (latest port, then heaviest, then the box with the fewest others on top of it in the yard). A deferred constraint trigger re-checks each placement against the cells directly above and below when the transaction commits. Export boxes are coloured by destination in the diorama, so you can watch the rule on deck.

### 4. Digging a box out: the recursive CTE

When a lorry wants a box that's buried, the yard crane needs the boxes above it, top first:

```sql
WITH RECURSIVE stack AS (
    SELECT c.id, c.block_code, c.bay, c.row, c.tier, 0 AS depth
    FROM containers c
    WHERE c.id = %(target)s AND c.state IN ('STACKED_IN_YARD', 'RETRIEVING')
  UNION ALL
    SELECT above.id, above.block_code, above.bay, above.row, above.tier, s.depth + 1
    FROM stack s
    JOIN containers above
      ON (above.block_code, above.bay, above.row, above.tier) = (s.block_code, s.bay, s.row, s.tier + 1)
)
SELECT id, tier, depth = 0 AS is_target, count(*) FILTER (WHERE depth > 0) OVER () AS shifter_moves
FROM stack ORDER BY tier DESC;
```

The recursion climbs one tier at a time and stops at the first gap. A deferred gravity trigger guarantees there are no gaps in a stack, so the CTE never needs to look past one.

<p align="center"><img src="docs/media/rehandle.gif" width="960" alt="A yard crane re-handling two boxes to reach one underneath"></p>

Click any box in the yard, or type its number, and the inspector runs this query live with its `EXPLAIN ANALYZE`:

<p align="center"><img src="docs/media/inspector.png" width="960" alt="Inspector showing two boxes above the target and the query plan"></p>

## What broke, and what caught it

These all happened while building it. Each one was a real bug in the engine, and each was stopped by a constraint rather than discovered as a corrupt yard later.

| Symptom | What the database said | Fix |
|---|---|---|
| A crane took the box *under* one another crane was still lifting | `stack_gravity: vessel stack 2-3-2 has 4 boxes but top tier 5` | Claim only boxes with nothing above them in any state |
| Two vehicles booking overlapping cells in opposite order | `deadlock detected … while checking exclusion constraint` | Insert bookings in cell order; retry deadlock victims |
| Two neighbouring cranes widening their claims toward each other | Deadlock on `equipment_qc_rail_no_crossing` | Serialise claims per rail with `pg_advisory_xact_lock` |
| A crane reassigned mid-job tried to drive back past its neighbour | Rail claim refused, forever | Move first, claim the box second; drain committed load work before reassigning |
| One unretried deadlock leaked an AGV and froze the quay at minute 13 | (the quay went silent) | Retry whole-transaction deadlock victims; every job returns its AGV on failure |

Each step in the engine is one transaction, so a retry is always safe.

## The diorama

<p align="center"><img src="docs/media/landside.gif" width="960" alt="A train being loaded by the rail crane, then the customs X-ray scanning a lorry"></p>

- **Server owns time.** Each movement is sent once over a WebSocket as a path with start and end times in simulation seconds. The browser interpolates against its own copy of the clock: arc length with acceleration ramps for vehicles, eased legs for cranes. Nothing is polled.
- **Few draw calls.** Every crane, truck, ship and building is merged into a single vertex-coloured mesh. All 1,600 container slots are one instanced mesh. That keeps it smooth on integrated graphics: about 38 fps on an Intel UHD laptop GPU with shadows on.
- **Zoom in and it gets more detailed.** Past a certain zoom, each model mounts a second layer (ladders, walkways, railings, spreader twistlocks, wheel hubs, lifeboats, mooring lines) and people appear (lashers under the cranes, gate clerks, customs officers, yard checkers).
- **Quality adapts.** It moves between flat, shadows, and shadows with ambient occlusion and tilt-shift depending on frame rate. Pin a level with `?q=0`, `?q=1` or `?q=2`.

## Architecture

```
browser ── Vite + React Three Fiber ─── WS /ws (tracks + 0.5 s stats) ── REST /api
api ────── FastAPI, one asyncio loop ── SimPy, stepped every 20 ms
              └─ psycopg 3 async pool: up to 16 transactions in flight
db ─────── PostgreSQL 16 ── ENUMs · composite keys · CHECKs · deferred constraint triggers ·
                            gist exclusion constraints · partial indexes
```

SimPy processes are synchronous generators and the database driver is async. The bridge is small. A process yields `self.db(fn, ...)`, which runs `fn` as an asyncio task in its own pooled transaction and resumes the process with the result, or throws Postgres's error into it. With 6 quay cranes, 16 AGVs, 6 yard cranes, 2 rail cranes and up to 10 lorries running at once, the locking is genuinely contended.

### The schema at a glance

| Rule | How it's enforced |
|---|---|
| One box per yard slot, ship cell and wagon slot | Composite foreign keys + `UNIQUE`, coordinates all-or-nothing via `num_nulls()` |
| Lifecycle per direction | Transition table `(from, to, flows[])` checked by trigger: imports never load onto a ship, exports never leave by road |
| No floating boxes, yard or ship | Deferred trigger: every touched stack satisfies `count(*) = max(tier)` at commit |
| Heavy under light, later port under earlier | Deferred trigger comparing neighbouring cells |
| Import and export stacks kept apart; hazmat only in two blocks | Trigger on block assignment |
| One ship per berth, and it has to fit | Partial unique index + trigger |
| Customs | `CHECK (NOT customs_hold OR inspected_at IS NOT NULL OR state NOT IN ('GATE_OUT','ON_RAIL','RAIL_OUT'))` |
| Real container numbers | `CHECK (iso6346_valid(id))`, the ISO 6346 check digit in PL/pgSQL |

Three hand-written Alembic migrations hold all of it ([`backend/alembic/versions`](backend/alembic/versions)), with SQLAlchemy models kept in step by a test. The full design notes are in [BACKBONE.md](BACKBONE.md).

## Run it

**Docker**

```bash
docker compose up --build        # web on :8080, Postgres on :5432 (port / port)
```

**Local**

```bash
cd backend
uv sync
uv run pytest                    # 24 tests against a throwaway Postgres
DATABASE_URL=postgresql://port:port@localhost:5432/port uv run alembic upgrade head
DATABASE_URL=… uv run python -m portdb.api

cd ../frontend
npm install
npm run dev                      # :5173, proxies /api and /ws to :8000
```

The API resets the terminal and restocks the yard on every start, because a half-finished simulation can't be resumed.

## Tests

Twenty-four tests run against a real Postgres. Most try to break a rule and expect a specific error:
- two boxes in one slot
- a floating box
- an import loaded onto a ship
- a heavy box on a light one
- a Gdansk box over a Rotterdam box
- an export in an import block
- two ships at one berth
- overlapping road bookings
- one crane sweeping through another

The last test runs the whole terminal headless for thirteen simulated minutes. It must discharge, load, gate out and rail out with zero refused transactions, and it must show vehicles giving way to each other.

## Limits

Twenty-foot boxes take a full cell rather than half of one. Customs never fails a box. Ship stability is only heavy-under-light. Lorries don't overtake on the through lane. The tuning in [`config.py`](backend/portdb/config.py) is chosen to make a port call last minutes rather than days.

## Built with

PostgreSQL 16 · Python 3.12 · SimPy · FastAPI · psycopg 3 · SQLAlchemy · Alembic · React · Three.js · React Three Fiber · Vite
