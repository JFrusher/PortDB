// Client mirror of the terminal. Mutated in place by the WebSocket; the 3D scene reads it every
// frame, the HUD re-renders on (batched) `version` bumps.

import { useSyncExternalStore } from "react";

export type V3 = [number, number, number];
export type Kind = "QC" | "RTG" | "RMG" | "AGV" | "LORRY" | "TRAIN" | "BOX";
export type Track = { id: string; kind: Kind; t0: number; t1: number; path: V3[] };
export type Box = {
  id: string;
  iso_type: string;
  hazmat: boolean;
  weight_kg: number;
  state: string;
  flow: "IMPORT" | "EXPORT" | "TRANSHIP";
  mode: "TRUCK" | "RAIL" | "VESSEL";
  pod?: string | null;
  customs_hold: boolean;
  inspected?: boolean;
  vessel_id?: number | null;
  vessel_bay?: number | null;
  vessel_row?: number | null;
  vessel_tier?: number | null;
  block_code?: string | null;
  bay?: number | null;
  row?: number | null;
  tier?: number | null;
  carrier_id?: string | null;
  train_id?: number | null;
  wagon?: number | null;
  wagon_slot?: number | null;
};
export type Block = { code: string; x0: number; z0: number; bays: number; rows: number; tiers: number; purpose: string; hazmat: boolean };
export type Berth = { id: number; name: string; x0: number; bays: number; rows: number; tiers: number };
export type Layout = {
  box_h: number;
  ship_deck: number;
  agv_bed_y: number;
  lorry_bed_y: number;
  wagon_bed_y: number;
  berths: Berth[];
  sail: { from_x: number; to_x: number };
  blocks: Block[];
  roads: [number, number][];
  spurs: [number, number][];
  parks: [number, number][];
  lanes: { load_z: number; thru_z: number; spawn_x: number; gate_in_x: number; gate_out_x: number; exit_x: number };
  rail: { lane_z: number; spur_z: number; z: number; x0: number; wagons: number; from_x: number; to_x: number };
  scanners: { id: string; pos: V3; across: "x" | "z" }[];
  customs: [number, number];
  legs: { qc: [number, number]; rtg: [number, number]; rmg: [number, number] };
  rtg_clear_y: number;
  qc_clear_y: number;
  rmg_clear_y: number;
  units: { id: string; kind: Kind; home: V3 }[];
};
export type Vessel = {
  id: number; name: string; imo: string; berth: number; bays: number; rows: number; tiers: number;
  status: string; phase: string | null; t0: number; t1: number; manifest: number; left: number; loaded: number;
};
export type Train = { id: number; unit: string; name: string; status: string; loaded: number; unloaded: number; inbound: number; capacity: number };
export type Scan = { id: string; box: string; t0: number; t1: number };
export type Reshuffle = { block: string; target: string; moves: number };
export type Stats = {
  t: number;
  speed: number;
  paused: boolean;
  yard: Record<string, number>;
  capacity: number;
  states: Record<string, number>;
  flows: Record<string, number>;
  discharged: number;
  loaded: number;
  sailed: number;
  gated_out: number;
  railed_out: number;
  reshuffles: number;
  customs_pending: number;
  inspected: number;
  scanning: number;
  bookings: number;
  rejects: number;
  vessels: Vessel[];
  train: Train | null;
  queues: { agv_wait: number; rtg: Record<string, number>; gate: number; waits: Record<string, number> };
};

export type Focus = "overview" | "quay" | "berth3" | "yard" | "gate" | "customs" | "rail";

export const world = {
  layout: null as Layout | null,
  boxes: new Map<string, Box>(),
  tracks: new Map<string, Track>(),
  lorries: new Set<string>(),
  clock: { t: 0, at: performance.now(), speed: 1, paused: false },
  vessels: new Map<number, Vessel>(), // by vessel id
  train: null as Train | null,
  scans: new Map<string, Scan>(),
  reshuffling: new Map<string, Reshuffle>(),
  stats: null as Stats | null,
  log: [] as { t: number; level: "info" | "warn" | "rail" | "customs" | "export"; text: string }[],
  focus: "overview" as Focus,
  connected: false,
  version: 0,
};

