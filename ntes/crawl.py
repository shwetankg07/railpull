"""crawl.py — polite, resume-safe crawl of the current Indian Railways
timetable through NTES (the National Train Enquiry System).

Two stages:
  1. discovery — NTES has no "list all trains" endpoint, so we find them by
     searching number prefixes. Query "000".."999"; any prefix that returns the
     60-result cap is drilled one digit deeper. Union is the train roster.
  2. schedules — fetch the full stop list for every discovered number, one JSON
     file per train. Re-runs skip trains already on disk, so the crawl resumes
     safely after any interruption.

Politeness is deliberate: ~1 request every ~1.2s with jitter, exponential
backoff on network trouble, and a hard stop if the endpoint refuses repeatedly.
Please keep it that way — this is a public government service, not a load test.

Output (relative to the repo root):
  data/raw/schedules/<number>.json   one NTES schedule per train
  data/raw/numbers.json              discovered roster + swept prefixes (resume)
  data/raw/errors.json               numbers that returned nothing (skip on resume)
  data/raw/crawl-status.json         live progress you can cat / watch

Usage:
  python ntes/crawl.py                 # full crawl (hours; resumable)
  python ntes/crawl.py --pause 1.5     # be even gentler
  python ntes/crawl.py --seed roster.json   # optional: seed extra train numbers
"""
import argparse
import functools
import json
import os
import random
import sys
import time
from pathlib import Path

from ntes import NTESClient
from ntes.exceptions import NTESError

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "raw"
SCHED = OUT / "schedules"
STATUS = OUT / "crawl-status.json"
NUMBERS = OUT / "numbers.json"
ERRORS = OUT / "errors.json"

CAP = 60  # observed search-result cap that triggers a deeper prefix
consecutive_failures = 0
PAUSE = 1.2


def status(**kw):
    d = json.loads(STATUS.read_text()) if STATUS.exists() else {}
    d.update(kw, updatedAt=time.strftime("%Y-%m-%d %H:%M:%S"))
    STATUS.write_text(json.dumps(d, indent=1))


def nap():
    time.sleep(PAUSE + random.uniform(0, 0.4))


def call(client_fn, *args):
    """Polite call with backoff for NETWORK trouble. An NTESError is a semantic
    'no / invalid' answer (bad query, discontinued number) — fail fast, no retry."""
    global consecutive_failures
    for attempt in range(4):
        try:
            r = client_fn(*args)
            consecutive_failures = 0
            return r
        except NTESError:
            consecutive_failures = 0
            return None
        except Exception as e:  # network / timeout / decode
            wait = [5, 25, 60, 0][attempt]
            if attempt == 3:
                consecutive_failures += 1
                if consecutive_failures >= 12:
                    status(phase="STALLED", error=f"{type(e).__name__}: {e}")
                    print("endpoint refusing repeatedly — stopping politely", flush=True)
                    sys.exit(2)
                return None
            time.sleep(wait)


def discover(client, seed_numbers):
    found = {}
    if NUMBERS.exists():
        found = dict(json.loads(NUMBERS.read_text()))
        print(f"resuming discovery over {len(found)} known numbers", flush=True)
    for no in seed_numbers:
        found.setdefault(str(no), {"src": "seed"})

    done = set(found.pop("__prefixes__", {}).get("done", [])) if "__prefixes__" in found else set()
    stack = [f"{i:03d}" for i in range(1000)]  # search rejects queries under 3 chars
    swept = 0
    while stack:
        p = stack.pop()
        if p in done:
            continue
        r = call(client.search, p)
        nap()
        swept += 1
        trains = (r or {}).get("Trains", []) or []
        for t in trains:
            no = str(t.get("TrainNumber", ""))
            if no.startswith(p) and no not in found:
                found[no] = {"name": t.get("TrainName", ""), "type": t.get("Type", ""), "src": f"search:{p}"}
        if len(trains) >= CAP and len(p) < 5:
            stack.extend(f"{p}{d}" for d in "0123456789")
        done.add(p)
        if swept % 25 == 0:
            _save_numbers(found, done)
            status(phase="discovery", prefixesSwept=swept, queue=len(stack), numbersFound=len(found))
            print(f"discovery: swept={swept} queue={len(stack)} found={len(found)}", flush=True)

    _save_numbers(found, done)
    status(phase="discovery-done", numbersFound=len(found))
    print(f"discovery complete: {len(found)} numbers", flush=True)
    return found


def _save_numbers(found, done):
    payload = dict(found)
    payload["__prefixes__"] = {"done": sorted(done)}
    NUMBERS.write_text(json.dumps(payload, ensure_ascii=False))


def fetch_schedules(client, numbers):
    errors = json.loads(ERRORS.read_text()) if ERRORS.exists() else {}
    todo = [n for n in sorted(numbers)
            if not n.startswith("__")
            and not (SCHED / f"{n}.json").exists()
            and n not in errors]
    print(f"schedules: {len(todo)} to fetch ({len(numbers)} known)", flush=True)
    ok = fail = 0
    for i, no in enumerate(todo):
        r = call(client.schedule, no)
        nap()
        if r and r.get("stations"):
            (SCHED / f"{no}.json").write_text(json.dumps(r, ensure_ascii=False))
            ok += 1
        else:
            errors[no] = "empty-or-error"
            fail += 1
        if (i + 1) % 100 == 0:
            ERRORS.write_text(json.dumps(errors, indent=1))
            status(phase="schedules", fetchedThisRun=ok, failedThisRun=fail,
                   remaining=len(todo) - i - 1, schedulesOnDisk=len(list(SCHED.glob("*.json"))))
            print(f"schedules: {i + 1}/{len(todo)} ok={ok} fail={fail}", flush=True)
    ERRORS.write_text(json.dumps(errors, indent=1))
    status(phase="DONE", schedulesOnDisk=len(list(SCHED.glob("*.json"))), failed=len(errors))
    print("crawl complete", flush=True)


def main():
    global PAUSE
    ap = argparse.ArgumentParser(description="Polite, resume-safe NTES timetable crawler")
    ap.add_argument("--pause", type=float, default=PAUSE, help="seconds between requests (default 1.2)")
    ap.add_argument("--seed", type=str, default="", help="optional JSON file of extra train numbers to seed discovery")
    args = ap.parse_args()
    PAUSE = max(0.6, args.pause)  # a floor: do not hammer a public service

    SCHED.mkdir(parents=True, exist_ok=True)
    seed_numbers = []
    if args.seed and Path(args.seed).exists():
        raw = json.loads(Path(args.seed).read_text())
        seed_numbers = raw if isinstance(raw, list) else list(raw.keys())
        print(f"seeded {len(seed_numbers)} extra numbers", flush=True)

    client = NTESClient()
    # the enquiry server sometimes accepts a connection then goes silent forever;
    # without a read timeout one dead socket hangs the whole crawl
    client.session.request = functools.partial(client.session.request, timeout=(10, 30))

    status(phase="starting", pid=os.getpid())
    roster = discover(client, seed_numbers)
    fetch_schedules(client, roster)


if __name__ == "__main__":
    main()
