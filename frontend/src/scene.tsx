import { Html, OrbitControls, OrthographicCamera, PerformanceMonitor } from "@react-three/drei";
import { Canvas, useFrame, useThree, type ThreeEvent } from "@react-three/fiber";
import { EffectComposer, N8AO, TiltShift2, Vignette } from "@react-three/postprocessing";
import { Component, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import * as THREE from "three";
import type { OrbitControls as OrbitControlsImpl } from "three-stdlib";
import { Agv, C, Gantry, GateHouse, Lorry, People, QuayCrane, Scanner, Ships, Train, setView, useDetail, useQuality, view } from "./models";
import { MAT, Parts } from "./parts";
import { boxPos, simNow, useWorldSelect, world, type Focus, type Layout, type V3 } from "./world";

// quality: ?q=0 flat, ?q=1 shadows, ?q=2 shadows + tilt-shift/AO. Without ?q it adapts to the frame rate.
const params = new URLSearchParams(location.search);
const FORCED_Q = params.has("q") ? Math.max(0, Math.min(2, Number(params.get("q")))) : null;
if (FORCED_Q != null) view.quality = FORCED_Q;

const WEST = -38;
const EAST = 92;
const SPAN = EAST - WEST;
const MIDX = (WEST + EAST) / 2;
const DETAIL_ON = 40;
const DETAIL_OFF = 34;

// --- camera ----------------------------------------------------------------------------------
export const FOCUS: Record<Focus, { label: string; t: V3; zoom: number }> = {
  overview: { label: "Overview", t: [26, 0, 6], zoom: 11 },
  quay: { label: "Berths 1–2", t: [14, 1, -1.5], zoom: 26 },
  berth3: { label: "Berth 3", t: [36, 1, -2.5], zoom: 28 },
  yard: { label: "Yard", t: [28, 0, 6.5], zoom: 21 },
  gate: { label: "In-gate", t: [-9, 0, 11], zoom: 30 },
  customs: { label: "Customs", t: [60, 0, 9], zoom: 30 },
  rail: { label: "Rail", t: [18, 0, 14], zoom: 24 },
};

/** Glides target + zoom to the chosen preset, then hands control back. Switches close-up detail. */
function Rig() {
  const controls = useRef<OrbitControlsImpl>(null!);
  const goal = useRef<{ t: THREE.Vector3; zoom: number } | null>(null);
  const last = useRef(world.focus);
  useFrame(({ camera }, dt) => {
    if (world.focus !== last.current) {
      last.current = world.focus;
      const f = FOCUS[world.focus];
      goal.current = { t: new THREE.Vector3(...f.t), zoom: f.zoom };
    }
    const g = goal.current;
    if (g && controls.current) {
      const k = 1 - Math.exp(-dt * 4);
      const step = g.t.clone().sub(controls.current.target).multiplyScalar(k);
      controls.current.target.add(step);
      camera.position.add(step);
      camera.zoom += (g.zoom - camera.zoom) * k;
      camera.updateProjectionMatrix();
      controls.current.update();
      if (step.length() < 0.002 && Math.abs(g.zoom - camera.zoom) < 0.05) goal.current = null;
    }
    const z = (camera as THREE.OrthographicCamera).zoom;
    if (!view.detail && z >= DETAIL_ON) setView({ detail: true });
    else if (view.detail && z <= DETAIL_OFF) setView({ detail: false });
  });
  const t = FOCUS.overview.t;
  return (
    <>
      <OrthographicCamera makeDefault position={[t[0] + 15, 30, t[2] + 33]} zoom={FOCUS.overview.zoom} near={-300} far={500} />
      <OrbitControls ref={controls} target={t} enableDamping minZoom={7} maxZoom={180} maxPolarAngle={1.3} minPolarAngle={0.3} />
    </>
  );
}

// --- terrain + environment -------------------------------------------------------------------
function Water() {
  const geo = useMemo(() => new THREE.PlaneGeometry(SPAN + 100, 50, 110, 26), []);
  const base = useMemo(() => Float32Array.from(geo.attributes.position.array), [geo]);
  useFrame(({ clock }) => {
    const t = clock.elapsedTime;
    const pos = geo.attributes.position as THREE.BufferAttribute;
    for (let i = 0; i < pos.count; i++) {
      const x = base[i * 3];
      const y = base[i * 3 + 1];
      pos.setZ(i, Math.sin(x * 0.55 + t * 1.1) * 0.045 + Math.cos(y * 0.9 + x * 0.2 + t * 0.8) * 0.035);
    }
    pos.needsUpdate = true;
    geo.computeVertexNormals();
  });
  return (
    <>
      <mesh geometry={geo} rotation-x={-Math.PI / 2} position={[MIDX, -0.2, -25]} receiveShadow>
        <meshStandardMaterial color={C.water} flatShading roughness={0.25} metalness={0.05} transparent opacity={0.9} />
      </mesh>
      <mesh position={[MIDX, -0.62, -25]}>
        <boxGeometry args={[SPAN + 100, 0.2, 50]} />
        <meshStandardMaterial color={C.waterDeep} />
      </mesh>
    </>
  );
}

/** One instanced mesh for many identical static props. */
function Instances({ items, children, cast = true }: { items: { p: V3; s?: V3 }[]; children: ReactNode; cast?: boolean }) {
  const set = (m: THREE.InstancedMesh | null) => {
    if (!m) return;
    const mat = new THREE.Matrix4();
    items.forEach((it, i) =>
      m.setMatrixAt(i, mat.compose(new THREE.Vector3(...it.p), new THREE.Quaternion(), new THREE.Vector3(...(it.s ?? [1, 1, 1])))),
    );
    m.instanceMatrix.needsUpdate = true;
    m.computeBoundingSphere();
  };
  return (
    <instancedMesh ref={set} args={[undefined, undefined, items.length]} castShadow={cast} receiveShadow>
      {children}
    </instancedMesh>
  );
}

/** Ground paint (lanes, pads, ballast, rails) merged into one flat geometry. */
function groundGeo(L: Layout) {
  const p = new Parts();
  const strip = (x0: number, x1: number, z: number, w: number, color: string, y = 0.004) => p.box([x1 - x0, 0.004, w], [(x0 + x1) / 2, y, z], color);
  strip(WEST, EAST, 1.85, 3.7, C.apron, 0.002);
  for (const z of L.legs.qc) strip(-6, 52, z, 0.1, C.rail, 0.008);
  for (const z of L.legs.rmg) strip(4, 32, z, 0.12, C.rail, 0.01);
  strip(WEST, EAST, (L.lanes.load_z + L.lanes.thru_z) / 2, 2.1, C.lane);
  strip(WEST, EAST, L.rail.z, 1.3, C.ballast, 0.006);
  for (const b of L.blocks)
    strip(b.x0 + 0.4, b.x0 + b.bays + 0.6, b.z0 + (b.rows + 1) / 2, b.rows + 0.2, b.hazmat ? C.padHazmat : b.purpose === "EXPORT" ? C.padExport : C.padImport, 0.008);
  strip(56.5, 66.5, 6.5, 5.6, "#EADBD2", 0.007);
  strip(-18, -9, 8.6, 1.6, C.lane, 0.006);
  for (const dz of [-0.22, 0.22]) p.box([SPAN, 0.05, 0.05], [MIDX, 0.08, L.rail.z + dz], C.rail);
  p.box([SPAN, 0.08, 0.1], [MIDX, -0.02, 0.04], C.steel);
  return p.build();
}

function Terrain({ L }: { L: Layout }) {
  const ground = useMemo(() => groundGeo(L), [L]);
  const roads = useMemo(() => L.roads.map(([x, z]) => ({ p: [x, 0.006, z] as V3 })), [L]);
  const spurs = useMemo(() => L.spurs.map(([x, z]) => ({ p: [x, 0.008, z] as V3 })), [L]);
  const parks = useMemo(() => L.parks.map(([x, z]) => ({ p: [x, 0.01, z] as V3 })), [L]);
  const sleepers = useMemo(() => Array.from({ length: SPAN * 2 }, (_, i) => ({ p: [WEST + i / 2, 0.03, L.rail.z] as V3 })), [L]);
  const dashes = useMemo(() => Array.from({ length: SPAN / 2 }, (_, i) => ({ p: [WEST + i * 2 + 0.5, 0.012, (L.lanes.load_z + L.lanes.thru_z) / 2] as V3 })), [L]);
  const bollards = useMemo(() => Array.from({ length: 36 }, (_, i) => ({ p: [-3 + i * 1.5, 0.08, 0.12] as V3 })), []);
  return (
    <group>
      <mesh rotation-x={-Math.PI / 2} position={[MIDX, -0.74, 4]} receiveShadow>
        <planeGeometry args={[500, 500]} />
        <meshStandardMaterial color={C.table} />
      </mesh>
      <Water />
      <mesh position={[MIDX, -0.3, 9.5]} receiveShadow>
        <boxGeometry args={[SPAN, 0.6, 19]} />
        <meshStandardMaterial color={C.land} />
      </mesh>
      <mesh geometry={ground} material={MAT} receiveShadow />
      <Instances items={roads} cast={false}>
        <boxGeometry args={[1.02, 0.012, 0.92]} />
        <meshStandardMaterial color={C.asphalt} roughness={0.95} />
      </Instances>
      <Instances items={spurs} cast={false}>
        <boxGeometry args={[0.86, 0.012, 0.8]} />
        <meshStandardMaterial color={C.spur} roughness={0.95} />
      </Instances>
      <Instances items={parks} cast={false}>
        <boxGeometry args={[0.86, 0.014, 0.8]} />
        <meshStandardMaterial color="#8C90A8" roughness={0.95} />
      </Instances>
      <Instances items={dashes} cast={false}>
        <boxGeometry args={[1, 0.005, 0.06]} />
        <meshStandardMaterial color={C.marking} />
      </Instances>
      <Instances items={sleepers} cast={false}>
        <boxGeometry args={[0.12, 0.05, 0.8]} />
        <meshStandardMaterial color={C.sleeper} />
      </Instances>
      <Instances items={bollards}>
        <cylinderGeometry args={[0.07, 0.09, 0.16, 8]} />
        <meshStandardMaterial color={C.steel} />
      </Instances>
      {L.blocks.map((b) => (
        <Html key={b.code} position={[b.x0 + (b.bays + 1) / 2, 0, b.z0 - 0.35]} center zIndexRange={[10, 0]}>
          <div className={`label ${b.purpose === "EXPORT" ? "export" : ""}`}>
            {b.code} · {b.purpose}
            {b.hazmat && <span> · IMDG</span>}
          </div>
        </Html>
      ))}
      {L.berths.map((b) => (
        <Html key={b.id} position={[b.x0 + b.bays / 2 + 0.5, 0, 0.4]} center zIndexRange={[10, 0]}>
          <div className="label quay">BERTH {b.id}</div>
        </Html>
      ))}
      <Html position={[-1, 0, L.rail.z + 1.2]} center zIndexRange={[10, 0]}>
        <div className="label">RAIL TERMINAL</div>
      </Html>
    </group>
  );
}

function sceneryGeo(L: Layout) {
  const p = new Parts();
  // light masts in the gaps between blocks
  for (const b of L.blocks)
    for (const z of [b.z0 - 0.6, b.z0 + b.rows + 1.4]) {
      const x = b.x0 + b.bays + 1.5;
      p.cyl(0.05, 4.8, [x, 2.4, z], "#B8BCC8", "y", 8, 0.035).box([0.6, 0.12, 0.3], [x, 4.85, z], "#FFF4D6");
    }
  // port control: low office + glass control room
  p.box([4, 1.8, 2.6], [-14, 0.9, 5], C.ivory).box([4.1, 0.08, 2.7], [-14, 1.84, 5], C.steel);
  for (const y of [0.45, 0.9, 1.35]) p.box([3.6, 0.14, 0.01], [-14, y, 6.305], C.glass);
  p.box([1.8, 0.7, 1.6], [-14, 2.2, 5], "#BFD7E0");
  // car park
  ["#E5989B", "#6D9DC5", "#E9C46A", "#A3B18A", "#B8BCC8", "#9C89B8"].forEach((c, i) => p.box([0.45, 0.24, 0.8], [-17.3 + i * 1.3, 0.15, 8.6], c));
  // customs shed
  const [cx, cz] = L.customs;
  p.box([3.4, 1.3, 2.4], [cx + 0.6, 0.65, cz], C.ivory).box([3.5, 0.08, 2.5], [cx + 0.6, 1.34, cz], C.boot)
    .box([0.02, 0.9, 1.4], [cx - 1.11, 0.5, cz], "#B8BCC8").box([2.8, 0.14, 0.01], [cx + 0.6, 0.85, cz + 1.205], C.glass);
  // perimeter fence along the back
  p.box([SPAN, 0.04, 0.02], [MIDX, 0.5, 18.9], C.hub).box([SPAN, 0.04, 0.02], [MIDX, 0.25, 18.9], C.hub);
  for (let x = WEST + 1; x < EAST; x += 3) p.box([0.04, 0.55, 0.04], [x, 0.27, 18.9], C.hub);
  return p.build();
}

function Scenery({ L }: { L: Layout }) {
  const geo = useMemo(() => sceneryGeo(L), [L]);
  const trees = useMemo(() => {
    const out: { p: V3; s: V3 }[] = [];
    for (let x = WEST + 1; x < EAST; x += 2.4) {
      const s = 0.8 + Math.abs(Math.sin(x * 3.1)) * 0.4;
      out.push({ p: [x + Math.sin(x) * 0.4, 0, 17.9 + Math.cos(x * 1.7) * 0.4], s: [s, s, s] });
    }
    for (let z = 3; z < 9; z += 1.6) out.push({ p: [WEST + 1.5, 0, z], s: [0.9, 0.9, 0.9] }, { p: [EAST - 1.5, 0, z + 0.5], s: [1, 1, 1] });
    return out;
  }, []);
  const part = (dy: number) => trees.map((t) => ({ p: [t.p[0], dy * t.s[1], t.p[2]] as V3, s: t.s }));
  return (
    <group>
      <mesh geometry={geo} material={MAT} castShadow receiveShadow />
      <Instances items={part(0.25)}>
        <cylinderGeometry args={[0.06, 0.08, 0.5, 6]} />
        <meshStandardMaterial color="#8A6F55" />
      </Instances>
      <Instances items={part(0.85)}>
        <coneGeometry args={[0.45, 1.0, 7]} />
        <meshStandardMaterial color="#81B29A" flatShading />
      </Instances>
      <Instances items={part(1.3)}>
        <coneGeometry args={[0.32, 0.7, 7]} />
        <meshStandardMaterial color="#94C2A6" flatShading />
      </Instances>
      <Html position={[-14, 3, 5]} center zIndexRange={[10, 0]}>
        <div className="label">PORT CONTROL</div>
      </Html>
      <Html position={[L.customs[0] + 0.6, 1.9, L.customs[1]]} center zIndexRange={[10, 0]}>
        <div className="label">
          CUSTOMS <span>X-RAY</span>
        </div>
      </Html>
      {L.scanners.map((s) => (
        <Scanner key={s.id} id={s.id} pos={s.pos} across={s.across} />
      ))}
      <GateHouse x={L.lanes.gate_in_x} z={L.lanes.thru_z} label="IN-GATE" />
      <GateHouse x={L.lanes.gate_out_x} z={L.lanes.thru_z} label="OUT-GATE" />
      <Tug />
    </group>
  );
}

function Tug() {
  const ref = useRef<THREE.Group>(null!);
  const geo = useMemo(
    () => new Parts().box([1.4, 0.35, 0.6], [0, 0.1, 0], C.boot).box([0.6, 0.4, 0.45], [-0.15, 0.45, 0], C.ivory).cyl(0.07, 0.3, [-0.35, 0.8, 0], C.steel).build(),
    [],
  );
  useFrame(({ clock }) => {
    const t = clock.elapsedTime;
    ref.current.position.set(48 + Math.sin(t * 0.12) * 6, -0.12 + Math.sin(t * 1.3) * 0.04, -12 + Math.cos(t * 0.12) * 2);
    ref.current.rotation.set(Math.sin(t * 1.1) * 0.03, Math.cos(t * 0.12) * 0.6 + Math.PI, Math.sin(t * 0.9) * 0.04);
  });
  return (
    <group ref={ref}>
      <mesh geometry={geo} material={MAT} castShadow />
    </group>
  );
}

// --- containers: one instanced mesh, rebuilt every frame from the world mirror ---------------
const MAX_BOXES = 1600;
const tmpM = new THREE.Matrix4();
const tmpQ = new THREE.Quaternion();
const tmpS = new THREE.Vector3();
const tmpP = new THREE.Vector3();
const tmpC = new THREE.Color();
const tmpV: V3 = [0, 0, 0];

function hashPick<T>(id: string, xs: T[]): T {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) | 0;
  return xs[Math.abs(h) % xs.length];
}

