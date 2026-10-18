<h1 align="center">PortDB</h1>

<p align="center">
A container terminal where PostgreSQL decides what is allowed to happen.<br>
Three berths, six cranes on one rail, sixteen robot trucks, a rail terminal, customs, weather and breakdowns,<br>
all simulated, all committed as transactions, all rendered as a small model port you can rewind.
</p>

<p align="center">
<a href="https://github.com/JFrusher/PortDB/actions/workflows/ci.yml"><img src="https://github.com/JFrusher/PortDB/actions/workflows/ci.yml/badge.svg" alt="ci"></a>
<a href="https://render.com/deploy?repo=https://github.com/JFrusher/PortDB"><img src="https://render.com/images/deploy-to-render-button.svg" alt="Deploy to Render" height="20"></a>
</p>

<p align="center"><img src="docs/media/hero.gif" width="960" alt="The yard at work: gantry cranes stacking liveried containers, AGVs on the roads, a train on the siding"></p>

```bash
docker compose up --build     # then open http://localhost:8080
```

---

## The idea

Most simulations keep the rules in application code and use the database to store results. PortDB does it the other way round. The SimPy engine proposes moves, and Postgres accepts or refuses them:

- A box cannot be stacked on thin air, and a 40 ft box cannot sit on 20s.
- A heavy box cannot go on top of a light one aboard ship, and a box for Hamburg cannot go above one for Rotterdam.
- Two robot trucks cannot hold the same piece of road at the same instant, and two quay cranes cannot pass on their shared rail.
- No crane lifts in wind above its rated limit. Equipment that has broken down takes no work.
- No lorry checks in while the gate is down.
- A flammable liquid cannot be stacked next to an oxidiser.
- Two ships cannot be planned onto the same stretch of quay at the same time.
- A customs officer's query returns held boxes only; a shipping line's returns its own.

Every rule is a constraint, a trigger, an exclusion index or a row-level security policy, and every one has a test that tries to break it against a real Postgres. A property test throws random move sequences at the schema and checks afterwards, in plain SQL, that nothing invalid was ever committed.

The browser knows nothing the database doesn't. Box changes reach it from the database's own change feed, and any moment of the last half hour can be replayed from the database's history.

## How a box moves

```
import    ship ─QC─▶ AGV ─RTG─▶ import yard ─┬─RTG─▶ lorry ─(X-ray if held)─▶ out-gate
                                              └─RTG─▶ AGV ─(X-ray if held)─RMG─▶ wagon ─▶ train
export    lorry ─RTG─▶ export yard           train ─RMG─▶ AGV ─RTG─▶ export yard
          export yard ─RTG─▶ AGV ─▶ QC ─▶ ship ─▶ sails
tranship  ship ─QC─▶ AGV ─RTG─▶ export yard ─▶ onto a later ship
```

Ships are discharged and loaded with exports that satisfy the stowage rules; once a crane has emptied a bay it starts loading it while still discharging the rest (dual cycling). Trains arrive with exports and leave with rail-booked imports. Lorries queue at the gate, are served under a yard crane, pass customs if needed and leave.

<p align="center"><img src="docs/media/closeup.gif" width="960" alt="Quay cranes working a ship of liveried containers, AGVs underneath"></p>

## Worth reading the code for

### 1. Traffic control is an exclusion constraint

Before an AGV or a lorry moves, it books every road cell on its route for the time it will be there. A booking is a rectangle in (cell, time), and the table refuses overlapping rectangles:

```sql
CREATE TABLE road_bookings (
  id bigserial PRIMARY KEY, vehicle text NOT NULL,
  x smallint NOT NULL, z smallint NOT NULL,
  t0 double precision NOT NULL, t1 double precision NOT NULL,
  CONSTRAINT road_bookings_no_overlap
    EXCLUDE USING gist (box(point(cell_key(x, z), t0), point(cell_key(x, z) + 0.5, t1)) WITH &&)
);
```

It runs on stock Postgres, no extension: the built-in gist operator class for `box` does the work. Vehicles only ever wait on dead-end spurs, so a parked truck can slow traffic but never trap it, and bookings are inserted in cell order so two writers can never deadlock on each other's uncommitted rows.

