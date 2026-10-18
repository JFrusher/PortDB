"""Feeds, speeds and terminal dimensions. Every number here is a calibration knob."""

TERMINAL_CODE, TERMINAL_NAME = "PDB", "PortDB Feeder Terminal"

# block code -> (purpose, allows hazmat). Import and export stacks are segregated; C and F take IMDG.
BLOCKS = {
    "A": ("IMPORT", False), "B": ("IMPORT", False), "C": ("IMPORT", True),
    "D": ("EXPORT", False), "E": ("EXPORT", False), "F": ("EXPORT", True),
}
BLOCK_BAYS, BLOCK_ROWS, BLOCK_TIERS = 6, 4, 5
BLOCK_CAPACITY = BLOCK_BAYS * BLOCK_ROWS * BLOCK_TIERS
# slots a block keeps free so the RTG can always dig out a box buried under tiers-1 others
BLOCK_RESHUFFLE_RESERVE = BLOCK_TIERS - 1
YARD_PRESTOCK = {"IMPORT": 30, "EXPORT": 45}       # boxes per block at shift start

# berth id -> (name, bays, rows, tiers, import boxes per call)
BERTHS = {
    1: ("Berth 1", 6, 4, 10, 55),
    2: ("Berth 2", 6, 4, 10, 55),
    3: ("Berth 3", 10, 6, 10, 100),
}
FIRST_CALL_S = {1: 0.0, 2: 25.0, 3: 50.0}         # staggered first arrivals
SHARE_40FT, SHARE_REEFER, SHARE_HAZMAT = 0.65, 0.10, 0.06   # by box count; the 20 ft share keeps falling
# IMDG class mix among hazardous boxes (flammable liquids and miscellaneous dominate real manifests)
IMDG_CLASSES = {"3": 35, "9": 25, "8": 15, "6.1": 10, "2.1": 8, "5.1": 5, "4.1": 2}
SHARE_RAIL, SHARE_CUSTOMS_HOLD = 0.35, 0.12       # of imports: onward by train / flagged for X-ray
SHARE_TRANSHIP = 0.15                             # of a manifest: stays in the terminal for another ship
# ports the service calls after leaving here, in rotation order (later port = stowed lower)
PORTS = [("NLRTM", "Rotterdam", 1), ("DEHAM", "Hamburg", 2), ("PLGDN", "Gdansk", 3)]

QC_COUNT, AGV_COUNT, GATE_LANES = 6, 16, 10
QC_MIN_BAYS = 2                                   # a QC frame is ~2 bays wide: one crane per 2 bays at most

# seconds of simulated time
QC_CYCLE = (3.0, 4.5)                             # uniform, ship cell <-> AGV
QC_GANTRY_SPEED = 2.0                             # units/s along the quay rail
LOAD_PREFETCH = 3                                 # export boxes an AGV may be fetching ahead of each QC
LOAD_WINDOW_S = 360.0                             # a vessel sails when nothing more fits or this runs out
AGV_NODES_PER_S = 4.0
RTG_MOVE_S = 2.5                                  # per box handled: stack, reshuffle hop or truck/AGV load
LORRY_MEAN_GAP_S = {"IMPORT": 8.0, "EXPORT": 15.0}  # two Poisson streams: pick-ups and drop-offs
LORRY_SPEED = 4.0                                 # cells per second on the truck lanes
GATE_CHECKIN_S = 1.5
GATE_CHECKOUT_S = 1.0
INSPECT_S = 4.0                                   # drive-through X-ray portal dwell
VESSEL_SAIL_S = 10.0
NEXT_VESSEL_GAP_S = 8.0
NEXT_VESSEL_YARD_MAX = 0.75                      # a berth takes its next ship once import stacks drop below this

TRAIN_WAGONS = 10                                 # 60 ft platforms: three 20 ft positions, a 40 takes two
TRAIN_INBOUND = 12                               # export boxes a train brings in
FIRST_TRAIN_S = 40.0
TRAIN_WINDOW_S = 200.0
TRAIN_RUN_S = 10.0
TRAIN_GAP_S = 25.0
RMG_MOVE_S = 3.0
RMG_GANTRY_SPEED = 2.0

# yard placement: BASIC fills stacks in order; SMART puts each box on one that will leave after it
YARD_STRATEGY = "SMART"
DUAL_CYCLE = True                                 # QCs load emptied bays while still discharging others
DUAL_CYCLE_FACTOR = 0.65                          # a paired move saves the trolley's empty return trip

# weather and the day: wind limits per crane live in the database (crane_wind_limits)
DAY_S = 900.0                                     # one simulated day
DAY_START_H = 6.5                                 # the shift starts just before dawn
WEATHER_EVERY_S = 4.0
FIRST_STORM_S = 240.0
STORM_GAP_S = (360.0, 540.0)
LIFT_RETRY_S = 3.0                                # a crane refused by the database tries again after this

# chaos: breakdowns and outages, by hand from the HUD or at random
CHAOS_GAP_S = (60.0, 120.0)
REPAIR_S = {"crane": 60.0, "agv": 45.0}
GATE_OUTAGE_S = 40.0

# time travel: event log + container history kept for this long, in 5 minute partitions
RETENTION_S = 1800.0
MAINTAIN_EVERY_S = 30.0

# berth plan: how long a call is planned to take alongside
BERTH_WINDOW_S = {1: 300.0, 2: 300.0, 3: 420.0}
WINDOW_STEP_S = 30.0

# road bookings (sim seconds): margins around each cell visit, and "parked" = open-ended
PARKED = 1e9
BOOK_MARGIN = {"AGV": (0.25, 0.25), "LORRY": (0.4, 0.8)}
BOOK_PRUNE_EVERY_S = 20.0
