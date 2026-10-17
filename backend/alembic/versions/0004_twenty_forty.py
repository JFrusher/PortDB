"""20 ft / 40 ft stowage: fore/aft halves, one length per stack, ISO bay codes, 60 ft wagons.

Every yard slot and ship cell is a 40 ft position. A 40 ft box fills it (half = 0); two 20 ft boxes
share it fore (half = 1) and aft (half = 2). A stack holds one length only, as yards are run in
practice: a 40 needs corner castings at both ends beneath it, and a 20 cannot sit mid-span on a 40.
Rail wagons are 60 ft platforms of three 20 ft positions; a 40 takes two adjacent ones.

Revision ID: 0004
Revises: 0003
"""
from alembic import op

revision = "0004"
down_revision = "0003"

UPGRADE = r"""
-- ISO bay numbering: 20 ft bays odd (01, 03), the 40 ft bay over them even (02)
CREATE FUNCTION bay_code(bay integer, half integer) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT lpad((4 * bay - CASE half WHEN 0 THEN 2 WHEN 1 THEN 3 ELSE 1 END)::text, 2, '0')
$$;

CREATE FUNCTION is_forty(iso_type text) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$ SELECT left(iso_type, 1) = '4' $$;

ALTER TABLE containers ADD COLUMN half smallint, ADD COLUMN vessel_half smallint;
UPDATE containers SET half = CASE WHEN is_forty(iso_type) THEN 0 ELSE 1 END WHERE block_code IS NOT NULL;
UPDATE containers SET vessel_half = CASE WHEN is_forty(iso_type) THEN 0 ELSE 1 END WHERE vessel_bay IS NOT NULL;

ALTER TABLE containers
  DROP CONSTRAINT containers_one_per_slot,
  DROP CONSTRAINT containers_one_per_cell,
  DROP CONSTRAINT containers_wagon_slot_check,
  ADD CONSTRAINT containers_one_per_slot UNIQUE (block_code, bay, row, tier, half),
  ADD CONSTRAINT containers_one_per_cell UNIQUE (vessel_id, vessel_bay, vessel_row, vessel_tier, vessel_half),
  -- a half is present exactly when the position is, and it says how long the box is
  ADD CONSTRAINT containers_half_matches_length CHECK (
        (half IS NULL) = (block_code IS NULL)
    AND (vessel_half IS NULL) = (vessel_bay IS NULL)
    AND (half IS NULL OR half IN (0, 1, 2) AND (half = 0) = is_forty(iso_type))
    AND (vessel_half IS NULL OR vessel_half IN (0, 1, 2) AND (vessel_half = 0) = is_forty(iso_type))),
  -- 60 ft wagon: three 20 ft positions; a 40 starts at 1 or 2 and takes the next one too
  ADD CONSTRAINT containers_wagon_position CHECK (
        wagon_slot IS NULL OR (wagon_slot BETWEEN 1 AND 3 AND (NOT is_forty(iso_type) OR wagon_slot <= 2)));

-- gravity per fore/aft column, one length per stack, no overlapping boxes on a wagon
CREATE OR REPLACE FUNCTION enforce_gravity() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  r record;
  n int;
  top int;
  lengths int;
BEGIN
  FOR r IN
    SELECT DISTINCT * FROM (VALUES
      ('Y', OLD.block_code::text, OLD.bay, OLD.row),
      ('Y', NEW.block_code::text, NEW.bay, NEW.row),
      ('V', OLD.vessel_id::text,  OLD.vessel_bay, OLD.vessel_row),
      ('V', NEW.vessel_id::text,  NEW.vessel_bay, NEW.vessel_row),
      ('W', OLD.train_id::text,   OLD.wagon, NULL::smallint),
      ('W', NEW.train_id::text,   NEW.wagon, NULL::smallint)
    ) AS s(kind, place, bay, row)
    WHERE place IS NOT NULL AND bay IS NOT NULL
  LOOP
    IF r.kind = 'W' THEN
      IF EXISTS (SELECT 1 FROM containers a JOIN containers b
                   ON (a.train_id, a.wagon) = (b.train_id, b.wagon) AND a.id < b.id
                 WHERE a.train_id = r.place::int AND a.wagon = r.bay
                   AND int4range(a.wagon_slot, a.wagon_slot + CASE WHEN is_forty(a.iso_type) THEN 2 ELSE 1 END)
                    && int4range(b.wagon_slot, b.wagon_slot + CASE WHEN is_forty(b.iso_type) THEN 2 ELSE 1 END)) THEN
        RAISE EXCEPTION 'wagon_overlap: train % wagon % has two boxes in one position', r.place, r.bay
          USING ERRCODE = 'check_violation';
      END IF;
      CONTINUE;
    END IF;
    IF r.kind = 'Y' THEN
      PERFORM 1 FROM yard_slots
        WHERE block_code = r.place AND bay = r.bay AND row = r.row
        ORDER BY tier FOR UPDATE;
      SELECT count(DISTINCT is_forty(iso_type)) INTO lengths FROM containers
        WHERE block_code = r.place AND bay = r.bay AND row = r.row;
    ELSE
      SELECT count(DISTINCT is_forty(iso_type)) INTO lengths FROM containers
        WHERE vessel_id = r.place::int AND vessel_bay = r.bay AND vessel_row = r.row;
    END IF;
    IF lengths > 1 THEN
      RAISE EXCEPTION 'stack_mixed_lengths: % stack %-%-% would mix 20 ft and 40 ft boxes',
        CASE r.kind WHEN 'Y' THEN 'yard' ELSE 'vessel' END, r.place, r.bay, r.row
        USING ERRCODE = 'check_violation';
    END IF;
    -- every fore/aft column (or the whole 40 ft column) is contiguous from tier 1
    FOR n, top IN
      SELECT count(*), max(tier) FROM containers
        WHERE r.kind = 'Y' AND block_code = r.place AND bay = r.bay AND row = r.row GROUP BY half
      UNION ALL
      SELECT count(*), max(vessel_tier) FROM containers
        WHERE r.kind = 'V' AND vessel_id = CASE WHEN r.kind = 'V' THEN r.place::int END
          AND vessel_bay = r.bay AND vessel_row = r.row GROUP BY vessel_half
    LOOP
      IF n <> top THEN
        RAISE EXCEPTION 'stack_gravity: % stack %-%-% has a column of % boxes but top tier %',
          CASE r.kind WHEN 'Y' THEN 'yard' ELSE 'vessel' END, r.place, r.bay, r.row, n, top
          USING ERRCODE = 'check_violation';
      END IF;
    END LOOP;
  END LOOP;
  RETURN NULL;
END $$;

DROP TRIGGER trg_stack_gravity ON containers;
CREATE CONSTRAINT TRIGGER trg_stack_gravity
  AFTER INSERT OR UPDATE OF block_code, bay, row, tier, half, vessel_id, vessel_bay, vessel_row, vessel_tier,
                            vessel_half, train_id, wagon, wagon_slot, iso_type
     OR DELETE ON containers
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION enforce_gravity();

-- stowage compares each box with the one directly beneath it in the same fore/aft column
CREATE OR REPLACE FUNCTION enforce_stowage() RETURNS trigger
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
      ON (hi.vessel_id, hi.vessel_bay, hi.vessel_row, hi.vessel_half, hi.vessel_tier)
       = (lo.vessel_id, lo.vessel_bay, lo.vessel_row, lo.vessel_half, lo.vessel_tier + 1)
    LEFT JOIN ports plo ON plo.code = lo.pod
    LEFT JOIN ports phi ON phi.code = hi.pod
    WHERE (lo.vessel_id, lo.vessel_bay, lo.vessel_row, lo.vessel_half)
        = (NEW.vessel_id, NEW.vessel_bay, NEW.vessel_row, NEW.vessel_half)
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
DROP TRIGGER trg_stowage ON containers;
CREATE CONSTRAINT TRIGGER trg_stowage
  AFTER INSERT OR UPDATE OF vessel_id, vessel_bay, vessel_row, vessel_tier, vessel_half, weight_kg, pod ON containers
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION enforce_stowage();
"""

