"""RaceBox CSV export (racebox.pro, My Sessions, export CSV).

Layout of a real RaceBox Mini export (RacePlex's sample, docs/formats.md
section 4.4)::

    Record,Time,Latitude,Longitude,Altitude,Speed,GForceX,GForceY,GForceZ,Lap,GyroX,GyroY,GyroZ
    1,2026-06-21T20:43:33.160Z,33.6528145,-117.3042013,390.6,3.41,-0.008,-0.010,1.001,0,0.48,-0.92,-0.36

- Time is ISO 8601 UTC with milliseconds.
- Latitude and longitude are signed degrees, west negative (unlike
  RaceBox's own VBO, which is in minutes with west positive).
- Speed carries no unit: the user picks km/h, mph or m/s at export. The
  reader infers it by comparing the column with speed derived from
  positions, and says so in a warning. Presets that write the unit in the
  column name (`Speed (m/s)`) are read by name.
- GForceX is longitudinal, positive forward, and GForceY lateral,
  positive left, as in our model: on the real sample GForceX correlates
  0.98 with the rate of change of speed and GForceY 0.98 with the
  left-positive centripetal acceleration from speed and turn rate.
  GForceZ reads 1.0 at rest, so 1 g is subtracted.
- A metadata block (`Format,RaceBox`, `Date UTC`, ...) may precede the
  header, which is found as the line starting `Record,Time,`.
- `Lap` counts laps the RaceBox timed; the times it changes are kept as
  the device's lap markers.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .importers import (
    PRECISE,
    SPEED_UNITS,
    Head,
    Kind,
    Recording,
    infer_speed_unit,
    keep_fixes,
    read_text,
    register,
    speed_unit,
)
from .model import Telemetry

_HEADER = re.compile(r"^\ufeff?[ \t]*\"?record\"?\s*,\s*\"?time\"?\s*,", re.IGNORECASE | re.MULTILINE)
_FORMAT = re.compile(r"^[ \t]*\"?format\"?\s*,\s*\"?racebox", re.IGNORECASE | re.MULTILINE)


def _sniff(head: Head) -> bool:
    text = head.text[:16384]
    return bool(_HEADER.search(text) or _FORMAT.search(text))


def _parse_time(value: str) -> float:
    return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


def _column(names: list[str], *wanted: str) -> tuple[int, str] | None:
    """Index of the first column whose name, before any '(unit)', is one of
    `wanted`, with the unit text."""
    for i, name in enumerate(names):
        m = re.match(r"\s*([^(]+?)\s*(?:\(([^)]*)\))?\s*$", name)
        if m and m[1].lower() in wanted:
            return i, (m[2] or "")
    return None


def parse_racebox_csv(text: str, name: str = "RaceBox CSV") -> Recording:
    m = _HEADER.search(text)
    if m is None:
        raise ValueError(f"{name}: no 'Record,Time,' header line")
    rows = list(csv.reader(io.StringIO(text[m.start():].lstrip("﻿"))))
    header = [h.strip() for h in rows[0]]
    rows = [r for r in rows[1:] if len(r) >= len(header) and r[0].strip()]
    if not rows:
        raise ValueError(f"{name}: no data rows")

    def col(*wanted):
        found = _column(header, *wanted)
        if found is None:
            return None, ""
        i, unit = found
        return np.array([float(r[i]) if r[i].strip() else np.nan for r in rows]), unit

    t_idx = _column(header, "time")[0]
    time = np.array([_parse_time(r[t_idx]) for r in rows])
    lat, _ = col("latitude", "lat")
    lon, _ = col("longitude", "lon", "lng")
    if lat is None or lon is None:
        raise ValueError(f"{name}: missing Latitude or Longitude column (have {header})")
    alt, _ = col("altitude", "alt")
    heading, _ = col("heading")
    speed, unit_text = col("speed", "kph", "mph")
    gx, _ = col("gforcex")
    gy, _ = col("gforcey")
    gz, _ = col("gforcez")
    lap, _ = col("lap")

    keep = keep_fixes(time) & np.isfinite(lat) & np.isfinite(lon) & ((lat != 0) | (lon != 0))
    warnings = []
    unit = speed_unit(unit_text) if unit_text else None
    if speed is not None and unit is None:
        header_name = header[_column(header, "speed", "kph", "mph")[0]].lower()
        unit = {"kph": "km/h", "mph": "mph"}.get(header_name)
    if speed is not None and unit is None:
        unit, ratio = infer_speed_unit(time[keep], lat[keep], lon[keep], speed[keep])
        if unit is None:
            unit = "km/h"
            warnings.append(f"{name}: the speed column has no unit and the car barely moved, "
                            "so km/h, RaceBox's default, was assumed.")
        else:
            warnings.append(f"{name}: speed unit inferred as {unit} from the positions.")

    laps = []
    if lap is not None:
        changed = np.flatnonzero(np.diff(np.nan_to_num(lap)) > 0) + 1
        laps = [float(time[i]) for i in changed]

    pick = lambda a: None if a is None else a[keep]  # noqa: E731
    telemetry = Telemetry(
        source="racebox",
        time=time[keep],
        lat=lat[keep],
        lon=lon[keep],
        alt=pick(alt),
        speed=None if speed is None else speed[keep] * SPEED_UNITS[unit],
        heading=pick(heading),
        accel_long=pick(gx),
        accel_lat=pick(gy),
        accel_vert=None if gz is None else gz[keep] - 1.0,
    )
    return Recording(Kind.RACEBOX_CSV, telemetry, "RaceBox", profile=PRECISE, laps=laps, warnings=warnings)


@register(Kind.RACEBOX_CSV, _sniff)
def read_racebox_csv(path: Path) -> Recording:
    return parse_racebox_csv(read_text(path), Path(path).name)


def write_racebox_csv(telemetry: Telemetry, path: str | Path, unit: str = "km/h",
                      unit_in_name: bool = False, metadata: bool = False, laps: list[float] = ()) -> None:
    """Write `telemetry` the way racebox.pro exports CSV.

    `unit` is the speed unit chosen at export; `unit_in_name` writes it in
    the column name as the Telemetry Overlay preset does; `metadata` adds
    the block some exports put before the header. `laps` are lap-start
    times, written as the Lap counter.
    """
    t = telemetry
    out = io.StringIO()
    if metadata:
        start = datetime.fromtimestamp(t.time[0], timezone.utc)
        out.write(f"Format,RaceBox\nDate UTC,{start:%Y-%m-%d %H:%M:%S}\nTrack,fugazy-delta test course\n\n")
    speed_name = f"Speed ({unit})" if unit_in_name else "Speed"
    out.write(f"Record,Time,Latitude,Longitude,Altitude,{speed_name},GForceX,GForceY,GForceZ,Lap,GyroX,GyroY,GyroZ\n")
    lap_no = np.searchsorted(np.asarray(sorted(laps)), t.time, side="right")
    factor = 1 / SPEED_UNITS[unit]
    for i in range(len(t)):
        iso = datetime.fromtimestamp(round(t.time[i], 3), timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        out.write(
            f"{i + 1},{iso},{t.lat[i]:.7f},{t.lon[i]:.7f},{_f(t.alt[i], 1)},{_f(t.speed[i] * factor, 2)},"
            f"{_f(t.accel_long[i], 3)},{_f(t.accel_lat[i], 3)},{_f(t.accel_vert[i] + 1.0, 3)},{lap_no[i]},0.00,0.00,0.00\n"
        )
    Path(path).write_text(out.getvalue(), encoding="utf-8")


def _f(value: float, digits: int) -> str:
    return f"{0.0 if np.isnan(value) else value:.{digits}f}"
