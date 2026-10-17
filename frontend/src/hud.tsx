import { useEffect, useState } from "react";
import { C, useDetail, useQuality } from "./models";
import { FOCUS } from "./scene";
import { bayCode, changed, fmtClock, fmtSlot, post, useWorld, world, type Box, type Focus } from "./world";

const SPEEDS = [0.5, 1, 2, 4, 8];
const PORTS: [string, string][] = [["NLRTM", "Rotterdam"], ["DEHAM", "Hamburg"], ["PLGDN", "Gdansk"]];

function Bar({ value, max, tone = "ok" }: { value: number; max: number; tone?: "ok" | "rail" | "export" }) {
  return (
    <div className={`meter ${tone}`}>
      <i style={{ width: `${Math.min(100, (value / Math.max(max, 1)) * 100)}%` }} />
    </div>
  );
}

function Dispatch() {
  useWorld();
  const s = world.stats;
  const L = world.layout;
  const tr = world.train;
  const byBerth = new Map([...world.vessels.values()].map((v) => [v.berth, v]));
  return (
    <section className="card dispatch">
      <header>
        <h1>PortDB</h1>
        <span className="mono dim">{world.connected ? `T+${fmtClock(s?.t ?? world.clock.t)}` : "OFFLINE"}</span>
      </header>
      <h2>Berths</h2>
      {L?.berths.map((b) => {
        const v = byBerth.get(b.id);
        return (
          <div key={b.id} className="berth-row">
            <div className="mono small">
              <b>B{b.id}</b> {v ? v.name : <span className="dim">empty · next call when the import yard has room</span>}
              {v && <span className="dim"> · {v.status}</span>}
            </div>
            {v && (
              <div className="bar mono">
                <span>↓</span>
                <Bar value={v.manifest - v.left} max={v.manifest} />
                <span>
                  {v.manifest - v.left}/{v.manifest}
                </span>
              </div>
            )}
            {v && v.phase === "LOAD" && (
              <div className="bar mono">
                <span>↑</span>
                <Bar value={v.loaded} max={Math.max(v.loaded, 40)} tone="export" />
                <span>{v.loaded}</span>
              </div>
            )}
          </div>
        );
      })}
      {s && (
        <>
          <h2>Yard</h2>
          {Object.entries(s.yard).map(([b, n]) => {
            const export_ = L?.blocks.find((x) => x.code === b)?.purpose === "EXPORT";
            return (
              <div key={b} className="bar mono">
                <span>{b}</span>
                <Bar value={n} max={120} tone={export_ ? "export" : "ok"} />
                <span>{n}/120</span>
              </div>
            );
          })}
          <h2>Rail</h2>
          {tr ? (
            <div className="bar mono">
              <span className="wide">{tr.name}</span>
              <Bar value={tr.loaded} max={tr.capacity} tone="rail" />
              <span>
                {tr.status === "UNLOADING" ? `↓${tr.unloaded}/${tr.inbound}` : `${tr.loaded}/${tr.capacity}`}
              </span>
            </div>
          ) : (
            <p className="dim mono small">No train on the siding.</p>
          )}
          <h2>Collision control</h2>
          <dl className="metrics mono">
            <dt>live road bookings</dt>
            <dd>{s.bookings}</dd>
            <dt>AGV re-routes/holds</dt>
            <dd>{s.queues.waits.agv ?? 0}</dd>
            <dt>lorry holds</dt>
            <dd>{s.queues.waits.lorry ?? 0}</dd>
            <dt>crane rail waits</dt>
            <dd>{s.queues.waits.crane ?? 0}</dd>
            <dt>booking races lost</dt>
            <dd>{s.queues.waits.race ?? 0}</dd>
            <dt>DB rejects</dt>
            <dd className={s.rejects ? "warn" : ""}>{s.rejects}</dd>
          </dl>
        </>
      )}
    </section>
  );
}

