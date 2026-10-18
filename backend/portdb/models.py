"""SQLAlchemy mirror of the schema. The alembic migrations are the source of truth (triggers,
CHECKs, exclusion constraints and functions live there); tests/test_db_rules.py asserts the two agree."""

from datetime import datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    Double,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Column,
    Integer,
    REAL,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INT4RANGE, JSONB, NUMRANGE, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def pg_enum(name, *values):
    return Enum(*values, name=name, create_type=False)


ContainerState = pg_enum(
    "container_state",
    "STOWED_ON_BOARD", "DISCHARGING", "IN_TRANSIT", "STACKED_IN_YARD", "RETRIEVING", "GATE_OUT",
    "ON_TRUCK", "ON_RAIL", "RAIL_OUT", "LOADING", "SAILED",
)
EquipmentKind = pg_enum("equipment_kind", "QC", "AGV", "RTG", "RMG", "SCANNER")
EquipmentState = pg_enum("equipment_state", "IDLE", "BUSY", "DOWN")
TaskKind = pg_enum("task_kind", "DISCHARGE", "TRANSPORT", "STACK", "RESHUFFLE", "RETRIEVE", "INSPECT", "RAIL_LOAD",
                   "LOAD", "DROP", "RAIL_UNLOAD")
TaskStatus = pg_enum("task_status", "PENDING", "IN_PROGRESS", "DONE", "FAILED")
VesselStatus = pg_enum("vessel_status", "INBOUND", "BERTHED", "DEPARTED", "LOADING")
LorryStatus = pg_enum("lorry_status", "QUEUED", "AT_LANE", "LOADED", "DEPARTED")
OutboundMode = pg_enum("outbound_mode", "TRUCK", "RAIL", "VESSEL")
TrainStatus = pg_enum("train_status", "ARRIVING", "LOADING", "DEPARTED", "UNLOADING")
ContainerFlow = pg_enum("container_flow", "IMPORT", "EXPORT", "TRANSHIP")

TS = TIMESTAMP(timezone=True)
NOW = text("clock_timestamp()")


class Base(DeclarativeBase):
    pass


class Terminal(Base):
    __tablename__ = "terminals"
    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    code: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)


class YardBlock(Base):
    __tablename__ = "yard_blocks"
    code: Mapped[str] = mapped_column(CHAR(1), primary_key=True)
    terminal_id: Mapped[int] = mapped_column(SmallInteger, ForeignKey("terminals.id"))
    bays: Mapped[int] = mapped_column(SmallInteger)
    rows: Mapped[int] = mapped_column(SmallInteger)
    tiers: Mapped[int] = mapped_column(SmallInteger)
    allows_hazmat: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    purpose: Mapped[str] = mapped_column(Text, server_default=text("'IMPORT'"))


class Berth(Base):
    __tablename__ = "berths"
    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(Text)
    x0: Mapped[int] = mapped_column(SmallInteger)
    max_bays: Mapped[int] = mapped_column(SmallInteger)
    max_rows: Mapped[int] = mapped_column(SmallInteger)


