"""RaceChrono Pro CSV v3 export.

Layout of a real export (UltraLog's excerpt, docs/formats.md section
4.2)::

    This file is created using RaceChrono Pro v10.1.3 ( http://racechrono.com/ ).
    Format,3
    Session title,"..."
    Created,04/08/2026,13:14
    (blank line)
    timestamp,fragment_id,lap_number,elapsed_time,...,latitude,longitude,satellites,speed,...,speed,...
    unix time,,,s,...,deg,deg,sats,m/s,...,m/s,...
    ,,,,...,100: gps,100: gps,100: gps,100: gps,...,300: canbus,...

Three header rows follow the preamble: names, units, sources. Names
repeat across sources (`speed` is both GPS and CAN speed), so columns are
keyed by (name, source) and position comes only from a `gps` source.
`timestamp` is Unix seconds, UTC.

RaceChrono writes a row whenever any channel updates and fills every
channel by linear interpolation, so most rows are not GPS fixes. In the
real file the GPS `device_update_rate` is 1 Hz and the position's slope
changes only at whole UTC seconds. The reader keeps the row nearest each
fix epoch (a multiple of 1 / `device_update_rate`); timing then
interpolates between the fixes the receiver measured, not between rows
RaceChrono made up. Without that column it keeps the rows where the
position changed.

The receiver can be the phone or an external one (a RaceBox Mini, an
XGPS160) and the file does not say which, so RaceChrono quotes the
GoPro's measured error (`importers.COARSE`), and a warning says when the
rate is phone-like. The accelerations' sign conventions are unverified,
so they are dropped; timing does not use them.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import numpy as np

from .importers import (
    COARSE,
    SPEED_UNITS,
    Head,
    Kind,
    Recording,
    UnsupportedFile,
    epoch_rows,
    keep_fixes,
    low_rate_warning,
    position_changed,
    read_text,
    register,
    speed_unit,
)
from .model import Telemetry

_SIGNATURE = "this file is created using racechrono"


def _sniff(head: Head) -> bool:
    return head.text.lstrip()[:200].lower().startswith(_SIGNATURE)


def parse_racechrono(text: str, name: str = "RaceChrono CSV") -> Recording:
    lines = text.lstrip("﻿").splitlines()
    fmt = next((ln.split(",", 1)[1].strip() for ln in lines[:10] if ln.lower().startswith("format,")), "")
    if fmt != "3":
        raise UnsupportedFile(
            f"{name}: RaceChrono CSV format {fmt or 'unknown'}; export it again as "
            "CSV v3 (RaceChrono Pro: share the session, choose CSV v3)."
        )
    blank = next(i for i, ln in enumerate(lines) if not ln.strip())
    rows = list(csv.reader(io.StringIO("\n".join(lines[blank + 1:]))))
    rows = [r for r in rows if any(c.strip() for c in r)]
    names, units, sources = rows[0], rows[1], rows[2]
    data = rows[3:]
    width = len(names)

    def find(column: str, gps: bool):
        for i, n in enumerate(names):
            src = sources[i].lower() if i < len(sources) else ""
            if n.strip().lower() == column and (("gps" in src) if gps else True):
                return i
        return None

    def values(i):
        out = np.full(len(data), np.nan)
        if i is None:
            return out
        for k, r in enumerate(data):
            if i < len(r) and r[i].strip():
                out[k] = float(r[i])
        return out

    i_time = find("timestamp", gps=False)
    i_lat, i_lon = find("latitude", gps=True), find("longitude", gps=True)
    if i_time is None or i_lat is None or i_lon is None:
        raise ValueError(f"{name}: no timestamp or GPS latitude/longitude columns")
    data = [r for r in data if len(r) <= width + 1]
    time, lat, lon = values(i_time), values(i_lat), values(i_lon)
    i_speed = find("speed", gps=True)
    speed = values(i_speed)
    if i_speed is not None:
        unit = speed_unit(units[i_speed]) or "m/s"
        speed = speed * SPEED_UNITS[unit]
    heading = values(find("bearing", gps=True))
    alt = values(find("altitude", gps=True))
    rate = values(find("device_update_rate", gps=True))
    laps_col = values(find("lap_number", gps=False))

    valid = np.isfinite(time) & np.isfinite(lat) & np.isfinite(lon) & ((lat != 0) | (lon != 0))
    gps_hz = float(np.nanmedian(rate)) if np.isfinite(rate).any() else float("nan")
    keep = None
    if np.isfinite(gps_hz) and gps_hz > 0:
        keep = valid & epoch_rows(time, gps_hz)
        epochs = len(np.unique(np.round(time[valid] * gps_hz)))
        if keep.sum() < 0.5 * epochs:
            keep = None  # fixes are not on UTC epochs; fall back
    if keep is None:
        keep = valid & position_changed(lat, lon)
    keep &= keep_fixes(time)

    t = time[keep]
    if not np.isfinite(gps_hz) or gps_hz <= 0:
        gps_hz = 1.0 / float(np.median(np.diff(t))) if len(t) > 1 else 0.0
    warnings = [w for w in [low_rate_warning("RaceChrono", gps_hz)] if w]

    # lap_number is blank before the first lap starts
    laps = []
    if np.isfinite(laps_col).any():
        lap = np.nan_to_num(laps_col[valid])
        laps = [float(x) for x in time[valid][1:][np.diff(lap) > 0]]

    telemetry = Telemetry(
        source="racechrono", time=t, lat=lat[keep], lon=lon[keep],
        alt=alt[keep], speed=speed[keep], heading=heading[keep],
    )
    return Recording(Kind.RACECHRONO_CSV, telemetry, "RaceChrono", profile=COARSE, laps=laps, warnings=warnings)


@register(Kind.RACECHRONO_CSV, _sniff)
def read_racechrono(path: Path) -> Recording:
    return parse_racechrono(read_text(path), Path(path).name)


def write_racechrono(telemetry: Telemetry, path: str | Path, gps_hz: float, row_hz: float = 50.0,
                     laps: list[float] = (), bom: bool = True, fmt: int = 3) -> None:
    """Write `telemetry` as RaceChrono Pro's CSV v3 export.

    `telemetry` holds the receiver's fixes at `gps_hz`, on UTC epochs. Rows
    are written at `row_hz` plus one at every fix, every channel linearly
    interpolated between fixes, the way RaceChrono resamples. A CAN-bus
    `speed` column in km/h repeats the name of the GPS one, so a reader
    that keys columns by name alone picks the wrong one. `laps` are
    lap-start times, written as `lap_number`.
    """
    t = telemetry
    rows_t = np.arange(np.ceil(t.time[0] * row_hz), np.floor(t.time[-1] * row_hz) + 1) / row_hz
    rows_t = np.unique(np.round(np.concatenate([rows_t, t.time]), 6))
    lap_no = np.searchsorted(np.asarray(sorted(laps)), rows_t, side="right")
    interp = lambda a: np.interp(rows_t, t.time, np.nan_to_num(a))  # noqa: E731
    lat, lon, spd, alt = interp(t.lat), interp(t.lon), interp(t.speed), interp(t.alt)
    hdg = np.mod(np.degrees(np.unwrap(np.radians(np.nan_to_num(t.heading)))), 360.0)
    hdg = np.mod(np.interp(rows_t, t.time, np.degrees(np.unwrap(np.radians(hdg)))), 360.0)
    out = io.StringIO()
    out.write("This file is created using RaceChrono Pro v10.1.3 ( http://racechrono.com/ ).\n")
    out.write(f"Format,{fmt}\nSession title,\"fugazy-delta test\"\nSession type,Lap timing\n"
              "Track name,\"fugazy-delta test course\"\nDriver name,\nCreated,13/06/2026,09:00\nNote,\n\n")
    out.write("timestamp,fragment_id,lap_number,elapsed_time,distance_traveled,accuracy,altitude,bearing,"
              "device_update_rate,fix_type,latitude,longitude,satellites,speed,speed,rpm\n")
    out.write("unix time,,,s,m,m,m,deg,Hz,,deg,deg,sats,m/s,km/h,rpm\n")
    out.write(",,,,,100: gps,100: gps,100: gps,100: gps,100: gps,100: gps,100: gps,100: gps,100: gps,"
              "300: canbus,300: canbus\n")
    for i, ts in enumerate(rows_t):
        lap = "" if lap_no[i] == 0 else str(lap_no[i])
        out.write(
            f"{ts:.3f},0,{lap},{ts - rows_t[0]:.3f},,1.5,{alt[i]:.2f},{hdg[i]:.2f},{gps_hz:g},3,"
            f"{lat[i]:.9f},{lon[i]:.9f},12,{spd[i]:.5f},{spd[i] * 3.6 + 7:.3f},\n"
        )
    data = out.getvalue()
    Path(path).write_text(("﻿" if bom else "") + data, encoding="utf-8")
