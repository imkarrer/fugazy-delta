"""TrackAddict CSV export ("Send Data File", the RaceRender data format).

Layout of a real export (OpenRaceRender's files, docs/formats.md section
4.5)::

    # RaceRender Data: TrackAddict 4.3.7 on Android 7.1.2 [...]
    # GPS: Android; Mode: Android
    "Time","UTC Time","Lap",...,"GPS_Update","GPS_Delay","Latitude","Longitude",
        "Altitude (m)","Altitude (ft)","Speed (Km/h)","Heading","Accuracy (m)",
        "Accel X","Accel Y","Accel Z",...
    ...
    # Lap 12: 00:01:07.354
    # Session End

- `UTC Time` is Unix seconds.
- A row is written whenever any sensor updates (50 Hz from the phone's
  accelerometer), and position columns repeat the last fix between
  fixes. `GPS_Update` is 1 on the rows with a new fix, and only those are
  kept, so timing interpolates between real fixes.
- The speed column's name carries the user's unit: `Speed (Km/h)` or
  `Speed (MPH)`.
- `Accel X` is positive when braking (the developer says so on the HP
  Tuners forum, and on the real Saint-Eustache file it correlates -0.80
  with the rate of change of OBD speed); `Accel Y` is positive left
  (+0.70 with the left-positive turning acceleration). Both are converted
  to our signs. How well a phone's axes match the car's depends on how
  it is mounted.
- The `# GPS:` comment names the receiver. `Android` or `iOS` is the
  phone's own, about 1 Hz; anything else is an external receiver.
- `Lap` counts the laps TrackAddict timed; where it steps up are the
  device's lap markers. The `# Lap N: hh:mm:ss.sss` lines at the end are
  TrackAddict's own lap times, returned by `lap_times`.

TrackAddict's receiver is a phone or an unknown external one, so its
times quote the GoPro's measured error (`importers.COARSE`).
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

import numpy as np

from .importers import (
    COARSE,
    SPEED_UNITS,
    Head,
    Kind,
    Recording,
    accel_to_model,
    keep_fixes,
    low_rate_warning,
    read_text,
    register,
    speed_unit,
)
from .model import Telemetry

_LAP_LINE = re.compile(r"^#\s*Lap\s+(\d+):\s*(\d+):(\d+):(\d+(?:\.\d*)?)", re.MULTILINE)
_PHONE = {"android", "ios", "iphone", "internal"}


def _sniff(head: Head) -> bool:
    first = head.text.lstrip()[:300].lower()
    return first.startswith("# racerender data") and "trackaddict" in first


def lap_times(text: str) -> dict[int, float]:
    """TrackAddict's own lap times from the `# Lap N:` lines, in seconds."""
    return {int(m[1]): int(m[2]) * 3600 + int(m[3]) * 60 + float(m[4]) for m in _LAP_LINE.finditer(text)}


def parse_trackaddict(text: str, name: str = "TrackAddict CSV") -> Recording:
    lines = text.splitlines()
    receiver = "unknown"
    for ln in lines:
        if ln.lower().startswith("# gps:"):
            receiver = ln.split(":", 1)[1].split(";")[0].strip() or "unknown"
            break
    body = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
    rows = list(csv.reader(io.StringIO("\n".join(body))))
    header = [h.strip() for h in rows[0]]
    rows = [r for r in rows[1:] if len(r) == len(header)]
    index = {h: i for i, h in enumerate(header)}

    def col(name_: str):
        i = index.get(name_)
        if i is None:
            return None
        return np.array([float(r[i]) if r[i].strip() else np.nan for r in rows])

    def find(prefix: str):
        for h in header:
            if h.lower().startswith(prefix.lower()):
                return h
        return None

    time = col("UTC Time")
    lat, lon = col("Latitude"), col("Longitude")
    if time is None or lat is None or lon is None:
        raise ValueError(f"{name}: missing UTC Time, Latitude or Longitude column")
    update = col("GPS_Update")
    changed = np.ones(len(rows), dtype=bool) if update is None else update == 1
    keep = keep_fixes(time, changed=changed & np.isfinite(lat) & ((lat != 0) | (lon != 0)))

    speed_col = find("Speed (")
    speed = None
    if speed_col:
        unit = speed_unit(speed_col[speed_col.index("(") + 1:speed_col.rindex(")")]) or "km/h"
        speed = col(speed_col) * SPEED_UNITS[unit]
    alt_col = "Altitude (m)" if "Altitude (m)" in index else None
    alt = col(alt_col) if alt_col else (col("Altitude (ft)") * 0.3048 if "Altitude (ft)" in index else None)
    along, alat = accel_to_model(col("Accel X"), col("Accel Y"), long_forward=False, lat_left=True)

    lap = col("Lap")
    laps = []
    if lap is not None:
        laps = [float(time[i]) for i in np.flatnonzero(np.diff(np.nan_to_num(lap)) > 0) + 1]

    pick = lambda a: None if a is None else a[keep]  # noqa: E731
    telemetry = Telemetry(
        source="trackaddict", time=time[keep], lat=lat[keep], lon=lon[keep], alt=pick(alt),
        speed=pick(speed), heading=pick(col("Heading")), accel_long=pick(along), accel_lat=pick(alat),
    )
    phone = receiver.lower() in _PHONE
    device = "TrackAddict (phone GPS)" if phone else f"TrackAddict ({receiver})"
    rate = telemetry.sample_rate if len(telemetry) > 1 else 0.0
    warnings = [w for w in [low_rate_warning("TrackAddict", rate)] if w]
    return Recording(Kind.TRACKADDICT_CSV, telemetry, device, profile=COARSE, laps=laps, warnings=warnings)


