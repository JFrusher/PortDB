"""Export + transhipment flows, three berths, stowage rules, space-time road bookings, crane rails.

Revision ID: 0003
Revises: 0002
"""
from alembic import op

revision = "0003"
down_revision = "0002"

NEW_LABELS = [
    ("container_state", "LOADING"), ("container_state", "SAILED"),
    ("vessel_status", "LOADING"),
    ("train_status", "UNLOADING"),
    ("outbound_mode", "VESSEL"),
    ("task_kind", "LOAD"), ("task_kind", "DROP"), ("task_kind", "RAIL_UNLOAD"),
]

UPGRADE = r"""
CREATE TYPE container_flow AS ENUM ('IMPORT','EXPORT','TRANSHIP');

-- berths + port rotation -------------------------------------------------------
CREATE TABLE berths (
  id       smallint PRIMARY KEY,
  name     text NOT NULL,
  x0       smallint NOT NULL,
  max_bays smallint NOT NULL CHECK (max_bays > 0),
  max_rows smallint NOT NULL CHECK (max_rows > 0)
);

-- the service calls these ports in rotation order after leaving here
CREATE TABLE ports (
  code     char(5) PRIMARY KEY CHECK (code ~ '^[A-Z]{5}$'),
  name     text NOT NULL,
  rotation smallint NOT NULL UNIQUE CHECK (rotation > 0)
);

ALTER TABLE vessels ADD COLUMN berth_id smallint REFERENCES berths;
CREATE UNIQUE INDEX vessels_one_per_berth ON vessels (berth_id) WHERE status <> 'DEPARTED';

CREATE FUNCTION enforce_berth_fit() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM berths b
             WHERE b.id = NEW.berth_id AND (NEW.bays > b.max_bays OR NEW.rows > b.max_rows)) THEN
    RAISE EXCEPTION 'berth_fit: vessel % (% bays x % rows) does not fit berth %',
      NEW.name, NEW.bays, NEW.rows, NEW.berth_id USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_berth_fit BEFORE INSERT OR UPDATE OF berth_id, bays, rows ON vessels
  FOR EACH ROW EXECUTE FUNCTION enforce_berth_fit();

-- yard: import and export blocks are segregated --------------------------------
ALTER TABLE yard_blocks ADD COLUMN purpose text NOT NULL DEFAULT 'IMPORT' CHECK (purpose IN ('IMPORT','EXPORT'));

-- containers: direction of travel and port of discharge --------------------------
ALTER TABLE containers
  ADD COLUMN flow container_flow NOT NULL DEFAULT 'IMPORT',
  ADD COLUMN pod  char(5) REFERENCES ports,
  DROP CONSTRAINT containers_truck_mode,
  DROP CONSTRAINT containers_rail_mode,
  DROP CONSTRAINT containers_on_board_has_cell,
  ADD CONSTRAINT containers_on_board_has_cell
    CHECK ((state IN ('STOWED_ON_BOARD','DISCHARGING','LOADING','SAILED')) = (vessel_bay IS NOT NULL)),
  ADD CONSTRAINT containers_flow_has_pod CHECK ((flow IN ('EXPORT','TRANSHIP')) = (pod IS NOT NULL)),
  ADD CONSTRAINT containers_flow_mode    CHECK ((flow IN ('EXPORT','TRANSHIP')) = (mode = 'VESSEL')),
  -- every exit and every hand-off matches how the box was booked
  ADD CONSTRAINT containers_exit_by_booked_mode CHECK (
        (state <> 'GATE_OUT' OR mode = 'TRUCK')
    AND (state <> 'RAIL_OUT' OR mode = 'RAIL')
    AND (state NOT IN ('LOADING','SAILED') OR mode = 'VESSEL')
    AND (state <> 'ON_TRUCK' OR mode = 'TRUCK' OR flow = 'EXPORT')
    AND (state <> 'ON_RAIL'  OR mode = 'RAIL'  OR flow = 'EXPORT'));

-- lifecycle edges are now per flow ---------------------------------------------
ALTER TABLE container_state_transitions
  ADD COLUMN flows container_flow[] NOT NULL DEFAULT '{IMPORT,EXPORT,TRANSHIP}';
UPDATE container_state_transitions SET flows = '{IMPORT,TRANSHIP}'
  WHERE (from_state, to_state) IN (('STOWED_ON_BOARD','DISCHARGING'), ('DISCHARGING','IN_TRANSIT'));
UPDATE container_state_transitions SET flows = '{IMPORT}'
  WHERE (from_state, to_state) IN (('RETRIEVING','ON_TRUCK'), ('ON_TRUCK','GATE_OUT'),
                                   ('IN_TRANSIT','ON_RAIL'), ('ON_RAIL','RAIL_OUT'));
INSERT INTO container_state_transitions (from_state, to_state, flows) VALUES
  ('ON_TRUCK',        'STACKED_IN_YARD', '{EXPORT}'),
  ('ON_RAIL',         'IN_TRANSIT',      '{EXPORT}'),
  ('IN_TRANSIT',      'LOADING',         '{EXPORT,TRANSHIP}'),
  ('LOADING',         'STOWED_ON_BOARD', '{EXPORT,TRANSHIP}'),
  ('STOWED_ON_BOARD', 'SAILED',          '{EXPORT,TRANSHIP}');

CREATE OR REPLACE FUNCTION enforce_state_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM container_state_transitions
                 WHERE from_state = OLD.state AND to_state = NEW.state AND NEW.flow = ANY (flows)) THEN
    RAISE EXCEPTION 'illegal_transition: % % % -> %', NEW.flow, NEW.id, OLD.state, NEW.state
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

-- yard segregation replaces the hazmat-only trigger ------------------------------
DROP TRIGGER trg_hazmat_segregation ON containers;
DROP FUNCTION enforce_hazmat_segregation;
CREATE FUNCTION enforce_yard_segregation() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  b record;
BEGIN
  IF NEW.block_code IS NULL THEN
    RETURN NEW;
  END IF;
  SELECT allows_hazmat, purpose INTO b FROM yard_blocks WHERE code = NEW.block_code;
  IF NEW.hazmat AND NOT b.allows_hazmat THEN
    RAISE EXCEPTION 'hazmat_segregation: % cannot be stacked in block %', NEW.id, NEW.block_code
      USING ERRCODE = 'check_violation';
  END IF;
  IF (NEW.flow = 'IMPORT') <> (b.purpose = 'IMPORT') THEN
    RAISE EXCEPTION 'yard_segregation: % box % cannot be stacked in % block %', NEW.flow, NEW.id, b.purpose, NEW.block_code
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_yard_segregation
  BEFORE INSERT OR UPDATE OF block_code, hazmat, flow ON containers
  FOR EACH ROW EXECUTE FUNCTION enforce_yard_segregation();

-- stowage: heavy under light, later port under earlier port ----------------------
-- Checked at commit against the neighbours directly below and above the touched cell.
CREATE FUNCTION enforce_stowage() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  r record;
BEGIN
  IF NEW.vessel_bay IS NULL THEN
    RETURN NULL;
  END IF;
  FOR r IN
    SELECT lo.id AS lo_id, lo.weight_kg AS lo_w, plo.rotation AS lo_rot,
           hi.id AS hi_id, hi.weight_kg AS hi_w, phi.rotation AS hi_rot
    FROM containers lo
    JOIN containers hi
      ON (hi.vessel_id, hi.vessel_bay, hi.vessel_row, hi.vessel_tier)
       = (lo.vessel_id, lo.vessel_bay, lo.vessel_row, lo.vessel_tier + 1)
    LEFT JOIN ports plo ON plo.code = lo.pod
    LEFT JOIN ports phi ON phi.code = hi.pod
    WHERE (lo.vessel_id, lo.vessel_bay, lo.vessel_row) = (NEW.vessel_id, NEW.vessel_bay, NEW.vessel_row)
      AND lo.vessel_tier IN (NEW.vessel_tier - 1, NEW.vessel_tier)
  LOOP
    IF r.hi_w > r.lo_w THEN
      RAISE EXCEPTION 'stowage_weight: % (% kg) cannot sit on % (% kg)', r.hi_id, r.hi_w, r.lo_id, r.lo_w
        USING ERRCODE = 'check_violation';
    END IF;
    IF r.hi_rot > r.lo_rot THEN
      RAISE EXCEPTION 'stowage_port: % is discharged after % but would sit on top of it', r.hi_id, r.lo_id
        USING ERRCODE = 'check_violation';
    END IF;
  END LOOP;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_stowage
  AFTER INSERT OR UPDATE OF vessel_id, vessel_bay, vessel_row, vessel_tier, weight_kg, pod ON containers
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION enforce_stowage();

-- equipment: positions for nearest-AGV dispatch, rail envelopes for cranes --------
ALTER TABLE equipment
  ADD COLUMN pos_x smallint,
  ADD COLUMN pos_z smallint,
  ADD COLUMN rail_span numrange,
  -- cranes on one rail occupy disjoint envelopes; to pass, one would have to overlap the other
  ADD CONSTRAINT equipment_qc_rail_no_crossing  EXCLUDE USING gist (rail_span WITH &&) WHERE (kind = 'QC'),
  ADD CONSTRAINT equipment_rmg_rail_no_crossing EXCLUDE USING gist (rail_span WITH &&) WHERE (kind = 'RMG');

-- space-time road bookings -----------------------------------------------------
-- A booking is a rectangle in (road cell, sim time). Two rectangles may not overlap, so two
-- vehicles can never hold the same cell at the same moment. Native gist on box: no extension.
CREATE FUNCTION cell_key(x integer, z integer) RETURNS double precision
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$ SELECT ((x + 128) * 64 + z)::double precision $$;

CREATE TABLE road_bookings (
  id      bigserial PRIMARY KEY,
  vehicle text NOT NULL,
  x       smallint NOT NULL CHECK (x BETWEEN -128 AND 300),
  z       smallint NOT NULL CHECK (z BETWEEN 0 AND 63),
  t0      double precision NOT NULL,
  t1      double precision NOT NULL,
  CHECK (t1 > t0),
  CONSTRAINT road_bookings_no_overlap
    EXCLUDE USING gist (box(point(cell_key(x, z), t0), point(cell_key(x, z) + 0.5, t1)) WITH &&)
);
CREATE INDEX road_bookings_vehicle ON road_bookings (vehicle, t1);

CREATE INDEX containers_export_ready ON containers (pod, weight_kg DESC)
  WHERE state = 'STACKED_IN_YARD' AND mode = 'VESSEL';
"""

