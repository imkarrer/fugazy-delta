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

Or without entering a shell: `flox activate -- pytest`.

## Layout

| Path | What |
|---|---|
| `fugazy_delta/model.py` | `Telemetry`, the one data type every parser produces |
| `fugazy_delta/sim.py` | virtual car: courses, speed profile, ground truth with known lap/sector times |
| `fugazy_delta/devices.py` | simulated GNSS / RaceBox Mini sampling and noise |
| `fugazy_delta/vbo.py` | Racelogic VBO read/write (RaceBox export format) |
| `fugazy_delta/gpmf.py` | GoPro GPMF encode/decode, HERO9-like payloads |
| `fugazy_delta/mp4.py` | read a GoPro telemetry track; add one to an MP4 |
| `fugazy_delta/fixtures.py` | build synthetic GoPro MP4 and RaceBox VBO files from a truth |
| `docs/spikes/` | time-boxed experiments and what they found |
