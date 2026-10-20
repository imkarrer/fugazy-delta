"""AiM CSV export from Race Studio 3 ("AiM CSV File").

Layout of a real export (uOpenFormula's PittRace files, docs/formats.md
section 4.3)::

    "Format","AiM CSV File"
    "Session","PittRace"
    ...
    "Date","Friday, August 1, 2025"
    "Time","11:24 AM"
    "Sample Rate","20"
    "Duration","18"
    "Beacon Markers","18"
    "Segment Times","0:18.000"
    (blank line)
    "Time","GPS Speed","GPS Nsat",...,"GPS Heading",...,"GPS Latitude","GPS Longitude"
    "s","km/h"," ",...,"deg",...,"deg","deg"
    (blank line)
    "0.000","0.2571","11",...

- `Time` is seconds from the session start in `Date` and `Time`, which
  are the logger's *local* time, with no zone, written in the PC's locale
  and only to the minute.
  The zone is looked up from the first position (`localtime`) and the
  recording is marked `tz_inferred`, with a warning naming the zone.
- Race Studio resamples every channel to `Sample Rate`, holding or
  interpolating GPS between fixes, so rows that repeat the previous
  position are dropped; the fixes themselves cannot be recovered.
- `GPS Heading` runs from -180 to 180 and is wrapped into [0, 360).
- `Beacon Markers` are the lap marker times, in seconds from the start.
- The GPS accelerations' sign conventions are unverified, so they are
  dropped; timing does not use them.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from pathlib import Path

import numpy as np

from .importers import (
    PRECISE,
    SPEED_UNITS,
    Head,
    Kind,
    Recording,
    keep_fixes,
    position_changed,
    read_text,
    register,
    speed_unit,
)
from .localtime import local_to_utc
from .model import Telemetry

_FORMAT = re.compile(r'^\s*"?format"?\s*[,;\t]\s*"?aim csv file"?', re.IGNORECASE)
_DATES = ("%A, %B %d, %Y", "%B %d, %Y", "%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d", "%d.%m.%Y", "%A %d %B %Y", "%d %B %Y")
_TIMES = ("%I:%M %p", "%I:%M:%S %p", "%H:%M:%S", "%H:%M")


def _sniff(head: Head) -> bool:
    return bool(_FORMAT.match(head.text))


def _parse(value: str, formats) -> datetime | None:
    for fmt in formats:
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


def parse_aim_csv(text: str, name: str = "AiM CSV") -> Recording:
    delim = ";" if text.split("\n", 1)[0].count(";") > text.split("\n", 1)[0].count(",") else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    meta: dict[str, list[str]] = {}
    i = 0
    while i < len(rows) and rows[i] and any(c.strip() for c in rows[i]):
        meta[rows[i][0].strip().lower()] = [c.strip() for c in rows[i][1:]]
        i += 1
    while i < len(rows) and not any(c.strip() for c in rows[i]):
        i += 1
    names = [c.strip() for c in rows[i]]
    units = [c.strip() for c in rows[i + 1]]
    data = [r for r in rows[i + 2:] if len(r) == len(names) and any(c.strip() for c in r)]
    if not data:
        raise ValueError(f"{name}: no data rows")

    def col(column):
        if column not in names:
            return None, ""
        k = names.index(column)
        return np.array([float(r[k].replace(",", ".")) if r[k].strip() else np.nan for r in data]), units[k]

    elapsed, _ = col("Time")
    lat, _ = col("GPS Latitude")
    lon, _ = col("GPS Longitude")
    if elapsed is None or lat is None or lon is None:
        raise ValueError(f"{name}: missing Time, GPS Latitude or GPS Longitude column")
    speed, speed_text = col("GPS Speed")
    heading, _ = col("GPS Heading")
    alt, _ = col("GPS Altitude")

    date = _parse((meta.get("date") or [""])[0], _DATES)
    clock = _parse((meta.get("time") or [""])[0], _TIMES)
    if date is None or clock is None:
        raise ValueError(f"{name}: cannot read the session date {meta.get('date')} and time {meta.get('time')}")
    fix = np.isfinite(lat) & np.isfinite(lon) & ((lat != 0) | (lon != 0))
    if not fix.any():
        raise ValueError(f"{name}: no GPS fixes (every latitude and longitude is zero)")
    first = int(np.flatnonzero(fix)[0])
    local = date.replace(hour=clock.hour, minute=clock.minute, second=clock.second)
    start, zone = local_to_utc(local, lat[first], lon[first])
    time = start + elapsed

    keep = fix & position_changed(lat, lon)
    keep &= keep_fixes(time, changed=keep)
    unit = speed_unit(speed_text) or "km/h"
    marks = []
    for value in meta.get("beacon markers", []):
        try:
            marks.append(start + float(value))
        except ValueError:
            pass
    warnings = [f"{name}: AiM CSV records only a local start time, to the minute, so its times were "
                f"converted to UTC using the time zone at the track ({zone}) and may be up to a minute "
                "off. Lap times are unaffected; to sync with a GoPro, upload the .xrk file instead."]
    telemetry = Telemetry(
        source="aim", time=time[keep], lat=lat[keep], lon=lon[keep],
        alt=None if alt is None else alt[keep],
        speed=None if speed is None else speed[keep] * SPEED_UNITS[unit],
        heading=None if heading is None else np.mod(heading[keep], 360.0),
    )
    return Recording(Kind.AIM_CSV, telemetry, "AiM", profile=PRECISE, tz_inferred=True, laps=marks,
                     warnings=warnings)


@register(Kind.AIM_CSV, _sniff)
def read_aim_csv(path: Path) -> Recording:
    return parse_aim_csv(read_text(path), Path(path).name)


def write_aim_csv(telemetry: Telemetry, path: str | Path, local_start: datetime, rate_hz: float = 20.0,
                  laps: list[float] = (), bom: bool = False) -> None:
    """Write `telemetry` as Race Studio 3 exports it: rows resampled to
    `rate_hz` from `local_start`, the logger's local time, which must be
    the local time of `telemetry.time[0]`, holding each fix until the
    next (as the real export's repeated rows do). `laps` are lap-start
    times in UTC, written as beacon markers relative to the start."""
    t = telemetry
    t0 = float(t.time[0])
    rows_t = t0 + np.arange(0.0, t.time[-1] - t0 + 1e-9, 1.0 / rate_hz)
    fix = np.searchsorted(t.time, rows_t + 1e-9, side="right") - 1
    out = io.StringIO()
    q = lambda *cells: ",".join(f'"{c}"' for c in cells) + "\n"  # noqa: E731
    out.write(q("Format", "AiM CSV File"))
    out.write(q("Session", "fugazy-delta"))
    out.write(q("Date", f"{local_start:%A}, {local_start:%B} {local_start.day}, {local_start.year}"))
    out.write(q("Time", f"{local_start:%I:%M %p}".lstrip("0")))
    out.write(q("Sample Rate", f"{rate_hz:g}"))
    out.write(q("Duration", f"{rows_t[-1] - t0:.0f}"))
    out.write(q("Beacon Markers", *[f"{x - t0:.3f}" for x in laps]))
    out.write("\n")
    out.write(q("Time", "GPS Speed", "GPS Nsat", "GPS Heading", "GPS Altitude", "GPS Latitude", "GPS Longitude"))
    out.write(q("s", "km/h", " ", "deg", "m", "deg", "deg"))
    out.write("\n")
    for k, ts in enumerate(rows_t):
        i = fix[k]
        hdg = (np.nan_to_num(t.heading[i]) + 180.0) % 360.0 - 180.0
        out.write(q(f"{ts - t0:.3f}", f"{np.nan_to_num(t.speed[i]) * 3.6:.4f}", "12", f"{hdg:.4f}",
                    f"{np.nan_to_num(t.alt[i]):.4f}", f"{t.lat[i]:.8f}", f"{t.lon[i]:.8f}"))
    Path(path).write_text(("﻿" if bom else "") + out.getvalue(), encoding="utf-8")
