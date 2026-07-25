#!/usr/bin/env python3
"""Render the session-stats history log as a standalone HTML dashboard.

Reads ~/.claude/session-stats/sessions.jsonl (the log written by
session_stats.py --log / --backfill) and writes a single self-contained HTML
file: no network, no dependencies, works offline, light and dark.

Usage:
    visualize.py                       write ~/.claude/session-stats/dashboard.html
    visualize.py --open                write it and open it in the browser
    visualize.py --out /tmp/usage.html
    visualize.py --log-file <path>     read a different log
"""

import argparse
import json
import os
import sys
import webbrowser
from collections import defaultdict
from datetime import datetime

DEFAULT_LOG = os.path.expanduser("~/.claude/session-stats/sessions.jsonl")
DEFAULT_OUT = os.path.expanduser("~/.claude/session-stats/dashboard.html")


def read_log(path):
    """Records deduped per session id, largest output wins (see session_stats.py)."""
    best = {}
    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except OSError as exc:
        sys.exit("Cannot read %s (%s).\nRun session_stats.py --backfill first."
                 % (path, exc))
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = rec.get("session_id")
            if not sid:
                continue
            prev = best.get(sid)
            if prev is None or (rec.get("grand_total", {}).get("output_tokens", 0) >
                                prev.get("grand_total", {}).get("output_tokens", 0)):
                best[sid] = rec
    return sorted(best.values(), key=lambda r: r.get("ended_at") or "")


