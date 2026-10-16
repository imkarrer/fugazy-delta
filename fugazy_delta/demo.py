"""Demo data for the clickable prototype in `prototype/`.

Builds one autocross and one track-day session on the virtual car, writes
the files each device setup would produce (GoPro MP4, RaceBox VBO, or
both), loads them with `Session.from_files`, and writes what the results
page needs as JSON: runs or laps with sector times, the optimal time, the
features the setup supports and why, and per-lap traces for a course map
and a delta chart.

What is real and what is not:

- Setup, feature support and reasons come from `Session.features()` on the
  real files.
- Map traces and delta curves come from the position source the session
  chose (RaceBox when present, else GoPro GPS), so the GoPro-only map
  carries the camera's GPS error.
- Lap, run and sector times are the simulator's ground truth: there is no
  lap detection yet. The JSON and the page both say "demo data".

Run inside the Flox environment (it needs ffmpeg):

    python -m fugazy_delta.demo prototype/data
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import numpy as np

from .fixtures import gopro_mp4, racebox_vbo
from .geo import to_local
from .model import Telemetry
from .session import Session, Setup
from .sim import Truth, autocross_session, example_autocross, example_track, track_session

GRID_POINTS = 241  # samples per lap on the shared distance grid (map and delta)

# Pace per (run, sector), a multiplier on grip. Chosen so the best run is
# not best in every sector, which makes the optimal time beat it.
AUTOCROSS_PACE = [
    [0.90, 0.92, 0.91],
    [0.95, 0.94, 0.95],
    [1.00, 0.95, 0.97],
    [0.96, 1.00, 0.96],
    [0.98, 0.98, 0.99],
]
# Lap 1 is the out-lap from the pit lane; it is shown but never counted.
TRACK_PACE = [
    [0.85, 0.90, 0.90],
    [0.95, 0.95, 0.95],
    [1.00, 0.96, 0.97],
    [0.97, 0.99, 0.98],
    [0.97, 0.98, 1.00],
    [0.96, 0.97, 0.96],
]

EVENTS = {
    "autocross": {
        "name": "CVSCC Points Event 1",
        "venue": "Chippewa Valley (demo course)",
        "unit": "run",
    },
    "track": {
        "name": "Track day, intermediate group",
        "venue": "Demo road course",
        "unit": "lap",
    },
}

SETUPS = {
    "gopro": Setup.GOPRO_ONLY,
    "gopro-racebox": Setup.GOPRO_RACEBOX,
    "racebox": Setup.RACEBOX_ONLY,
}


def demo_truth(event: str) -> Truth:
    if event == "autocross":
        return autocross_session(example_autocross(), AUTOCROSS_PACE, idle=8.0)
    if event == "track":
        return track_session(example_track(), TRACK_PACE)
    raise ValueError(f"unknown event {event!r}")


def write_files(truth: Truth, setup: Setup, directory: Path) -> list[Path]:
    """The files a driver with this setup would drop in."""
    paths = []
    if setup in (Setup.GOPRO_ONLY, Setup.GOPRO_RACEBOX):
        path = directory / "GX010042.MP4"
        gopro_mp4(truth, path, record_start=1.0, record_seconds=float(truth.t[-1]) - 2.0)
        paths.append(path)
    if setup in (Setup.GOPRO_RACEBOX, Setup.RACEBOX_ONLY):
        path = directory / "RaceBox_Session.vbo"
        racebox_vbo(truth, path)
        paths.append(path)
    return paths


def _lap_on_grid(position: Telemetry, start: float, end: float, length: float, origin) -> dict:
    """One lap's elapsed time and position on a common distance grid.

    Distance is integrated from speed and scaled to the course length, so
    every lap shares one x axis and the delta at the finish equals the
    difference in lap time.
    """
    lap = position.slice_time(start, end)
    t = np.concatenate([[start], lap.time, [end]])
    speed = np.interp(t, position.time, position.speed)
    lat = np.interp(t, position.time, position.lat)
    lon = np.interp(t, position.time, position.lon)
    d = np.concatenate([[0.0], np.cumsum(np.diff(t) * 0.5 * (speed[:-1] + speed[1:]))])
    d *= length / d[-1]
    grid = np.linspace(0.0, length, GRID_POINTS)
    x, y = to_local(lat, lon, *origin)
    return {
        "elapsed": np.interp(grid, d, t - start),
        "x": np.interp(grid, d, x),
        "y": np.interp(grid, d, y),
    }


def _r(values, digits: int) -> list[float]:
    return [round(float(v), digits) for v in values]


def build(event: str, setup_key: str, truth: Truth | None = None) -> dict:
    """The results-page JSON for one event and device setup."""
    setup = SETUPS[setup_key]
    truth = truth if truth is not None else demo_truth(event)
    course = truth.course
    with tempfile.TemporaryDirectory() as tmp:
        files = write_files(truth, setup, Path(tmp))
        session = Session.from_files(files)
    if session.setup is not setup:
        raise AssertionError(f"files for {setup.value} were read as {session.setup.value}")

    out_lap = 0 if event == "track" else None
    counted = [i for i in range(len(truth.laps)) if i != out_lap]
    times = truth.lap_times
    best = min(counted, key=lambda i: times[i])
    sectors = np.array([lap.sector_times for lap in truth.laps])
    best_sector_laps = [counted[int(np.argmin(sectors[counted, s]))] for s in range(sectors.shape[1])]
    optimal = float(sum(sectors[lap, s] for s, lap in enumerate(best_sector_laps)))

    position = session.position
    grids = [
        _lap_on_grid(position, truth.utc0 + lap.start, truth.utc0 + lap.end, course.length, course.origin)
        for lap in truth.laps
    ]
    distance = np.linspace(0.0, course.length, GRID_POINTS)
    sector_marks = [
        {"distance": round(float(d), 1),
         "x": round(float(np.interp(d, course.s, course.east)), 1),
         "y": round(float(np.interp(d, course.s, course.north)), 1)}
        for d in course.sector_distances
    ]

    laps = []
    for i, lap in enumerate(truth.laps):
        laps.append({
            "number": i + 1,
            "time": round(float(times[i]), 3),
            "sectors": _r(lap.sector_times, 3),
            "counted": i in counted,
            "out_lap": i == out_lap,
            "best": i == best,
            "trace": {"x": _r(grids[i]["x"], 1), "y": _r(grids[i]["y"], 1)},
            "delta_to_best": _r(grids[i]["elapsed"] - grids[best]["elapsed"], 3),
        })

    return {
        "demo": True,
        "event": {**EVENTS[event], "kind": event, "course_length_m": round(course.length, 1),
                  "closed": course.closed},
        "setup": {"key": setup_key, "label": session.setup.value},
        "files": [{"name": p.name, "kind": "GoPro video" if p.suffix.upper() == ".MP4" else "RaceBox VBO"}
                  for p in files],
        "position_source": position.source,
        "warnings": session.warnings,
        "features": [
            {"key": f.name, "label": f.value, "support": a.support.value, "reason": a.reason}
            for f, a in session.features().items()
        ],
        "best": best + 1,
        "optimal": {"time": round(optimal, 3), "sector_laps": [i + 1 for i in best_sector_laps]},
        "course": {
            "start": {"x": round(float(course.east[0]), 1), "y": round(float(course.north[0]), 1)},
            "finish": {"x": round(float(course.east[-1]), 1), "y": round(float(course.north[-1]), 1)},
            "sector_marks": sector_marks,
        },
        "distance": _r(distance, 1),
        "laps": laps,
    }


def main(argv: list[str]) -> None:
    out = Path(argv[0]) if argv else Path("prototype/data")
    out.mkdir(parents=True, exist_ok=True)
    for event in EVENTS:
        truth = demo_truth(event)
        for setup_key in SETUPS:
            data = build(event, setup_key, truth)
            path = out / f"{event}-{setup_key}.json"
            path.write_text(json.dumps(data, separators=(",", ":")) + "\n")
            best = data["laps"][data["best"] - 1]
            print(f"{path}: {data['setup']['label']}, best {data['event']['unit']} {best['number']} "
                  f"{best['time']:.3f} s, optimal {data['optimal']['time']:.3f} s")


if __name__ == "__main__":
    main(sys.argv[1:])
