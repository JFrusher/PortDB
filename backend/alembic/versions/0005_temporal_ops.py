"""Time travel, change feed and operating rules.

* container_history: every committed version of every box, valid over a range of simulated seconds,
  partitioned by when it began. "Where was everything at t?" is one indexed query.
* event_log: everything the engine streamed (vehicle tracks, ship and train calls, weather), partitioned
  the same way and BRIN-indexed on time. History gives the snapshot, the log gives the motion between.
* container_feed: one view shapes a box for the wire, the history and the live snapshot. A deferred
  trigger publishes each committed change with pg_notify, so the browser hears about a move when it commits.
* Weather and breakdowns: no crane lifts above its wind limit and no equipment that is down takes work.
* IMDG segregation by class distance in the yard; berth windows that cannot overlap in quay x time.
* Row-level security: customs, gate clerks and shipping lines see different rows through the same query.

Revision ID: 0005
Revises: 0004
"""
from alembic import op

revision = "0005"
down_revision = "0004"

NEW_LABELS = [("equipment_state", "DOWN")]

UPGRADE = r"""
-- the simulated second a transaction acts at: the engine sets it per transaction, tests leave it at 0
CREATE FUNCTION sim_now() RETURNS double precision
LANGUAGE sql STABLE AS $$ SELECT coalesce(nullif(current_setting('portdb.sim_t', true), '')::double precision, 0) $$;

-- containers: hazard class, yard dwell clock, re-handle count ---------------------------------
ALTER TABLE containers
  ADD COLUMN imdg_class text CHECK (imdg_class IN ('2.1','3','4.1','5.1','6.1','8','9')),
  ADD COLUMN yard_in_t  double precision,
  ADD COLUMN rehandles  smallint NOT NULL DEFAULT 0;
UPDATE containers SET imdg_class = '9' WHERE hazmat;
ALTER TABLE containers ADD CONSTRAINT containers_imdg_class CHECK (hazmat = (imdg_class IS NOT NULL));

ALTER TABLE gate_transactions ADD COLUMN vehicle text;
CREATE INDEX gate_transactions_box ON gate_transactions (container_id, id);
CREATE INDEX equipment_tasks_box ON equipment_tasks (container_id);
-- the ledger only grows, in time order: a BRIN index is a few pages for any number of rows
CREATE INDEX equipment_tasks_sim_t ON equipment_tasks USING brin (sim_t0);

CREATE FUNCTION stamp_yard_in() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.state = 'STACKED_IN_YARD' AND (TG_OP = 'INSERT' OR OLD.state NOT IN ('STACKED_IN_YARD', 'RETRIEVING')) THEN
    NEW.yard_in_t := sim_now();
  ELSIF NEW.block_code IS NULL THEN
    NEW.yard_in_t := NULL;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_stamp_yard_in BEFORE INSERT OR UPDATE OF state ON containers
  FOR EACH ROW EXECUTE FUNCTION stamp_yard_in();

-- one shape for a box everywhere it is published. security_invoker: row-level security applies
-- to whoever reads the view, not to its owner
CREATE VIEW container_feed WITH (security_invoker = true) AS
SELECT c.id, c.iso_type, c.hazmat, c.imdg_class, c.weight_kg, c.state, c.flow, c.mode, c.pod, c.customs_hold,
       c.inspected_at IS NOT NULL AS inspected,
       c.vessel_id, c.vessel_bay, c.vessel_row, c.vessel_tier, c.vessel_half,
       c.block_code, c.bay, c.row, c.tier, c.half,
       coalesce(c.carrier_id, CASE WHEN c.state IN ('ON_TRUCK', 'GATE_OUT') THEN
         (SELECT g.vehicle FROM gate_transactions g WHERE g.container_id = c.id ORDER BY g.id DESC LIMIT 1) END) AS carrier_id,
       c.train_id, c.wagon, c.wagon_slot, c.yard_in_t, c.rehandles
FROM containers c;

-- temporal history ---------------------------------------------------------------------------
CREATE TABLE container_history (
  container_id char(11) NOT NULL,
  valid        numrange NOT NULL CHECK (NOT isempty(valid)),     -- simulated seconds, [from, to)
  state        container_state NOT NULL,
  snapshot     jsonb NOT NULL                                     -- the container_feed row as it stood
) PARTITION BY RANGE ((lower(valid)));
CREATE INDEX container_history_valid ON container_history USING gist (valid);
CREATE INDEX container_history_box ON container_history (container_id, (lower(valid)));
CREATE INDEX container_history_open ON container_history (container_id) WHERE upper_inf(valid);

CREATE TABLE event_log (
  t     double precision NOT NULL,
  type  text NOT NULL,
  unit  text,
  body  jsonb NOT NULL
) PARTITION BY RANGE (t);
CREATE INDEX event_log_t ON event_log USING brin (t);
CREATE INDEX event_log_unit ON event_log (unit, t) WHERE unit IS NOT NULL;

-- a safety net: rows land here only if maintenance ever falls behind
CREATE TABLE container_history_default PARTITION OF container_history DEFAULT;
CREATE TABLE event_log_default PARTITION OF event_log DEFAULT;

-- five-minute partitions, created ahead of the clock by the engine's maintenance task
CREATE FUNCTION ensure_partitions(t_from double precision, t_to double precision) RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
  k int;
  parent text;
  made int := 0;
BEGIN
  FOR k IN floor(greatest(t_from, 0) / 300)::int .. floor(greatest(t_to, 0) / 300)::int LOOP
    FOREACH parent IN ARRAY ARRAY['container_history', 'event_log'] LOOP
      IF to_regclass(format('%s_p%s', parent, k)) IS NULL THEN
        EXECUTE format('CREATE TABLE %I PARTITION OF %I FOR VALUES FROM (%s) TO (%s)',
                       parent || '_p' || k, parent, k * 300, (k + 1) * 300);
        made := made + 1;
      END IF;
    END LOOP;
  END LOOP;
  RETURN made;
END $$;

-- retention: drop whole partitions older than the cutoff. History rows still valid at the cutoff
-- are carried forward first, so the oldest replayable moment still has every box in it.
CREATE FUNCTION drop_history_before(cutoff double precision) RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
  part record;
  dropped int := 0;
BEGIN
  FOR part IN
    SELECT c.relname, p.relname AS parent, substring(c.relname FROM '_p(\d+)$')::int AS k
    FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid JOIN pg_class p ON p.oid = i.inhparent
    WHERE p.relname IN ('container_history', 'event_log') AND c.relname ~ '_p\d+$'
    ORDER BY 3
  LOOP
    CONTINUE WHEN (part.k + 1) * 300 > cutoff;
    IF part.parent = 'container_history' THEN
      EXECUTE format('INSERT INTO container_history (container_id, valid, state, snapshot)
                      SELECT container_id, numrange(%s, upper(valid)), state, snapshot FROM %I
                      WHERE upper_inf(valid) OR upper(valid) > %s', cutoff, part.relname, cutoff);
    END IF;
    EXECUTE format('DROP TABLE %I', part.relname);
    dropped := dropped + 1;
  END LOOP;
  RETURN dropped;
END $$;

SELECT ensure_partitions(0, 600);

-- change feed: at commit, record the new version and announce it. Deferred, and it reads the row
-- as committed, so several updates to one box in one transaction publish once.
CREATE FUNCTION publish_container() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  t numeric := sim_now();
  cur jsonb;
  st container_state;
  o record;
BEGIN
  SELECT to_jsonb(f), f.state INTO cur, st FROM container_feed f WHERE f.id = NEW.id;
  IF cur IS NULL THEN
    RETURN NULL;
  END IF;
  SELECT lower(valid) AS t0, snapshot INTO o FROM container_history
    WHERE container_id = NEW.id AND upper_inf(valid) FOR UPDATE;
  IF FOUND AND o.snapshot = cur THEN
    RETURN NULL;
  END IF;
  IF FOUND AND o.t0 >= t THEN
    UPDATE container_history SET snapshot = cur, state = st WHERE container_id = NEW.id AND upper_inf(valid);
  ELSE
    IF FOUND THEN
      UPDATE container_history SET valid = numrange(o.t0, t) WHERE container_id = NEW.id AND upper_inf(valid);
    END IF;
    INSERT INTO container_history (container_id, valid, state, snapshot) VALUES (NEW.id, numrange(t, NULL), st, cur);
  END IF;
  PERFORM pg_notify('container_feed', jsonb_build_object('t', t, 'box', cur)::text);
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_publish_container
  AFTER INSERT OR UPDATE ON containers
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION publish_container();

-- weather, wind limits, breakdowns ------------------------------------------------------------
CREATE TABLE weather (
  t       double precision PRIMARY KEY,
  wind_ms real NOT NULL CHECK (wind_ms >= 0),
  gust_ms real NOT NULL CHECK (gust_ms >= wind_ms),
  rain    real NOT NULL CHECK (rain BETWEEN 0 AND 1),
  fog     real NOT NULL CHECK (fog BETWEEN 0 AND 1)
);

CREATE TABLE crane_wind_limits (
  kind        equipment_kind PRIMARY KEY CHECK (kind IN ('QC', 'RTG', 'RMG')),
  max_wind_ms real NOT NULL CHECK (max_wind_ms > 0)
);
INSERT INTO crane_wind_limits VALUES ('QC', 20), ('RTG', 22), ('RMG', 24);

CREATE FUNCTION wind_at(at double precision) RETURNS real
LANGUAGE sql STABLE AS $$ SELECT wind_ms FROM weather WHERE t <= at ORDER BY t DESC LIMIT 1 $$;

-- every lift is a task row: equipment that is down takes none, and no crane lifts in a gale
CREATE FUNCTION enforce_lift_rules() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  e record;
  w real;
  lim real;
BEGIN
  SELECT kind, state INTO e FROM equipment WHERE id = NEW.equipment_id;
  IF e.state = 'DOWN' THEN
    RAISE EXCEPTION 'equipment_down: % is out of service', NEW.equipment_id USING ERRCODE = 'check_violation';
  END IF;
  SELECT max_wind_ms INTO lim FROM crane_wind_limits WHERE kind = e.kind;
  IF lim IS NOT NULL THEN
    w := wind_at(sim_now());
    IF w > lim THEN
      RAISE EXCEPTION 'wind_limit: % cannot lift in % m/s wind (limit % m/s)', NEW.equipment_id, round(w::numeric, 1), lim
        USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_lift_rules BEFORE INSERT ON equipment_tasks
  FOR EACH ROW EXECUTE FUNCTION enforce_lift_rules();

-- a crane that is down cannot claim more rail
CREATE FUNCTION enforce_rail_while_down() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.state = 'DOWN' AND NOT (OLD.rail_span @> NEW.rail_span) THEN
    RAISE EXCEPTION 'equipment_down: % is out of service', NEW.id USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_rail_while_down BEFORE UPDATE OF rail_span ON equipment
  FOR EACH ROW EXECUTE FUNCTION enforce_rail_while_down();

-- re-handles are counted where they are recorded
CREATE FUNCTION count_rehandle() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  UPDATE containers SET rehandles = rehandles + 1 WHERE id = NEW.container_id;
  RETURN NULL;
END $$;
CREATE TRIGGER trg_count_rehandle AFTER INSERT ON equipment_tasks
  FOR EACH ROW WHEN (NEW.kind = 'RESHUFFLE') EXECUTE FUNCTION count_rehandle();

-- gate outages: no lorry checks in while the in-gate is down
CREATE TABLE gate_outages (
  id     serial PRIMARY KEY,
  during numrange NOT NULL,
  reason text NOT NULL,
  CONSTRAINT gate_outages_no_overlap EXCLUDE USING gist (during WITH &&)
);
CREATE FUNCTION enforce_gate_open() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  o record;
BEGIN
  SELECT reason, upper(during) AS until INTO o FROM gate_outages WHERE during @> sim_now()::numeric;
  IF FOUND THEN
    RAISE EXCEPTION 'gate_closed: in-gate down (%) until t=%', o.reason, round(o.until) USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_gate_open BEFORE UPDATE OF status ON gate_transactions
  FOR EACH ROW WHEN (NEW.status = 'AT_LANE') EXECUTE FUNCTION enforce_gate_open();

-- IMDG segregation in the yard ------------------------------------------------------------------
-- After IMDG 7.2.4: 1 = "away from" (not in the same or a neighbouring stack), 2 = "separated from"
-- (two clear stacks). Pairs not listed may share a stack.
CREATE TABLE imdg_segregation (
  class_a text NOT NULL,
  class_b text NOT NULL,
  code    smallint NOT NULL CHECK (code IN (1, 2)),
  PRIMARY KEY (class_a, class_b),
  CHECK (class_a <= class_b)
);
INSERT INTO imdg_segregation VALUES
  ('2.1','3',2), ('2.1','4.1',1), ('2.1','5.1',2), ('2.1','8',1),
  ('3','5.1',2), ('4.1','5.1',1), ('4.1','8',1), ('5.1','6.1',1), ('5.1','8',2), ('2.1','2.1',1);

CREATE FUNCTION enforce_imdg_segregation() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  me record;
  r record;
BEGIN
  SELECT id, imdg_class, block_code, bay, row INTO me FROM containers WHERE id = NEW.id;
  IF me.imdg_class IS NULL OR me.block_code IS NULL THEN
    RETURN NULL;
  END IF;
  -- hazmat placements in one block take turns, so the check below sees the other's commit
  PERFORM pg_advisory_xact_lock(hashtext('imdg:' || me.block_code));
  SELECT o.id, o.imdg_class, s.code INTO r
  FROM containers o
  JOIN imdg_segregation s ON (s.class_a, s.class_b) = (least(o.imdg_class, me.imdg_class), greatest(o.imdg_class, me.imdg_class))
  WHERE o.block_code = me.block_code AND o.id <> me.id
    AND greatest(abs(o.bay - me.bay), abs(o.row - me.row)) <= s.code
  LIMIT 1;
  IF FOUND THEN
    RAISE EXCEPTION 'imdg_segregation: % (class %) must be % % (class %)', me.id, me.imdg_class,
      CASE r.code WHEN 1 THEN 'away from' ELSE 'separated from' END, r.id, r.imdg_class
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_imdg_segregation
  AFTER INSERT OR UPDATE OF block_code, bay, row, imdg_class ON containers
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION enforce_imdg_segregation();

-- berth plan: a window of quay metres x simulated time; no two may overlap -----------------------
CREATE TABLE berth_windows (
  id          serial PRIMARY KEY,
  berth_id    smallint NOT NULL REFERENCES berths,
  vessel_name text NOT NULL,
  vessel_id   integer REFERENCES vessels,
  quay        int4range NOT NULL,
  during      numrange NOT NULL CHECK (NOT isempty(during)),
  status      text NOT NULL DEFAULT 'PLANNED' CHECK (status IN ('PLANNED', 'ALONGSIDE', 'DONE')),
  CONSTRAINT berth_windows_no_overlap EXCLUDE USING gist (quay WITH &&, during WITH &&)
);

-- row-level security: one query, three audiences ------------------------------------------------
-- The engine owns the table and is not subject to these; they bind whoever SET ROLEs into them.
-- Managed Postgres without CREATEROLE skips this block (and the "view as" preview) with a notice.
ALTER TABLE containers ENABLE ROW LEVEL SECURITY;
DO $$
DECLARE
  r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['portdb_customs', 'portdb_gate', 'portdb_line'] LOOP
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('CREATE ROLE %I NOLOGIN', r);
    END IF;
    EXECUTE format('GRANT %I TO CURRENT_USER', r);
    EXECUTE format('GRANT USAGE ON SCHEMA public TO %I', r);
    EXECUTE format('GRANT SELECT ON containers, container_feed, gate_transactions, vessels, trains, ports TO %I', r);
  END LOOP;
  CREATE POLICY customs_sees_held ON containers FOR SELECT TO portdb_customs
    USING (customs_hold OR hazmat);
  CREATE POLICY gate_sees_road ON containers FOR SELECT TO portdb_gate
    USING (mode = 'TRUCK' OR EXISTS (SELECT 1 FROM gate_transactions g WHERE g.container_id = containers.id));
  CREATE POLICY line_sees_own ON containers FOR SELECT TO portdb_line
    USING (left(id, 3) = current_setting('portdb.line', true));
EXCEPTION WHEN insufficient_privilege THEN
  RAISE NOTICE 'row-level security roles skipped: %', SQLERRM;
END $$;
"""