/** Corrugated side panels with a darker frame, multiplied by each instance's colour. */
function ribTexture() {
  const c = document.createElement("canvas");
  c.width = 128;
  c.height = 64;
  const g = c.getContext("2d")!;
  g.fillStyle = "#ffffff";
  g.fillRect(0, 0, 128, 64);
  g.fillStyle = "rgba(0,0,0,0.12)";
  for (let x = 6; x < 122; x += 7) g.fillRect(x, 5, 3, 54);
  g.fillStyle = "rgba(0,0,0,0.22)";
  g.fillRect(0, 0, 128, 5);
  g.fillRect(0, 59, 128, 5);
  g.fillRect(0, 0, 5, 64);
  g.fillRect(123, 0, 5, 64);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 4;
  return t;
}

function Containers({ L, selected, onPick }: { L: Layout; selected: string | null; onPick: (id: string) => void }) {
  const mesh = useRef<THREE.InstancedMesh>(null!);
  const order = useRef<string[]>([]);
  const map = useMemo(ribTexture, []);
  useFrame(() => {
    const now = simNow();
    const m = mesh.current;
    let n = 0;
    order.current.length = 0;
    for (const b of world.boxes.values()) {
      if (n >= MAX_BOXES || !boxPos(L, b, now, tmpV)) continue;
      const forty = b.iso_type.startsWith("4");
      tmpP.set(tmpV[0], tmpV[1] + L.box_h * 0.46, tmpV[2]);
      tmpS.set(forty ? 0.94 : 0.46, L.box_h * 0.92, 0.78);
      m.setMatrixAt(n, tmpM.compose(tmpP, tmpQ, tmpS));
      // exports wear the colour of their port of discharge, so the stowage rule shows on deck
      const color =
        b.id === selected ? C.selected
        : b.state === "RETRIEVING" ? C.retrieving
        : b.hazmat ? C.hazmat
        : b.pod ? C.pod[b.pod]
        : b.iso_type.endsWith("R1") ? C.reefer
        : hashPick(b.id, C.dry);
      m.setColorAt(n, tmpC.set(color));
      order.current.push(b.id);
      n++;
    }
    m.count = n;
    m.instanceMatrix.needsUpdate = true;
    if (m.instanceColor) m.instanceColor.needsUpdate = true;
    m.computeBoundingSphere();
  });
  const click = (e: ThreeEvent<MouseEvent>) => {
    e.stopPropagation();
    if (e.instanceId != null) onPick(order.current[e.instanceId]);
  };
  return (
    <instancedMesh ref={mesh} args={[undefined, undefined, MAX_BOXES]} castShadow receiveShadow onClick={click}>
      <boxGeometry />
      <meshStandardMaterial map={map} roughness={0.65} />
    </instancedMesh>
  );
}