@register(Kind.TRACKADDICT_CSV, _sniff)
def read_trackaddict(path: Path) -> Recording:
    return parse_trackaddict(read_text(path), Path(path).name)


def write_trackaddict(fixes: Telemetry, path: str | Path, row_hz: float = 50.0, unit: str = "Km/h",
                      receiver: str = "Android", laps: list[float] = ()) -> None:
    """Write `fixes` the way TrackAddict exports a session.

    Rows run at `row_hz` from the first fix; each repeats the latest fix,
    and the row at a fix's time has `GPS_Update` 1. The accelerations are
    written in TrackAddict's signs (X positive when braking). `laps` are
    lap-start times: the Lap column counts them and `# Lap N:` lines give
    the time between them.
    """
    t = fixes
    rows_t = np.arange(t.time[0], t.time[-1] + 1e-9, 1.0 / row_hz)
    rows_t = np.unique(np.round(np.concatenate([rows_t, t.time]), 6))
    fix = np.searchsorted(t.time, rows_t + 1e-9, side="right") - 1
    new = np.r_[True, np.diff(fix) > 0]
    starts = np.asarray(sorted(laps))
    lap_no = np.searchsorted(starts, rows_t, side="right")
    factor = 1 / SPEED_UNITS[speed_unit(unit)]
    out = io.StringIO()
    out.write("# RaceRender Data: TrackAddict 4.3.7 on Android 7.1.2 [fugazy-delta] (Mode: 0)\n")
    out.write(f"# GPS: {receiver}; Mode: {receiver}\n")
    out.write('"Time","UTC Time","Lap","GPS_Update","GPS_Delay","Latitude","Longitude","Altitude (m)",'
              f'"Altitude (ft)","Speed ({unit})","Heading","Accuracy (m)","Accel X","Accel Y","Accel Z"\n')
    for k, ts in enumerate(rows_t):
        i = fix[k]
        out.write(
            f"{ts - rows_t[0]:.3f},{ts:.3f},{lap_no[k]},{int(new[k])},0.000,{t.lat[i]:.7f},{t.lon[i]:.7f},"
            f"{np.nan_to_num(t.alt[i]):.1f},{np.nan_to_num(t.alt[i]) / 0.3048:.0f},"
            f"{np.nan_to_num(t.speed[i]) * factor:.2f},{np.nan_to_num(t.heading[i]):.1f},4.6,"
            f"{-np.nan_to_num(t.accel_long[i]):.2f},{np.nan_to_num(t.accel_lat[i]):.2f},0.00\n"
        )
    for n, (a, b) in enumerate(zip(starts, starts[1:]), 1):
        hh, rem = divmod(b - a, 3600)
        mm, ss = divmod(rem, 60)
        out.write(f"# Lap {n}: {int(hh):02d}:{int(mm):02d}:{ss:06.3f}\n")
    out.write("# Session End\n")
    Path(path).write_text(out.getvalue(), encoding="utf-8")