DOWNGRADE = r"""
DROP INDEX containers_export_ready;
DROP TABLE road_bookings;
DROP FUNCTION cell_key;
ALTER TABLE equipment
  DROP CONSTRAINT equipment_qc_rail_no_crossing, DROP CONSTRAINT equipment_rmg_rail_no_crossing,
  DROP COLUMN pos_x, DROP COLUMN pos_z, DROP COLUMN rail_span;
DROP TRIGGER trg_stowage ON containers;
DROP FUNCTION enforce_stowage;
DROP TRIGGER trg_yard_segregation ON containers;
DROP FUNCTION enforce_yard_segregation;
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
CREATE TRIGGER trg_hazmat_segregation BEFORE INSERT OR UPDATE OF block_code, hazmat ON containers
  FOR EACH ROW EXECUTE FUNCTION enforce_hazmat_segregation();
DELETE FROM container_state_transitions
  WHERE (from_state, to_state) IN (('ON_TRUCK','STACKED_IN_YARD'), ('ON_RAIL','IN_TRANSIT'),
                                   ('IN_TRANSIT','LOADING'), ('LOADING','STOWED_ON_BOARD'),
                                   ('STOWED_ON_BOARD','SAILED'));
ALTER TABLE container_state_transitions DROP COLUMN flows;
CREATE OR REPLACE FUNCTION enforce_state_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM container_state_transitions
                 WHERE from_state = OLD.state AND to_state = NEW.state) THEN
    RAISE EXCEPTION 'illegal_transition: % % -> %', NEW.id, OLD.state, NEW.state
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
ALTER TABLE containers
  DROP CONSTRAINT containers_exit_by_booked_mode, DROP CONSTRAINT containers_flow_mode,
  DROP CONSTRAINT containers_flow_has_pod, DROP CONSTRAINT containers_on_board_has_cell,
  DROP COLUMN pod, DROP COLUMN flow,
  ADD CONSTRAINT containers_on_board_has_cell
    CHECK ((state IN ('STOWED_ON_BOARD','DISCHARGING')) = (vessel_bay IS NOT NULL)),
  ADD CONSTRAINT containers_truck_mode CHECK (state NOT IN ('ON_TRUCK','GATE_OUT') OR mode = 'TRUCK'),
  ADD CONSTRAINT containers_rail_mode  CHECK (state NOT IN ('ON_RAIL','RAIL_OUT') OR mode = 'RAIL');
ALTER TABLE yard_blocks DROP COLUMN purpose;
DROP TRIGGER trg_berth_fit ON vessels;
DROP FUNCTION enforce_berth_fit;
DROP INDEX vessels_one_per_berth;
ALTER TABLE vessels DROP COLUMN berth_id;
DROP TABLE ports, berths;
DROP TYPE container_flow;
-- enum labels added above stay: PostgreSQL cannot drop enum values
"""


def upgrade():
    with op.get_context().autocommit_block():
        for enum, label in NEW_LABELS:
            op.execute(f"ALTER TYPE {enum} ADD VALUE IF NOT EXISTS '{label}'")
    op.execute(UPGRADE)


def downgrade():
    op.execute(DOWNGRADE)