function Lorries() {
  const ids = useWorldSelect((w) => [...w.lorries].join());
  return (
    <>
      {ids
        .split(",")
        .filter(Boolean)
        .map((id) => (
          <Lorry key={id} id={id} />
        ))}
    </>
  );
}

function Badges({ L }: { L: Layout }) {
  useWorldSelect((w) => [...w.reshuffling.values()].map((r) => `${r.block}${r.moves}${r.target}`).join());
  return (
    <>
      {/* one always-mounted tag per block: mounting/unmounting Html roots mid-render upsets React 19 */}
      {L.blocks.map((b) => {
        const r = world.reshuffling.get(b.code);
        return (
          <Html key={b.code} position={[b.x0 + (b.bays + 1) / 2, L.rtg_clear_y + 1.5, b.z0 + (b.rows + 1) / 2]} center zIndexRange={[20, 0]}>
            <div className="badge-rehandle" hidden={!r}>
              RE-RECYCLE IN PROGRESS
              {r && (
                <span>
                  {r.moves} shift{r.moves > 1 ? "s" : ""} over {r.target}
                </span>
              )}
            </div>
          </Html>
        );
      })}
    </>
  );
}

function Lights() {
  const quality = useQuality();
  const target = useMemo(() => {
    const o = new THREE.Object3D();
    o.position.set(26, 0, 4);
    return o;
  }, []);
  return (
    <>
      <hemisphereLight args={["#FFF4E6", "#C9D6DF", 1.15]} />
      <primitive object={target} />
      <directionalLight
        target={target}
        position={[4, 34, 26]}
        intensity={2.3}
        color="#FFE1C2"
        castShadow={quality >= 1}
        shadow-mapSize={[2048, 2048]}
        shadow-camera-left={-70}
        shadow-camera-right={70}
        shadow-camera-top={34}
        shadow-camera-bottom={-34}
        shadow-camera-near={1}
        shadow-camera-far={140}
        shadow-bias={-0.0004}
      />
    </>
  );
}

