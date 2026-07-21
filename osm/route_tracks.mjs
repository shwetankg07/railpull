#!/usr/bin/env node
/**
 * route_tracks.mjs — draw each train's journey along the REAL rails, not
 * straight lines between stations.
 *
 * It builds a graph of India's railway lines from an OSM extract, then for every
 * pair of stations that are consecutive stops on some train, finds the shortest
 * path along the tracks (Dijkstra) and simplifies it. The result is one GeoJSON
 * LineString per station-to-station segment — drop it straight onto a map.
 *
 * Prereqs:
 *   - data/out/stations.csv with coordinates filled (run osm/geocode_stations.mjs)
 *   - data/out/stops.csv (run transform/export.py)
 *   - the India OSM extract: https://download.geofabrik.de/asia/india-latest.osm.pbf
 *
 * Usage:  node --max-old-space-size=4096 osm/route_tracks.mjs <india-latest.osm.pbf>
 * Output: data/out/tracks.geojson
 */
import { createReadStream, readFileSync, writeFileSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import parseOSM from 'osm-pbf-parser';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const OUT = join(ROOT, 'data', 'out');
const pbf = process.argv[2];
if (!pbf || !existsSync(pbf)) {
  console.error('usage: node osm/route_tracks.mjs <india-latest.osm.pbf>');
  process.exit(1);
}
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);
const R = 6371, rad = Math.PI / 180;

// ---- stations (code -> lon/lat), from the geocoded CSV
const stationRows = parseCsv(readFileSync(join(OUT, 'stations.csv'), 'utf8'));
const stCoord = new Map();
for (const r of stationRows) if (r.lat && r.lon) stCoord.set(r.code, [+r.lon, +r.lat]);
log(`geocoded stations: ${stCoord.size}`);

// ---- adjacent station pairs from consecutive stops per train
const stopRows = parseCsv(readFileSync(join(OUT, 'stops.csv'), 'utf8'));
const byTrain = new Map();
for (const r of stopRows) {
  if (!byTrain.has(r.train_number)) byTrain.set(r.train_number, []);
  byTrain.get(r.train_number).push(r);
}
const pairs = new Set();
for (const stops of byTrain.values()) {
  stops.sort((a, b) => (+a.seq) - (+b.seq));
  for (let i = 1; i < stops.length; i++) {
    const a = stops[i - 1].station_code, b = stops[i].station_code;
    if (a && b && a !== b && stCoord.has(a) && stCoord.has(b)) {
      pairs.add(a < b ? `${a}\t${b}` : `${b}\t${a}`);
    }
  }
}
log(`unique station pairs to route: ${pairs.size}`);

// ---- build the rail graph from the OSM extract (two passes)
function streamPass(handler) {
  return new Promise((resolve, reject) => {
    createReadStream(pbf).pipe(parseOSM())
      .on('data', (items) => { for (const it of items) handler(it); })
      .on('end', resolve).on('error', reject);
  });
}
log('pass 1: rail ways …');
const RAIL = new Set(['rail', 'narrow_gauge']);
const ways = [];
await streamPass((it) => {
  if (it.type !== 'way' || !it.tags || !RAIL.has(it.tags.railway) || it.tags.service) return;
  ways.push(it.refs.map(Number));
});
const needed = new Set();
for (const refs of ways) for (const r of refs) needed.add(r);
log(`rail ways: ${ways.length}, nodes needed: ${needed.size}`);

log('pass 2: node coords …');
const nodeIdx = new Map();
const lon = new Float64Array(needed.size), lat = new Float64Array(needed.size);
let nn = 0;
await streamPass((it) => {
  if (it.type !== 'node' || !needed.has(Number(it.id))) return;
  nodeIdx.set(Number(it.id), nn); lon[nn] = it.lon; lat[nn] = it.lat; nn++;
});

// adjacency (forward-star)
const adjHead = new Int32Array(nn).fill(-1);
const adjNext = [], adjTo = [], adjW = [];
const hav = (a, b) => {
  const dLat = (lat[b] - lat[a]) * rad, dLon = (lon[b] - lon[a]) * rad;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(lat[a] * rad) * Math.cos(lat[b] * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
};
const addEdge = (a, b) => {
  const w = hav(a, b);
  adjTo.push(b); adjW.push(w); adjNext.push(adjHead[a]); adjHead[a] = adjTo.length - 1;
  adjTo.push(a); adjW.push(w); adjNext.push(adjHead[b]); adjHead[b] = adjTo.length - 1;
};
for (const refs of ways) {
  let prev = -1;
  for (const r of refs) {
    const idx = nodeIdx.get(r);
    if (idx == null) { prev = -1; continue; }
    if (prev >= 0) addEdge(prev, idx);
    prev = idx;
  }
}
log(`graph: ${nn} nodes, ${adjTo.length / 2} edges`);

// spatial hash to snap a station onto the nearest graph node
const CELL = 0.05, grid = new Map();
for (let i = 0; i < nn; i++) {
  const k = `${Math.floor(lon[i] / CELL)},${Math.floor(lat[i] / CELL)}`;
  (grid.get(k) || grid.set(k, []).get(k)).push(i);
}
function snap(slon, slat, maxKm = 3) {
  const cx = Math.floor(slon / CELL), cy = Math.floor(slat / CELL);
  let best = -1, bd = Infinity;
  for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) {
    for (const i of grid.get(`${cx + dx},${cy + dy}`) || []) {
      const dLat = (lat[i] - slat) * rad, dLon = (lon[i] - slon) * rad;
      const h = Math.sin(dLat / 2) ** 2 + Math.cos(slat * rad) * Math.cos(lat[i] * rad) * Math.sin(dLon / 2) ** 2;
      const d = 2 * R * Math.asin(Math.sqrt(h));
      if (d < bd) { bd = d; best = i; }
    }
  }
  return bd <= maxKm ? best : -1;
}
const snapCache = new Map();
const snapOf = (code) => {
  if (snapCache.has(code)) return snapCache.get(code);
  const [x, y] = stCoord.get(code);
  const g = snap(x, y);
  snapCache.set(code, g);
  return g;
};