Press **X-ray** in the diorama and the bookings are drawn as they sit in the table, with time as height: each column is one row, rising from now into the next twelve seconds. The columns never intersect, which is the constraint at work. Cranes' rail envelopes show as bands, and every transaction the database refuses flashes red where it happened, labelled with the rule that refused it.

<p align="center"><img src="docs/media/xray.gif" width="960" alt="X-ray view: road bookings as space-time columns, crane rail envelopes, refusals labelled by constraint"></p>

### 2. Cranes that can't overtake

Six quay cranes share one rail. Each owns a stretch of it stored as a `numrange`, and `EXCLUDE USING gist (rail_span WITH &&)` keeps the stretches apart. To travel, a crane claims everything between where it is and where it's going; passing a neighbour would mean claiming rail the neighbour holds, so it is impossible by construction. A crane with nothing to do steps aside when a neighbour is blocked on its rail, and if it is boxed in too, its own neighbour does the same.

### 3. Rewind any moment

Every committed version of every box lands in a temporal table, valid over a range of simulated seconds:

```sql
CREATE TABLE container_history (
  container_id char(11) NOT NULL,
  valid        numrange NOT NULL,          -- [from, to) in simulated seconds
  state        container_state NOT NULL,
  snapshot     jsonb NOT NULL
) PARTITION BY RANGE ((lower(valid)));
CREATE INDEX ON container_history USING gist (valid);
```

"Where was everything at t?" is `WHERE valid @> t`, one GiST probe. Everything else the engine streams (vehicle and crane tracks, ship and train calls, weather) goes into `event_log`, written in half-second batches with `COPY` and indexed with BRIN, which stays a few pages long however big the table gets because rows arrive in time order. Both tables are partitioned into five-minute slices made ahead of the clock. Retention drops whole partitions, after carrying any still-open versions forward so the oldest replayable moment still has every box in it.

Drag the scrubber and the diorama rebuilds itself from the history, then plays forward from the log through the same code path as live. Pick any box and its journey is listed from the history and drawn from the log; click a step to rewind the whole terminal to it.

<p align="center"><img src="docs/media/replay.gif" width="960" alt="Replaying the terminal from two minutes ago"></p>

The history is written by a deferred trigger that also announces the change with `pg_notify` at commit, so box state reaches the browser from the database's change feed rather than from the engine. Several updates to one box in one transaction publish once, because the trigger reads the row as committed.

### 4. Weather and breakdowns are rows

Wind readings are rows in `weather`; crane ratings are rows in `crane_wind_limits`; a breakdown is `equipment.state = 'DOWN'`; a gate outage is a `numrange` in `gate_outages`. Every lift is a task row, and a trigger on that table refuses it:

```
wind_limit: QC-3 cannot lift in 23.4 m/s wind (limit 20 m/s)
equipment_down: QC-5 is out of service
gate_closed: in-gate down (barrier fault) until t=412
```

The engine stamps each transaction with the simulated second it acts at (`set_config('portdb.sim_t', …, true)`), so the triggers judge time-dependent rules themselves. When a lift is refused the crane holds and asks again; the backlog builds on screen and clears when the wind drops. The **Chaos** tab raises storms, crane and AGV faults and gate outages on demand, or at random.

<p align="center"><img src="docs/media/storm.gif" width="960" alt="A storm at dusk: every crane held by the wind limit, floodlights on the yard"></p>

### 5. More rules the schema enforces

- **IMDG segregation.** Hazardous boxes carry a class. A segregation table (after IMDG 7.2.4: "away from" is one clear stack, "separated from" two) is checked at commit against every hazardous box nearby, with an advisory lock per block so two concurrent placements can't both slip in. The yard allocator reads the same table, so it never proposes a slot the trigger would refuse.
- **Berth windows in quay × time.** `EXCLUDE USING gist (quay WITH &&, during WITH &&)` on `int4range` × `numrange`. A call that runs over asks to extend its window; when that would overlap the next call the database refuses, and the engine pushes the next call back first. The **Berths** tab draws the plan as a Gantt chart.
- **Row-level security.** Customs, gate clerks and shipping lines are database roles with policies on `containers`. The API runs one `SELECT` under `SET LOCAL ROLE`; **view as** in the diorama greys out every box the chosen role can't see.
- **Stowage at commit.** Heavy under light and later port under earlier, re-checked by a deferred trigger against the cells above and below.
- **One length per stack.** A 40 needs corner castings at both ends beneath it, so stacks hold 40s or fore-and-aft pairs of 20s, never a mix. Bays print as real ISO bay numbers, computed in SQL.