const listeners = new Set<() => void>();
let pending = false;
/** Coalesce bursts of events into one React pass every 100 ms; motion never waits on React. */
export const changed = () => {
  world.version++;
  if (pending) return;
  pending = true;
  setTimeout(() => {
    pending = false;
    listeners.forEach((f) => f());
  }, 100);
};
const subscribe = (f: () => void) => (listeners.add(f), () => listeners.delete(f));
export const useWorld = () => useSyncExternalStore(subscribe, () => world.version);
/** Re-render only when `pick(world)` changes (compare primitives or stable references). */
export const useWorldSelect = <T,>(pick: (w: typeof world) => T): T => useSyncExternalStore(subscribe, () => pick(world));

// --- sim clock ----------------------------------------------------------------------------
export function simNow(): number {
  const c = world.clock;
  return c.paused ? c.t : c.t + ((performance.now() - c.at) / 1000) * c.speed;
}

function syncClock(t: number, speed: number, paused: boolean) {
  const c = world.clock;
  // only snap on real drift; tiny corrections would make movers jitter backwards
  if (speed !== c.speed || paused !== c.paused || Math.abs(simNow() - t) > 0.3) {
    world.clock = { t, at: performance.now(), speed, paused };
  }
}

// --- motion -------------------------------------------------------------------------------
const clamp01 = (x: number) => Math.min(Math.max(x, 0), 1);
export const smooth = (u: number) => u * u * (3 - 2 * u);

/** Trapezoid speed profile: accelerate for `ramp` s, cruise, brake for `ramp` s. Returns distance fraction. */
function trapezoid(t: number, T: number, ramp: number): number {
  if (T <= 0) return 1;
  const r = Math.min(ramp, T / 2);
  const v = 1 / (T - r);
  if (t <= 0) return 0;
  if (t >= T) return 1;
  if (t < r) return (v * t * t) / (2 * r);
  if (t > T - r) return 1 - (v * (T - t) * (T - t)) / (2 * r);
  return v * (t - r / 2);
}

function along(path: V3[], f: number, out: V3): V3 {
  let total = 0;
  for (let i = 1; i < path.length; i++) total += Math.hypot(path[i][0] - path[i - 1][0], path[i][1] - path[i - 1][1], path[i][2] - path[i - 1][2]);
  let d = f * total;
  for (let i = 1; i < path.length; i++) {
    const a = path[i - 1];
    const b = path[i];
    const len = Math.hypot(b[0] - a[0], b[1] - a[1], b[2] - a[2]);
    if (d <= len || i === path.length - 1) {
      const u = len > 0 ? clamp01(d / len) : 1;
      out[0] = a[0] + (b[0] - a[0]) * u;
      out[1] = a[1] + (b[1] - a[1]) * u;
      out[2] = a[2] + (b[2] - a[2]) * u;
      return out;
    }
    d -= len;
  }
  return Object.assign(out, path[path.length - 1]);
}

/**
 * Vehicles (AGV, lorry, train) move by arc length with a trapezoid speed profile.
 * Cranes and the boxes they carry take equal time per leg, eased in and out of each leg,
 * so a spreader settles onto a box instead of slamming into it.
 */
export function trackAt(tr: Track, now: number, out: V3 = [0, 0, 0]): V3 {
  const n = tr.path.length - 1;
  if (n <= 0) return Object.assign(out, tr.path[0]);
  const T = tr.t1 - tr.t0;
  const t = now - tr.t0;
  if (tr.kind === "AGV" || tr.kind === "LORRY" || tr.kind === "TRAIN") {
    const ramp = tr.kind === "TRAIN" ? T * 0.45 : tr.kind === "LORRY" ? 0.7 : 0.3;
    return along(tr.path, trapezoid(t, T, ramp), out);
  }
  const f = T > 0 ? clamp01(t / T) * n : n;
  const i = Math.min(Math.floor(f), n - 1);
  const u = smooth(f - i);
  const a = tr.path[i];
  const b = tr.path[i + 1];
  out[0] = a[0] + (b[0] - a[0]) * u;
  out[1] = a[1] + (b[1] - a[1]) * u;
  out[2] = a[2] + (b[2] - a[2]) * u;
  return out;
}

export const moving = (tr: Track | undefined, now: number) => !!tr && now >= tr.t0 && now < tr.t1;

// --- geometry -----------------------------------------------------------------------------
export function berthOf(L: Layout, id: number): Berth {
  return L.berths.find((b) => b.id === id)!;
}

