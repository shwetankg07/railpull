#!/usr/bin/env node
/**
 * geocode_stations.mjs — fill in station coordinates from OpenStreetMap.
 *
 * NTES gives you station codes and names but no coordinates. OSM has ~17,000
 * Indian railway stations, ~11,000 of them tagged with the official station
 * code (`railway:ref`). This scans an OSM extract once and fills the lat/lon
 * columns of data/out/stations.csv, matching first by code, then by name.
 *
 * Prereq: download the India extract (~1.7 GB) from Geofabrik:
 *   https://download.geofabrik.de/asia/india-latest.osm.pbf
 *
 * Usage:  node osm/geocode_stations.mjs path/to/india-latest.osm.pbf
 * Output: rewrites data/out/stations.csv with lat/lon filled where found,
 *         and prints coverage.
 */
import { createReadStream, readFileSync, writeFileSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import parseOSM from 'osm-pbf-parser';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const STATIONS = join(ROOT, 'data', 'out', 'stations.csv');
const pbf = process.argv[2];
if (!pbf || !existsSync(pbf)) {
  console.error('usage: node osm/geocode_stations.mjs <india-latest.osm.pbf>');
  console.error('get it from https://download.geofabrik.de/asia/india-latest.osm.pbf');
  process.exit(1);
}
if (!existsSync(STATIONS)) {
  console.error(`no ${STATIONS} — run \`python transform/export.py\` first`);
  process.exit(1);
}

const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);
const normName = (s) =>
  (s || '').toLowerCase()
    .replace(/\b(junction|jn|central|city|terminus|terminal|station|halt|cantt|cantonment)\b/g, '')
    .replace(/[^a-z0-9]+/g, ' ')
    .trim();

// ---- pass over the extract, collecting railway station points
log('scanning OSM extract for railway stations (a few minutes) …');
const byRef = new Map();
const byName = new Map();
let stationsFound = 0;
await new Promise((resolve, reject) => {
  createReadStream(pbf).pipe(parseOSM())
    .on('data', (items) => {
      for (const it of items) {
        if (it.type !== 'node' || !it.tags) continue;
        const t = it.tags;
        const isStation = ['station', 'halt', 'stop'].includes(t.railway) || t.public_transport === 'station';
        if (!isStation) continue;
        const name = t.name || t['name:en'];
        if (!name) continue;
        const rec = { lon: Math.round(it.lon * 1e5) / 1e5, lat: Math.round(it.lat * 1e5) / 1e5 };
        const ref = t['railway:ref'] || t.ref || t['ref:railway'];
        if (ref) byRef.set(ref.toUpperCase(), rec);
        const nn = normName(name);
        if (nn && !byName.has(nn)) byName.set(nn, rec);
        stationsFound++;
      }
    })
    .on('end', resolve).on('error', reject);
});
log(`OSM railway stations: ${stationsFound} (${byRef.size} with a code)`);

// ---- fill coordinates
const lines = readFileSync(STATIONS, 'utf8').trim().split(/\r?\n/);
const header = lines[0];
let ref = 0, name = 0, miss = 0;
const out = [header];
for (const line of lines.slice(1)) {
  const [code, nm] = parseCsvRow(line);
  let hit = byRef.get((code || '').toUpperCase());
  let via = 'ref';
  if (!hit) { hit = byName.get(normName(nm)); via = 'name'; }
  if (hit) {
    via === 'ref' ? ref++ : name++;
    out.push(`${csv(code)},${csv(nm)},${hit.lat},${hit.lon}`);
  } else {
    miss++;
    out.push(`${csv(code)},${csv(nm)},,`);
  }
}
writeFileSync(STATIONS, out.join('\n') + '\n');
log(`geocoded ${ref} by code + ${name} by name; ${miss} left blank -> ${STATIONS}`);

// minimal CSV helpers (fields may contain commas/quotes)
function csv(s) {
  s = String(s ?? '');
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}
function parseCsvRow(row) {
  const out = [];
  let cur = '', q = false;
  for (let i = 0; i < row.length; i++) {
    const c = row[i];
    if (q) {
      if (c === '"' && row[i + 1] === '"') { cur += '"'; i++; }
      else if (c === '"') q = false;
      else cur += c;
    } else if (c === '"') q = true;
    else if (c === ',') { out.push(cur); cur = ''; }
    else cur += c;
  }
  out.push(cur);
  return out;
}
