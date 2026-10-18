// Day, night and weather. Light follows the simulated clock; fog, rain and wind follow the weather
// rows the engine writes (the same rows the database's wind-limit trigger reads).

import { useFrame, useThree } from "@react-three/fiber";
import { useMemo, useRef } from "react";
import * as THREE from "three";
import { C, useQuality } from "./models";
import { daylight, simNow, trackAt, world, type Layout, type V3 } from "./world";

const DAY_BG = new THREE.Color(C.table);
const NIGHT_BG = new THREE.Color("#283049");
const STORM_BG = new THREE.Color("#A7ACB8");
const SUN_DAY = new THREE.Color("#FFE1C2");
const SUN_LOW = new THREE.Color("#FFB37A");
const MOON = new THREE.Color("#9FB4E8");
const SKY_DAY = new THREE.Color("#FFF4E6");
const SKY_NIGHT = new THREE.Color("#4A5A8C");
const tmpCol = new THREE.Color();

/** Night factor (0 day .. 1 night), shared with anything that glows after dark. */
export function nightAt(L: Layout, t: number) {
  return 1 - daylight(L, t).day;
}

export function Sky({ L }: { L: Layout }) {
  const quality = useQuality();
  const scene = useThree((s) => s.scene);
  const sun = useRef<THREE.DirectionalLight>(null!);
  const hemi = useRef<THREE.HemisphereLight>(null!);
  const target = useMemo(() => {
    const o = new THREE.Object3D();
    o.position.set(26, 0, 4);
    return o;
  }, []);
  const fog = useMemo(() => new THREE.Fog(DAY_BG.clone(), 400, 600), []);
  const bg = useMemo(() => DAY_BG.clone(), []);
  useFrame(() => {
    const t = simNow();
    const { hour, sun: elev, day } = daylight(L, t);
    const w = world.weather;
    const overcast = Math.min(1, w.rain * 1.2 + w.fog * 0.5);
    // sun sweeps east to west; at night a dimmer moon light from the opposite side
    const az = ((hour - 6) / 12) * Math.PI;
    const up = Math.max(0.25, Math.abs(elev));
    sun.current.position.set(26 + Math.cos(az) * 30 * (day > 0.02 ? 1 : -1), 10 + up * 28, 4 + 24);
    sun.current.color.copy(day > 0.02 ? tmpCol.copy(SUN_LOW).lerp(SUN_DAY, Math.min(1, elev * 2.5)) : MOON);
    // a bright moon and the terminal's own lighting: night reads as night but every move stays visible
    sun.current.intensity = (0.9 + 1.45 * day) * (1 - 0.45 * overcast);
    hemi.current.intensity = (0.85 + 0.35 * day) * (1 - 0.2 * overcast);
    hemi.current.color.copy(SKY_NIGHT).lerp(SKY_DAY, day);
    bg.copy(NIGHT_BG).lerp(DAY_BG, day).lerp(STORM_BG, overcast * 0.45 * day);
    scene.background = bg;
    // fog closes in with the weather; the orthographic camera sits ~50 units from the yard
    fog.color.copy(bg);
    const thick = Math.max(w.fog, w.rain * 0.6);
    fog.near = 45 + 220 * (1 - thick);
    fog.far = fog.near + 25 + 260 * (1 - thick);
    scene.fog = thick > 0.03 ? fog : null;
  });
  return (
    <>
      <hemisphereLight ref={hemi} args={["#FFF4E6", "#C9D6DF", 1.15]} />
      <primitive object={target} />
      <directionalLight
        ref={sun}
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
      {quality >= 1 && <Rain />}
      <FloodPools L={L} />
      <Windsock />
    </>
  );
}

/** Streaks falling at a wind-driven slant over the whole terminal. */
function Rain() {
  const N = 1400;
  const geo = useMemo(() => {
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(new Float32Array(N * 6), 3));
    return g;
  }, []);
  const seeds = useMemo(() => Array.from({ length: N }, () => [Math.random() * 140 - 42, Math.random() * 46 - 24, Math.random()]), []);
  const mat = useRef<THREE.LineBasicMaterial>(null!);
  const lines = useRef<THREE.LineSegments>(null!);
  useFrame(() => {
    const w = world.weather;
    lines.current.visible = w.rain > 0.03;
    if (!lines.current.visible) return;
    mat.current.opacity = Math.min(0.55, w.rain * 0.6);
    const t = performance.now() / 1000;
    const slant = 0.04 * w.wind;
    const pos = geo.attributes.position as THREE.BufferAttribute;
    const n = Math.floor(N * Math.min(1, w.rain * 1.4));
    for (let i = 0; i < N; i++) {
      const [x, z, ph] = seeds[i];
      const y = i < n ? 14 - (((t * 1.1 + ph) % 1) * 15) : -50;
      pos.setXYZ(i * 2, x + slant * y, y, z);
      pos.setXYZ(i * 2 + 1, x + slant * (y - 0.55), y - 0.55, z);
    }
    pos.needsUpdate = true;
  });
  return (
    <lineSegments ref={lines} geometry={geo} frustumCulled={false}>
      <lineBasicMaterial ref={mat} color="#C8D3E6" transparent opacity={0.4} depthWrite={false} />
    </lineSegments>
  );
}

