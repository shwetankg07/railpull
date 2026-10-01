"""gtfs.py — package the export as a GTFS feed, data/out/gtfs.zip.

GTFS is the timetable format transit software reads: Google Maps,
OpenTripPlanner, and most routing and analysis libraries. It needs coordinates
for every stop, so run it after the exporter and the geocoder:

  python transform/export.py
  node osm/geocode_stations.mjs india-latest.osm.pbf
  python transform/gtfs.py

What becomes what:
  stations.csv (with lat/lon)   -> stops.txt
  each train                    -> one route + one trip
  each train's exact run dates  -> calendar_dates.txt
  each train's stops            -> stop_times.txt

Stations the geocoder couldn't place are left out, with their stop times, and
so are parcel trains (no passengers). The script prints how many of each. Times past midnight keep counting (25:10:00 is
01:10 the next day), which is how GTFS writes trips that run for days.

Usage:  python transform/gtfs.py
        python transform/gtfs.py --selftest
"""
import csv
import io
import json
import sys
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "out"


def minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def times(stops):
    """NTES clock times -> minutes since midnight of the day the train starts.

    Each stop has an HH:MM time and a `day` (1 = the start day). `day` is the
    day the train *leaves* the stop, so a train that arrives at 23:55 and leaves
    at 00:05 arrived the day before. Checked on 9,324 trains: with that rule,
    time never runs backwards along a route. Returns (index, code, arr, dep)."""
    out = []
    for i, s in enumerate(stops):
        a, d = s.get("arr") or "", s.get("dep") or ""
        if not a and not d:
            continue
        base = (int(s.get("day") or 1) - 1) * 1440
        arr = base + minutes(a) if a else None
        dep = base + minutes(d) if d else None
        arr = dep if arr is None else arr
        dep = arr if dep is None else dep
        if dep < arr:
            arr -= 1440
        out.append((i, s["code"], arr, dep))
    return out


def hms(mins):
    return f"{mins // 60:02d}:{mins % 60:02d}:00"


def main():
    jsonl, stations = OUT / "schedules.jsonl", OUT / "stations.csv"
    if not jsonl.exists():
        sys.exit(f"no {jsonl}, run `python transform/export.py` first")
    with stations.open(encoding="utf-8") as fh:
        coords = {r["code"]: (r["name"] or r["code"], r["lat"], r["lon"])  # a few NTES stations have no name
                  for r in csv.DictReader(fh) if r["lat"] and r["lon"]}
    if not coords:
        sys.exit("stations.csv has no coordinates, run `node osm/geocode_stations.mjs <pbf>` first")

    routes, trips, stop_times, dates = [], [], [], []
    services = {}  # run dates -> service_id, so trains running on the same days share one
    used = set()
    dropped = skipped = parcel = 0
    with jsonl.open(encoding="utf-8") as fh:
        for line in fh:
            t = json.loads(line)
            if "run_dates" not in t:
                sys.exit("schedules.jsonl is from an older export, re-run `python transform/export.py`")
            if t["type"] == "PEXP":  # parcel trains carry no passengers, keep them out of trip planners
                parcel += 1
                continue
            timed = times(t["stops"])
            rows = [r for r in timed if r[1] in coords]
            dropped += len(timed) - len(rows)
            if len(rows) < 2 or not t["run_dates"]:
                skipped += 1
                continue
            key = tuple(t["run_dates"])
            if key not in services:
                services[key] = sid = f"s{len(services) + 1}"
                dates += [[sid, d.replace("-", ""), 1] for d in key]
            no = t["number"]
            desc = "" if t["type_label"].lower() == t["name"].lower() else t["type_label"]  # some trains are just named "JAN SHATABDI"
            routes.append([no, "IR", no, t["name"], desc, 2])  # 2 = rail
            trips.append([no, services[key], no, t["destination"]])
            for i, code, arr, dep in rows:
                stop_times.append([no, hms(arr), hms(dep), code, i + 1])
                used.add(code)

    span = sorted(d.replace("-", "") for k in services for d in k)
    if not span:
        sys.exit("no trains with run dates and placed stops, nothing to write")
    files = {
        "feed_info.txt": (["feed_publisher_name", "feed_publisher_url", "feed_lang", "feed_start_date",
                           "feed_end_date", "feed_version", "feed_contact_url"],
                          [["railpull", "https://github.com/shwetankg07/railpull", "en", span[0], span[-1],
                            date.today().strftime("%Y%m%d"), "https://github.com/shwetankg07/railpull/issues"]]),
        "agency.txt": (["agency_id", "agency_name", "agency_url", "agency_timezone", "agency_lang"],
                       [["IR", "Indian Railways", "https://indianrailways.gov.in", "Asia/Kolkata", "en"]]),
        "stops.txt": (["stop_id", "stop_code", "stop_name", "stop_lat", "stop_lon"],
                      [[c, c, *coords[c]] for c in sorted(used)]),
        "routes.txt": (["route_id", "agency_id", "route_short_name", "route_long_name", "route_desc", "route_type"], routes),
        "trips.txt": (["route_id", "service_id", "trip_id", "trip_headsign"], trips),
        "calendar_dates.txt": (["service_id", "date", "exception_type"], dates),
        "stop_times.txt": (["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"], stop_times),
    }
    with zipfile.ZipFile(OUT / "gtfs.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for name, (header, rows) in files.items():
            buf = io.StringIO()
            w = csv.writer(buf, lineterminator="\n")
            w.writerow(header)
            w.writerows(rows)
            z.writestr(name, buf.getvalue())

    print(f"done: {len(trips)} trips, {len(used)} stops, {len(stop_times)} stop times, {len(services)} service patterns")
    print(f"  service dates {span[0]} to {span[-1]}")
    print(f"  left out: {parcel} parcel trains, {dropped} stop times at stations with no coordinates, "
          f"{skipped} trains with under 2 placed stops or no run dates")
    print(f"  -> {OUT}/gtfs.zip")


def selftest():
    # arrives 23:55, leaves 00:05: NTES calls that stop day 2, the arrival is day 1
    stops = [{"code": "A", "arr": "", "dep": "22:00", "day": 1},
             {"code": "B", "arr": "23:55", "dep": "00:05", "day": 2},
             {"code": "C", "arr": "01:30", "dep": "", "day": 2}]
    got = [(c, hms(a), hms(d)) for _, c, a, d in times(stops)]
    assert got == [("A", "22:00:00", "22:00:00"), ("B", "23:55:00", "24:05:00"),
                   ("C", "25:30:00", "25:30:00")], got
    print("selftest ok")


if __name__ == "__main__":
    selftest() if sys.argv[1:] == ["--selftest"] else main()
