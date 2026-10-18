import { useEffect, useState } from "react";
import { C, useDetail, useQuality } from "./models";
import { AHEAD, REEFER_KW, SECONDS_UP } from "./overlays";
import { FOCUS } from "./scene";
import {
  bayCode, changed, daylight, fmtClock, fmtSlot, goLive, loadJourney, post, seek, setReplayClock, setViewAs, simNow, useWorld,
  world, type Box, type ColorBy, type Focus,
} from "./world";

const SPEEDS = [0.5, 1, 2, 4, 8];
const PORTS: [string, string][] = [["NLRTM", "Rotterdam"], ["DEHAM", "Hamburg"], ["PLGDN", "Gdansk"]];
const COLOR_BY: [ColorBy, string][] = [
  ["line", "Shipping line"], ["port", "Port of discharge"], ["weight", "Weight"], ["dwell", "Yard dwell"],
  ["rehandles", "Re-handles"], ["reefer", "Reefer power"],
];
const VIEW_AS: [string, string][] = [
  ["", "Operator (all rows)"], ["customs", "Customs officer"], ["gate", "Gate clerk"], ["line:MSC", "Line: MSC"], ["line:MAE", "Line: MAE"],
];

function Bar({ value, max, tone = "ok" }: { value: number; max: number; tone?: "ok" | "rail" | "export" }) {
  return (
    <div className={`meter ${tone}`}>
      <i style={{ width: `${Math.min(100, (value / Math.max(max, 1)) * 100)}%` }} />
    </div>
  );
}

/** Poll a JSON endpoint while mounted. */
function usePoll<T>(url: string, ms: number): T | null {
  const [data, setData] = useState<T | null>(null);
  useEffect(() => {
    let live = true;
    const tick = () =>
      fetch(url)
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => live && d && setData(d))
        .catch(() => {});
    tick();
    const id = setInterval(tick, ms);
    return () => {
      live = false;
      clearInterval(id);
    };
  }, [url, ms]);
  return data;
}

// --- weather + time of day ------------------------------------------------------------------
function WeatherStrip() {
  useWorld();
  const L = world.layout;
  const w = world.weather;
  const limit = world.stats?.limits?.QC ?? 20;
  if (!L) return null;
  const { hour } = daylight(L, simNow());
  const hh = Math.floor(hour);
  const held = Object.keys(world.stats?.queues.held ?? {}).length;
  return (
    <div className="weather mono">
      <span>
        {String(hh).padStart(2, "0")}:{String(Math.floor((hour - hh) * 60)).padStart(2, "0")}
      </span>
      <div className="gauge" title={`wind ${w.wind} m/s, gusts ${w.gust}; QC limit ${limit} m/s (crane_wind_limits)`}>
        <i style={{ width: `${Math.min(100, (w.wind / 30) * 100)}%` }} className={w.wind > limit ? "over" : ""} />
        <b style={{ left: `${(limit / 30) * 100}%` }} />
      </div>
      <span className={w.wind > limit ? "warn" : ""}>{w.wind.toFixed(0)} m/s</span>
      {w.rain > 0.05 && <span className="dim">rain</span>}
      {w.fog > 0.15 && <span className="dim">fog</span>}
      {held > 0 && <span className="warn">· {held} cranes held</span>}
    </div>
  );
}

// --- ops tabs ---------------------------------------------------------------------------------
type Tab = "terminal" | "database" | "yard" | "berths" | "chaos";

function Ops() {
  const [tab, setTab] = useState<Tab>("terminal");
  useWorld();
  const s = world.stats;
  return (
    <section className="card dispatch">
      <header>
        <h1>PortDB</h1>
        <span className="mono dim">
          {world.mode === "replay" ? <b className="replay-badge">REPLAY T+{fmtClock(simNow())}</b> : world.connected ? `T+${fmtClock(s?.t ?? world.clock.t)}` : "OFFLINE"}
        </span>
      </header>
      <WeatherStrip />
      <nav className="tabs">
        {(["terminal", "database", "yard", "berths", "chaos"] as Tab[]).map((t) => (
          <button key={t} className={tab === t ? "on" : ""} onClick={() => setTab(t)}>
            {t}
          </button>
        ))}
      </nav>
      {tab === "terminal" && <Terminal />}
      {tab === "database" && <Database />}
      {tab === "yard" && <YardKpi />}
      {tab === "berths" && <BerthPlan />}
      {tab === "chaos" && <Chaos />}
    </section>
  );
}

