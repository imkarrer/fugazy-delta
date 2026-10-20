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
- Every time carries its error: the `accuracy` block is the measured error
  of each kind of time for the setup's position source, from
  `accuracy.py`'s committed table.
- A sector the lap finder could not time is written as null, not NaN,
  which is not valid JSON.
- Lap, run and sector times are found by `segment.find_laps` on the
  position source the session chose (RaceBox when present, else GoPro GPS),
  against the course's timing lines, so GoPro-only times carry the camera's
  GPS error. So do the map traces and delta curves.
- The driving is simulated: each lap or run has its own pace and its own
  line, so the map shows two lines apart. The JSON and the page both say
  "demo data".

Run inside the Flox environment (it needs ffmpeg):

    python -m fugazy_delta.demo prototype/data
"""

from __future__ import annotations

import json
import math
import sys
import tempfile
from pathlib import Path

import numpy as np

from . import accuracy
from .fixtures import gopro_mp4, racebox_vbo
from .geo import to_local
from .model import Telemetry
from .segment import LapKind, best_lap, find_laps, optimal_lap
from .session import METHOD, Session, Setup
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
# Lap 1 is the out-lap from a standstill; it is shown but never counted.
TRACK_PACE = [
    [0.85, 0.90, 0.90],
    [0.95, 0.95, 0.95],
    [1.00, 0.96, 0.97],
    [0.97, 0.99, 0.98],
    [0.97, 0.98, 1.00],
    [0.96, 0.97, 0.96],
]

# How far each run or lap strays from the centre line mid-way round, in
# metres (+ left), so comparing two of them on the map shows two lines.
AUTOCROSS_LINE = [0.0, 2.5, -2.0, 1.5, -2.5]
TRACK_LINE = [0.0, 4.0, -5.0, 3.0, -4.0, 5.0]
# After the last lap the car drives half a lap to pit entry. That in-lap
# never crosses the line, so it is not shown.
TRACK_IN_LAP = 0.5

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
        return autocross_session(example_autocross(), AUTOCROSS_PACE, idle=8.0, line_offset=AUTOCROSS_LINE)
    if event == "track":
        return track_session(example_track(), TRACK_PACE, line_offset=TRACK_LINE, in_lap=TRACK_IN_LAP)
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


def _num(value, digits: int) -> float | None:
    """Rounded, or None (JSON null) for NaN: a time that was not measured."""
    value = float(value)
    return round(value, digits) if math.isfinite(value) else None


def _r(values, digits: int) -> list[float | None]:
    return [_num(v, digits) for v in values]


# The rows of the accuracy panel, in the order the page shows them.
ACCURACY_ROWS = ("lap", "sector_1", "sector_2", "sector_3", "best", "optimal")


def accuracy_block(event: str, setup: Setup) -> dict:
    """The measured error of each kind of time this setup reports."""
    method = METHOD[setup]
    rows = []
    for q in ACCURACY_ROWS:
        err = accuracy.error(event, method, q)
        rows.append({
            "key": q,
            "label": accuracy.label(q, event),
            "n": err.n,
            "bias": round(err.bias, 3),
            "bias_ci": _r(err.bias_ci, 3),
            "biased": err.biased,
            "rms": round(err.rms, 3),
            "p95": round(err.p95, 3),
            "worst": round(err.max, 3),
            "summary": err.summary(),
        })
    return {
        "method": method,
        "label": accuracy.METHOD_LABELS[method],
        "detail": accuracy.METHOD_DETAILS[method],
        "approximate": setup is Setup.GOPRO_ONLY,
        "seeds": accuracy.table()["seeds"],
        "flagged_rate": accuracy.flagged_rate(event, method),
        "rows": rows,
    }


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

    position = session.position
    laps_found = [lap for lap in find_laps(position, course.timing_lines()) if lap.kind is not LapKind.IN]
    counted = [i for i, lap in enumerate(laps_found) if lap.counted]
    best = best_lap(laps_found)
    optimal = optimal_lap(laps_found)

    grids = [_lap_on_grid(position, lap.start, lap.end, course.length, course.origin) for lap in laps_found]
    distance = np.linspace(0.0, course.length, GRID_POINTS)
    sector_marks = [
        {"distance": round(float(d), 1),
         "x": round(float(np.interp(d, course.s, course.east)), 1),
         "y": round(float(np.interp(d, course.s, course.north)), 1)}
        for d in course.sector_distances
    ]

    laps = []
    for i, lap in enumerate(laps_found):
        laps.append({
            "number": i + 1,
            "time": _num(lap.time, 3),
            "sectors": _r(lap.sector_times, 3),
            "counted": i in counted,
            "out_lap": lap.kind is LapKind.OUT,
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
            for f, a in session.features(event).items()
        ],
        "accuracy": accuracy_block(event, setup),
        "best": best + 1,
        "optimal": {"time": _num(optimal.time, 3),
                    "sector_laps": [None if i is None else i + 1 for i in optimal.sector_laps]},
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
            path.write_text(dumps(data) + "\n")
            best = data["laps"][data["best"] - 1]
            print(f"{path}: {data['setup']['label']}, best {data['event']['unit']} {best['number']} "
                  f"{best['time']:.3f} s, optimal {data['optimal']['time']} s")


def dumps(data: dict) -> str:
    """Compact JSON; refuses NaN, which browsers' JSON.parse rejects."""
    return json.dumps(data, separators=(",", ":"), allow_nan=False)


if __name__ == "__main__":
    main(sys.argv[1:])
