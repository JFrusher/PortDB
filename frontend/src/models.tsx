// Equipment, vehicles, ships and people. Each model is one merged, vertex-coloured geometry (one
// draw call), plus a "detail" geometry that only mounts when the camera is zoomed in close.

import { Html } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useMemo, useRef, useSyncExternalStore, type RefObject } from "react";
import * as THREE from "three";
import { GLASS, GLOW, MAT, Parts } from "./parts";
import { moving, shipOffset, simNow, trackAt, useWorldSelect, world, type Layout, type V3, type Vessel } from "./world";

export const C = {
  table: "#F4F1EA",
  land: "#E9E3D6",
  apron: "#DCD6CA",
  water: "#A2D2DF",
  waterDeep: "#86BFD0",
  asphalt: "#5E6280",
  lane: "#6B6F8C",
  spur: "#7B7F9A",
  marking: "#F4F1EA",
  padImport: "#D8F3DC",
  padExport: "#DCE7F5",
  padHazmat: "#F6DCD3",
  ballast: "#B9AE9C",
  rail: "#5B5B66",
  sleeper: "#8A7560",
  hull: "#3D405B",
  boot: "#E07A5F",
  ivory: "#FFFDF9",
  glass: "#2B2D42",
  qc: "#8DB3C9",
  rtg: "#E9C46A",
  rmg: "#8FB996",
  steel: "#4A4E69",
  agv: "#F77F00",
  tyre: "#2B2D42",
  hub: "#B8BCC8",
  amber: "#F4A261",
  dry: ["#94A89A", "#A3A9B5", "#9CAFA4", "#B3A79C"],
  reefer: "#CFE8F3",
  hazmat: "#E07A5F",
  pod: { NLRTM: "#81B29A", DEHAM: "#6C8EAD", PLGDN: "#B5838D" } as Record<string, string>,
  retrieving: "#F4A261",
  selected: "#F2B134",
  vest: "#F77F00",
  officer: "#3D405B",
};

// --- view state: close-up detail + quality tier, set by the camera rig / perf monitor --------
export const view = { detail: false, quality: 2 };
const viewListeners = new Set<() => void>();
export function setView(patch: Partial<typeof view>) {
  Object.assign(view, patch);
  viewListeners.forEach((f) => f());
}
const subView = (f: () => void) => (viewListeners.add(f), () => viewListeners.delete(f));
export const useDetail = () => useSyncExternalStore(subView, () => view.detail);
export const useQuality = () => useSyncExternalStore(subView, () => view.quality);

// --- pose from tracks ------------------------------------------------------------------------
const tmp: V3 = [0, 0, 0];

/**
 * Group follows the unit's latest track (or its home). Nothing turns: containers always lie along
 * the quay (x), and so must whatever carries them. AGVs crab-steer, as real terminal AGVs do, and
 * lorries only ever drive east, stepping sideways into their loading spots.
 */
export function useMover(id: string, home: V3) {
  const ref = useRef<THREE.Group>(null!);
  useFrame(() => {
    const tr = world.tracks.get(id);
    if (tr) trackAt(tr, simNow(), tmp);
    else Object.assign(tmp, home);
    ref.current.position.set(tmp[0], tmp[1], tmp[2]);
  });
  return ref;
}

function Mesh({ geo, mat = MAT, shadow = true }: { geo: THREE.BufferGeometry; mat?: THREE.Material; shadow?: boolean }) {
  return <mesh geometry={geo} material={mat} castShadow={shadow} receiveShadow={shadow} />;
}

const geoCache = new Map<string, unknown>();
function cached<T>(key: string, make: () => T): T {
  if (!geoCache.has(key)) geoCache.set(key, make());
  return geoCache.get(key) as T;
}

// --- AGV -------------------------------------------------------------------------------------
const agvGeo = (() => {
  const body = new Parts()
    .box([1.0, 0.2, 0.56], [0, 0.16, 0], C.agv)
    .box([0.92, 0.04, 0.5], [0, 0.28, 0], C.steel)
    .box([1.02, 0.06, 0.58], [0, 0.07, 0], C.steel);
  for (const x of [-0.44, 0.44]) for (const z of [-0.22, 0.22]) body.box([0.04, 0.08, 0.04], [x, 0.33, z], C.steel);
  for (const x of [-0.32, 0.32]) for (const z of [-0.27, 0.27]) body.cyl(0.08, 0.07, [x, 0.08, z], C.tyre, "z", 12);
  const detail = new Parts().cyl(0.04, 0.05, [0.38, 0.33, 0], C.glass, "y", 10);
  for (const x of [-0.32, 0.32]) for (const z of [-0.31, 0.31]) detail.cyl(0.04, 0.01, [x, 0.08, z], C.hub, "z", 8);
  detail.box([0.9, 0.03, 0.005], [0, 0.18, 0.282], C.ivory).box([0.9, 0.03, 0.005], [0, 0.18, -0.282], C.ivory);
  const glow = new Parts().box([0.01, 0.05, 0.4], [0.505, 0.16, 0], "#FFE8B0").box([0.01, 0.05, 0.4], [-0.505, 0.16, 0], "#E0503A");
  return { body: body.build(), detail: detail.build(), glow: glow.build() };
})();