DOWNGRADE = r"""
DROP POLICY IF EXISTS line_sees_own ON containers;
DROP POLICY IF EXISTS gate_sees_road ON containers;
DROP POLICY IF EXISTS customs_sees_held ON containers;
ALTER TABLE containers DISABLE ROW LEVEL SECURITY;
DO $$
DECLARE
  r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['portdb_customs', 'portdb_gate', 'portdb_line'] LOOP
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %I', r);
      EXECUTE format('REVOKE USAGE ON SCHEMA public FROM %I', r);
      EXECUTE format('DROP ROLE %I', r);
    END IF;
  END LOOP;
END $$;
DROP TABLE berth_windows;
DROP TRIGGER trg_imdg_segregation ON containers;
DROP FUNCTION enforce_imdg_segregation;
DROP TABLE imdg_segregation;
DROP TRIGGER trg_gate_open ON gate_transactions;
DROP FUNCTION enforce_gate_open;
DROP TABLE gate_outages;
DROP TRIGGER trg_count_rehandle ON equipment_tasks;
DROP FUNCTION count_rehandle;
DROP TRIGGER trg_rail_while_down ON equipment;
DROP FUNCTION enforce_rail_while_down;
DROP TRIGGER trg_lift_rules ON equipment_tasks;
DROP FUNCTION enforce_lift_rules;
DROP FUNCTION wind_at;
DROP TABLE crane_wind_limits, weather;
DROP TRIGGER trg_publish_container ON containers;
DROP FUNCTION publish_container;
DROP FUNCTION drop_history_before, ensure_partitions;
DROP TABLE event_log, container_history;
DROP VIEW container_feed;
DROP TRIGGER trg_stamp_yard_in ON containers;
DROP FUNCTION stamp_yard_in;
DROP INDEX equipment_tasks_sim_t, equipment_tasks_box, gate_transactions_box;
ALTER TABLE gate_transactions DROP COLUMN vehicle;
ALTER TABLE containers DROP CONSTRAINT containers_imdg_class,
  DROP COLUMN imdg_class, DROP COLUMN yard_in_t, DROP COLUMN rehandles;
DROP FUNCTION sim_now;
-- equipment_state 'DOWN' stays: PostgreSQL cannot drop enum values
"""


def upgrade():
    with op.get_context().autocommit_block():
        for enum, label in NEW_LABELS:
            op.execute(f"ALTER TYPE {enum} ADD VALUE IF NOT EXISTS '{label}'")
    op.execute(UPGRADE)


def downgrade():
    op.execute(DOWNGRADE)
