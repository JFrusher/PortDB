"""Terminal schema: spatial grid, container lifecycle, equipment ledger.

Revision ID: 0001
Revises:
"""
from alembic import op

revision = "0001"
down_revision = None

UPGRADE = r"""
CREATE TYPE container_state AS ENUM
  ('STOWED_ON_BOARD','DISCHARGING','IN_TRANSIT','STACKED_IN_YARD','RETRIEVING','GATE_OUT');
CREATE TYPE equipment_kind  AS ENUM ('QC','AGV','RTG');
CREATE TYPE equipment_state AS ENUM ('IDLE','BUSY');
CREATE TYPE task_kind       AS ENUM ('DISCHARGE','TRANSPORT','STACK','RESHUFFLE','RETRIEVE');
CREATE TYPE task_status     AS ENUM ('PENDING','IN_PROGRESS','DONE','FAILED');
CREATE TYPE vessel_status   AS ENUM ('INBOUND','BERTHED','DEPARTED');
CREATE TYPE lorry_status    AS ENUM ('QUEUED','AT_LANE','LOADED','DEPARTED');

-- ISO 6346: owner code (3) + category U + serial (6) + check digit.
-- Letters map A=10 .. Z=38 skipping multiples of 11; digit i weighted 2^i; sum mod 11 mod 10.
CREATE FUNCTION iso6346_valid(code text) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE STRICT AS $$
DECLARE
  total int := 0;
  n int;
BEGIN
  IF code !~ '^[A-Z]{3}U[0-9]{7}$' THEN
    RETURN false;
  END IF;
  FOR i IN 1..10 LOOP
    n := ascii(substr(code, i, 1));
    n := CASE WHEN n BETWEEN 48 AND 57 THEN n - 48 ELSE (n - 55) + (n - 56) / 10 END;
    total := total + n * (1 << (i - 1));
  END LOOP;
  RETURN total % 11 % 10 = substr(code, 11, 1)::int;
END $$;

CREATE TABLE terminals (
  id   smallserial PRIMARY KEY,
  code text UNIQUE NOT NULL,
  name text NOT NULL
);

CREATE TABLE yard_blocks (
  code          char(1) PRIMARY KEY,
  terminal_id   smallint NOT NULL REFERENCES terminals,
  bays          smallint NOT NULL CHECK (bays > 0),
  rows          smallint NOT NULL CHECK (rows > 0),
  tiers         smallint NOT NULL CHECK (tiers BETWEEN 1 AND 6),
  allows_hazmat boolean  NOT NULL DEFAULT false
);

CREATE TABLE yard_slots (
  block_code char(1)  NOT NULL REFERENCES yard_blocks ON DELETE CASCADE,
  bay        smallint NOT NULL CHECK (bay  > 0),
  row        smallint NOT NULL CHECK (row  > 0),
  tier       smallint NOT NULL CHECK (tier > 0),
  PRIMARY KEY (block_code, bay, row, tier)
);

CREATE TABLE vessels (
  id          serial PRIMARY KEY,
  name        text NOT NULL,
  imo         char(7) UNIQUE NOT NULL CHECK (imo ~ '^[0-9]{7}$'),
  bays        smallint NOT NULL,
  rows        smallint NOT NULL,
  tiers       smallint NOT NULL,
  status      vessel_status NOT NULL DEFAULT 'INBOUND',
  berthed_at  timestamptz,
  departed_at timestamptz
);

CREATE TABLE equipment (
  id         text PRIMARY KEY,
  kind       equipment_kind  NOT NULL,
  state      equipment_state NOT NULL DEFAULT 'IDLE',
  home_block char(1) REFERENCES yard_blocks,
  idle_since timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK ((kind = 'RTG') = (home_block IS NOT NULL))
);

CREATE TABLE containers (
  id          char(11) PRIMARY KEY CHECK (iso6346_valid(id)),
  iso_type    char(4)  NOT NULL CHECK (iso_type IN ('22G1','45G1','22R1','45R1')),
  weight_kg   integer  NOT NULL CHECK (weight_kg BETWEEN 2000 AND 32500),
  hazmat      boolean  NOT NULL DEFAULT false,
  state       container_state NOT NULL DEFAULT 'STOWED_ON_BOARD',
  vessel_id   integer REFERENCES vessels,
  vessel_bay  smallint,
  vessel_row  smallint,
  vessel_tier smallint,
  block_code  char(1),
  bay         smallint,
  row         smallint,
  tier        smallint,
  carrier_id  text REFERENCES equipment,
  CONSTRAINT containers_slot_fk FOREIGN KEY (block_code, bay, row, tier) REFERENCES yard_slots,
  CONSTRAINT containers_one_per_slot UNIQUE (block_code, bay, row, tier),
  CONSTRAINT containers_one_per_cell UNIQUE (vessel_id, vessel_bay, vessel_row, vessel_tier),
  CONSTRAINT containers_slot_complete CHECK (num_nulls(block_code, bay, row, tier) IN (0, 4)),
  CONSTRAINT containers_cell_complete CHECK (num_nulls(vessel_id, vessel_bay, vessel_row, vessel_tier) IN (0, 4)
                                             OR (vessel_id IS NOT NULL AND vessel_bay IS NULL
                                                 AND vessel_row IS NULL AND vessel_tier IS NULL)),
  CONSTRAINT containers_in_yard_has_slot CHECK ((state IN ('STACKED_IN_YARD','RETRIEVING')) = (block_code IS NOT NULL)),
  CONSTRAINT containers_on_board_has_cell CHECK ((state IN ('STOWED_ON_BOARD','DISCHARGING')) = (vessel_bay IS NOT NULL)),
  CONSTRAINT containers_in_transit_has_carrier CHECK ((state = 'IN_TRANSIT') = (carrier_id IS NOT NULL))
);

CREATE TABLE equipment_tasks (
  id           bigserial PRIMARY KEY,
  equipment_id text NOT NULL REFERENCES equipment,
  container_id char(11) NOT NULL REFERENCES containers,
  kind         task_kind   NOT NULL,
  status       task_status NOT NULL DEFAULT 'PENDING',
  origin       jsonb NOT NULL,
  target       jsonb NOT NULL,
  sim_t0       double precision,
  sim_t1       double precision,
  error        text,
  created_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
  finished_at  timestamptz
);

CREATE TABLE gate_transactions (
  id            bigserial PRIMARY KEY,
  plate         text NOT NULL,
  container_id  char(11) NOT NULL REFERENCES containers,
  lane          smallint CHECK (lane BETWEEN 1 AND 4),
  status        lorry_status NOT NULL DEFAULT 'QUEUED',
  shifter_moves smallint NOT NULL DEFAULT 0,
  arrived_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
  departed_at   timestamptz
);

-- Indexes ------------------------------------------------------------------
CREATE INDEX equipment_tasks_live ON equipment_tasks (equipment_id, status)
  WHERE status IN ('PENDING','IN_PROGRESS');
CREATE INDEX containers_on_board ON containers (vessel_id, vessel_bay, vessel_tier DESC)
  WHERE state = 'STOWED_ON_BOARD';
CREATE INDEX containers_in_yard ON containers (block_code) WHERE state = 'STACKED_IN_YARD';
CREATE UNIQUE INDEX gate_one_live_pickup ON gate_transactions (container_id) WHERE status <> 'DEPARTED';

-- Lifecycle: legal state edges live in data, the trigger only looks them up.
CREATE TABLE container_state_transitions (
  from_state container_state NOT NULL,
  to_state   container_state NOT NULL,
  PRIMARY KEY (from_state, to_state)
);
INSERT INTO container_state_transitions VALUES
  ('STOWED_ON_BOARD','DISCHARGING'),
  ('DISCHARGING','IN_TRANSIT'),
  ('IN_TRANSIT','STACKED_IN_YARD'),
  ('STACKED_IN_YARD','RETRIEVING'),
  ('RETRIEVING','GATE_OUT');

CREATE FUNCTION enforce_state_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM container_state_transitions
                 WHERE from_state = OLD.state AND to_state = NEW.state) THEN
    RAISE EXCEPTION 'illegal_transition: % % -> %', NEW.id, OLD.state, NEW.state
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER trg_state_transition
  BEFORE UPDATE OF state ON containers
  FOR EACH ROW WHEN (OLD.state IS DISTINCT FROM NEW.state)
  EXECUTE FUNCTION enforce_state_transition();

-- IMDG-style segregation: hazmat only in blocks flagged for it.
CREATE FUNCTION enforce_hazmat_segregation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.hazmat AND NEW.block_code IS NOT NULL
     AND NOT (SELECT allows_hazmat FROM yard_blocks WHERE code = NEW.block_code) THEN
    RAISE EXCEPTION 'hazmat_segregation: % cannot be stacked in block %', NEW.id, NEW.block_code
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER trg_hazmat_segregation
  BEFORE INSERT OR UPDATE OF block_code, hazmat ON containers
  FOR EACH ROW EXECUTE FUNCTION enforce_hazmat_segregation();

-- Gravity: every stack (yard or ship) must be contiguous from tier 1.
-- Slots are unique, so "count = max(tier)" is exactly "no gaps".
-- Deferred to commit so a multi-row move is judged on its final state.
-- Yard stacks lock their slot rows first: concurrent writers to one stack serialise,
-- and the check below (a fresh READ COMMITTED snapshot) sees the winner's commit.
CREATE FUNCTION enforce_gravity() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  r record;
  n int;
  top int;
BEGIN
  FOR r IN
    SELECT DISTINCT * FROM (VALUES
      ('Y', OLD.block_code::text, OLD.bay, OLD.row),
      ('Y', NEW.block_code::text, NEW.bay, NEW.row),
      ('V', OLD.vessel_id::text,  OLD.vessel_bay, OLD.vessel_row),
      ('V', NEW.vessel_id::text,  NEW.vessel_bay, NEW.vessel_row)
    ) AS s(kind, place, bay, row)
    WHERE place IS NOT NULL AND bay IS NOT NULL
  LOOP
    IF r.kind = 'Y' THEN
      PERFORM 1 FROM yard_slots
        WHERE block_code = r.place AND bay = r.bay AND row = r.row
        ORDER BY tier FOR UPDATE;
      SELECT count(*), coalesce(max(tier), 0) INTO n, top FROM containers
        WHERE block_code = r.place AND bay = r.bay AND row = r.row;
    ELSE
      SELECT count(*), coalesce(max(vessel_tier), 0) INTO n, top FROM containers
        WHERE vessel_id = r.place::int AND vessel_bay = r.bay AND vessel_row = r.row;
    END IF;
    IF n <> top THEN
      RAISE EXCEPTION 'stack_gravity: % stack %-%-% has % boxes but top tier %',
        CASE r.kind WHEN 'Y' THEN 'yard' ELSE 'vessel' END, r.place, r.bay, r.row, n, top
        USING ERRCODE = 'check_violation';
    END IF;
  END LOOP;
  RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER trg_stack_gravity
  AFTER INSERT OR UPDATE OF block_code, bay, row, tier, vessel_id, vessel_bay, vessel_row, vessel_tier
     OR DELETE ON containers
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION enforce_gravity();
"""

DOWNGRADE = """
DROP TABLE gate_transactions, equipment_tasks, containers, container_state_transitions,
           equipment, vessels, yard_slots, yard_blocks, terminals;
DROP FUNCTION enforce_gravity, enforce_hazmat_segregation, enforce_state_transition, iso6346_valid;
DROP TYPE lorry_status, vessel_status, task_status, task_kind, equipment_state, equipment_kind, container_state;
"""


def upgrade():
    op.execute(UPGRADE)


def downgrade():
    op.execute(DOWNGRADE)