/** Adapts quality to the frame rate unless ?q= pins it; keeps device pixel ratio in step. */
function Quality() {
  const quality = useQuality();
  const setDpr = useThree((s) => s.setDpr);
  useEffect(() => setDpr(quality >= 2 ? Math.min(1.5, window.devicePixelRatio) : 1), [quality, setDpr]);
  if (FORCED_Q != null) return null;
  return (
    <PerformanceMonitor
      onDecline={() => setView({ quality: Math.max(0, view.quality - 1) })}
      onIncline={() => setView({ quality: Math.min(2, view.quality + 1) })}
      flipflops={3}
      onFallback={() => setView({ quality: 0 })}
    />
  );
}

/** If post-processing throws (lost context, driver quirk), drop to the shadows tier instead of losing the canvas. */
class FxBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  componentDidCatch() {
    setView({ quality: 1 });
  }
  render() {
    return this.state.failed ? null : this.props.children;
  }
}

/** Tracks WebGL context loss: effects must not mount on a dead context; three restores the rest. */
function useContextAlive() {
  const gl = useThree((s) => s.gl);
  const [alive, setAlive] = useState(() => !gl.getContext().isContextLost());
  useEffect(() => {
    const el = gl.domElement;
    const lost = (e: Event) => {
      e.preventDefault();                                 // ask the browser to restore it
      setAlive(false);
    };
    const back = () => setAlive(true);
    el.addEventListener("webglcontextlost", lost);
    el.addEventListener("webglcontextrestored", back);
    return () => {
      el.removeEventListener("webglcontextlost", lost);
      el.removeEventListener("webglcontextrestored", back);
    };
  }, [gl]);
  return alive;
}