export function shipCell(L: Layout, berth: number, bay: number, row: number, tier: number): V3 {
  return [berthOf(L, berth).x0 + bay, L.ship_deck + (tier - 1) * L.box_h, -row - 0.5];
}

export function slotPos(L: Layout, block: string, bay: number, row: number, tier: number): V3 {
  const b = L.blocks.find((x) => x.code === block)!;
  return [b.x0 + bay, (tier - 1) * L.box_h, b.z0 + row];
}

/** x offset of a vessel from its berth (null when it should not be drawn). */
export function shipOffset(L: Layout, v: Vessel | undefined, now: number): number | null {
  if (!v) return null;
  const b = berthOf(L, v.berth);
  const span = Math.max(v.t1 - v.t0, 0.001);
  const u = clamp01((now - v.t0) / span);
  if (v.status === "ARRIVING") return (L.sail.from_x - b.x0) * (1 - u) ** 3;
  if (v.status === "DEPARTING") return (L.sail.to_x - b.x0) * u * u;
  return 0;
}

/** Reference point of a train: wagon w slot s sits at x + 2(w-1) + s. */
export function trainAt(L: Layout, trainId: number, now: number, out: V3): V3 {
  const tr = world.tracks.get(`TRAIN-${trainId}`);
  if (tr) return trackAt(tr, now, out);
  return Object.assign(out, [L.rail.x0, 0, L.rail.z] as V3);
}

const tmpT: V3 = [0, 0, 0];
const ON_SHIP = new Set(["STOWED_ON_BOARD", "DISCHARGING", "LOADING", "SAILED"]);

/** Where a box is drawn (bottom centre). */
export function boxPos(L: Layout, b: Box, now: number, out: V3): V3 | null {
  const own = world.tracks.get(b.id);
  if (own && now < own.t1) return trackAt(own, now, out); // mid-crane

  if (b.carrier_id && (b.state === "IN_TRANSIT" || b.state === "ON_TRUCK" || b.state === "GATE_OUT")) {
    const carrier = world.tracks.get(b.carrier_id);
    if (carrier) {
      trackAt(carrier, now, out);
      out[1] = b.state === "IN_TRANSIT" ? L.agv_bed_y : L.lorry_bed_y;
      return out;
    }
    if (b.state !== "IN_TRANSIT") return null;
  }
  if (b.state === "ON_RAIL" && b.train_id != null) {
    trainAt(L, b.train_id, now, tmpT);
    out[0] = tmpT[0] + 2 * (b.wagon! - 1) + b.wagon_slot!;
    out[1] = L.wagon_bed_y;
    out[2] = tmpT[2];
    return out;
  }
  if (b.vessel_bay != null && b.vessel_id != null && ON_SHIP.has(b.state) && !(own && b.state === "DISCHARGING")) {
    const v = world.vessels.get(b.vessel_id);
    const dx = shipOffset(L, v, now);
    if (dx == null || !v) return null;
    Object.assign(out, shipCell(L, v.berth, b.vessel_bay, b.vessel_row!, b.vessel_tier!));
    out[0] += dx;
    return out;
  }
  if (own) return trackAt(own, now, out); // resting where its last crane move left it
  if (b.block_code && b.bay != null) return Object.assign(out, slotPos(L, b.block_code, b.bay!, b.row!, b.tier!));
  return null;
}

export const fmtSlot = (b: { block_code?: string | null; bay?: number | null; row?: number | null; tier?: number | null }) =>
  `${b.block_code}-${String(b.bay).padStart(2, "0")}-${String(b.row).padStart(2, "0")}-${b.tier}`;

export const fmtClock = (t: number) => {
  const s = Math.floor(t);
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
};

// --- stream -------------------------------------------------------------------------------
type Level = (typeof world.log)[number]["level"];
function log(t: number, text: string, level: Level = "info") {
  world.log.push({ t, level, text });
  if (world.log.length > 80) world.log.shift();
}

function setVessel(v: Vessel) {
  if (v.status === "DEPARTED") world.vessels.delete(v.id);
  else world.vessels.set(v.id, v);
}

