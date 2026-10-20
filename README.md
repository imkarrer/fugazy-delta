# fugazy-delta

Autocross and track-day telemetry: drop in RaceBox and GoPro files, get your
runs and laps, the delta between them, your optimal lap, and an overlay
video. See [PLAN.md](PLAN.md) for what is being built and in what order.

## Develop

Everything runs inside the Flox environment (Python 3.12, uv, ffmpeg, fonts):

```sh
flox activate                 # first activation installs Python deps into .flox/cache/venv
pytest                        # synthetic-data tests
./scripts/fetch-fixtures.sh   # real GoPro samples + GoPro's reference parser
pytest                        # now also runs the real-file tests
```

Or without entering a shell: `flox activate -- pytest`. CI
(`.github/workflows/ci.yml`) does the same, with the real GoPro files, on
every push to main.

## Prototype

A clickable demo of the upload and results pages, on simulated data:

```sh
flox activate --start-services   # serves prototype/ at http://localhost:8642
```

On the upload page, either press one of the three setup buttons (GoPro only,
GoPro + RaceBox, RaceBox only) or drop files: the demo reads only their
extensions, counting `.mp4` as GoPro and `.vbo` or `.csv` as RaceBox, and
picks the setup from that mix. The results page then shows that setup's demo
session. The event switch picks an autocross or a track day; a link
with `?event=track` opens the track day directly. Set `PROTOTYPE_PORT` to
use another port.

Lap and run times are found in the device files by
`fugazy_delta/segment.py`, so GoPro-only times differ from RaceBox times by
the camera's GPS error. Every time column, tile and feature reason carries
that setup's measured error, and the "How accurate is this?" panel lists
the error of each kind of time; PLAN.md explains how the errors are
measured. Each lap or run is driven on its own line, so
comparing two on the map shows two lines. The feature labels are what `Session.features()`
returns for simulated device files written to disk and read back. The map
and delta use the position source that session picks: the RaceBox when
present, otherwise the GoPro's GPS. `tests/test_demo.py` regenerates the data
and fails when it differs from the committed files, so after changing the
simulator or `session.py`, regenerate it:

```sh
flox activate -c 'python -m fugazy_delta.demo prototype/data'
```

The error table has its own test, `tests/test_timing_accuracy.py`, which
fails when the committed table no longer matches the code. After changing the simulator, a noise model or the lap finder, regenerate
the table first (about 30 s on 18 cores), then the demo data, which quotes
it:

```sh
flox activate -c 'python -m fugazy_delta.accuracy && python -m fugazy_delta.demo prototype/data'
```

## Layout

| Path | What |
|---|---|
| `fugazy_delta/model.py` | `Telemetry`, the one data type every parser produces |
| `fugazy_delta/sim.py` | virtual car: courses, speed profile, a line per lap, ground truth with known lap/sector times |
| `fugazy_delta/segment.py` | laps and runs from start/finish and sector line crossings |
| `fugazy_delta/devices.py` | simulated GNSS / RaceBox Mini sampling and noise |
| `fugazy_delta/vbo.py` | Racelogic VBO read/write (RaceBox export format) |
| `fugazy_delta/gpmf.py` | GoPro GPMF encode/decode, HERO9-like payloads |
| `fugazy_delta/mp4.py` | read a GoPro telemetry track; add one to an MP4 |
| `fugazy_delta/fixtures.py` | build synthetic GoPro MP4 and RaceBox VBO files from a truth |
| `fugazy_delta/accuracy.py` | measured error of every reported time, per position source and event; writes `accuracy_table.json` |
| `fugazy_delta/demo.py` | demo results data for the prototype, one JSON file per event and setup |
| `prototype/` | clickable prototype of the upload and results pages (plain HTML/CSS/JS) |
| `docs/spikes/` | time-boxed experiments and what they found |