export function Agv({ id, home }: { id: string; home: V3 }) {
  const ref = useMover(id, home);
  const detail = useDetail();
  const beacon = useRef<THREE.MeshStandardMaterial>(null!);
  useFrame(() => {
    const on = moving(world.tracks.get(id), simNow()) && Math.sin(simNow() * 9) > 0;
    beacon.current.emissiveIntensity = on ? 2.4 : 0.1;
  });
  return (
    <group ref={ref}>
      <Mesh geo={agvGeo.body} />
      <Mesh geo={agvGeo.glow} mat={GLOW} shadow={false} />
      {detail && <Mesh geo={agvGeo.detail} shadow={false} />}
      <mesh position={[0.4, 0.31, 0.22]}>
        <cylinderGeometry args={[0.03, 0.03, 0.05, 8]} />
        <meshStandardMaterial ref={beacon} color={C.amber} emissive={C.amber} emissiveIntensity={0.1} />
      </mesh>
    </group>
  );
}

// --- lorry -----------------------------------------------------------------------------------
const TINTS = ["#E9C46A", "#B5838D", "#6D9DC5", "#A3B18A", "#E5989B", "#9C89B8"];
function lorryGeo(tint: string) {
  return cached(`lorry:${tint}`, () => {
    const body = new Parts()
      .box([0.4, 0.44, 0.5], [0.78, 0.38, 0], tint)
      .box([0.24, 0.1, 0.46], [0.72, 0.64, 0], tint)
      .box([0.06, 0.08, 0.52], [1.0, 0.16, 0], C.steel)
      .box([1.7, 0.06, 0.16], [0.1, 0.2, 0], C.steel)
      .box([1.08, 0.06, 0.46], [0, 0.38, 0], C.steel);
    for (const x of [0.78, 0.38, -0.3, -0.45]) for (const z of [-0.21, 0.21]) body.cyl(0.1, 0.08, [x, 0.1, z], C.tyre, "z", 12);
    const glass = new Parts().box([0.02, 0.17, 0.42], [0.985, 0.46, 0], C.glass)
      .box([0.2, 0.12, 0.005], [0.8, 0.48, 0.252], C.glass).box([0.2, 0.12, 0.005], [0.8, 0.48, -0.252], C.glass);
    const detail = new Parts()
      .box([0.02, 0.09, 0.04], [0.98, 0.5, 0.29], C.steel).box([0.02, 0.09, 0.04], [0.98, 0.5, -0.29], C.steel)
      .cyl(0.025, 0.36, [0.6, 0.52, 0.23], C.hub, "y", 8)
      .box([0.12, 0.08, 0.01], [0.62, 0.12, 0.27], C.tyre).box([0.12, 0.08, 0.01], [0.62, 0.12, -0.27], C.tyre)
      .cyl(0.07, 0.03, [0.42, 0.25, 0], C.steel, "y", 10)
      .box([0.03, 0.2, 0.03], [0.25, 0.1, 0.18], C.steel).box([0.03, 0.2, 0.03], [0.25, 0.1, -0.18], C.steel);
    for (const x of [0.78, 0.38, -0.3, -0.45]) for (const z of [-0.255, 0.255]) detail.cyl(0.045, 0.01, [x, 0.1, z], C.hub, "z", 8);
    const glow = new Parts().box([0.01, 0.05, 0.08], [1.03, 0.24, 0.17], "#FFE8B0").box([0.01, 0.05, 0.08], [1.03, 0.24, -0.17], "#FFE8B0")
      .box([0.01, 0.04, 0.06], [-0.55, 0.32, 0.19], "#E0503A").box([0.01, 0.04, 0.06], [-0.55, 0.32, -0.19], "#E0503A");
    return { body: body.build(), glass: glass.build(), detail: detail.build(), glow: glow.build() };
  });
}

export function Lorry({ id }: { id: string }) {
  const ref = useMover(id, [-80, 0, 0]);
  const detail = useDetail();
  const g = useMemo(() => {
    let h = 0;
    for (const c of id) h = (h * 31 + c.charCodeAt(0)) | 0;
    return lorryGeo(TINTS[Math.abs(h) % TINTS.length]);
  }, [id]);
  return (
    <group ref={ref}>
      <Mesh geo={g.body} />
      <Mesh geo={g.glass} mat={GLASS} shadow={false} />
      <Mesh geo={g.glow} mat={GLOW} shadow={false} />
      {detail && <Mesh geo={g.detail} shadow={false} />}
    </group>
  );
}