function Terminal() {
  const s = world.stats;
  const L = world.layout;
  const tr = world.train;
  const byBerth = new Map([...world.vessels.values()].map((v) => [v.berth, v]));
  return (
    <>
      <h2>Berths</h2>
      {L?.berths.map((b) => {
        const v = byBerth.get(b.id);
        return (
          <div key={b.id} className={`berth-row ${world.mode === "replay" ? "stale" : ""}`}>
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
            {v && (v.phase === "LOAD" || v.loaded > 0) && (
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
        <div className={world.mode === "replay" ? "stale" : ""} title={world.mode === "replay" ? "live figures: not replayed" : undefined}>
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
              <span>{tr.status === "UNLOADING" ? `↓${tr.unloaded}/${tr.inbound}` : `${tr.loaded}/${tr.capacity}`}</span>
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
            <dt>DB rejects</dt>
            <dd className={s.rejects ? "warn" : ""}>{s.rejects}</dd>
          </dl>
        </div>
      )}
    </>
  );
}

type DbStats = {
  xact_commit: number; xact_rollback: number; deadlocks: number; waiting: number; held: number; cache_hit_pct: number;
  tup_inserted: number; tup_updated: number; history_rows: number; event_rows: number; partitions: number; history_bytes: number;
  retries: number; refusals: Record<string, number>;
  top: { query: string; calls: number; total_ms: number; mean_ms: number }[] | null;
};

/** What PostgreSQL itself reports, and which of its rules have been saying no. */
function Database() {
  const d = usePoll<DbStats>("/api/dbstats", 2000);
  if (!d) return <p className="dim small">Asking pg_stat_database…</p>;
  const refusals = Object.entries(d.refusals).sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...refusals.map((r) => r[1]));
  return (
    <>
      <h2>pg_stat_database</h2>
      <dl className="metrics mono">
        <dt>commits</dt>
        <dd>{d.xact_commit.toLocaleString()}</dd>
        <dt>rollbacks</dt>
        <dd>{d.xact_rollback.toLocaleString()}</dd>
        <dt>deadlocks detected</dt>
        <dd>{d.deadlocks}</dd>
        <dt>engine retries</dt>
        <dd>{d.retries}</dd>
        <dt>locks held / waiting</dt>
        <dd>
          {d.held} / <span className={d.waiting ? "warn" : ""}>{d.waiting}</span>
        </dd>
        <dt>buffer cache hit</dt>
        <dd>{d.cache_hit_pct}%</dd>
      </dl>
      <h2>Time travel store</h2>
      <dl className="metrics mono">
        <dt>container_history rows</dt>
        <dd>{d.history_rows.toLocaleString()}</dd>
        <dt>event_log rows</dt>
        <dd>{d.event_rows.toLocaleString()}</dd>
        <dt>partitions (5 min each)</dt>
        <dd>{d.partitions}</dd>
        <dt>on disk</dt>
        <dd>{(d.history_bytes / 1048576).toFixed(1)} MB</dd>
      </dl>
      <h2>Refused by the database</h2>
      {refusals.length === 0 && <p className="dim small">Nothing refused yet.</p>}
      {refusals.map(([rule, n]) => (
        <div key={rule} className="refusal-row mono">
          <span>{rule}</span>
          <div className="meter rail">
            <i style={{ width: `${(n / max) * 100}%` }} />
          </div>
          <span>{n}</span>
        </div>
      ))}
      <h2>pg_stat_statements</h2>
      {d.top ? (
        <ol className="statements mono">
          {d.top.map((s, i) => (
            <li key={i} title={s.query}>
              <span>{s.query}</span>
              <small className="dim">
                {s.calls.toLocaleString()} calls · {s.mean_ms} ms avg · {Math.round(s.total_ms).toLocaleString()} ms total
              </small>
            </li>
          ))}
        </ol>
      ) : (
        <p className="dim small">Not loaded on this server (needs shared_preload_libraries; the Compose stack has it).</p>
      )}
    </>
  );
}

type Kpi = { buckets: { t: number; rehandles: number; retrievals: number; qc_moves: number }[]; strategy: { t: number; name: string }[]; dual_cycles: number; now: number };

/** Re-handles per 100 retrievals per minute, coloured by the placement strategy in force at the time. */
function YardKpi() {
  useWorld();
  const k = usePoll<Kpi>("/api/kpi", 3000);
  const s = world.stats;
  const strat = (t: number) => [...(k?.strategy ?? [])].reverse().find((x) => x.t <= t + 30)?.name ?? "SMART";
  const rates = (k?.buckets ?? []).map((b) => ({ ...b, rate: b.retrievals ? (100 * b.rehandles) / b.retrievals : 0, name: strat(b.t) }));
  const avg = (name: string) => {
    const xs = rates.filter((r) => r.name === name && r.retrievals);
    const re = xs.reduce((a, r) => a + r.rehandles, 0);
    const rt = xs.reduce((a, r) => a + r.retrievals, 0);
    return rt ? ((100 * re) / rt).toFixed(0) : "–";
  };
  const top = Math.max(100, ...rates.map((r) => r.rate));
  const qcRate = rates.slice(-5).reduce((a, r) => a + r.qc_moves, 0) * (60 / Math.max(1, Math.min(5, rates.length)));
  return (
    <>
      <h2>Yard placement</h2>
      <div className="seg">
        {(["BASIC", "SMART"] as const).map((n) => (
          <button key={n} className={s?.strategy === n ? "on" : ""} onClick={() => post(`/api/strategy?name=${n}`)}>
            {n === "BASIC" ? "Basic: fill stack by stack" : "Smart: by departure, low"}
          </button>
        ))}
      </div>
      <h2>Re-handles per 100 retrievals, per minute</h2>
      <svg className="kpi" viewBox="0 0 300 90" preserveAspectRatio="none">
        {rates.map((r, i) => (
          <rect key={r.t} x={(i * 300) / Math.max(rates.length, 1)} width={300 / Math.max(rates.length, 1) - 1} y={90 - (r.rate / top) * 86}
            height={(r.rate / top) * 86} fill={r.name === "SMART" ? "#81B29A" : "#E9A25F"}>
            <title>{`t=${r.t}s ${r.name}: ${r.rehandles} re-handles / ${r.retrievals} retrievals`}</title>
          </rect>
        ))}
      </svg>
      <dl className="metrics mono">
        <dt>
          <i className="dot" style={{ background: "#E9A25F" }} /> basic average
        </dt>
        <dd>{avg("BASIC")}</dd>
        <dt>
          <i className="dot" style={{ background: "#81B29A" }} /> smart average
        </dt>
        <dd>{avg("SMART")}</dd>
      </dl>
      <h2>Quay cranes</h2>
      <div className="seg">
        <button className={s?.dual ? "on" : ""} onClick={() => post(`/api/dual?on=${!s?.dual}`)}>
          Dual cycling {s?.dual ? "on" : "off"}
        </button>
      </div>
      <dl className="metrics mono">
        <dt>QC moves, last 5 min</dt>
        <dd>{Math.round(qcRate)}/h</dd>
        <dt>dual cycles so far</dt>
        <dd>{k?.dual_cycles ?? 0}</dd>
      </dl>
      <p className="dim small">Dual cycling loads exports into bays a crane has already emptied, so its trolley carries a box both ways.</p>
    </>
  );
}

type Window = { id: number; berth: number; name: string; status: string; t0: number; t1: number };

/** The berth plan: alongside windows in quay x time. The database refuses any two that overlap. */
function BerthPlan() {
  const p = usePoll<{ now: number; windows: Window[] }>("/api/berth_plan", 2000);
  const L = world.layout;
  if (!p || !L) return <p className="dim small">Loading berth plan…</p>;
  const from = p.now - 120;
  const span = 900;
  const x = (t: number) => Math.max(0, Math.min(100, ((t - from) / span) * 100));
  return (
    <>
      <h2>Berth plan · next 13 minutes</h2>
      <div className="gantt">
        {L.berths.map((b) => (
          <div key={b.id} className="lane">
            <span className="mono">B{b.id}</span>
            <div className="track">
              {p.windows.filter((w) => w.berth === b.id).map((w) => (
                <i key={w.id} className={`win ${w.status.toLowerCase()}`} style={{ left: `${x(w.t0)}%`, width: `${x(w.t1) - x(w.t0)}%` }}
                  title={`${w.name} ${w.status} ${w.t0.toFixed(0)}–${w.t1.toFixed(0)} s`}>
                  {w.name}
                </i>
              ))}
              <b className="now" style={{ left: `${x(p.now)}%` }} />
            </div>
          </div>
        ))}
      </div>
      <p className="dim small">
        berth_windows: EXCLUDE USING gist (quay WITH &&, during WITH &&). A call that overruns asks to extend its window; when that would
        overlap the next call the database refuses and the next call is pushed back first.
      </p>
      <dl className="metrics mono">
        <dt>overrun refusals</dt>
        <dd>{world.stats?.queues.refusals?.berth_windows_no_overlap ?? 0}</dd>
      </dl>
    </>
  );
}

/** Break things on purpose and watch the rules route work around them. */
function Chaos() {
  useWorld();
  const s = world.stats;
  const recent = world.log.filter((l) => l.text.startsWith("CHAOS") || l.text.startsWith("REPAIRED")).slice(-6);
  return (
    <>
      <h2>Break something</h2>
      <div className="chaos-grid">
        <button onClick={() => post("/api/chaos/storm")}>Storm front</button>
        <button onClick={() => post("/api/chaos/crane")}>Quay crane fault</button>
        <button onClick={() => post("/api/chaos/agv")}>AGV fault</button>
        <button onClick={() => post("/api/chaos/gate")}>In-gate outage</button>
      </div>
      <div className="seg">
        <button className={s?.chaos_auto ? "on" : ""} onClick={() => post(`/api/chaos/auto?on=${!s?.chaos_auto}`)}>
          Random chaos {s?.chaos_auto ? "on" : "off"}
        </button>
      </div>
      <dl className="metrics mono">
        <dt>out of service</dt>
        <dd className={s?.queues.down.length ? "warn" : ""}>{s?.queues.down.join(", ") || "none"}</dd>
        <dt>held by wind</dt>
        <dd>{Object.keys(s?.queues.held ?? {}).length}</dd>
        <dt>lorries queued at gate</dt>
        <dd>{s?.queues.gate ?? 0}</dd>
      </dl>
      <p className="dim small">
        Each fault is a row: equipment.state = DOWN, a weather reading, a gate_outages range. Triggers then refuse the work, and the engine
        waits and retries.
      </p>
      {recent.map((l, i) => (
        <div key={i} className={`mono small ${l.level}`}>
          <span className="dim">{fmtClock(l.t)}</span> {l.text}
        </div>
      ))}
    </>
  );
}

// --- top controls: run, camera, time travel, views ------------------------------------------
function TimeBar() {
  useWorld();
  const replaying = world.mode === "replay";
  const live = world.liveT;
  const from = world.stats?.history_from ?? 0;
  const [drag, setDrag] = useState<number | null>(null);
  const t = drag ?? (replaying ? simNow() : live);
  return (
    <div className="timebar mono">
      <button className={replaying ? "" : "on live"} onClick={() => replaying && goLive()}>
        LIVE
      </button>
      <input
        type="range"
        min={from}
        max={Math.max(live, from + 1)}
        step={0.5}
        value={t}
        onChange={(e) => setDrag(Number(e.target.value))}
        onPointerUp={() => {
          if (drag != null) seek(drag);
          setDrag(null);
        }}
        onKeyUp={() => {
          if (drag != null) seek(drag);
          setDrag(null);
        }}
        aria-label="Time travel"
      />
      <span className="dim">{replaying || drag != null ? `T+${fmtClock(t)} · ${fmtClock(live - t)} ago` : "drag to rewind"}</span>
      {replaying && (
        <button onClick={() => setReplayClock({ paused: !world.clock.paused })}>{world.clock.paused ? "Play" : "Pause"}</button>
      )}
    </div>
  );
}

function Controls() {
  useWorld();
  const quality = useQuality();
  const detail = useDetail();
  const c = world.clock;
  const replaying = world.mode === "replay";
  const focus = (f: Focus) => {
    world.focus = f;
    changed();
  };
  return (
    <section className="card controls">
      <div>
        {!replaying && (
          <button onClick={() => post(c.paused ? "/api/sim/resume" : "/api/sim/pause")}>{c.paused ? "Resume" : "Pause"}</button>
        )}
        {SPEEDS.map((x) => (
          <button
            key={x}
            className={c.speed === x ? "on" : ""}
            onClick={() => (replaying ? setReplayClock({ speed: x }) : post(`/api/sim/speed?x=${x}`))}
          >
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
      <TimeBar />
      <div className="views">
        <button
          className={world.xray ? "on" : ""}
          disabled={replaying}
          onClick={() => {
            world.xray = !world.xray;
            changed();
          }}
          title="See what the database is holding: road bookings in space-time, crane rail envelopes, refusals"
        >
          X-ray
        </button>
        <label>
          colour by
          <select
            value={world.colorBy}
            onChange={(e) => {
              world.colorBy = e.target.value as ColorBy;
              changed();
            }}
          >
            {COLOR_BY.map(([v, l]) => (
              <option key={v} value={v}>
                {l}
              </option>
            ))}
          </select>
        </label>
        <label title="Row-level security: the same query run as another database role">
          view as
          <select value={world.viewAs} onChange={(e) => setViewAs(e.target.value)}>
            {VIEW_AS.map(([v, l]) => (
              <option key={v} value={v}>
                {l}
              </option>
            ))}
          </select>
        </label>
        {world.visible && <span className="dim small">{world.visible.size} rows visible</span>}
      </div>
      {world.xray && !replaying && (
        <div className="xray-legend">
          <b>X-RAY</b> road bookings drawn in space-time: each column is a (cell, t0..t1) row in road_bookings, rising {SECONDS_UP} per
          second for the next {AHEAD} s. Columns never intersect: that is EXCLUDE USING gist. Grey = parked, open-ended. Bands = crane
          rail envelopes (numrange &&). Red rings = a transaction the database refused, named by its rule.
        </div>
      )}
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
    <section className={`card flow ${world.mode === "replay" ? "stale" : ""}`}>
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
        <Legend />
      </div>
    </section>
  );
}

function Legend() {
  const mode = world.colorBy;
  if (mode === "port")
    return (
      <div className="legend mono">
        {PORTS.map(([code, name]) => (
          <span key={code}>
            <i style={{ background: C.pod[code] }} />
            {name}
          </span>
        ))}
      </div>
    );
  const scales: Partial<Record<ColorBy, [string, string, string]>> = {
    weight: ["#EAF2F3", "#2B4A6F", "2 t → 32 t"],
    dwell: ["#9BD3AE", "#C8553D", "0 → 6 min in yard"],
    rehandles: ["#D5D8DE", "#C8553D", "0 → 2+ re-handles"],
    reefer: ["#E4E4E4", "#3F88C5", `reefers ${REEFER_KW} kW each`],
  };
  const sc = scales[mode];
  if (!sc) return <div className="legend mono"><span>liveries by owner code</span><span><i style={{ background: C.hazmat }} />IMDG</span></div>;
  return (
    <div className="legend mono">
      <span>
        <i style={{ background: `linear-gradient(90deg, ${sc[0]}, ${sc[1]})`, width: 40 }} />
        {sc[2]}
      </span>
    </div>
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
type Stop = { t0: number; t1: number | null; state: string; snapshot: Box };

const place = (b: Box) =>
  b.block_code && b.bay != null ? fmtSlot(b)
  : b.vessel_bay != null ? `ship bay ${bayCode(b.vessel_bay, b.vessel_half)}`
  : b.train_id != null ? `wagon ${b.wagon}/${b.wagon_slot}`
  : (b.carrier_id ?? "");

function Inspector({ id, onPick }: { id: string | null; onPick: (id: string | null) => void }) {
  useWorld();
  const [query, setQuery] = useState("");
  const [cl, setCl] = useState<Clearance | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [found, setFound] = useState<Found | null>(null); // a searched box that has already left the terminal
  const [stops, setStops] = useState<Stop[] | null>(null);
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
  const boxId = box?.id;

  // re-query clearance when the box lands somewhere new (the box object itself changes on every event)
  useEffect(() => {
    setCl(null);
    if (id) setFound(null);
    if (!id || !box?.block_code || box.bay == null || world.mode === "replay") return;
    fetch(`/api/containers/${id}/clearance`)
      .then(async (r) => (r.ok ? setCl(await r.json()) : setErr((await r.json()).detail)))
      .catch((e) => setErr(String(e)));
  }, [id, where]);

  // its whole story, from the temporal history
  useEffect(() => {
    setStops(null);
    if (!boxId) {
      world.journey = null;
      return;
    }
    loadJourney(boxId).then((j) => setStops(j.stops)).catch(() => {});
  }, [boxId, where]);

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
      {!box && !err && <p className="dim">Select a container to see its stack clearance query and its journey, replayable from the history.</p>}
      {box && (
        <>
          <h2 className="mono">{box.id}</h2>
          <dl className="metrics mono">
            <dt>flow</dt>
            <dd>{box.flow}</dd>
            <dt>type</dt>
            <dd>
              {box.iso_type.startsWith("4") ? "40 ft" : "20 ft"} · {box.iso_type}
              {box.imdg_class ? ` · IMDG ${box.imdg_class}` : ""}
            </dd>
            <dt>gross</dt>
            <dd>{box.weight_kg.toLocaleString()} kg</dd>
            <dt>leaves by</dt>
            <dd>{box.mode === "VESSEL" ? `ship to ${box.pod}` : box.mode}</dd>
            <dt>customs</dt>
            <dd className={box.customs_hold && !box.inspected ? "warn" : ""}>{box.customs_hold ? (box.inspected ? "X-rayed, released" : "HOLD") : "free"}</dd>
            <dt>re-handled</dt>
            <dd>{box.rehandles ?? 0}×</dd>
            <dt>state</dt>
            <dd>{box.state}</dd>
            <dt>at</dt>
            <dd>{where}</dd>
          </dl>
          {stops && stops.length > 0 && (
            <>
              <h2>Journey · container_history</h2>
              <ol className="journey mono">
                {stops.map((s) => (
                  <li key={s.t0} onClick={() => seek(s.t0 + 0.05)} title="Replay the terminal from this moment">
                    <span className="dim">{fmtClock(s.t0)}</span> {s.state.toLowerCase().replaceAll("_", " ")}
                    <small className="dim"> {place(s.snapshot)}</small>
                  </li>
                ))}
              </ol>
              <p className="dim small">Click a step to rewind the whole terminal to it. The gold line is every move that carried the box.</p>
            </>
          )}
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
      <Ops />
      <Controls />
      <Inspector id={selected} onPick={onPick} />
      <Flow />
      <Log />
    </div>
  );
}
