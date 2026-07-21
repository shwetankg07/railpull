"""poll_delays.py — sweep NTES station boards into a single live-delay snapshot.

The trick that makes whole-network delay tracking affordable: one station board
lists every train arriving/departing there in a time window, each with its live
delay and cancellation flag. So sweeping a few hundred busy junctions covers most
of the moving mainline fleet in a few hundred polite requests — no need to query
trains one by one.

Stations are seeded from ntes/major_stations.json (the ~250 busiest junctions,
bundled). One sweep writes data/out/delays.json:

  { "updatedAt": <epoch>, "source": "ntes-station-boards",
    "trains": { "<number>": {"d": <delayMinutes>} | {"c": 1}, ... } }

Usage:
  python ntes/poll_delays.py                 # one sweep
  python ntes/poll_delays.py --loop          # sweep ~every 5 min forever
  python ntes/poll_delays.py --stations 200 --hours 4
"""
import argparse
import functools
import json
import os
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ntes import NTESClient
from ntes.exceptions import NTESError

ROOT = Path(__file__).resolve().parent.parent
SEED = Path(__file__).resolve().parent / "major_stations.json"
OUT = ROOT / "data" / "out" / "delays.json"
STATUS = ROOT / "data" / "out" / "poller-status.json"

IST = timezone(timedelta(hours=5, minutes=30))
VALID_HOURS = {2, 4, 8}          # NTES only accepts these look-ahead windows
MAX_BELIEVABLE_DELAY = 720       # min; beyond ~12h a "delay" is a stale board entry
PAUSE = 1.2


def log(*a):
    print(datetime.now(IST).strftime("%H:%M:%S"), *a, flush=True)


def parse_delay(s):
    """'HH:MM' -> minutes; anything else -> 0."""
    if not s or ":" not in str(s):
        return 0
    try:
        h, m = str(s).split(":")[:2]
        return max(0, int(h) * 60 + int(m))
    except ValueError:
        return 0


def parse_sched(s, now):
    """'15:08 10-Jul' -> aware datetime (current year), handling year rollover."""
    try:
        dt = datetime.strptime(f"{s}-{now.year}", "%H:%M %d-%b-%Y").replace(tzinfo=IST)
        if (dt - now).days > 180:
            dt = dt.replace(year=now.year - 1)
        if (now - dt).days > 180:
            dt = dt.replace(year=now.year + 1)
        return dt
    except ValueError:
        return None


def sweep(client, codes, hours):
    now = datetime.now(IST)
    best = {}  # number -> (proximity_seconds, delay_min, cancelled) — keep the nearest sighting
    calls = ok = 0
    for code in codes:
        try:
            r = client.station_live(code, hours)
            ok += 1
        except NTESError:
            r = None  # stale/invalid code or empty window — skip, no penalty
        except Exception as e:
            log(f"  {code}: {type(e).__name__} — backing off")
            time.sleep(4)
            r = None
        calls += 1
        time.sleep(PAUSE + random.uniform(0, 0.3))
        if not r:
            continue
        for t in r.get("TrainsAtStation", []) or []:
            no = str(t.get("TrainNumber", "")).strip()
            if not no:
                continue
            cancelled = bool(t.get("Cancel")) or bool(t.get("DepCancelFlag")) or bool(t.get("ArrCancelFlag"))
            delay = max(parse_delay(t.get("DelayArr")), parse_delay(t.get("DelayDep")))
            sched = parse_sched(t.get("STA") or t.get("STD") or "", now)
            prox = abs((sched - now).total_seconds()) if sched else 9e9
            cur = best.get(no)
            if cur is None or prox < cur[0]:
                best[no] = (prox, delay, cancelled)

    trains = {}
    for no, (_, d, c) in best.items():
        if c:
            trains[no] = {"c": 1}
        elif 5 <= d <= MAX_BELIEVABLE_DELAY:
            trains[no] = {"d": d}
    return trains, calls, ok


def publish(trains):
    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updatedAt": int(time.time()), "source": "ntes-station-boards", "trains": trains}
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")))
    os.replace(tmp, OUT)  # atomic — readers never see a half-written file


def main():
    ap = argparse.ArgumentParser(description="Sweep NTES station boards into one delays.json")
    ap.add_argument("--stations", type=int, default=150, help="how many top junctions to sweep")
    ap.add_argument("--hours", type=int, default=4, choices=sorted(VALID_HOURS))
    ap.add_argument("--loop", action="store_true", help="keep sweeping ~every 5 minutes")
    args = ap.parse_args()

    seed = json.loads(SEED.read_text())
    codes = [s["code"] for s in seed[: args.stations]]
    client = NTESClient()
    client.session.request = functools.partial(client.session.request, timeout=(10, 30))
    log(f"poller: {len(codes)} stations, window +/-{args.hours}h, loop={args.loop}")

    while True:
        t0 = time.time()
        trains, calls, ok = sweep(client, codes, args.hours)
        publish(trains)
        late = sum(1 for v in trains.values() if "d" in v)
        canc = sum(1 for v in trains.values() if "c" in v)
        dur = int(time.time() - t0)
        log(f"sweep done in {dur}s: {calls} calls ({ok} ok) -> {late} late, {canc} cancelled")
        STATUS.write_text(json.dumps({
            "at": datetime.now(IST).isoformat(), "calls": calls, "ok": ok,
            "late": late, "cancelled": canc, "sweepSeconds": dur,
        }, indent=1))
        if not args.loop:
            break
        time.sleep(max(10, 300 - (time.time() - t0)))


if __name__ == "__main__":
    main()
