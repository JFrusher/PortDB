// Views onto the database drawn in the diorama: what it is holding (X-ray), what it refused, where a
// box has been (journey), and per-stack heat.

import { Html, Line } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { C } from "./models";
import { simNow, trackAt, useWorldSelect, world, type Layout, type V3 } from "./world";

type Booking = { vehicle: string; x: number; z: number; t0: number; t1: number; parked: boolean };
type Rail = { id: string; kind: string; lo: number; hi: number; state: string };
type XRayFrame = { t: number; bookings: Booking[]; rails: Rail[]; gate_outage: { reason: string; until: number } | null };

export const SECONDS_UP = 0.28;   // one simulated second of booking = this much height
export const AHEAD = 12;
const MAX_BOOKINGS = 3000;
const FLASH_MS = 2600;

function hue(id: string) {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) | 0;
  return new THREE.Color().setHSL((Math.abs(h) % 360) / 360, 0.65, 0.55);
}

/** Poll the database while the overlay is open. */
function usePoll<T>(url: string, ms: number, on: boolean): T | null {
  const [data, setData] = useState<T | null>(null);
  useEffect(() => {
    if (!on) return setData(null);
    let live = true;
    const tick = async () => {
      try {
        const r = await fetch(url);
        if (live && r.ok) setData(await r.json());
      } catch {
        /* next tick */
      }
    };
    tick();
    const id = setInterval(tick, ms);
    return () => {
      live = false;
      clearInterval(id);
    };
  }, [url, ms, on]);
  return data;
}

/**
 * Road bookings as space-time columns: each (cell, t0..t1) rectangle the exclusion constraint guards
 * is drawn with time as height above the road, now at ground level. Columns never intersect — that
 * is the constraint. Crane rail envelopes show as bands along their rails.
 */
export function XRay({ L }: { L: Layout }) {
  // Html overlays stay mounted and are hidden instead: React 19 objects to Html roots unmounting mid-render
  const on = useWorldSelect((w) => w.xray && w.mode === "live");
  const frame = usePoll<XRayFrame>("/api/xray", 400, on);
  const cranes = useMemo(() => L.units.filter((u) => u.kind === "QC" || u.kind === "RMG"), [L]);
  const mesh = useRef<THREE.InstancedMesh>(null!);
  const m = useMemo(() => new THREE.Matrix4(), []);
  const q = useMemo(() => new THREE.Quaternion(), []);
  const p = useMemo(() => new THREE.Vector3(), []);
  const s = useMemo(() => new THREE.Vector3(), []);
  const colors = useMemo(() => new Map<string, THREE.Color>(), []);
  const grey = useMemo(() => new THREE.Color("#9AA0B4"), []);
  useFrame(() => {
    const ms = mesh.current;
    if (!ms) return;
    const now = simNow();
    let n = 0;
    for (const b of frame?.bookings ?? []) {
      if (n >= MAX_BOOKINGS) break;
      const lo = Math.max(b.t0, now) - now;
      const hi = Math.min(b.parked ? now + 1.5 : b.t1, now + AHEAD) - now;
      if (hi <= lo) continue;
      p.set(b.x, 0.02 + ((lo + hi) / 2) * SECONDS_UP, b.z);
      ms.setMatrixAt(n, m.compose(p, q, s.set(b.parked ? 0.5 : 0.74, (hi - lo) * SECONDS_UP, b.parked ? 0.5 : 0.74)));
      let c = colors.get(b.vehicle);
      if (!c) colors.set(b.vehicle, (c = hue(b.vehicle)));
      ms.setColorAt(n, b.parked ? grey : c);
      n++;
    }
    ms.count = n;
    ms.instanceMatrix.needsUpdate = true;
    if (ms.instanceColor) ms.instanceColor.needsUpdate = true;
  });
  const [qz0, qz1] = L.legs.qc;
  const [rz0, rz1] = L.legs.rmg;
  return (
    <group visible={on}>
      <instancedMesh ref={mesh} args={[undefined, undefined, MAX_BOOKINGS]} frustumCulled={false} renderOrder={3}>
        <boxGeometry />
        <meshBasicMaterial transparent opacity={0.62} depthWrite={false} />
      </instancedMesh>
      {cranes.map((u) => {
        const r = frame?.rails.find((x) => x.id === u.id);
        const qc = u.kind === "QC";
        const [z0, z1] = qc ? [qz0, qz1] : [rz0, rz1];
        const down = r?.state === "DOWN";
        const lo = r?.lo ?? u.home[0];
        const hi = r?.hi ?? u.home[0] + 0.01;
        return (
          <group key={u.id} visible={!!r}>
            <mesh position={[(lo + hi) / 2, 0.06, (z0 + z1) / 2]} scale={[hi - lo, 1, 1]} renderOrder={3}>
              <boxGeometry args={[1, 0.08, z1 - z0 + 0.3]} />
              <meshBasicMaterial color={down ? C.hazmat : qc ? "#6FA8DC" : "#7FC8A0"} transparent opacity={0.35} depthWrite={false} />
            </mesh>
            <Html position={[(lo + hi) / 2, 0.1, qc ? z1 + 0.5 : z1 + 0.4]} center zIndexRange={[15, 0]}>
              <div className={`xray-tag ${down ? "down" : ""}`} hidden={!on || !r}>
                {u.id} [{lo.toFixed(1)}, {hi.toFixed(1)})
              </div>
            </Html>
          </group>
        );
      })}
      <mesh position={[L.lanes.gate_in_x, 0.6, L.lanes.thru_z]} visible={!!frame?.gate_outage}>
        <boxGeometry args={[0.15, 1.2, 1.6]} />
        <meshBasicMaterial color={C.hazmat} transparent opacity={0.6} />
      </mesh>
      <Refusals on={on} />
    </group>
  );
}