// Dijkstra with a distance cap, resetting only touched nodes each run
class Heap {
  constructor() { this.a = []; }
  push(n, d) { const a = this.a; a.push([d, n]); let i = a.length - 1; while (i) { const p = (i - 1) >> 1; if (a[p][0] <= a[i][0]) break;[a[p], a[i]] = [a[i], a[p]]; i = p; } }
  pop() { const a = this.a, top = a[0], last = a.pop(); if (a.length) { a[0] = last; let i = 0; for (; ;) { const l = i * 2 + 1, r = l + 1; let m = i; if (l < a.length && a[l][0] < a[m][0]) m = l; if (r < a.length && a[r][0] < a[m][0]) m = r; if (m === i) break;[a[m], a[i]] = [a[i], a[m]]; i = m; } } return top; }
  get size() { return this.a.length; }
}
const dist = new Float64Array(nn).fill(Infinity), prev = new Int32Array(nn).fill(-1), touched = [];
function route(a, b, capKm) {
  for (const t of touched) { dist[t] = Infinity; prev[t] = -1; }
  touched.length = 0;
  const h = new Heap(); dist[a] = 0; touched.push(a); h.push(a, 0);
  while (h.size) {
    const [d, u] = h.pop();
    if (u === b) break;
    if (d > dist[u]) continue;
    if (d > capKm) return null;
    for (let e = adjHead[u]; e >= 0; e = adjNext[e]) {
      const v = adjTo[e], nd = d + adjW[e];
      if (nd < dist[v]) { if (dist[v] === Infinity) touched.push(v); dist[v] = nd; prev[v] = u; h.push(v, nd); }
    }
  }
  if (dist[b] === Infinity) return null;
  const path = [];
  for (let u = b; u >= 0; u = prev[u]) path.push(u);
  return { path: path.reverse(), km: dist[b] };
}

// Douglas-Peucker simplification (~60 m tolerance)
function simplify(pts, tol) {
  if (pts.length <= 2) return pts;
  const keep = new Uint8Array(pts.length); keep[0] = keep[pts.length - 1] = 1;
  const stack = [[0, pts.length - 1]];
  while (stack.length) {
    const [i0, i1] = stack.pop();
    let maxD = 0, maxI = -1;
    const [x0, y0] = pts[i0], [x1, y1] = pts[i1];
    const dx = x1 - x0, dy = y1 - y0, len2 = dx * dx + dy * dy || 1e-12;
    for (let i = i0 + 1; i < i1; i++) {
      const [x, y] = pts[i];
      const t = Math.max(0, Math.min(1, ((x - x0) * dx + (y - y0) * dy) / len2));
      const ex = x0 + t * dx - x, ey = y0 + t * dy - y, d = ex * ex + ey * ey;
      if (d > maxD) { maxD = d; maxI = i; }
    }
    if (maxD > tol * tol) { keep[maxI] = 1; stack.push([i0, maxI], [maxI, i1]); }
  }
  return pts.filter((_, i) => keep[i]);
}

// ---- route every pair, emit GeoJSON
log('routing …');
const features = [];
let ok = 0, skip = 0;
const gc = (a, b) => {
  const [ax, ay] = stCoord.get(a), [bx, by] = stCoord.get(b);
  const dLat = (by - ay) * rad, dLon = (bx - ax) * rad;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(ay * rad) * Math.cos(by * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
};
for (const key of pairs) {
  const [a, b] = key.split('\t');
  const ga = snapOf(a), gb = snapOf(b);
  const straight = gc(a, b);
  if (ga < 0 || gb < 0 || ga === gb) { skip++; continue; }
  const r = route(ga, gb, Math.max(straight * 3, straight + 40));
  if (!r || r.km > straight * 2.6 + 15) { skip++; continue; } // no path / implausible detour
  const pts = simplify(r.path.map((i) => [Math.round(lon[i] * 1e5) / 1e5, Math.round(lat[i] * 1e5) / 1e5]), 0.0006);
  if (pts.length < 2) { skip++; continue; }
  features.push({ type: 'Feature', properties: { from: a, to: b, km: Math.round(r.km) }, geometry: { type: 'LineString', coordinates: pts } });
  ok++;
}
writeFileSync(join(OUT, 'tracks.geojson'), JSON.stringify({ type: 'FeatureCollection', features }));
log(`routed ${ok} pairs (${skip} skipped) -> ${join(OUT, 'tracks.geojson')}`);

// ---- tiny CSV reader (handles quoted fields)
function parseCsv(text) {
  const lines = text.trim().split(/\r?\n/);
  const head = splitRow(lines[0]);
  return lines.slice(1).map((line) => {
    const cells = splitRow(line), o = {};
    head.forEach((h, i) => (o[h] = cells[i] ?? ''));
    return o;
  });
}
function splitRow(row) {
  const out = []; let cur = '', q = false;
  for (let i = 0; i < row.length; i++) {
    const c = row[i];
    if (q) { if (c === '"' && row[i + 1] === '"') { cur += '"'; i++; } else if (c === '"') q = false; else cur += c; }
    else if (c === '"') q = true; else if (c === ',') { out.push(cur); cur = ''; } else cur += c;
  }
  out.push(cur); return out;
}