// --- train -----------------------------------------------------------------------------------
function trainGeo(L: Layout) {
  const body = new Parts();
  const detail = new Parts();
  for (let w = 1; w <= L.rail.wagons; w++) {
    const cx = 2 * w - 0.5;
    body.box([1.9, 0.1, 0.5], [cx, L.wagon_bed_y - 0.05, 0], "#7D6B5D").box([1.6, 0.1, 0.18], [cx, L.wagon_bed_y - 0.15, 0], C.steel);
    for (const x of [-0.65, 0.65]) {
      body.box([0.36, 0.1, 0.4], [cx + x, 0.14, 0], C.tyre);
      for (const dx of [-0.1, 0.1]) for (const z of [-0.2, 0.2]) body.cyl(0.07, 0.04, [cx + x + dx, 0.1, z], C.tyre, "z", 10);
    }
    for (const x of [-0.92, 0.92]) for (const z of [-0.22, 0.22]) detail.box([0.03, 0.12, 0.03], [cx + x, L.wagon_bed_y + 0.06, z], C.steel);
    detail.box([0.12, 0.05, 0.06], [cx + 1.0, 0.3, 0], C.steel);
  }
  const lx = 2 * L.rail.wagons + 2;
  body.box([2.0, 0.56, 0.52], [lx - 0.2, 0.62, 0], C.boot).box([0.5, 0.76, 0.56], [lx + 0.85, 0.72, 0], C.ivory)
    .box([1.2, 0.08, 0.4], [lx + 0.2, 0.93, 0], C.steel).box([2.4, 0.08, 0.5], [lx - 0.3, 0.32, 0], C.steel);
  for (const x of [-1.0, 0.5]) body.box([0.6, 0.12, 0.42], [lx + x, 0.14, 0], C.tyre);
  detail.railing([lx - 1.2, 0.36, 0.27], [lx + 0.6, 0.36, 0.27], C.ivory, 0.18, 8)
    .railing([lx - 1.2, 0.36, -0.27], [lx + 0.6, 0.36, -0.27], C.ivory, 0.18, 8)
    .cyl(0.02, 0.08, [lx + 0.9, 1.15, 0.12], C.hub, "y", 6)
    .box([0.6, 0.02, 0.005], [lx - 0.2, 0.62, 0.262], C.ivory).box([0.6, 0.02, 0.005], [lx - 0.2, 0.62, -0.262], C.ivory);
  const glass = new Parts().box([0.02, 0.2, 0.46], [lx + 1.105, 0.85, 0], C.glass);
  const glow = new Parts().box([0.01, 0.06, 0.3], [lx + 1.12, 0.45, 0], "#FFE8B0");
  return { body: body.build(), detail: detail.build(), glass: glass.build(), glow: glow.build() };
}

export function Train({ L }: { L: Layout }) {
  const ref = useRef<THREE.Group>(null!);
  const detail = useDetail();
  const g = useMemo(() => trainGeo(L), [L]);
  useFrame(() => {
    const t = world.train;
    const tr = t ? world.tracks.get(t.unit) : undefined;
    ref.current.visible = !!tr;
    if (tr) {
      trackAt(tr, simNow(), tmp);
      ref.current.position.set(tmp[0], 0, tmp[2]);
    }
  });
  return (
    <group ref={ref}>
      <Mesh geo={g.body} />
      <Mesh geo={g.glass} mat={GLASS} shadow={false} />
      <Mesh geo={g.glow} mat={GLOW} shadow={false} />
      {detail && <Mesh geo={g.detail} shadow={false} />}
    </group>
  );
}

// --- ships -----------------------------------------------------------------------------------
function buildShip(L: Layout, x0: number, bays: number, rows: number) {
  const z0 = -0.9;
  const z1 = -(rows + 1.1);
  const xs = x0 - 1.6;
  const xb = x0 + bays + 1.0;
  const cz = (z0 + z1) / 2;
  const beam = z0 - z1;
  const shape = new THREE.Shape();
  shape.moveTo(xs, z0);
  shape.lineTo(xb, z0);
  shape.quadraticCurveTo(xb + 1.6, cz + 0.2, xb + 2.2, cz);
  shape.quadraticCurveTo(xb + 1.6, cz - 0.2, xb, z1);
  shape.lineTo(xs, z1);
  shape.lineTo(xs - 0.2, cz);
  shape.closePath();
  // the hull outline is drawn in (x, z) and extruded downwards from y
  const slab = (depth: number, y: number) => new THREE.ExtrudeGeometry(shape, { depth, bevelEnabled: false }).rotateX(Math.PI / 2).translate(0, y, 0);
  const body = new Parts()
    .add(slab(1.15, L.ship_deck - 0.02), C.hull)
    .add(slab(0.24, -0.06).scale(1, 1, 1.01).translate(0, 0, cz * -0.01), C.boot)
    .add(slab(0.03, L.ship_deck + 0.005), "#B9BDC9");
  for (let i = 0; i < bays; i++) body.box([0.96, 0.04, beam - 0.5], [x0 + i + 1, L.ship_deck + 0.01, cz], "#6C7A89");
  const ax = xs + 0.75;
  const h = L.ship_deck;
  body.box([1.0, 2.0, beam - 1.2], [ax, h + 1.0, cz], C.ivory)
    .box([1.2, 0.24, beam - 0.3], [ax + 0.05, h + 2.12, cz], C.ivory)
    .box([0.45, 0.8, 0.6], [ax - 0.3, h + 2.6, cz], C.boot)
    .box([0.47, 0.12, 0.62], [ax - 0.3, h + 2.85, cz], C.hull)
    .rod([ax + 0.1, h + 2.24, cz], [ax + 0.1, h + 3.3, cz], 0.025, C.steel);
  const glass = new Parts();
  for (const y of [0.55, 1.05, 1.55]) glass.box([0.01, 0.12, beam - 1.5], [ax + 0.505, h + y, cz], C.glass);
  glass.box([0.01, 0.12, beam - 0.5], [ax + 0.66, h + 2.12, cz], C.glass);
  const detail = new Parts()
    .railing([xs, h, z0 - 0.05], [xb, h, z0 - 0.05], C.ivory, 0.12, Math.ceil(bays * 1.5))
    .railing([xs, h, z1 + 0.05], [xb, h, z1 + 0.05], C.ivory, 0.12, Math.ceil(bays * 1.5))
    .box([0.5, 0.14, 0.18], [ax, h + 1.3, z0 + 0.05], C.amber)       // lifeboats on their davits
    .box([0.5, 0.14, 0.18], [ax, h + 1.3, z1 - 0.05], C.amber)
    .rod([ax - 0.3, h + 1.45, z0 + 0.05], [ax - 0.3, h + 1.2, z0 - 0.05], 0.015, C.steel)
    .rod([ax + 0.3, h + 1.45, z0 + 0.05], [ax + 0.3, h + 1.2, z0 - 0.05], 0.015, C.steel)
    .box([0.5, 0.05, 0.05], [ax + 0.1, h + 3.2, cz], C.steel)         // radar scanner
    .cone(0.1, 0.3, [xb + 1.3, h + 0.2, cz], C.steel, 6);             // forecastle mast
  for (const x of [xs + 0.4, xb - 0.2])
    detail.rod([x, h + 0.1, z0], [x + (x < x0 ? -0.6 : 0.6), 0.1, 0.2], 0.012, "#C9B79C", 4);   // mooring lines
  const glow = new Parts().box([0.05, 0.05, 0.05], [ax + 0.1, h + 3.35, cz], "#FFE8B0")
    .box([0.04, 0.04, 0.04], [xb + 1.3, h + 0.4, cz], "#FFE8B0");
  return { body: body.build(), glass: glass.build(), detail: detail.build(), glow: glow.build(), label: [ax, -0.2, z0 + 0.15] as V3 };
}

