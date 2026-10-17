"""Landside: outbound mode (truck/rail), customs holds, trains and wagon slots.

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"

# ALTER TYPE ... ADD VALUE must commit before the new labels can be used below
NEW_LABELS = [
    ("container_state", "ON_TRUCK"), ("container_state", "ON_RAIL"), ("container_state", "RAIL_OUT"),
    ("equipment_kind", "RMG"), ("equipment_kind", "SCANNER"),
    ("task_kind", "INSPECT"), ("task_kind", "RAIL_LOAD"),
]

UPGRADE = r"""
CREATE TYPE outbound_mode AS ENUM ('TRUCK','RAIL');
CREATE TYPE train_status  AS ENUM ('ARRIVING','LOADING','DEPARTED');

CREATE TABLE trains (
  id          serial PRIMARY KEY,
  name        text NOT NULL,
  wagons      smallint NOT NULL CHECK (wagons BETWEEN 1 AND 20),
  status      train_status NOT NULL DEFAULT 'ARRIVING',
  arrived_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
  departed_at timestamptz
);

ALTER TABLE containers
  ADD COLUMN mode         outbound_mode NOT NULL DEFAULT 'TRUCK',
  ADD COLUMN customs_hold boolean NOT NULL DEFAULT false,
  ADD COLUMN inspected_at timestamptz,
  ADD COLUMN train_id     integer REFERENCES trains,
  ADD COLUMN wagon        smallint CHECK (wagon > 0),
  ADD COLUMN wagon_slot   smallint CHECK (wagon_slot IN (1, 2)),
  ADD CONSTRAINT containers_one_per_wagon_slot UNIQUE (train_id, wagon, wagon_slot),
  ADD CONSTRAINT containers_wagon_complete CHECK (num_nulls(train_id, wagon, wagon_slot) IN (0, 3)),
  ADD CONSTRAINT containers_on_rail_has_wagon CHECK ((state IN ('ON_RAIL','RAIL_OUT')) = (train_id IS NOT NULL)),
  -- a box leaves by the mode it was booked on
  ADD CONSTRAINT containers_truck_mode CHECK (state NOT IN ('ON_TRUCK','GATE_OUT') OR mode = 'TRUCK'),
  ADD CONSTRAINT containers_rail_mode  CHECK (state NOT IN ('ON_RAIL','RAIL_OUT') OR mode = 'RAIL'),
  -- customs: a held box cannot leave the terminal until it has been through a scanner
  ADD CONSTRAINT containers_customs_release
    CHECK (NOT customs_hold OR inspected_at IS NOT NULL OR state NOT IN ('GATE_OUT','ON_RAIL','RAIL_OUT'));

-- lorries leave via the truck stage now; rail boxes go yard -> AGV -> wagon
DELETE FROM container_state_transitions WHERE from_state = 'RETRIEVING' AND to_state = 'GATE_OUT';
INSERT INTO container_state_transitions VALUES
  ('RETRIEVING','ON_TRUCK'),
  ('ON_TRUCK','GATE_OUT'),
  ('RETRIEVING','IN_TRANSIT'),
  ('IN_TRANSIT','ON_RAIL'),
  ('ON_RAIL','RAIL_OUT');

DROP INDEX containers_in_yard;
CREATE INDEX containers_in_yard ON containers (mode, block_code) WHERE state = 'STACKED_IN_YARD';
CREATE INDEX containers_customs_pending ON containers (id) WHERE customs_hold AND inspected_at IS NULL;
"""

DOWNGRADE = r"""
DELETE FROM container_state_transitions
  WHERE to_state IN ('ON_TRUCK','ON_RAIL','RAIL_OUT')
     OR from_state IN ('ON_TRUCK','ON_RAIL')
     OR (from_state = 'RETRIEVING' AND to_state = 'IN_TRANSIT');
INSERT INTO container_state_transitions VALUES ('RETRIEVING','GATE_OUT');
DROP INDEX containers_customs_pending;
DROP INDEX containers_in_yard;
CREATE INDEX containers_in_yard ON containers (block_code) WHERE state = 'STACKED_IN_YARD';
ALTER TABLE containers
  DROP COLUMN mode, DROP COLUMN customs_hold, DROP COLUMN inspected_at,
  DROP COLUMN train_id, DROP COLUMN wagon, DROP COLUMN wagon_slot;
DROP TABLE trains;
DROP TYPE train_status, outbound_mode;
-- labels added to container_state / equipment_kind / task_kind stay: PostgreSQL cannot drop enum values
"""


def upgrade():
    with op.get_context().autocommit_block():
        for enum, label in NEW_LABELS:
            op.execute(f"ALTER TYPE {enum} ADD VALUE IF NOT EXISTS '{label}'")
    op.execute(UPGRADE)


def downgrade():
    op.execute(DOWNGRADE)
