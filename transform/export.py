"""export.py — turn the raw NTES crawl into tidy, analysis-ready tables.

Reads data/raw/schedules/*.json (produced by ntes/crawl.py) and writes:

  data/out/trains.csv      one row per train: number, name, type, running days,
                           source, destination, distance, stop count
  data/out/stops.csv       one row per stop: train, seq, station, day offset,
                           scheduled arrival/departure, halt, distance
  data/out/stations.csv    every station seen: code + name (coordinates are left
                           blank — fill them with osm/geocode_stations.mjs)
  data/out/schedules.jsonl one cleaned JSON record per train (for programmatic use)

The useful bit NTES doesn't hand you directly: `runs_days`. NTES returns the list
of dates a train is scheduled over the coming weeks; we fold those into the set of
weekdays it runs (e.g. "Mon,Wed,Fri", or "Daily"). That's how you tell a daily
train from a biweekly one.

Usage:  python transform/export.py
"""
import csv
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHED = ROOT / "data" / "raw" / "schedules"
OUT = ROOT / "data" / "out"

WEEK = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# NTES TrainType code -> human label. Codes verified against a full crawl.
TYPE_LABEL = {
    "RAJ": "Rajdhani", "SHT": "Shatabdi", "JSH": "Jan Shatabdi", "DRNT": "Duronto",
    "GBR": "Garib Rath", "TEJ": "Tejas", "GT": "Gatimaan",
    "VNDB": "Vande Bharat", "VNDM": "Vande Bharat Metro", "VNDS": "Vande Bharat Sleeper",
    "SUF": "Superfast", "AMTB": "Amrit Bharat", "SKR": "Sampark Kranti", "HUM": "Humsafar",
    "ANT": "Antyodaya", "SUV": "Suvidha", "YPR": "Yuva", "PEXP": "Premium Express",
    "MEX": "Mail/Express", "EXP": "Express", "TOD": "Special", "TRST": "Tourist", "SPL": "Special",
    "SUB": "Suburban", "PAS": "Passenger", "MEMU": "MEMU", "DEMU": "DEMU", "DMU": "DMU",
    "EMU": "EMU", "MMTS": "MMTS", "TOY": "Toy Train", "": "Express",
}


def running_days(sched):
    """Fold NTES's run-date list into the weekdays the train runs. 'Daily' or all
    seven -> 'Daily'; otherwise a comma list in Mon..Sun order."""
    dates = sched.get("vStartDateList") or []
    if not dates:
        return "Daily" if "daily" in (sched.get("DaysOfRun") or "").lower() else "Unknown"
    seen = set()
    for d in dates:
        try:
            seen.add(datetime.strptime(d, "%d-%b-%Y").weekday())  # Mon=0..Sun=6
        except ValueError:
            continue
    if len(seen) >= 7 or not seen:
        return "Daily"
    return ",".join(WEEK[i] for i in range(7) if i in seen)


def main():
    files = sorted(SCHED.glob("*.json")) if SCHED.exists() else []
    if not files:
        print(f"no schedules at {SCHED} — run `python ntes/crawl.py` first")
        return
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"exporting {len(files)} schedules …")

    trains_w = csv.writer((OUT / "trains.csv").open("w", newline="", encoding="utf-8"), lineterminator="\n")
    stops_w = csv.writer((OUT / "stops.csv").open("w", newline="", encoding="utf-8"), lineterminator="\n")
    jsonl = (OUT / "schedules.jsonl").open("w", encoding="utf-8")
    trains_w.writerow(["number", "name", "type", "type_label", "runs_days",
                       "source_code", "source", "dest_code", "destination",
                       "distance_km", "travel_time", "num_stops"])
    stops_w.writerow(["train_number", "seq", "station_code", "station_name",
                      "day", "arrival", "departure", "halt_min", "distance_km"])

    stations = {}  # code -> name
    n_trains = n_stops = 0
    for f in files:
        try:
            s = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        stops = s.get("stations") or []
        if len(stops) < 2:
            continue
        no = str(s.get("TrainNumber") or f.stem)
        ttype = (s.get("TrainType") or "").strip().upper()
        days = running_days(s)
        dist = stops[-1].get("Distance", "")
        trains_w.writerow([
            no, s.get("TrainName", ""), ttype, TYPE_LABEL.get(ttype, ttype or "Express"),
            days, s.get("Source", ""), s.get("SourceName", ""),
            s.get("Destination", ""), s.get("DestinationName", ""),
            dist, s.get("TravelTime", ""), len(stops),
        ])
        clean_stops = []
        for st in stops:
            code = (st.get("StationCode") or "").strip()
            name = (st.get("StationName") or "").strip()
            if code and code not in stations:
                stations[code] = name
            row = [no, st.get("Sr", ""), code, name, st.get("Day", ""),
                   st.get("STA", ""), st.get("STD", ""), st.get("Halt", ""), st.get("Distance", "")]
            stops_w.writerow(row)
            clean_stops.append({
                "seq": st.get("Sr"), "code": code, "name": name, "day": st.get("Day"),
                "arr": st.get("STA") or "", "dep": st.get("STD") or "",
                "halt_min": st.get("Halt"), "distance_km": st.get("Distance"),
            })
            n_stops += 1
        jsonl.write(json.dumps({
            "number": no, "name": s.get("TrainName", ""), "type": ttype,
            "type_label": TYPE_LABEL.get(ttype, ttype or "Express"), "runs_days": days,
            "source": s.get("SourceName", ""), "destination": s.get("DestinationName", ""),
            "distance_km": dist, "stops": clean_stops,
        }, ensure_ascii=False) + "\n")
        n_trains += 1

    with (OUT / "stations.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["code", "name", "lat", "lon"])  # lat/lon filled by geocoder
        for code in sorted(stations):
            w.writerow([code, stations[code], "", ""])

    jsonl.close()
    print(f"done: {n_trains} trains, {n_stops} stops, {len(stations)} stations")
    print(f"  -> {OUT}/trains.csv, stops.csv, stations.csv, schedules.jsonl")


if __name__ == "__main__":
    main()