function ShipModel({ L, v }: { L: Layout; v: Vessel }) {
  const ref = useRef<THREE.Group>(null!);
  const lines = useRef<THREE.Group>(null);
  const detail = useDetail();
  const b = L.berths.find((x) => x.id === v.berth)!;
  const g = cached(`ship:${b.x0}:${v.bays}:${v.rows}`, () => buildShip(L, b.x0, v.bays, v.rows));
  useFrame(() => {
    const dx = shipOffset(L, world.vessels.get(v.id), simNow());
    ref.current.visible = dx != null;
    if (dx == null) return;
    const t = simNow();
    ref.current.position.set(dx, Math.sin(t * 0.9 + v.berth) * 0.025, 0);
    ref.current.rotation.x = Math.sin(t * 0.7 + v.berth) * 0.004;
    if (lines.current) lines.current.visible = Math.abs(dx) < 0.01;   // mooring lines only while alongside
  });
  return (
    <group ref={ref}>
      <Mesh geo={g.body} />
      <Mesh geo={g.glass} mat={GLASS} shadow={false} />
      <Mesh geo={g.glow} mat={GLOW} shadow={false} />
      {detail && (
        <group ref={lines}>
          <Mesh geo={g.detail} shadow={false} />
        </group>
      )}
      <Html position={g.label} center zIndexRange={[10, 0]}>
        <div className="label">{v.name.toUpperCase()}</div>
      </Html>
    </group>
  );
}

/** One ship per occupied berth; mounts when a vessel id appears, unmounts after it sails. */
export function Ships({ L }: { L: Layout }) {
  const ids = useWorldSelect((w) => [...w.vessels.keys()].join());
  return (
    <>
      {ids
        .split(",")
        .filter(Boolean)
        .map((id) => {
          const v = world.vessels.get(Number(id));
          return v ? <ShipModel key={id} L={L} v={v} /> : null;
        })}
    </>
  );
}

// --- cranes ----------------------------------------------------------------------------------
type CraneProps = { id: string; home: V3; L: Layout };

/** Frame slides along x with the spreader; trolley + hoist cables follow the spreader. */
function useGantry(id: string, home: V3, top: number, L: Layout) {
  const spreader = useMover(id, home);
  const frame = useRef<THREE.Group>(null!);
  const trolley = useRef<THREE.Group>(null!);
  const cable = useRef<THREE.Mesh>(null!);
  useFrame(() => {
    const s = spreader.current.position;
    frame.current.position.x = s.x;
    trolley.current.position.set(s.x, top, s.z);
    const base = s.y + L.box_h + 0.06;
    const h = Math.max(top - base, 0.01);
    cable.current.position.set(s.x, base + h / 2, s.z);
    cable.current.scale.y = h;
  });
  return { spreader, frame, trolley, cable };
}

function spreaderGeo(color: string) {
  const body = new Parts().box([1.0, 0.07, 0.5], [0, 0.03, 0], C.steel).box([0.3, 0.06, 0.22], [0, 0.09, 0], color);
  const detail = new Parts();
  for (const x of [-0.48, 0.48])
    for (const z of [-0.23, 0.23]) {
      detail.cyl(0.018, 0.05, [x, -0.02, z], C.hub, "y", 6);                                       // twistlocks
      detail.box([0.08, 0.1, 0.012], [x + Math.sign(x) * 0.03, -0.03, z + Math.sign(z) * 0.02], color);  // guide flippers
    }
  detail.cyl(0.05, 0.12, [0, 0.13, 0], C.steel, "z", 10);                                           // headblock sheave
  return { body: body.build(), detail: detail.build() };
}