// The server's event frames, as parsed JSON: shapes documented in BACKBONE.md section 5.
function apply(ev: any) {
  switch (ev.type) {
    case "hello":
      world.layout = ev.layout;
      world.boxes = new Map(ev.boxes.map((b: Box) => [b.id, b]));
      world.tracks = new Map(ev.tracks.map((t: Track) => [t.id, t]));
      world.lorries = new Set(ev.tracks.filter((t: Track) => t.kind === "LORRY").map((t: Track) => t.id));
      world.reshuffling = new Map(ev.reshuffling.map((r: Reshuffle) => [r.block, r]));
      world.scans = new Map(ev.scans.map((s: Scan) => [s.id, s]));
      world.vessels = new Map(ev.vessels.map((v: Vessel) => [v.id, v]));
      world.train = ev.train;
      world.clock = { t: ev.sim.t, at: performance.now(), speed: ev.sim.speed, paused: ev.sim.paused };
      break;
    case "track":
      world.tracks.set(ev.id, ev);
      if (ev.kind === "LORRY") world.lorries.add(ev.id);
      return; // motion only: no HUD change
    case "boxes":
      for (const b of ev.boxes) world.boxes.set(b.id, b);
      break;
    case "box": {
      const { type: _type, t: _t, ...fields } = ev;
      const b = { ...world.boxes.get(ev.id), ...fields } as Box;
      world.boxes.set(b.id, b);
      if (ev.state === "STACKED_IN_YARD" && ev.block_code) log(ev.t, `STACK     ${b.id} → ${fmtSlot(b)}`, b.flow === "IMPORT" ? "info" : "export");
      else if (ev.block_code) log(ev.t, `SHIFT     ${b.id} → ${fmtSlot(b)}`);
      else if (ev.state === "ON_TRUCK") log(ev.t, `TRUCK     ${b.id} on ${ev.carrier_id}`);
      else if (ev.state === "ON_RAIL") log(ev.t, `WAGON     ${b.id} → W${ev.wagon}/${ev.wagon_slot}`, "rail");
      else if (ev.state === "STOWED_ON_BOARD") log(ev.t, `LOADED    ${b.id} ${b.pod ?? ""} T${b.vessel_tier}`, "export");
      else if (ev.state === "GATE_OUT") log(ev.t, `GATE OUT  ${b.id} on ${ev.carrier_id}`);
      else if (ev.inspected) log(ev.t, `CLEARED   ${b.id} released by customs`, "customs");
      break;
    }
    case "remove":
      for (const id of ev.ids) {
        world.boxes.delete(id);
        world.tracks.delete(id);
        world.lorries.delete(id);
      }
      break;
    case "vessel":
      setVessel(ev);
      if (ev.status !== "BERTHED") log(ev.t, `VESSEL    ${ev.name} berth ${ev.berth} ${ev.status}`, ev.status === "LOADING" ? "export" : "info");
      break;
    case "train":
      world.train = ev.status === "DEPARTED" ? null : ev;
      if (ev.status !== "LOADING" || ev.loaded === 0)
        log(ev.t, `RAIL      ${ev.name} ${ev.status}${ev.status === "DEPARTING" ? ` with ${ev.loaded}/${ev.capacity}` : ""}`, "rail");
      break;
    case "scan":
      world.scans.set(ev.id, ev);
      log(ev.t, `X-RAY     ${ev.box} at ${ev.id}`, "customs");
      break;
    case "reshuffle":
      if (ev.active) world.reshuffling.set(ev.block, ev);
      else world.reshuffling.delete(ev.block);
      if (ev.active) log(ev.t, `RE-HANDLE block ${ev.block}: ${ev.moves} over ${ev.target}`, "warn");
      break;
    case "lorry":
      log(ev.t, `GATE IN   ${ev.plate} ${ev.flow === "EXPORT" ? "drop-off" : "pick-up"} ${ev.box}${ev.hold ? " · customs hold" : ""}`,
        ev.flow === "EXPORT" ? "export" : "info");
      break;
    case "reject":
      log(ev.t, `REJECTED  ${ev.error}`, "warn");
      break;
    case "stats":
      world.stats = ev;
      for (const v of ev.vessels) setVessel(v);
      world.train = ev.train;
      syncClock(ev.t, ev.speed, ev.paused);
      break;
  }
  changed();
}

export function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onopen = () => {
    world.connected = true;
    changed();
  };
  ws.onmessage = (m) => apply(JSON.parse(m.data));
  ws.onclose = () => {
    world.connected = false;
    changed();
    setTimeout(connect, 1500);
  };
}

export async function post(path: string) {
  await fetch(path, { method: "POST" });
}