function Effects() {
  const quality = useQuality();
  const alive = useContextAlive();
  if (quality < 2 || !alive) return null;
  return (
    <FxBoundary>
      <EffectsInner />
    </FxBoundary>
  );
}

function EffectsInner() {
  return (
    <EffectComposer multisampling={0}>
      <N8AO halfRes aoRadius={0.9} intensity={1.5} distanceFalloff={1} />
      <TiltShift2 blur={0.1} />
      <Vignette offset={0.3} darkness={0.3} />
    </EffectComposer>
  );
}

function CloseUp({ L }: { L: Layout }) {
  return useDetail() ? <People L={L} /> : null;
}

export function Diorama({ selected, onPick }: { selected: string | null; onPick: (id: string | null) => void }) {
  const L = useWorldSelect((w) => w.layout);
  if (!L) return <div className="boot">CONNECTING TO TERMINAL…</div>;
  return (
    <Canvas shadows dpr={1} onPointerMissed={() => onPick(null)}>
      <color attach="background" args={[C.table]} />
      <Quality />
      <Rig />
      <Lights />
      <Terrain L={L} />
      <Scenery L={L} />
      <Ships L={L} />
      <Train L={L} />
      <Containers L={L} selected={selected} onPick={onPick} />
      {L.units.map((u) =>
        u.kind === "AGV" ? (
          <Agv key={u.id} id={u.id} home={u.home} />
        ) : u.kind === "QC" ? (
          <QuayCrane key={u.id} id={u.id} home={u.home} L={L} />
        ) : (
          <Gantry key={u.id} id={u.id} home={u.home} L={L} kind={u.kind as "RTG" | "RMG"} />
        ),
      )}
      <Lorries />
      <Badges L={L} />
      <CloseUp L={L} />
      <Effects />
    </Canvas>
  );
}