function Cables({ cable }: { cable: RefObject<THREE.Mesh> }) {
  return (
    <mesh ref={cable}>
      <boxGeometry args={[0.2, 1, 0.02]} />
      <meshStandardMaterial color={C.steel} transparent opacity={0.85} />
    </mesh>
  );
}

function Spreader({ L, g, detail, spreader }: { L: Layout; g: ReturnType<typeof spreaderGeo>; detail: boolean; spreader: RefObject<THREE.Group> }) {
  return (
    <group ref={spreader}>
      <group position={[0, L.box_h, 0]}>
        <Mesh geo={g.body} />
        {detail && <Mesh geo={g.detail} shadow={false} />}
      </group>
    </group>
  );
}

function qcGeo(L: Layout) {
  const top = L.qc_clear_y + 0.8;
  const [zw, zl] = L.legs.qc;
  const tip = -(Math.max(...L.berths.map((b) => b.rows)) + 3.2);
  const back = 4.2;
  const apexY = top + 2.0;
  const apexZ = (zw + zl) / 2;
  const f = new Parts();
  for (const z of [zw, zl]) {
    for (const dx of [-0.75, 0.75]) f.box([0.18, top, 0.18], [dx, top / 2, z], C.qc);
    f.box([1.9, 0.18, 0.28], [0, 0.12, z], C.steel).box([1.5, 0.16, 0.16], [0, top * 0.42, z], C.qc);
    for (const x of [-0.8, -0.5, 0.5, 0.8]) f.cyl(0.07, 0.22, [x, 0.07, z], C.tyre, "z", 10);
  }
  for (const dx of [-0.75, 0.75]) {
    f.box([0.14, 0.14, zl - zw], [dx, top * 0.42, apexZ], C.qc)
      .box([0.16, 0.26, back - tip], [dx, top, (tip + back) / 2], C.ivory)
      .rod([dx, top + 0.1, zw], [dx * 0.4, apexY, apexZ], 0.05, C.qc)
      .rod([dx, top + 0.1, zl], [dx * 0.4, apexY, apexZ], 0.05, C.qc)
      .rod([dx * 0.4, apexY, apexZ], [dx, top + 0.1, tip + 0.3], 0.022, C.steel)
      .rod([dx * 0.4, apexY, apexZ], [dx, top + 0.1, (tip + zw) / 2], 0.018, C.steel)
      .rod([dx * 0.4, apexY, apexZ], [dx, top + 0.1, back - 0.2], 0.022, C.steel);
  }
  for (let z = tip + 0.6; z < back; z += 1.5) f.box([1.5, 0.08, 0.08], [0, top + 0.08, z], C.steel);
  f.box([0.7, 0.16, 0.16], [0, apexY, apexZ], C.qc)
    .box([1.6, 0.65, 1.3], [0, top + 0.45, back - 0.8], C.ivory)
    .box([1.62, 0.1, 1.32], [0, top + 0.82, back - 0.8], C.qc);
  const d = new Parts()
    .ladder([0.75, 0.25, zl + 0.14], [0.75, top - 0.1, zl + 0.14], 0.12, "x", C.steel)
    .box([1.5, 0.02, 0.4], [0, top * 0.42 + 0.09, zl + 0.3], C.steel)
    .railing([-0.75, top * 0.42 + 0.1, zl + 0.5], [0.75, top * 0.42 + 0.1, zl + 0.5], C.ivory, 0.16, 5)
    .box([1.2, 0.18, 0.01], [0, top + 0.5, back - 0.14], C.glass);
  for (const dx of [-1.0, 1.0]) {
    d.box([0.22, 0.02, back - tip - 0.4], [dx, top - 0.09, (tip + back) / 2], C.steel)
      .railing([dx + Math.sign(dx) * 0.1, top - 0.08, tip + 0.3], [dx + Math.sign(dx) * 0.1, top - 0.08, back - 0.3], C.ivory, 0.16, 16);
  }
  const glow = new Parts();
  for (const dx of [-0.75, 0.75]) glow.box([0.07, 0.07, 0.07], [dx, top + 0.18, tip + 0.1], "#E0503A");
  glow.box([0.07, 0.07, 0.07], [0, apexY + 0.12, apexZ], "#E0503A");
  for (const z of [-2, -4.5]) glow.box([0.3, 0.03, 0.12], [0, top - 0.16, z], "#FFF1D0");
  const trolley = new Parts().box([1.15, 0.2, 0.65], [0, -0.05, 0], C.steel).box([0.32, 0.32, 0.34], [0.62, -0.33, 0.12], C.ivory);
  const trolleyGlass = new Parts().box([0.01, 0.16, 0.28], [0.785, -0.31, 0.12], C.glass).box([0.24, 0.005, 0.26], [0.62, -0.495, 0.12], C.glass);
  const trolleyDetail = new Parts();
  for (const x of [-0.4, 0.4]) for (const z of [-0.3, 0.3]) trolleyDetail.cyl(0.05, 0.04, [x, 0.06, z], C.hub, "z", 8);
  return { top, frame: f.build(), detail: d.build(), glow: glow.build(), trolley: trolley.build(),
           trolleyGlass: trolleyGlass.build(), trolleyDetail: trolleyDetail.build(), spreader: spreaderGeo(C.qc) };
}