class Port(Base):
    __tablename__ = "ports"
    code: Mapped[str] = mapped_column(CHAR(5), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    rotation: Mapped[int] = mapped_column(SmallInteger, unique=True)


class YardSlot(Base):
    __tablename__ = "yard_slots"
    block_code: Mapped[str] = mapped_column(CHAR(1), ForeignKey("yard_blocks.code", ondelete="CASCADE"), primary_key=True)
    bay: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    row: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    tier: Mapped[int] = mapped_column(SmallInteger, primary_key=True)


class Vessel(Base):
    __tablename__ = "vessels"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    imo: Mapped[str] = mapped_column(CHAR(7), unique=True)
    bays: Mapped[int] = mapped_column(SmallInteger)
    rows: Mapped[int] = mapped_column(SmallInteger)
    tiers: Mapped[int] = mapped_column(SmallInteger)
    status: Mapped[str] = mapped_column(VesselStatus, server_default=text("'INBOUND'"))
    berthed_at: Mapped[datetime | None] = mapped_column(TS)
    departed_at: Mapped[datetime | None] = mapped_column(TS)
    berth_id: Mapped[int | None] = mapped_column(SmallInteger, ForeignKey("berths.id"))

    __table_args__ = (
        Index("vessels_one_per_berth", "berth_id", unique=True, postgresql_where=text("status <> 'DEPARTED'")),
    )


class Train(Base):
    __tablename__ = "trains"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    wagons: Mapped[int] = mapped_column(SmallInteger)
    status: Mapped[str] = mapped_column(TrainStatus, server_default=text("'ARRIVING'"))
    arrived_at: Mapped[datetime] = mapped_column(TS, server_default=NOW)
    departed_at: Mapped[datetime | None] = mapped_column(TS)


class Equipment(Base):
    __tablename__ = "equipment"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(EquipmentKind)
    state: Mapped[str] = mapped_column(EquipmentState, server_default=text("'IDLE'"))
    home_block: Mapped[str | None] = mapped_column(CHAR(1), ForeignKey("yard_blocks.code"))
    idle_since: Mapped[datetime] = mapped_column(TS, server_default=NOW)
    pos_x: Mapped[int | None] = mapped_column(SmallInteger)
    pos_z: Mapped[int | None] = mapped_column(SmallInteger)
    rail_span = mapped_column(NUMRANGE)


class Container(Base):
    __tablename__ = "containers"
    id: Mapped[str] = mapped_column(CHAR(11), primary_key=True)
    iso_type: Mapped[str] = mapped_column(CHAR(4))
    weight_kg: Mapped[int] = mapped_column(Integer)
    hazmat: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    state: Mapped[str] = mapped_column(ContainerState, server_default=text("'STOWED_ON_BOARD'"))
    vessel_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("vessels.id"))
    vessel_bay: Mapped[int | None] = mapped_column(SmallInteger)
    vessel_row: Mapped[int | None] = mapped_column(SmallInteger)
    vessel_tier: Mapped[int | None] = mapped_column(SmallInteger)
    block_code: Mapped[str | None] = mapped_column(CHAR(1))
    bay: Mapped[int | None] = mapped_column(SmallInteger)
    row: Mapped[int | None] = mapped_column(SmallInteger)
    tier: Mapped[int | None] = mapped_column(SmallInteger)
    half: Mapped[int | None] = mapped_column(SmallInteger)            # 0 = 40 ft, 1 fore / 2 aft 20 ft
    vessel_half: Mapped[int | None] = mapped_column(SmallInteger)
    carrier_id: Mapped[str | None] = mapped_column(Text, ForeignKey("equipment.id"))
    mode: Mapped[str] = mapped_column(OutboundMode, server_default=text("'TRUCK'"))
    customs_hold: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    inspected_at: Mapped[datetime | None] = mapped_column(TS)
    train_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("trains.id"))
    wagon: Mapped[int | None] = mapped_column(SmallInteger)
    wagon_slot: Mapped[int | None] = mapped_column(SmallInteger)
    flow: Mapped[str] = mapped_column(ContainerFlow, server_default=text("'IMPORT'"))
    pod: Mapped[str | None] = mapped_column(CHAR(5), ForeignKey("ports.code"))
    imdg_class: Mapped[str | None] = mapped_column(Text)
    yard_in_t: Mapped[float | None] = mapped_column(Double)
    rehandles: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["block_code", "bay", "row", "tier"],
            ["yard_slots.block_code", "yard_slots.bay", "yard_slots.row", "yard_slots.tier"],
            name="containers_slot_fk",
        ),
        UniqueConstraint("block_code", "bay", "row", "tier", "half", name="containers_one_per_slot"),
        UniqueConstraint("vessel_id", "vessel_bay", "vessel_row", "vessel_tier", "vessel_half", name="containers_one_per_cell"),
        Index("containers_on_board", "vessel_id", "vessel_bay", text("vessel_tier DESC"),
              postgresql_where=text("state = 'STOWED_ON_BOARD'")),
        UniqueConstraint("train_id", "wagon", "wagon_slot", name="containers_one_per_wagon_slot"),
        Index("containers_in_yard", "mode", "block_code", postgresql_where=text("state = 'STACKED_IN_YARD'")),
        Index("containers_customs_pending", "id", postgresql_where=text("customs_hold AND inspected_at IS NULL")),
        Index("containers_export_ready", "pod", text("weight_kg DESC"),
              postgresql_where=text("state = 'STACKED_IN_YARD' AND mode = 'VESSEL'")),
    )


class EquipmentTask(Base):
    __tablename__ = "equipment_tasks"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    equipment_id: Mapped[str] = mapped_column(Text, ForeignKey("equipment.id"))
    container_id: Mapped[str] = mapped_column(CHAR(11), ForeignKey("containers.id"))
    kind: Mapped[str] = mapped_column(TaskKind)
    status: Mapped[str] = mapped_column(TaskStatus, server_default=text("'PENDING'"))
    origin: Mapped[dict] = mapped_column(JSONB)
    target: Mapped[dict] = mapped_column(JSONB)
    sim_t0: Mapped[float | None] = mapped_column(Double)
    sim_t1: Mapped[float | None] = mapped_column(Double)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=NOW)
    finished_at: Mapped[datetime | None] = mapped_column(TS)

    __table_args__ = (
        Index("equipment_tasks_live", "equipment_id", "status",
              postgresql_where=text("status IN ('PENDING','IN_PROGRESS')")),
        Index("equipment_tasks_box", "container_id"),
        Index("equipment_tasks_sim_t", "sim_t0", postgresql_using="brin"),
    )