def compact(records):
    """Trim each record to what the page plots -- keeps the HTML small."""
    out = []
    for rec in records:
        g = rec.get("grand_total") or {}
        ended = (rec.get("ended_at") or rec.get("logged_at") or "")[:10]
        if not ended:
            continue
        models = {}
        for model, m in (g.get("by_model") or {}).items():
            models[model] = m.get("output_tokens") or 0
        cwd = (rec.get("cwd") or "?").rstrip("/")
        out.append({
            "d": ended,
            "p": os.path.basename(cwd) or cwd,
            "o": g.get("output_tokens") or 0,
            "i": g.get("input_tokens") or 0,
            "cw": g.get("cache_creation_input_tokens") or 0,
            "cr": g.get("cache_read_input_tokens") or 0,
            "r": g.get("requests") or 0,
            "a": rec.get("agent_count") or 0,
            "s": rec.get("duration_s") or 0,
            "m": models,
        })
    return out


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Code usage</title>
<style>
  :root {
    color-scheme: light;
    --page:           #f9f9f7;
    --surface-1:      #fcfcfb;
    --text-primary:   #0b0b0b;
    --text-secondary: #52514e;
    --text-muted:     #898781;
    --grid:           #e1e0d9;
    --axis:           #c3c2b7;
    --border:         rgba(11,11,11,0.10);
    --series-1:       #2a78d6;
    --series-2:       #eb6834;
    --series-3:       #1baf7a;
    --wash:           rgba(11,11,11,0.05);
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) {
      color-scheme: dark;
      --page:           #0d0d0d;
      --surface-1:      #1a1a19;
      --text-primary:   #ffffff;
      --text-secondary: #c3c2b7;
      --text-muted:     #898781;
      --grid:           #2c2c2a;
      --axis:           #383835;
      --border:         rgba(255,255,255,0.10);
      --series-1:       #3987e5;
      --series-2:       #d95926;
      --series-3:       #199e70;
      --wash:           rgba(255,255,255,0.06);
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --page:           #0d0d0d;
    --surface-1:      #1a1a19;
    --text-primary:   #ffffff;
    --text-secondary: #c3c2b7;
    --text-muted:     #898781;
    --grid:           #2c2c2a;
    --axis:           #383835;
    --border:         rgba(255,255,255,0.10);
    --series-1:       #3987e5;
    --series-2:       #d95926;
    --series-3:       #199e70;
    --wash:           rgba(255,255,255,0.06);
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 28px 20px 64px;
    background: var(--page); color: var(--text-primary);
    font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  .wrap { max-width: 1080px; margin: 0 auto; }
  h1 { font-size: 20px; font-weight: 600; margin: 0 0 4px; letter-spacing: -0.01em; }
  .sub { color: var(--text-secondary); font-size: 13px; margin: 0 0 24px; }
  .sub code { font-size: 12px; color: var(--text-muted); }

  .hero { margin: 0 0 20px; }
  .hero .n { font-size: 52px; font-weight: 600; line-height: 1.05; letter-spacing: -0.02em; }
  .hero .l { color: var(--text-secondary); font-size: 13px; }

  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin: 0 0 20px; }
  .tile { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; }
  .tile .l { color: var(--text-secondary); font-size: 12px; }
  .tile .v { font-size: 22px; font-weight: 600; margin-top: 2px; }

  .filters { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 0 0 20px; }
  .filters .lab { color: var(--text-secondary); font-size: 12px; margin-right: 2px; }
  button {
    font: inherit; font-size: 13px; cursor: pointer; padding: 5px 11px;
    background: var(--surface-1); color: var(--text-primary);
    border: 1px solid var(--border); border-radius: 999px;
  }
  button:hover { background: var(--wash); }
  button[aria-pressed="true"] { border-color: var(--series-1); color: var(--series-1); font-weight: 600; }
  button:focus-visible { outline: 2px solid var(--series-1); outline-offset: 2px; }

  .card { position: relative; background: var(--surface-1); border: 1px solid var(--border);
          border-radius: 10px; padding: 16px 16px 10px; margin: 0 0 16px; }
  .card h2 { font-size: 14px; font-weight: 600; margin: 0 0 2px; }
  .card .note { color: var(--text-secondary); font-size: 12px; margin: 0 0 12px; }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  @media (max-width: 720px) { .grid2 { grid-template-columns: 1fr; } }
  .scroll { overflow-x: auto; }
  svg { display: block; width: 100%; height: auto; }

  .legend { display: flex; flex-wrap: wrap; gap: 14px; margin: 2px 0 10px; }
  .legend span { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text-secondary); }
  .legend i { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }

  .tip {
    position: absolute; pointer-events: none; opacity: 0; transform: translate(-50%, -100%);
    background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px;
    padding: 7px 9px; font-size: 12px; white-space: nowrap; z-index: 5;
    box-shadow: 0 4px 16px rgba(0,0,0,0.14); transition: opacity .08s;
  }
  .tip .th { color: var(--text-secondary); font-size: 11px; margin-bottom: 3px; }
  .tip .row { display: flex; align-items: center; gap: 6px; }
  .tip .row + .row { margin-top: 2px; }
  .tip .key { width: 10px; height: 2px; border-radius: 1px; }
  .tip .val { font-weight: 600; font-variant-numeric: tabular-nums; }
  .tip .nm { color: var(--text-secondary); }

  table { border-collapse: collapse; width: 100%; font-size: 12.5px; }
  th, td { text-align: right; padding: 6px 8px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
  th:first-child, td:first-child { text-align: left; }
  th { color: var(--text-secondary); font-weight: 500; }
  td { font-variant-numeric: tabular-nums; }
  tbody tr:hover { background: var(--wash); }
  details { margin: 0 0 16px; }
  summary { cursor: pointer; color: var(--text-secondary); font-size: 13px; padding: 4px 0; }
  .empty { color: var(--text-secondary); padding: 8px 0 16px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Claude Code usage</h1>
  <p class="sub" id="sub"></p>

  <div class="filters">
    <span class="lab">Range</span>
    <button data-days="7">7 days</button>
    <button data-days="30">30 days</button>
    <button data-days="90">90 days</button>
    <button data-days="0" aria-pressed="true">All</button>
  </div>

  <div class="hero"><div class="n" id="heroN">–</div><div class="l" id="heroL"></div></div>
  <div class="kpis" id="kpis"></div>

  <div class="card">
    <h2>Output tokens per day</h2>
    <p class="note" id="note-out"></p>
    <div class="scroll"><svg id="c-out" viewBox="0 0 940 240" role="img" aria-label="Output tokens per day"></svg></div>
    <div class="tip" id="t-out"></div>
  </div>

  <div class="card">
    <h2>Input tokens per day, by kind</h2>
    <p class="note">Cache reads dominate by design — every request re-sends the conversation.</p>
    <div class="legend" id="lg-in"></div>
    <div class="scroll"><svg id="c-in" viewBox="0 0 940 240" role="img" aria-label="Input tokens per day by kind"></svg></div>
    <div class="tip" id="t-in"></div>
  </div>

  <div class="grid2">
    <div class="card">
      <h2>Output tokens by model</h2>
      <p class="note">Which models did the writing.</p>
      <svg id="c-model" viewBox="0 0 460 240" role="img" aria-label="Output tokens by model"></svg>
      <div class="tip" id="t-model"></div>
    </div>
    <div class="card">
      <h2>Output tokens by project</h2>
      <p class="note">Top 10 working directories.</p>
      <svg id="c-proj" viewBox="0 0 460 240" role="img" aria-label="Output tokens by project"></svg>
      <div class="tip" id="t-proj"></div>
    </div>
  </div>

  <details>
    <summary>Show daily numbers (table view)</summary>
    <div class="card scroll"><table id="tbl-day"></table></div>
  </details>

  <div class="card scroll">
    <h2>Biggest sessions</h2>
    <p class="note">Top 20 by output tokens.</p>
    <table id="tbl-sess"></table>
  </div>
</div>

<script>
const DATA = __DATA__;
const META = __META__;

const SVG = "http://www.w3.org/2000/svg";
const el = (t, attrs) => {
  const n = document.createElementNS(SVG, t);
  for (const k in (attrs || {})) n.setAttribute(k, attrs[k]);
  return n;
};
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const fullNum = v => Math.round(v).toLocaleString("en-US");
const compactNum = v => {
  const a = Math.abs(v);
  if (a >= 1e9) return (v / 1e9).toFixed(a >= 1e10 ? 0 : 1) + "B";
  if (a >= 1e6) return (v / 1e6).toFixed(a >= 1e7 ? 0 : 1) + "M";
  if (a >= 1e3) return (v / 1e3).toFixed(a >= 1e4 ? 0 : 1) + "K";
  return String(Math.round(v));
};
const niceTicks = (max, count) => {
  if (max <= 0) return [0];
  const raw = max / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw) || 10 * mag;
  const out = [];
  // run past `max` so the top tick always covers the tallest mark
  for (let v = 0; v < max - step * 0.001 || out.length < 2; v += step) out.push(v);
  out.push(out[out.length - 1] + step);
  return out;
};
// Top-rounded column: square at the baseline, 4px rounded data-end.
const colPath = (x, y, w, h, r) => {
  r = Math.max(0, Math.min(r, w / 2, h));
  return `M${x},${y + h}V${y + r}a${r},${r} 0 0 1 ${r},${-r}h${w - 2 * r}a${r},${r} 0 0 1 ${r},${r}V${y + h}Z`;
};
const rowPath = (x, y, w, h, r) => {
  r = Math.max(0, Math.min(r, h / 2, w));
  return `M${x},${y}h${w - r}a${r},${r} 0 0 1 ${r},${r}v${h - 2 * r}a${r},${r} 0 0 1 ${-r},${r}H${x}Z`;
};

function tooltip(box, tip, x, y, title, rows) {
  tip.textContent = "";
  const th = document.createElement("div");
  th.className = "th";
  th.textContent = title;
  tip.appendChild(th);
  for (const r of rows) {
    const line = document.createElement("div");
    line.className = "row";
    if (r.color) {
      const k = document.createElement("span");
      k.className = "key";
      k.style.background = r.color;
      line.appendChild(k);
    }
    const v = document.createElement("span");
    v.className = "val";
    v.textContent = r.value;
    line.appendChild(v);
    if (r.name) {
      const n = document.createElement("span");
      n.className = "nm";
      n.textContent = r.name;
      line.appendChild(n);
    }
    tip.appendChild(line);
  }
  const w = box.clientWidth;
  tip.style.left = Math.max(60, Math.min(w - 60, x)) + "px";
  tip.style.top = (y - 8) + "px";
  tip.style.opacity = "1";
}
const hideTip = tip => { tip.style.opacity = "0"; };

/* ---- columns: single series (sequential hue) or stacked (categorical) ---- */
function columns(svg, tip, days, series, opts) {
  opts = opts || {};
  const W = 940, H = 240, L = 58, R = 14, T = 14, B = 30;
  svg.textContent = "";
  if (!days.length) return;
  const iw = W - L - R, ih = H - T - B;
  const totals = days.map(d => series.reduce((s, se) => s + se.get(d), 0));
  const max = Math.max(...totals, 1);
  const ticks = niceTicks(max, 4);
  const scale = v => ih * (v / ticks[ticks.length - 1]);
  const band = iw / days.length;
  const bw = Math.min(24, Math.max(1, band * 0.68));
  const surface = css("--surface-1"), muted = css("--text-muted");

  for (const t of ticks) {
    const y = T + ih - scale(t);
    svg.appendChild(el("line", {x1: L, y1: y, x2: W - R, y2: y,
      stroke: t === 0 ? css("--axis") : css("--grid"), "stroke-width": 1}));
    const lb = el("text", {x: L - 8, y: y + 4, fill: muted, "font-size": 11, "text-anchor": "end"});
    lb.textContent = compactNum(t);
    svg.appendChild(lb);
  }

  const maxIdx = totals.indexOf(Math.max(...totals));
  days.forEach((d, i) => {
    const cx = L + band * i + band / 2;
    let acc = 0;
    series.forEach((se, si) => {
      const v = se.get(d);
      if (v <= 0) return;
      const h = scale(v), y0 = T + ih - scale(acc) - h;
      const isTop = si === series.length - 1 || series.slice(si + 1).every(s2 => s2.get(d) <= 0);
      // 2px surface gap between stacked segments (neighbouring columns get band air)
      const gap = isTop ? 0 : 2;
      const hh = Math.max(0.6, h - gap);
      svg.appendChild(el("path", {
        d: (isTop ? colPath : (x, y, w, hgt) => colPath(x, y, w, hgt, 0))(cx - bw / 2, y0 + gap, bw, hh, 4),
        fill: se.color}));
      acc += v;
    });
    // direct label on the extreme only -- never a number on every column
    if (i === maxIdx && totals[i] > 0 && band > 26) {
      const lb = el("text", {x: cx, y: T + ih - scale(totals[i]) - 6, fill: css("--text-secondary"),
        "font-size": 11, "text-anchor": "middle", "font-weight": 600});
      lb.textContent = compactNum(totals[i]);
      svg.appendChild(lb);
    }
    // x labels: thin them out so they never collide
    const every = Math.ceil(days.length / Math.floor(iw / 74));
    if (i % every === 0) {
      const lb = el("text", {x: cx, y: H - 10, fill: muted, "font-size": 11, "text-anchor": "middle"});
      lb.textContent = d.slice(5);
      svg.appendChild(lb);
    }
    // hit target = whole band, wider than the mark
    const hit = el("rect", {x: L + band * i, y: T, width: band, height: ih, fill: "transparent",
      tabindex: 0, role: "img"});
    const rows = series.map(se => ({color: se.color, value: fullNum(se.get(d)), name: se.label}))
                       .filter((r, idx) => series.length === 1 || se_nonzero(series[idx], d));
    hit.setAttribute("aria-label", d + ": " + rows.map(r => r.value + " " + (r.name || "")).join(", "));
    const show = () => {
      const box = svg.parentElement.parentElement;
      const rect = svg.getBoundingClientRect(), bx = box.getBoundingClientRect();
      const px = (rect.left - bx.left) + (cx / W) * rect.width;
      const py = (rect.top - bx.top) + ((T + ih - scale(totals[i])) / H) * rect.height;
      tooltip(box, tip, px, py, d, rows.length ? rows : [{value: "0"}]);
      hit.setAttribute("fill", css("--wash"));
    };
    const hide = () => { hideTip(tip); hit.setAttribute("fill", "transparent"); };
    hit.addEventListener("pointermove", show);
    hit.addEventListener("pointerenter", show);
    hit.addEventListener("focus", show);
    hit.addEventListener("pointerleave", hide);
    hit.addEventListener("blur", hide);
    svg.appendChild(hit);
  });
  function se_nonzero(se, d) { return se.get(d) > 0; }
}

/* ---- horizontal bars: magnitude, one hue, value at the tip ---- */
function bars(svg, tip, rows) {
  const W = 460, rowH = 26, T = 6, L = 0, R = 58;
  svg.textContent = "";
  const H = Math.max(60, rows.length * rowH + T + 6);
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  if (!rows.length) return;
  const labelW = 132, iw = W - labelW - R;
  const max = Math.max(...rows.map(r => r.value), 1);
  const hue = css("--series-1");
  rows.forEach((r, i) => {
    const y = T + i * rowH;
    const w = Math.max(1, iw * (r.value / max));
    const lb = el("text", {x: L, y: y + 15, fill: css("--text-secondary"), "font-size": 12});
    lb.textContent = r.label.length > 20 ? r.label.slice(0, 19) + "…" : r.label;
    svg.appendChild(lb);
    svg.appendChild(el("path", {d: rowPath(labelW, y + 4, w, 14, 4), fill: hue}));
    const vt = el("text", {x: labelW + w + 7, y: y + 15, fill: css("--text-secondary"), "font-size": 11,
      "font-variant-numeric": "tabular-nums"});
    vt.textContent = compactNum(r.value);
    svg.appendChild(vt);
    const hit = el("rect", {x: 0, y: y, width: W, height: rowH, fill: "transparent", tabindex: 0,
      role: "img", "aria-label": r.label + ": " + fullNum(r.value) + " output tokens"});
    const show = () => {
      const box = svg.parentElement;
      const rect = svg.getBoundingClientRect(), bx = box.getBoundingClientRect();
      tooltip(box, tip,
        (rect.left - bx.left) + ((labelW + w) / W) * rect.width,
        (rect.top - bx.top) + ((y + 4) / H) * rect.height,
        r.label, [{color: hue, value: fullNum(r.value), name: "output tokens"},
                  {value: fullNum(r.sessions), name: r.sessions === 1 ? "session" : "sessions"}]);
    };
    hit.addEventListener("pointerenter", show);
    hit.addEventListener("pointermove", show);
    hit.addEventListener("focus", show);
    hit.addEventListener("pointerleave", () => hideTip(tip));
    hit.addEventListener("blur", () => hideTip(tip));
    svg.appendChild(hit);
  });
}

function table(node, cols, rows) {
  node.textContent = "";
  const thead = document.createElement("thead"), tr = document.createElement("tr");
  for (const c of cols) {
    const th = document.createElement("th");
    th.textContent = c;
    tr.appendChild(th);
  }
  thead.appendChild(tr);
  node.appendChild(thead);
  const tb = document.createElement("tbody");
  for (const r of rows) {
    const t = document.createElement("tr");
    for (const cell of r) {
      const td = document.createElement("td");
      td.textContent = cell;
      t.appendChild(td);
    }
    tb.appendChild(t);
  }
  node.appendChild(tb);
}

function tile(label, value) {
  const d = document.createElement("div");
  d.className = "tile";
  const l = document.createElement("div");
  l.className = "l";
  l.textContent = label;
  const v = document.createElement("div");
  v.className = "v";
  v.textContent = value;
  d.append(l, v);
  return d;
}

let days = 0;

function render() {
  let rows = DATA;
  if (days) {
    const cut = new Date(Date.now() - days * 864e5).toISOString().slice(0, 10);
    rows = DATA.filter(r => r.d >= cut);
  }

  const sum = f => rows.reduce((s, r) => s + f(r), 0);
  const out = sum(r => r.o), unc = sum(r => r.i), cw = sum(r => r.cw), cr = sum(r => r.cr);
  const totalIn = unc + cw + cr;
  document.getElementById("heroN").textContent = compactNum(out);
  document.getElementById("heroL").textContent =
    "output tokens across " + fullNum(rows.length) + (rows.length === 1 ? " session" : " sessions") +
    (days ? " in the last " + days + " days" : " on record");

  const kpis = document.getElementById("kpis");
  kpis.textContent = "";
  kpis.append(
    tile("Total input", compactNum(totalIn)),
    tile("Cache hit rate", totalIn ? (100 * cr / totalIn).toFixed(1) + "%" : "–"),
    tile("API requests", fullNum(sum(r => r.r))),
    tile("Subagents spawned", fullNum(sum(r => r.a))),
    tile("Elapsed (incl. idle)", (sum(r => r.s) / 3600).toFixed(0) + "h"));

  // by day
  const byDay = new Map();
  for (const r of rows) {
    let d = byDay.get(r.d);
    if (!d) byDay.set(r.d, d = {o: 0, i: 0, cw: 0, cr: 0, n: 0});
    d.o += r.o; d.i += r.i; d.cw += r.cw; d.cr += r.cr; d.n += 1;
  }
  const dayKeys = [...byDay.keys()].sort();

  document.getElementById("note-out").textContent =
    dayKeys.length ? "One column per active day — " + dayKeys.length + " of them." : "";

  columns(document.getElementById("c-out"), document.getElementById("t-out"), dayKeys,
    [{label: "output tokens", color: css("--series-1"), get: d => byDay.get(d).o}]);

  const inSeries = [
    {label: "cache read", color: css("--series-1"), get: d => byDay.get(d).cr},
    {label: "cache write", color: css("--series-2"), get: d => byDay.get(d).cw},
    {label: "uncached input", color: css("--series-3"), get: d => byDay.get(d).i},
  ];
  const lg = document.getElementById("lg-in");
  lg.textContent = "";
  for (const s of inSeries) {
    const sp = document.createElement("span");
    const i = document.createElement("i");
    i.style.background = s.color;
    sp.append(i, document.createTextNode(s.label));
    lg.appendChild(sp);
  }
  columns(document.getElementById("c-in"), document.getElementById("t-in"), dayKeys, inSeries);

  // by model / by project
  const agg = key => {
    const m = new Map();
    for (const r of rows) {
      if (key === "m") {
        for (const name in r.m) {
          const e = m.get(name) || {value: 0, sessions: 0};
          e.value += r.m[name]; e.sessions += 1; m.set(name, e);
        }
      } else {
        const e = m.get(r.p) || {value: 0, sessions: 0};
        e.value += r.o; e.sessions += 1; m.set(r.p, e);
      }
    }
    return [...m.entries()].map(([label, v]) => ({label, ...v}))
      .sort((a, b) => b.value - a.value);
  };
  bars(document.getElementById("c-model"), document.getElementById("t-model"), agg("m").slice(0, 10));
  bars(document.getElementById("c-proj"), document.getElementById("t-proj"), agg("p").slice(0, 10));

  table(document.getElementById("tbl-day"),
    ["Day", "Sessions", "Output", "Uncached in", "Cache write", "Cache read"],
    dayKeys.slice().reverse().map(d => {
      const v = byDay.get(d);
      return [d, fullNum(v.n), fullNum(v.o), fullNum(v.i), fullNum(v.cw), fullNum(v.cr)];
    }));

  table(document.getElementById("tbl-sess"),
    ["Day", "Project", "Models", "Requests", "Output", "Total input", "Subagents"],
    rows.slice().sort((a, b) => b.o - a.o).slice(0, 20).map(r => [
      r.d, r.p, Object.keys(r.m).map(m => m.replace(/^claude-/, "")).join(", ") || "–",
      fullNum(r.r), fullNum(r.o), fullNum(r.i + r.cw + r.cr), fullNum(r.a)]));
}

document.getElementById("sub").textContent =
  META.sessions + " sessions logged" + (META.span ? " · " + META.span : "") +
  " · generated " + META.generated + " from " + META.log;

for (const b of document.querySelectorAll(".filters button")) {
  b.addEventListener("click", () => {
    for (const o of document.querySelectorAll(".filters button")) o.setAttribute("aria-pressed", "false");
    b.setAttribute("aria-pressed", "true");
    days = Number(b.dataset.days);
    render();
  });
}
render();
// re-read the CSS custom properties when the OS theme flips
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", render);
// a tooltip left behind by a scroll is worse than no tooltip
addEventListener("scroll", () => {
  for (const t of document.querySelectorAll(".tip")) t.style.opacity = "0";
}, {passive: true});
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--log-file", default=DEFAULT_LOG, help="history log (default %s)" % DEFAULT_LOG)
    ap.add_argument("--out", default=DEFAULT_OUT, help="output HTML (default %s)" % DEFAULT_OUT)
    ap.add_argument("--open", action="store_true", help="open the file in a browser")
    args = ap.parse_args()

    records = read_log(args.log_file)
    rows = compact(records)
    if not rows:
        sys.exit("No usable records in %s. Run session_stats.py --backfill first." % args.log_file)

    span = "%s – %s" % (rows[0]["d"], rows[-1]["d"]) if rows else ""
    meta = {
        "sessions": len(rows),
        "span": span,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "log": args.log_file,
    }
    html = (HTML.replace("__DATA__", json.dumps(rows, separators=(",", ":")))
                .replace("__META__", json.dumps(meta)))

    out = os.path.abspath(os.path.expanduser(args.out))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)
    print("session-stats: wrote %s (%d sessions, %.0f KB)"
          % (out, len(rows), len(html) / 1024))
    if args.open:
        webbrowser.open("file://" + out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