export function QuayCrane({ id, home, L }: CraneProps) {
  const g = cached("qc", () => qcGeo(L));
  const detail = useDetail();
  const { spreader, frame, trolley, cable } = useGantry(id, home, g.top, L);
  return (
    <group>
      <group ref={frame} position={[home[0], 0, 0]}>
        <Mesh geo={g.frame} />
        <Mesh geo={g.glow} mat={GLOW} shadow={false} />
        {detail && <Mesh geo={g.detail} shadow={false} />}
      </group>
      <group ref={trolley}>
        <Mesh geo={g.trolley} />
        <Mesh geo={g.trolleyGlass} mat={GLASS} shadow={false} />
        {detail && <Mesh geo={g.trolleyDetail} shadow={false} />}
      </group>
      <Cables cable={cable} />
      <Spreader L={L} g={g.spreader} detail={detail} spreader={spreader} />
    </group>
  );
}

function gantryGeo(L: Layout, kind: "RTG" | "RMG") {
  const rtg = kind === "RTG";
  const top = (rtg ? L.rtg_clear_y : L.rmg_clear_y) + (rtg ? 0.55 : 0.75);
  const [zA, zB] = rtg ? L.legs.rtg : L.legs.rmg;
  const color = rtg ? C.rtg : C.rmg;
  const f = new Parts();
  const d = new Parts();
  for (const z of [zA, zB]) {
    for (const dx of [-0.62, 0.62]) {
      f.box([0.15, top - 0.1, 0.18], [dx, top / 2 + 0.12, z], color);
      if (rtg)
        for (const w of [-0.12, 0.12]) {
          f.cyl(0.11, 0.12, [dx + w, 0.1, z], C.tyre, "z", 12);
          d.cyl(0.05, 0.13, [dx + w, 0.1, z], C.hub, "z", 8);
        }
      else f.box([0.42, 0.14, 0.2], [dx, 0.08, z], C.steel);
    }
    f.box([1.42, 0.22, 0.24], [0, top, z], color).box([1.42, 0.1, 0.16], [0, 0.24, z], color)
      .rod([-0.62, 0.3, z], [0, top - 0.1, z], 0.025, C.steel).rod([0.62, 0.3, z], [0, top - 0.1, z], 0.025, C.steel);
  }
  for (const dx of [-0.62, 0.62]) f.box([0.16, 0.28, zB - zA + 0.2], [dx, top + 0.06, (zA + zB) / 2], color);
  f.box([0.32, 0.32, 0.3], [0.4, top - 0.55, zB - 0.05], C.ivory).box([0.5, 0.5, 0.5], [-0.4, top + 0.42, zA + 0.3], C.ivory);
  d.ladder([-0.62, 0.3, zA - 0.14], [-0.62, top - 0.05, zA - 0.14], 0.12, "x", C.steel)
    .railing([-0.75, top + 0.2, zA], [-0.75, top + 0.2, zB], C.ivory, 0.14, 12)
    .railing([0.75, top + 0.2, zA], [0.75, top + 0.2, zB], C.ivory, 0.14, 12)
    .box([0.01, 0.16, 0.22], [0.565, top - 0.53, zB - 0.05], C.glass);
  if (!rtg) d.cyl(0.18, 0.12, [0.7, top - 0.5, zA], C.steel, "x", 14);   // power cable reel
  const glow = new Parts().box([0.06, 0.06, 0.06], [0.62, top + 0.18, zA], C.amber).box([0.06, 0.06, 0.06], [-0.62, top + 0.18, zB], C.amber);
  const trolley = new Parts().box([1.1, 0.2, 0.6], [0, 0.22, 0], C.steel);
  return { top, frame: f.build(), detail: d.build(), glow: glow.build(), trolley: trolley.build(), spreader: spreaderGeo(color) };
}

export function Gantry({ id, home, L, kind }: CraneProps & { kind: "RTG" | "RMG" }) {
  const g = cached(kind, () => gantryGeo(L, kind));
  const detail = useDetail();
  const { spreader, frame, trolley, cable } = useGantry(id, home, g.top, L);
  return (
    <group>
      <group ref={frame} position={[home[0], 0, 0]}>
        <Mesh geo={g.frame} />
        <Mesh geo={g.glow} mat={GLOW} shadow={false} />
        {detail && <Mesh geo={g.detail} shadow={false} />}
      </group>
      <group ref={trolley}>
        <Mesh geo={g.trolley} />
      </group>
      <Cables cable={cable} />
      <Spreader L={L} g={g.spreader} detail={detail} spreader={spreader} />
    </group>
  );
}