function Controls() {
  useWorld();
  const quality = useQuality();
  const detail = useDetail();
  const c = world.clock;
  const focus = (f: Focus) => {
    world.focus = f;
    changed();
  };
  return (
    <section className="card controls">
      <div>
        <button onClick={() => post(c.paused ? "/api/sim/resume" : "/api/sim/pause")}>{c.paused ? "Resume" : "Pause"}</button>
        {SPEEDS.map((x) => (
          <button key={x} className={c.speed === x ? "on" : ""} onClick={() => post(`/api/sim/speed?x=${x}`)}>
            {x}×
          </button>
        ))}
      </div>
      <div>
        {(Object.keys(FOCUS) as Focus[]).map((f) => (
          <button key={f} className={world.focus === f ? "on" : ""} onClick={() => focus(f)}>
            {FOCUS[f].label}
          </button>
        ))}
      </div>
      <div className="mono dim small">
        quality {["flat", "shadows", "full"][quality]}
        {detail ? " · close-up detail" : " · zoom in for detail"}
      </div>
    </section>
  );
}

/** The life of a box in both directions, with live counts per stage. */
function Flow() {
  useWorld();
  const s = world.stats;
  if (!s) return null;
  const n = (...states: string[]) => states.reduce((a, k) => a + (s.states[k] ?? 0), 0);
  const yard = (purpose: string) =>
    (world.layout?.blocks ?? []).filter((b) => b.purpose === purpose).reduce((a, b) => a + (s.yard[b.code] ?? 0), 0);
  const stage = (label: string, value: number, sub?: string, tone = "") => (
    <div className={`stage ${tone}`}>
      <b className="mono">{value}</b>
      <span>{label}</span>
      {sub && <small className="mono dim">{sub}</small>}
    </div>
  );
  return (
    <section className="card flow">
      <div className="flow-row">
        <em>IMPORT</em>
        {stage("aboard", n("STOWED_ON_BOARD", "DISCHARGING"), `${s.discharged} discharged`)}
        <i>›</i>
        {stage("AGV", n("IN_TRANSIT"), `${s.queues.agv_wait} waiting`)}
        <i>›</i>
        {stage("import yard", yard("IMPORT"), `${s.customs_pending} held`)}
        <i>›</i>
        {stage("customs", s.scanning, `${s.inspected} X-rayed`, "customs")}
        <i>›</i>
        {stage("out", s.gated_out + s.railed_out, `${s.gated_out} road · ${s.railed_out} rail`, "rail")}
      </div>
      <div className="flow-row">
        <em>EXPORT</em>
        {stage("gate + rail in", n("ON_TRUCK") + n("ON_RAIL"), "drop-offs, trains")}
        <i>›</i>
        {stage("export yard", yard("EXPORT"), `${s.flows.TRANSHIP ?? 0} tranship live`, "export")}
        <i>›</i>
        {stage("loading", n("LOADING"), `${s.loaded} stowed`, "export")}
        <i>›</i>
        {stage("sailed", s.sailed, "", "export")}
        <div className="legend mono">
          {PORTS.map(([code, name]) => (
            <span key={code}>
              <i style={{ background: C.pod[code] }} />
              {name}
            </span>
          ))}
        </div>
      </div>
    </section>
  );
}

function Log() {
  useWorld();
  return (
    <section className="card log mono">
      {world.log.slice(-10).map((l, i) => (
        <div key={`${l.t}-${i}`} className={l.level}>
          <span className="dim">{fmtClock(l.t)}</span> {l.text}
        </div>
      ))}
      {world.log.length === 0 && <div className="dim">Awaiting first move.</div>}
    </section>
  );
}

type Clearance = { rows: { id: string; tier: number; is_target: boolean; shifter_moves: number }[]; plan: string };
type Found = Box & { vessel_name?: string | null; train_name?: string | null };

