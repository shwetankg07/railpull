# railpull

Pull the current Indian Railways timetable out of NTES (the National Train
Enquiry System) and turn it into files you can use: CSVs, JSON lines, and a
GTFS feed. It also has a live delay poller, and OpenStreetMap scripts that place
every station and draw each route along the real tracks.

You don't need an API key or a paid service. The core is Python, and the
optional map steps use Node.

It reads a public government service through an unofficial client, so please
read [Responsible use](#responsible-use) before you run it.

## Why

India has one of the largest railway networks in the world and no clean,
current, official open dataset of its timetable. Most projects still use a
community snapshot from around 2016. That snapshot has no Vande Bharat trains,
uses old station names, and treats every train as if it runs daily.

NTES has the current data, but it only hands it out one train (or one station
board) at a time. railpull walks through all of it slowly and writes the whole
timetable to disk.

## What you get

Run the crawler and the exporter, and `data/out/` fills up with:

| File | One row per | Columns |
|------|-------------|---------|
| `trains.csv` | train | number, name, type, `runs_days`, source, destination, distance, stop count |
| `stops.csv` | stop | train, sequence, station code and name, day, arrival, departure, halt, distance |
| `stations.csv` | station | code, name, lat, lon (the OSM step fills in the coordinates) |
| `schedules.jsonl` | train | the full record as one JSON object, including `run_dates` |
| `gtfs.zip` | | a GTFS feed, see [GTFS](#gtfs-transformgtfspy) |

A full crawl in July 2026 came to 9,324 trains, 185,271 stops and 8,417
stations.

The column worth having is `runs_days`. NTES lists the exact dates each train
starts over the coming weeks, and the exporter folds those into weekdays
(`Daily`, or `Mon,Wed,Fri`). That's how you tell a daily express from a
twice-a-week special. The exact dates are kept too, as `run_dates` in
`schedules.jsonl`.

With the optional OpenStreetMap step you also get coordinates for each station
and `tracks.geojson`, which draws every station-to-station hop along the actual
rails.

## Quickstart

```bash
# crawl + export (Python)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python ntes/crawl.py          # a few hours for the full roster, resumable
python transform/export.py    # -> data/out/*.csv, schedules.jsonl

# live delays (optional): one snapshot of which trains are late right now
python ntes/poll_delays.py    # -> data/out/delays.json

# coordinates, tracks and GTFS (optional, needs Node and a 1.7 GB OSM extract)
npm install
curl -O https://download.geofabrik.de/asia/india-latest.osm.pbf
node osm/geocode_stations.mjs india-latest.osm.pbf                         # fills station lat/lon
node --max-old-space-size=4096 osm/route_tracks.mjs india-latest.osm.pbf  # -> tracks.geojson
python transform/gtfs.py                                                   # -> gtfs.zip
```

The repo doesn't include any data. Every file above comes from running these
scripts.

## How it works

### The crawler (`ntes/crawl.py`)

NTES has no endpoint that lists every train, so the crawler builds the list
itself.

1. Discovery. It searches every three-digit prefix, `000` to `999`. NTES returns
   at most 60 results per search, so a prefix that comes back full gets split
   one digit deeper (`125` becomes `1250` to `1259`). Everything it finds makes
   up the roster, about 12,000 numbers, and about a fifth of those turn out to
   be discontinued.
2. Schedules. It fetches the full stop list for each number and writes one JSON
   file per train. A re-run skips files that are already on disk, so you can
   stop it any time and carry on later.

It sends about one request every 1.2 seconds, with jitter and backoff, and
writes its progress to `data/raw/crawl-status.json`:

```bash
watch -n 30 'cat data/raw/crawl-status.json'
```

### The exporter (`transform/export.py`)

Reads the raw JSON and writes the tables above. It's plain Python with no
dependencies. Besides `runs_days`, it turns NTES's type codes into labels
(`VNDB` is Vande Bharat, `SUF` is Superfast, and so on).

### GTFS (`transform/gtfs.py`)

GTFS is the timetable format most transit software reads, including Google
Maps, OpenTripPlanner, and most routing libraries. `gtfs.py` packs the export
into `data/out/gtfs.zip`:

| GTFS file | From |
|-----------|------|
| `stops.txt` | `stations.csv`, with the geocoder's coordinates |
| `routes.txt`, `trips.txt` | one route and one trip per train |
| `calendar_dates.txt` | each train's exact run dates |
| `stop_times.txt` | each train's stops |

Run it after the geocoder, because GTFS needs coordinates for every stop. It
leaves out stations the geocoder couldn't place, with their stop times, and it
leaves out parcel trains, since nobody can board them. It prints how many of
each it dropped.

Times past midnight keep counting, so `25:30:00` means 01:30 the next day. That's
how GTFS writes trips that run for several days. Kanyakumari to Dibrugarh fits
fine.

The feed only covers the dates NTES listed when you crawled, which is about ten
weeks. For a current feed, crawl again. From the July 2026 crawl it came to about
9,300 trips and 1.7 MB zipped.

MobilityData's [GTFS validator](https://github.com/MobilityData/gtfs-validator)
(v8.0.1) finds 0 errors in that feed. Its warnings are the expired dates (it's
July data), station names in capitals (that's how NTES writes them), and a
handful of impossible speeds where a station got the coordinates of a
same-named place somewhere else (Chennai's Indira Nagar ended up in Gujarat).

To check the time handling: `python transform/gtfs.py --selftest`.

### The delay poller (`ntes/poll_delays.py`)

A station board lists every train passing through in a time window, along with
its delay and whether it's cancelled. So the poller doesn't ask about thousands
of trains one by one. It sweeps a few hundred busy junctions (listed in
`ntes/major_stations.json`) and merges what it sees into one `delays.json`:

```json
{ "updatedAt": 1783683912, "source": "ntes-station-boards",
  "trains": { "12951": {"d": 18}, "12009": {"c": 1} } }
```

`{"d": 18}` means 18 minutes late and `{"c": 1}` means cancelled. With `--loop`
it sweeps again about every 5 minutes.

### OpenStreetMap scripts (`osm/`, optional)

`geocode_stations.mjs` reads the OSM India extract once and fills in station
coordinates. OSM has about 17,000 Indian railway stations, and about 11,000 of
them carry the official station code, so most stations match by code. The rest
fall back to matching by name.

`route_tracks.mjs` builds a graph of the rail lines (about 600,000 nodes), snaps
each station onto it, and runs Dijkstra between every pair of stations that are
consecutive stops on some train. It simplifies the result and writes GeoJSON. If
a route comes out implausibly long, it's dropped rather than drawn wrong.

## Things I learned the slow way

- Set a read timeout. The enquiry server sometimes accepts a connection and
  then never answers. Without a socket timeout, one dead read hangs the whole
  crawl. The crawler sets one, so keep it if you fork the client.
- Search rejects queries shorter than 3 characters, which is why discovery
  starts at three-digit prefixes.
- Station boards only accept look-ahead windows of 2, 4 or 8 hours. Anything
  else returns an error.
- A clean "no" from NTES is not a network error. A bad query or a discontinued
  train number gets a proper error response. Give up on those straight away and
  only back off on real network trouble, or the crawl slows to a crawl.
- A stop's `Day` is the day the train leaves it. A train that arrives at 23:55
  and leaves at 00:05 arrived the day before. If you read `Day` as the arrival
  day, time runs backwards along 91 of the 9,324 routes.
- Use NTES's type codes, not the old community ones: `SUF` for superfast (not
  `SF`), `VNDB` Vande Bharat, `VNDS` Vande Bharat Sleeper, `VNDM` Namo Bharat
  Rapid Rail (it used to be called Vande Bharat Metro), `DRNT` Duronto, `GBR`
  Garib Rath, `MEX` Mail/Express, `SUB` suburban, and `PEXP` Parcel Express,
  which carries parcels, not passengers. Get these wrong and your superfast
  bucket comes out empty.
- Board delays can be junk. Now and then a board says a train is "57:18" late,
  left over from a service that's long gone. The poller ignores anything over
  12 hours.
- Some station codes have changed: Mughal Sarai is now DDU, and Allahabad is
  Prayagraj. A fresh crawl uses the current codes, but if you mix in older data,
  expect a few renames.

## Used by

These teams cite railpull as a data source. All four are working on SIH26028,
*Dynamic Forecast of ETA for Coaching Trains*, a Ministry of Railways problem
statement in Smart India Hackathon 2026.

| Project | How it uses railpull |
|---------|----------------------|
| [Vivek-Biswal/SIH_ETA](https://github.com/Vivek-Biswal/SIH_ETA) | pulls railpull's output into its data pipeline |
| [p4rthh/sih-train-prototype](https://github.com/p4rthh/sih-train-prototype) | plans its schedule data and track geometry around it |
| [gogoiboss/demo1](https://github.com/gogoiboss/demo1) | lists it in its data sources brief |
| [rehan-ftw/YatraPulse](https://github.com/rehan-ftw/YatraPulse) | lists it in its data sources |

Using it somewhere? Open a PR and add a row.

## Responsible use

railpull talks to a public government service through an unofficial,
reverse-engineered client ([`ntes-client`](https://pypi.org/project/ntes-client/)).

- Keep the request rate low. The defaults already are, so don't lower the pause.
- Crawl once and keep the result. The timetable changes every few weeks, not
  every few minutes.
- It's meant for personal, research and educational use. The schedules are
  public facts, but bulk collection and redistribution of the data may go
  against the operator's terms of use. That's why this repo ships the tools and
  not a dataset. If you publish data you collect, that's on you.
- railpull has no connection to Indian Railways, IRCTC, CRIS or NTES.

## Licenses and credits

- Code: MIT, see `LICENSE`.
- Schedules: fetched from NTES through `ntes-client` (MIT). Public factual data,
  see [Responsible use](#responsible-use).
- Station coordinates and track geometry: from
  [OpenStreetMap](https://www.openstreetmap.org/copyright), © OpenStreetMap
  contributors, ODbL.
- The major-station list started from the CC0
  [datameet/railways](https://github.com/datameet/railways) dataset.

## Keywords

`indian-railways` · `ntes` · `irctc` · `train-schedule` · `timetable` ·
`railway` · `india` · `dataset` · `open-data` · `gtfs` · `geospatial` ·
`openstreetmap`

Built by [shwetank](https://github.com/shwetankg07). This data also drives
[RailRaag](https://railraag.vercel.app), a map of every train in India moving at
once ([code](https://github.com/shwetankg07/RailRaag)).