/** Soft glow texture for pools of floodlight on the ground. */
function poolTexture() {
  const c = document.createElement("canvas");
  c.width = c.height = 64;
  const g = c.getContext("2d")!;
  const r = g.createRadialGradient(32, 32, 0, 32, 32, 32);
  r.addColorStop(0, "rgba(255,226,170,1)");
  r.addColorStop(0.5, "rgba(255,214,150,0.45)");
  r.addColorStop(1, "rgba(255,200,130,0)");
  g.fillStyle = r;
  g.fillRect(0, 0, 64, 64);
  return new THREE.CanvasTexture(c);
}

/** After dark: light masts and quay cranes throw pools of light on the ground. */
function FloodPools({ L }: { L: Layout }) {
  const masts = useMemo(() => {
    const out: V3[] = [];
    for (const b of L.blocks) for (const z of [b.z0 - 0.6, b.z0 + b.rows + 1.4]) out.push([b.x0 + b.bays + 1.5, 0.03, z]);
    for (let x = L.rail.x0; x < L.rail.x0 + 2 * L.rail.wagons + 2; x += 5) out.push([x, 0.03, L.rail.z - 0.5]);
    return out;
  }, [L]);
  const qcs = useMemo(() => L.units.filter((u) => u.kind === "QC"), [L]);
  const tex = useMemo(poolTexture, []);
  const mat = useMemo(
    () => new THREE.MeshBasicMaterial({ map: tex, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false, opacity: 0 }),
    [tex],
  );
  const mesh = useRef<THREE.InstancedMesh>(null!);
  const m = useMemo(() => new THREE.Matrix4(), []);
  const q = useMemo(() => new THREE.Quaternion().setFromEuler(new THREE.Euler(-Math.PI / 2, 0, 0)), []);
  const p = useMemo(() => new THREE.Vector3(), []);
  const s = useMemo(() => new THREE.Vector3(), []);
  const at: V3 = [0, 0, 0];
  useFrame(() => {
    const night = nightAt(L, simNow());
    mat.opacity = Math.max(0, night - 0.15) * 0.5;
    mesh.current.visible = mat.opacity > 0.01;
    if (!mesh.current.visible) return;
    let i = 0;
    for (const v of masts) mesh.current.setMatrixAt(i++, m.compose(p.set(...v), q, s.set(5, 5, 1)));
    for (const u of qcs) {
      const tr = world.tracks.get(u.id);
      const x = tr ? trackAt(tr, simNow(), at)[0] : u.home[0];
      mesh.current.setMatrixAt(i++, m.compose(p.set(x, 0.04, -1.2), q, s.set(3.2, 5.5, 1)));
    }
    mesh.current.count = i;
    mesh.current.instanceMatrix.needsUpdate = true;
  });
  return (
    <instancedMesh ref={mesh} args={[undefined, mat, masts.length + qcs.length]} frustumCulled={false} renderOrder={2}>
      <planeGeometry />
    </instancedMesh>
  );
}

/** A windsock on the port control roof: it fills and swings with the wind. */
function Windsock() {
  const sock = useRef<THREE.Group>(null!);
  useFrame(() => {
    const w = world.weather;
    const fill = Math.min(1, w.wind / 18);
    const t = performance.now() / 1000;
    sock.current.rotation.z = -Math.PI / 2 + (1 - fill) * 1.1 + Math.sin(t * (2 + w.wind * 0.3)) * 0.05 * (0.5 + fill);
    sock.current.rotation.y = Math.sin(t * 0.7) * 0.25 * fill;
  });
  return (
    <group position={[-12.6, 2.6, 5.6]}>
      <mesh position={[0, -0.3, 0]}>
        <cylinderGeometry args={[0.02, 0.02, 0.7, 6]} />
        <meshStandardMaterial color={C.steel} />
      </mesh>
      <group ref={sock}>
        <mesh position={[0, 0.25, 0]}>
          <coneGeometry args={[0.09, 0.5, 10, 1, true]} />
          <meshStandardMaterial color={C.boot} side={THREE.DoubleSide} />
        </mesh>
      </group>
    </group>
  );
}