// --- landside fixtures -----------------------------------------------------------------------
const scannerGeo = (() => {
  const p = new Parts();
  for (const z of [-0.75, 0.75]) {
    for (const x of [-0.75, 0.75]) p.box([0.18, 1.7, 0.16], [x, 0.85, z], C.ivory);
    p.box([1.5, 0.08, 0.06], [0, 0.6, z], C.boot);
  }
  p.box([1.8, 0.2, 1.8], [0, 1.78, 0], C.ivory).box([1.6, 0.02, 1.6], [0, 1.89, 0], C.boot);
  const d = new Parts().box([0.6, 0.25, 0.03], [0, 1.6, 0.92], C.officer).box([0.2, 0.4, 0.2], [1.2, 0.2, 0.9], C.steel);
  return { body: p.build(), detail: d.build() };
})();

/** Drive-through X-ray portal. A light bar sweeps the vehicle while a scan is live. */
export function Scanner({ id, pos, across }: { id: string; pos: V3; across: "x" | "z" }) {
  const bar = useRef<THREE.Mesh>(null!);
  const lamp = useRef<THREE.MeshStandardMaterial>(null!);
  const detail = useDetail();
  useFrame(() => {
    const s = world.scans.get(id);
    const now = simNow();
    const live = !!s && now >= s.t0 && now < s.t1;
    bar.current.visible = live;
    if (live) bar.current.position.x = Math.sin((now - s!.t0) * Math.PI) * 0.7;
    lamp.current.emissiveIntensity = live ? (Math.sin(simNow() * 8) > 0 ? 2.2 : 0.4) : 0.15;
  });
  return (
    <group position={pos} rotation-y={across === "x" ? Math.PI / 2 : 0}>
      <Mesh geo={scannerGeo.body} />
      {detail && <Mesh geo={scannerGeo.detail} shadow={false} />}
      <mesh position={[0.7, 1.98, 0.7]}>
        <cylinderGeometry args={[0.06, 0.06, 0.1, 10]} />
        <meshStandardMaterial ref={lamp} color={C.amber} emissive={C.amber} emissiveIntensity={0.15} />
      </mesh>
      <mesh ref={bar} position={[0, 0.85, 0]} visible={false}>
        <boxGeometry args={[0.05, 1.5, 1.4]} />
        <meshStandardMaterial color="#9EE6FF" emissive="#6FD6FF" emissiveIntensity={2} transparent opacity={0.45} />
      </mesh>
    </group>
  );
}

const gateGeo = (() => {
  const p = new Parts();
  for (const dx of [-1.1, 1.1]) for (const dz of [-1.2, 1.3]) p.box([0.1, 1.6, 0.1], [dx, 0.8, dz], C.steel);
  p.box([2.6, 0.14, 3.0], [0, 1.65, 0.05], C.ivory).box([2.62, 0.06, 3.02], [0, 1.55, 0.05], C.boot)
    .box([0.6, 0.84, 0.5], [0.2, 0.42, 1.0], C.ivory).box([0.12, 0.5, 0.12], [0.7, 0.25, 0.65], C.steel);
  const glass = new Parts().box([0.46, 0.22, 0.01], [0.2, 0.55, 0.745], C.glass);
  const d = new Parts().box([0.06, 0.6, 0.06], [-0.9, 0.3, -0.9], C.steel).box([0.1, 0.25, 0.08], [-0.9, 0.72, -0.9], C.glass)
    .box([1.6, 0.3, 0.03], [0, 1.95, -1.35], C.officer);
  const glow = new Parts().box([0.04, 0.04, 0.04], [-0.9, 0.78, -0.86], "#81E07A").box([0.04, 0.04, 0.04], [-0.9, 0.68, -0.86], "#E0503A");
  return { body: p.build(), glass: glass.build(), detail: d.build(), glow: glow.build() };
})();

/** Gate canopy with booth; the barrier lifts when a lorry is close. */
export function GateHouse({ x, z, label }: { x: number; z: number; label: string }) {
  const arm = useRef<THREE.Group>(null!);
  const detail = useDetail();
  useFrame((_, dt) => {
    const now = simNow();
    let near = false;
    for (const id of world.lorries) {
      const tr = world.tracks.get(id);
      if (!tr) continue;
      trackAt(tr, now, tmp);
      if (Math.abs(tmp[0] - x) < 1.6 && Math.abs(tmp[2] - z) < 0.6) near = true;
    }
    const want = near ? Math.PI / 2.3 : 0;
    arm.current.rotation.x += (want - arm.current.rotation.x) * Math.min(1, dt * 6);
  });
  return (
    <group position={[x, 0, z]}>
      <Mesh geo={gateGeo.body} />
      <Mesh geo={gateGeo.glass} mat={GLASS} shadow={false} />
      <Mesh geo={gateGeo.glow} mat={GLOW} shadow={false} />
      {detail && <Mesh geo={gateGeo.detail} shadow={false} />}
      <group ref={arm} position={[0.7, 0.45, 0.65]}>
        <mesh position={[0, 0, -0.65]}>
          <boxGeometry args={[0.06, 0.06, 1.3]} />
          <meshStandardMaterial color={C.boot} />
        </mesh>
      </group>
      <Html position={[0, 1.95, 0]} center zIndexRange={[10, 0]}>
        <div className="label">{label}</div>
      </Html>
    </group>
  );
}