/** Each refusal flashes where it happened, labelled with the rule that refused it. */
const SLOTS = 14;

/** A fixed pool of flash slots, filled with the most recent refusals. */
function Refusals({ on }: { on: boolean }) {
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((x) => x + 1), 250);
    return () => clearInterval(id);
  }, []);
  const now = performance.now();
  const live = world.refusals.filter((r) => now - r.born < FLASH_MS).slice(-SLOTS);
  return (
    <>
      {Array.from({ length: SLOTS }, (_, i) => (
        <Flash key={i} r={on ? live[i] : undefined} />
      ))}
    </>
  );
}

function Flash({ r }: { r: (typeof world.refusals)[number] | undefined }) {
  const group = useRef<THREE.Group>(null!);
  const ring = useRef<THREE.Mesh>(null!);
  const mat = useRef<THREE.MeshBasicMaterial>(null!);
  useFrame(() => {
    group.current.visible = !!r;
    if (!r) return;
    const u = Math.min(1, (performance.now() - r.born) / FLASH_MS);
    group.current.position.set(r.at[0], Math.max(0.1, r.at[1]), r.at[2]);
    ring.current.scale.setScalar(0.4 + u * 1.6);
    mat.current.opacity = 0.9 * (1 - u);
  });
  return (
    <group ref={group}>
      <mesh ref={ring} rotation-x={-Math.PI / 2}>
        <ringGeometry args={[0.55, 0.7, 32]} />
        <meshBasicMaterial ref={mat} color="#E0503A" transparent depthWrite={false} side={THREE.DoubleSide} />
      </mesh>
      <Html position={[0, 0.6, 0]} center zIndexRange={[25, 0]}>
        <div className="refusal-tag" title={r?.detail} hidden={!r}>
          ✕ {r?.rule}
        </div>
      </Html>
    </group>
  );
}

/** The path a selected box took through the terminal, from the event log. */
export function Journey() {
  const j = useWorldSelect((w) => w.journey);
  if (!j || j.points.length < 2) return null;
  return (
    <group>
      <Line points={j.points} color="#F2B134" lineWidth={3} dashed={false} transparent opacity={0.9} />
      {j.stops.map((p, i) => (
        <mesh key={i} position={p}>
          <sphereGeometry args={[0.16, 12, 12]} />
          <meshBasicMaterial color={i ? "#E07A5F" : "#81B29A"} />
        </mesh>
      ))}
    </group>
  );
}