### 6. Digging a box out: the recursive CTE

When a lorry wants a buried box, the yard crane needs the boxes above it, top first. A recursive CTE climbs one tier at a time and stops at the first gap; the gravity trigger guarantees there are no gaps, so it never needs to look further. Click any box and the inspector runs it live with its `EXPLAIN ANALYZE`, next to the box's history.

<p align="center"><img src="docs/media/rehandle.gif" width="960" alt="A yard crane re-handling boxes to reach one underneath"></p>

<p align="center"><img src="docs/media/inspector.png" width="960" alt="Inspector: a box re-handled five times, its journey from container_history, pg_stat_database and refusal counts"></p>

## Measured

`python -m portdb.bench` runs the whole simulation headless against fresh databases, storms included, three runs of 25 simulated minutes per variant:

| Variant | Re-handles per 100 yard retrievals | Quay crane moves / hour |
|---|---:|---:|
| Basic placement: fill each stack to tier 3 in order | 86 | 472 |
| Smart placement: on a box that leaves later, lowest tier first | **44** | 497 |
| Smart placement, dual cycling off | 50 | 510 |

Smart placement halves re-handles. For exports it puts each box on one the load planner will want later (earlier port, or same port and lighter); for imports it groups by onward mode and keeps stacks low, because lorry pick-ups arrive in random order and every tier saved is a re-handle saved. The **Yard** tab switches strategies live and charts re-handles per minute in the strategy's colour.

Dual cycling made no measurable difference to crane throughput here. The quay is limited by the AGVs feeding it, not by the cranes' empty return trips, so saving trolley time buys nothing until there are more AGVs. It stays on because it shortens a call when AGVs are free.

## What broke, and what caught it

Each of these was a real bug in the engine, stopped by the database or by a test rather than found later as a corrupt yard.

| Symptom | What caught it | Fix |
|---|---|---|
| A crane took the box *under* one another crane was still lifting | `stack_gravity: vessel stack 2-3-2 has 4 boxes but top tier 5` | Claim only boxes with nothing above them in any state |
| Two vehicles booking overlapping cells in opposite order | `deadlock detected … while checking exclusion constraint` | Insert bookings in cell order; retry deadlock victims |
| Two neighbouring cranes widening their claims toward each other | Deadlock on `equipment_qc_rail_no_crossing` | Serialise claims per rail with `pg_advisory_xact_lock` |
| One unretried deadlock leaked an AGV and froze the quay | (the quay went silent) | Retry whole-transaction deadlock victims; every job returns its AGV on failure |
| Dual cycling: a crane tried to discharge the exports it had just loaded | `illegal_transition: EXPORT … STOWED_ON_BOARD -> DISCHARGING` | Discharge only looks in bays that still hold inbound cargo |
| An idle crane parked where its neighbour needed to load; reallocation waited for that load | Headless run: quay frozen from t=540 | Idle cranes step aside for a blocked neighbour |

## The diorama

<p align="center"><img src="docs/media/landside.gif" width="960" alt="A train loaded by the rail crane, then the customs X-ray scanning a lorry"></p>

- **Server owns time.** Each movement is sent once as a path with start and end times in simulated seconds; the browser interpolates against its own copy of the clock. Spreaders swing as the trolley runs and settle as they lower, and the box swings with them.
- **A simulated day.** The sun crosses in fifteen minutes, floodlights pool on the yard at night, ships show navigation lights, and fog, rain and a choppy harbour follow the weather rows. Tugs bring ships alongside, leaving a wake.
- **Colour by data.** Boxes wear their shipping line's livery from a texture atlas, one tile index per instance. Switch to port of discharge, weight, yard dwell, re-handles or reefer power, read straight from the change feed.
- **Few draw calls.** Every model is one merged vertex-coloured mesh and all containers are one instanced mesh, so it stays smooth on integrated graphics. Quality adapts to the frame rate; pin it with `?q=0`, `?q=1` or `?q=2`.