function Inspector({ id, onPick }: { id: string | null; onPick: (id: string | null) => void }) {
  useWorld();
  const [query, setQuery] = useState("");
  const [cl, setCl] = useState<Clearance | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [found, setFound] = useState<Found | null>(null); // a searched box that has already left the terminal
  const box = id ? world.boxes.get(id) : (found ?? undefined);
  const vessel = box?.vessel_id != null ? world.vessels.get(box.vessel_id) : undefined;
  const where = !box
    ? ""
    : box.block_code && box.bay != null
      ? fmtSlot(box)
      : box.state === "ON_RAIL" || box.state === "RAIL_OUT"
        ? `${found?.train_name ?? world.train?.name ?? "train"} wagon ${box.wagon}/${box.wagon_slot}`
        : box.state === "SAILED"
          ? `sailed on ${found?.vessel_name ?? "a ship"} for ${box.pod}`
          : (box.carrier_id ??
              (box.vessel_bay
                ? `${vessel?.name ?? "ship"} bay ${bayCode(box.vessel_bay, box.vessel_half)} row ${box.vessel_row} tier ${box.vessel_tier}`
                : ""));

  // re-query clearance when the box lands somewhere new (the box object itself changes on every event)
  useEffect(() => {
    setCl(null);
    if (id) setFound(null);
    if (!id || !box?.block_code || box.bay == null) return;
    fetch(`/api/containers/${id}/clearance`)
      .then(async (r) => (r.ok ? setCl(await r.json()) : setErr((await r.json()).detail)))
      .catch((e) => setErr(String(e)));
  }, [id, where]);

  /** Looks the number up in the database, so boxes that have sailed or gated out are found too. */
  const search = async (e: React.FormEvent) => {
    e.preventDefault();
    const q = query.trim().toUpperCase();
    setErr(null);
    setFound(null);
    const r = await fetch(`/api/containers/${encodeURIComponent(q)}`);
    if (!r.ok) {
      setErr(r.status === 422 ? `${q} is not an ISO 6346 container number` : (await r.json()).detail);
      return;
    }
    const hit: Found = await r.json();
    if (world.boxes.has(hit.id)) onPick(hit.id);
    else {
      onPick(null);
      setFound(hit);
    }
  };

  return (
    <section className="card inspector">
      <form onSubmit={search}>
        <input className="mono" placeholder="Container no. e.g. MSCU1234565" value={query} onChange={(e) => setQuery(e.target.value)} />
      </form>
      {err && <p className="warn mono">{err}</p>}
      {!box && !err && <p className="dim">Select a container in the diorama to run the stack-clearance query.</p>}
      {box && (
        <>
          <h2 className="mono">{box.id}</h2>
          <dl className="metrics mono">
            <dt>flow</dt>
            <dd>{box.flow}</dd>
            <dt>type</dt>
            <dd>
              {box.iso_type.startsWith("4") ? "40 ft" : "20 ft"} · {box.iso_type}
              {box.hazmat ? " · IMDG" : ""}
            </dd>
            <dt>gross</dt>
            <dd>{box.weight_kg.toLocaleString()} kg</dd>
            <dt>leaves by</dt>
            <dd>{box.mode === "VESSEL" ? `ship to ${box.pod}` : box.mode}</dd>
            <dt>customs</dt>
            <dd className={box.customs_hold && !box.inspected ? "warn" : ""}>{box.customs_hold ? (box.inspected ? "X-rayed, released" : "HOLD") : "free"}</dd>
            <dt>state</dt>
            <dd>{box.state}</dd>
            <dt>at</dt>
            <dd>{where}</dd>
          </dl>
          {cl && (
            <>
              <h2>
                Stack clearance · {cl.rows[0].shifter_moves} shifter move{cl.rows[0].shifter_moves === 1 ? "" : "s"}
              </h2>
              <ol className="mono stack">
                {cl.rows.map((r) => (
                  <li key={r.id} className={r.is_target ? "target" : ""} onClick={() => onPick(r.id)}>
                    T{r.tier} {r.id}
                  </li>
                ))}
              </ol>
              <details>
                <summary>EXPLAIN ANALYZE</summary>
                <pre>{cl.plan}</pre>
              </details>
            </>
          )}
        </>
      )}
    </section>
  );
}

export function Hud({ selected, onPick }: { selected: string | null; onPick: (id: string | null) => void }) {
  return (
    <div className="hud">
      <Dispatch />
      <Controls />
      <Inspector id={selected} onPick={onPick} />
      <Flow />
      <Log />
    </div>
  );
}
