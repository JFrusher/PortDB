// Build a whole model as one vertex-coloured BufferGeometry: one draw call per model instead of
// one per box. Integrated GPUs choke on draw calls long before they choke on triangles.

import * as THREE from "three";
import { mergeGeometries } from "three/examples/jsm/utils/BufferGeometryUtils.js";
import type { V3 } from "./world";

export const MAT = new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.72, metalness: 0.04 });
export const GLASS = new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.15, metalness: 0.3 });
export const GLOW = new THREE.MeshBasicMaterial({ vertexColors: true, toneMapped: false });

const UP = new THREE.Vector3(0, 1, 0);
const col = new THREE.Color();

export class Parts {
  private list: THREE.BufferGeometry[] = [];

  add(g: THREE.BufferGeometry, color: string): this {
    const flat = g.index ? g.toNonIndexed() : g;
    flat.deleteAttribute("uv");
    col.set(color);
    const n = flat.attributes.position.count;
    const c = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) c.set([col.r, col.g, col.b], i * 3);
    flat.setAttribute("color", new THREE.BufferAttribute(c, 3));
    this.list.push(flat);
    return this;
  }

  box(size: V3, at: V3, color: string, rotY = 0): this {
    const g = new THREE.BoxGeometry(...size);
    if (rotY) g.rotateY(rotY);
    return this.add(g.translate(...at), color);
  }

  /** Cylinder along an axis. */
  cyl(r: number, h: number, at: V3, color: string, axis: "x" | "y" | "z" = "y", seg = 10, rTop = r): this {
    const g = new THREE.CylinderGeometry(rTop, r, h, seg);
    if (axis === "x") g.rotateZ(Math.PI / 2);
    if (axis === "z") g.rotateX(Math.PI / 2);
    return this.add(g.translate(...at), color);
  }

  cone(r: number, h: number, at: V3, color: string, seg = 7): this {
    return this.add(new THREE.ConeGeometry(r, h, seg).translate(...at), color);
  }

  /** Round bar from a to b. */
  rod(a: V3, b: V3, r: number, color: string, seg = 6): this {
    const va = new THREE.Vector3(...a);
    const dir = new THREE.Vector3(...b).sub(va);
    const g = new THREE.CylinderGeometry(r, r, dir.length(), seg);
    g.applyQuaternion(new THREE.Quaternion().setFromUnitVectors(UP, dir.clone().normalize()));
    g.translate(va.x + dir.x / 2, va.y + dir.y / 2, va.z + dir.z / 2);
    return this.add(g, color);
  }

  /** Ladder: two stiles and rungs every `pitch`, climbing from a to b. */
  ladder(a: V3, b: V3, width: number, across: "x" | "z", color: string, pitch = 0.22): this {
    const off: V3 = across === "x" ? [width / 2, 0, 0] : [0, 0, width / 2];
    const plus = (p: V3, s: number): V3 => [p[0] + off[0] * s, p[1] + off[1] * s, p[2] + off[2] * s];
    this.rod(plus(a, -1), plus(b, -1), 0.012, color, 4).rod(plus(a, 1), plus(b, 1), 0.012, color, 4);
    const n = Math.floor(Math.hypot(b[0] - a[0], b[1] - a[1], b[2] - a[2]) / pitch);
    for (let i = 1; i < n; i++) {
      const t = i / n;
      const p: V3 = [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
      this.rod(plus(p, -1), plus(p, 1), 0.008, color, 3);
    }
    return this;
  }

  /** Hand railing along a straight run: top rail, knee rail and posts. */
  railing(a: V3, b: V3, color: string, h = 0.16, posts = 6): this {
    const up = (p: V3, y: number): V3 => [p[0], p[1] + y, p[2]];
    this.rod(up(a, h), up(b, h), 0.008, color, 3).rod(up(a, h / 2), up(b, h / 2), 0.006, color, 3);
    for (let i = 0; i <= posts; i++) {
      const t = i / posts;
      const p: V3 = [a[0] + (b[0] - a[0]) * t, a[1], a[2] + (b[2] - a[2]) * t];
      this.rod(p, up(p, h), 0.008, color, 3);
    }
    return this;
  }

  build(): THREE.BufferGeometry {
    const g = this.list.length ? mergeGeometries(this.list, false)! : new THREE.BufferGeometry();
    g.computeBoundingSphere();
    return g;
  }
}