type Heat = { rehandles: { block: string; bay: number; row: number; n: number }[]; reefers: { block: string; reefers: number }[] };
export const REEFER_KW = 4.2;
export const REEFER_PLUGS = 24;

/** Ground heat under each stack (re-handles), or reefer power draw per block. */
export function HeatPads({ L }: { L: Layout }) {
  const mode = useWorldSelect((w) => w.colorBy);
  const on = mode === "rehandles" || mode === "reefer";
  const heat = usePoll<Heat>("/api/heat", 3000, on);
  const reefer = mode === "reefer" && !!heat;
  const blocks = (
    <>
      {L.blocks.map((b) => {
        const n = heat?.reefers.find((x) => x.block === b.code)?.reefers ?? 0;
        return (
          <group key={b.code} visible={reefer}>
            <mesh position={[b.x0 + (b.bays + 1) / 2, 0.015, b.z0 + (b.rows + 1) / 2]}>
              <boxGeometry args={[b.bays + 0.2, 0.01, b.rows + 0.2]} />
              <meshBasicMaterial color={new THREE.Color("#CFE8F3").lerp(new THREE.Color("#3F88C5"), Math.min(1, n / REEFER_PLUGS))} />
            </mesh>
            <Html position={[b.x0 + (b.bays + 1) / 2, 3.2, b.z0 + (b.rows + 1) / 2]} center zIndexRange={[12, 0]}>
              <div className="heat-tag" hidden={!reefer}>
                {(n * REEFER_KW).toFixed(0)} kW · {n}/{REEFER_PLUGS} plugs
              </div>
            </Html>
          </group>
        );
      })}
    </>
  );
  if (!on || !heat || reefer) return blocks;
  const max = Math.max(1, ...heat.rehandles.map((h) => h.n));
  return (
    <>
      {blocks}
      {heat.rehandles.map((h) => {
        const b = L.blocks.find((x) => x.code === h.block);
        if (!b) return null;
        return (
          <mesh key={`${h.block}${h.bay}${h.row}`} position={[b.x0 + h.bay, 0.016, b.z0 + h.row]}>
            <boxGeometry args={[0.96, 0.01, 0.96]} />
            <meshBasicMaterial color={new THREE.Color("#FCE7C8").lerp(new THREE.Color("#C8553D"), h.n / max)} />
          </mesh>
        );
      })}
    </>
  );
}

/** Badges over equipment the database is holding: out of service, or stopped by the wind limit. */
export function UnitAlerts({ L }: { L: Layout }) {
  useWorldSelect((w) => {
    const q = w.stats?.queues;
    return q ? `${q.down.join()}|${Object.entries(q.held).join()}|${w.mode}` : "";
  });
  const q = world.stats?.queues;
  return (
    <>
      {L.units.map((u) => {
        const down = !!q?.down.includes(u.id);
        const state = world.mode !== "live" ? null : down ? "down" : q?.held[u.id] ? "held" : null;
        return <UnitAlert key={u.id} id={u.id} home={u.home} agv={u.kind === "AGV"} state={state} />;
      })}
    </>
  );
}

function UnitAlert({ id, home, agv, state }: { id: string; home: V3; agv: boolean; state: "down" | "held" | null }) {
  const g = useRef<THREE.Group>(null!);
  useFrame(() => {
    if (!state) return;
    const tr = world.tracks.get(id);
    const p = tr ? trackAt(tr, simNow(), alertAt) : home;
    g.current.position.set(p[0], agv ? 0.8 : p[1] + 1.2, p[2]);
  });
  return (
    <group ref={g}>
      <Html center zIndexRange={[22, 0]}>
        <div className={`unit-alert ${state ?? ""}`} hidden={!state}>
          {id} {state === "down" ? "OUT OF SERVICE" : "WIND HOLD"}
        </div>
      </Html>
    </group>
  );
}
const alertAt: V3 = [0, 0, 0];