DOWNGRADE = r"""
DROP TRIGGER trg_stowage ON containers;
DROP TRIGGER trg_stack_gravity ON containers;
ALTER TABLE containers
  DROP CONSTRAINT containers_wagon_position, DROP CONSTRAINT containers_half_matches_length,
  DROP CONSTRAINT containers_one_per_cell, DROP CONSTRAINT containers_one_per_slot,
  DROP COLUMN half, DROP COLUMN vessel_half,
  ADD CONSTRAINT containers_one_per_slot UNIQUE (block_code, bay, row, tier),
  ADD CONSTRAINT containers_one_per_cell UNIQUE (vessel_id, vessel_bay, vessel_row, vessel_tier),
  ADD CONSTRAINT containers_wagon_slot_check CHECK (wagon_slot IN (1, 2));
-- restore the pre-0004 bodies (from 0001 and 0003)
CREATE OR REPLACE FUNCTION enforce_gravity() RETURNS trigger
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

CREATE OR REPLACE FUNCTION enforce_stowage() RETURNS trigger
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
CREATE CONSTRAINT TRIGGER trg_stack_gravity
  AFTER INSERT OR UPDATE OF block_code, bay, row, tier, vessel_id, vessel_bay, vessel_row, vessel_tier
     OR DELETE ON containers
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION enforce_gravity();
CREATE CONSTRAINT TRIGGER trg_stowage
  AFTER INSERT OR UPDATE OF vessel_id, vessel_bay, vessel_row, vessel_tier, weight_kg, pod ON containers
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION enforce_stowage();
DROP FUNCTION bay_code, is_forty;
"""


def upgrade():
    op.execute(UPGRADE)


def downgrade():
    op.execute(DOWNGRADE)