// --- people ----------------------------------------------------------------------------------
const personGeo = (vest: string, hat: string) =>
  new Parts()
    .box([0.075, 0.13, 0.05], [0, 0.065, 0], "#3D405B")
    .box([0.1, 0.12, 0.065], [0, 0.19, 0], vest)
    .box([0.11, 0.015, 0.07], [0, 0.2, 0], C.ivory)          // reflective band
    .box([0.055, 0.055, 0.055], [0, 0.28, 0], "#E8C4A6")
    .box([0.07, 0.025, 0.07], [0, 0.318, 0], hat)
    .build()
    .scale(1.5, 1.5, 1.5);                                   // diorama scale: people read at a glance
const WORKER = personGeo(C.vest, "#F2CC8F");
const OFFICER = personGeo(C.officer, C.ivory);

type Walker = { kind: "worker" | "officer"; at: (t: number, out: V3) => number };   // returns heading

/** Everyone on foot, in two instanced meshes. Positions are pure functions of time (and crane poses). */
export function People({ L }: { L: Layout }) {
  const workers = useRef<THREE.InstancedMesh>(null!);
  const officers = useRef<THREE.InstancedMesh>(null!);
  const walkers = useMemo<Walker[]>(() => {
    const out: Walker[] = [];
    const pace = (x0: number, x1: number, z: number, speed: number, phase: number) => (t: number, o: V3) => {
      const u = (Math.sin(t * speed + phase) + 1) / 2;
      o[0] = x0 + (x1 - x0) * u;
      o[1] = Math.abs(Math.sin(t * 7 + phase)) * 0.012;
      o[2] = z;
      return Math.cos(t * speed + phase) > 0 ? 0 : Math.PI;
    };
    // lashers on the quay apron, working under each quay crane
    for (const u of L.units.filter((u) => u.kind === "QC"))
      for (const k of [0, 1])
        out.push({
          kind: "worker",
          at: (t, o) => {
            const tr = world.tracks.get(u.id);
            const cx = tr ? trackAt(tr, simNow(), tmp)[0] : u.home[0];
            o[0] = cx + (k ? 0.6 : -0.6) + Math.sin(t * 0.6 + k * 2) * 0.35;
            o[1] = Math.abs(Math.sin(t * 7 + k)) * 0.012;
            o[2] = 0.6;
            return Math.cos(t * 0.6 + k * 2) > 0 ? 0 : Math.PI;
          },
        });
    // yard checkers walking the strip between the stacks and the lorry spots
    for (const b of L.blocks) out.push({ kind: "worker", at: pace(b.x0 + 1, b.x0 + b.bays, b.z0 + b.rows + 1.3, 0.25, b.x0) });
    // gate clerks, customs officers at both portals and the shed
    for (const gx of [L.lanes.gate_in_x, L.lanes.gate_out_x]) out.push({ kind: "officer", at: pace(gx + 0.1, gx + 0.3, L.lanes.thru_z + 1.35, 0.4, gx) });
    for (const s of L.scanners)
      for (const side of [-1, 1])
        out.push({
          kind: "officer",
          at:
            s.across === "z"
              ? pace(s.pos[0] - 0.4, s.pos[0] + 0.4, s.pos[2] + side * 1.05, 0.3, side)
              : (t, o) => {
                  o[0] = s.pos[0] + side * 1.05;
                  o[1] = 0;
                  o[2] = s.pos[2] + Math.sin(t * 0.3 + side) * 0.4;
                  return Math.PI / 2;
                },
        });
    out.push({ kind: "officer", at: pace(L.customs[0] - 1.2, L.customs[0] + 1.6, L.customs[1] + 1.6, 0.2, 1) });
    // rail crew beside the siding
    for (const k of [0, 1]) out.push({ kind: "worker", at: pace(L.rail.x0 + 2 + k * 9, L.rail.x0 + 8 + k * 9, L.rail.z + 1.2, 0.18, k * 3) });
    return out;
  }, [L]);
  const nWorkers = walkers.filter((w) => w.kind === "worker").length;
  const nOfficers = walkers.length - nWorkers;
  const m = useMemo(() => new THREE.Matrix4(), []);
  const q = useMemo(() => new THREE.Quaternion(), []);
  const one = useMemo(() => new THREE.Vector3(1, 1, 1), []);
  const p = useMemo(() => new THREE.Vector3(), []);
  useFrame(() => {
    const t = simNow();
    let wi = 0;
    let oi = 0;
    const pos: V3 = [0, 0, 0];
    for (const w of walkers) {
      q.setFromAxisAngle(THREE.Object3D.DEFAULT_UP, w.at(t, pos));
      m.compose(p.set(pos[0], pos[1], pos[2]), q, one);
      if (w.kind === "worker") workers.current.setMatrixAt(wi++, m);
      else officers.current.setMatrixAt(oi++, m);
    }
    workers.current.instanceMatrix.needsUpdate = true;
    officers.current.instanceMatrix.needsUpdate = true;
  });
  return (
    <>
      <instancedMesh ref={workers} args={[WORKER, MAT, nWorkers]} castShadow frustumCulled={false} />
      <instancedMesh ref={officers} args={[OFFICER, MAT, nOfficers]} castShadow frustumCulled={false} />
    </>
  );
}