class GateTransaction(Base):
    __tablename__ = "gate_transactions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    plate: Mapped[str] = mapped_column(Text)
    container_id: Mapped[str] = mapped_column(CHAR(11), ForeignKey("containers.id"))
    lane: Mapped[int | None] = mapped_column(SmallInteger)
    status: Mapped[str] = mapped_column(LorryStatus, server_default=text("'QUEUED'"))
    shifter_moves: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    arrived_at: Mapped[datetime] = mapped_column(TS, server_default=NOW)
    departed_at: Mapped[datetime | None] = mapped_column(TS)
    vehicle: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("gate_one_live_pickup", "container_id", unique=True,
              postgresql_where=text("status <> 'DEPARTED'")),
        Index("gate_transactions_box", "container_id", "id"),
    )


class ContainerStateTransition(Base):
    __tablename__ = "container_state_transitions"
    from_state: Mapped[str] = mapped_column(ContainerState, primary_key=True)
    to_state: Mapped[str] = mapped_column(ContainerState, primary_key=True)
    flows = mapped_column(ARRAY(ContainerFlow), nullable=False, server_default=text("'{IMPORT,EXPORT,TRANSHIP}'"))


class RoadBooking(Base):
    """(cell, sim-time) rectangles; the no-overlap exclusion constraint lives in migration 0003."""
    __tablename__ = "road_bookings"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    vehicle: Mapped[str] = mapped_column(Text)
    x: Mapped[int] = mapped_column(SmallInteger)
    z: Mapped[int] = mapped_column(SmallInteger)
    t0: Mapped[float] = mapped_column(Double)
    t1: Mapped[float] = mapped_column(Double)

    __table_args__ = (Index("road_bookings_vehicle", "vehicle", "t1"),)


# --- migration 0005: time travel, change feed, operating rules ---------------------------------
# Partitioned by simulated time; the partitions themselves are made at run time (ensure_partitions).
ContainerHistory = Table(
    "container_history", Base.metadata,
    Column("container_id", CHAR(11), nullable=False),
    Column("valid", NUMRANGE, nullable=False),
    Column("state", ContainerState, nullable=False),
    Column("snapshot", JSONB, nullable=False),
    Index("container_history_valid", "valid", postgresql_using="gist"),
    Index("container_history_box", "container_id", text("(lower(valid))")),
    Index("container_history_open", "container_id", postgresql_where=text("upper_inf(valid)")),
    postgresql_partition_by="RANGE ((lower(valid)))",
)

EventLog = Table(
    "event_log", Base.metadata,
    Column("t", Double, nullable=False),
    Column("type", Text, nullable=False),
    Column("unit", Text),
    Column("body", JSONB, nullable=False),
    Index("event_log_t", "t", postgresql_using="brin"),
    Index("event_log_unit", "unit", "t", postgresql_where=text("unit IS NOT NULL")),
    postgresql_partition_by="RANGE (t)",
)


class Weather(Base):
    __tablename__ = "weather"
    t: Mapped[float] = mapped_column(Double, primary_key=True)
    wind_ms: Mapped[float] = mapped_column(REAL)
    gust_ms: Mapped[float] = mapped_column(REAL)
    rain: Mapped[float] = mapped_column(REAL)
    fog: Mapped[float] = mapped_column(REAL)


class CraneWindLimit(Base):
    __tablename__ = "crane_wind_limits"
    kind: Mapped[str] = mapped_column(EquipmentKind, primary_key=True)
    max_wind_ms: Mapped[float] = mapped_column(REAL)


class GateOutage(Base):
    """No two outages overlap (exclusion constraint in 0005); a trigger refuses check-ins during one."""
    __tablename__ = "gate_outages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    during = mapped_column(NUMRANGE, nullable=False)
    reason: Mapped[str] = mapped_column(Text)


class ImdgSegregation(Base):
    __tablename__ = "imdg_segregation"
    class_a: Mapped[str] = mapped_column(Text, primary_key=True)
    class_b: Mapped[str] = mapped_column(Text, primary_key=True)
    code: Mapped[int] = mapped_column(SmallInteger)


class BerthWindow(Base):
    """Planned alongside windows; EXCLUDE (quay WITH &&, during WITH &&) lives in 0005."""
    __tablename__ = "berth_windows"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    berth_id: Mapped[int] = mapped_column(SmallInteger, ForeignKey("berths.id"))
    vessel_name: Mapped[str] = mapped_column(Text)
    vessel_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("vessels.id"))
    quay = mapped_column(INT4RANGE, nullable=False)
    during = mapped_column(NUMRANGE, nullable=False)
    status: Mapped[str] = mapped_column(Text, server_default=text("'PLANNED'"))