## Architecture

```
browser ── Vite + React Three Fiber ── WS /ws (tracks, calls, box changes, stats) ── REST /api
api ────── FastAPI, one asyncio loop ── SimPy, stepped every 20 ms
              ├─ psycopg 3 async pool: up to 16 transactions in flight
              ├─ LISTEN container_feed ──▶ box changes to every browser
              └─ COPY event_log every 0.5 s
db ─────── PostgreSQL 16 ── constraint triggers · gist exclusion on box, numrange, int4range ·
                            range partitions · BRIN · pg_notify · row-level security
```

A SimPy process yields `self.db(fn, ...)`, which runs `fn` as an asyncio task in its own pooled transaction and resumes the process with the result, or throws Postgres's error into it. With 6 quay cranes, 16 AGVs, 6 yard cranes, 2 rail cranes and up to 10 lorries at once, the locking is genuinely contended. The **Database** tab shows what Postgres reports about it: commits, rollbacks, deadlocks, lock waits, the history store's size, refusals by rule and, under Compose, the heaviest statements from `pg_stat_statements`.

Five hand-written Alembic migrations hold the schema ([`backend/alembic/versions`](backend/alembic/versions)), with SQLAlchemy models kept in step by a test. Design notes are in [BACKBONE.md](BACKBONE.md).

## Run it

```bash
docker compose up --build        # web on :8080, Postgres on :5432 (port / port)
```

Locally:

```bash
cd backend
uv sync
uv run pytest                    # 39 tests against a throwaway Postgres
DATABASE_URL=postgresql://port:port@localhost:5432/port uv run alembic upgrade head
DATABASE_URL=… uv run python -m portdb.api

cd ../frontend
npm install
npm run dev                      # :5173, proxies /api and /ws to :8000
```

The API resets the terminal on every start, because a half-finished simulation can't be resumed.

**Deploy your own** on Supabase (Postgres) and Render (API and diorama):

1. Create a Supabase project. From **Connect**, copy the **Session pooler** string (port 5432) and put the database password in it. The transaction pooler (6543) won't do: the change feed holds a `LISTEN` session and every transaction sets `portdb.sim_t`.
2. On Render, **New → Blueprint** on this repo. [`render.yaml`](render.yaml) creates both services and asks for `DATABASE_URL`: paste the string. The API runs the migrations on boot.

On a Postgres without `CREATEROLE` the row-level security roles are skipped with a notice and everything else works.

## Tests and CI

Thirty-nine tests run against a real Postgres. Most try to break one rule and expect its error: a floating box, a 40 on 20s, a heavy box on a light one, two vehicles in one cell, one crane sweeping through another, a lift in a gale, work for a broken crane, a check-in during an outage, an oxidiser beside a flammable liquid, two calls on one stretch of quay, two incompatible hazmat placements racing each other. Others check the history answers "where was it at t", the change feed publishes once per commit, retention keeps every box, and each role sees only its rows.

`test_fuzz.py` uses Hypothesis to generate random sequences of yard, ship, wagon and lift moves, most of them illegal, and then checks eight invariants in plain SQL. `test_sim.py` runs the whole terminal headless for thirteen simulated minutes and requires zero unexpected refusals.

GitHub Actions runs the tests against a Postgres service container, builds the frontend, and boots the Compose stack to check it serves. A manual workflow re-records this README's footage with Playwright ([`scripts/capture.js`](scripts/capture.js)).

## Limits

Customs never fails a box. Ship stability is only heavy-under-light, and IMDG segregation is enforced in the yard, not aboard. Lorries don't overtake. The tuning in [`config.py`](backend/portdb/config.py) makes a port call last minutes rather than days.

## Built with

PostgreSQL 16 · Python 3.12 · SimPy · FastAPI · psycopg 3 · SQLAlchemy · Alembic · Hypothesis · React · Three.js · React Three Fiber · Vite
