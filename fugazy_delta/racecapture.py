"""AutoSport Labs RaceCapture log (`rc_N.log` from the SD card).

The official reference (wiki.autosportlabs.com,
RaceCapturePro2_logfile_reference) and real files (EOSCogniton/Test-Data,
docs/formats.md section 4.8) agree on:

- Each header cell is `"Name"|"Units"|min|max|rateHz`. Names can repeat
  (two `Battery` channels in one real file).
- `Interval` is logger uptime in ms and `Utc` is ms since the Unix epoch,
  0 before the GPS has a fix. `Interval` went backwards in one real file,
  so rows are ordered by `Utc`.
- A row is written at the fastest channel's rate; an empty cell means that
  channel has no new sample on that row. Rows with a latitude are the GPS
  fixes, and only they are kept.
- Speed and altitude units are the user's choice (MPH and ft in one real
  file, kph in another), so the unit cell is read.

Files edited in a spreadsheet on a European-locale PC come back
tab-separated, with decimal commas and the header cells quoted whole
(`"Interval|""ms""|0|0|1"`); those are read too.

RaceCapture's accelerometer axes depend on how the unit is mounted and
are unverified, so accelerations are dropped; timing does not use them.
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

import numpy as np

from .importers import (
    PRECISE,
    SPEED_UNITS,
    Head,
    Kind,
    Recording,
    keep_fixes,
    low_rate_warning,
    read_text,
    register,
    speed_unit,
)
from .model import Telemetry

_FIRST = re.compile(r'^\s*"?"?Interval"?"?\|', re.IGNORECASE)


def _sniff(head: Head) -> bool:
    return bool(_FIRST.match(head.text))


def _header_cell(cell: str) -> tuple[str, str, float]:
    parts = [p.strip().strip('"') for p in cell.strip().strip('"').replace('""', '"').split("|")]
    name = parts[0]
    unit = parts[1] if len(parts) > 1 else ""
    try:
        rate = float(parts[4].replace(",", ".")) if len(parts) > 4 else float("nan")
    except ValueError:
        rate = float("nan")
    return name, unit, rate


def parse_racecapture(text: str, name: str = "RaceCapture log") -> Recording:
    first = text.split("\n", 1)[0]
    delim = "\t" if "\t" in first else (";" if first.count(";") > first.count(",") else ",")
    decimal_comma = delim != ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    header = [_header_cell(c) for c in rows[0]]
    names = [h[0].lower() for h in header]
    data = [r for r in rows[1:] if r and any(c.strip() for c in r)]

    def idx(column):
        return names.index(column) if column in names else None

    def col(column):
        k = idx(column)
        out = np.full(len(data), np.nan)
        if k is None:
            return out
        for j, r in enumerate(data):
            if k < len(r) and r[k].strip():
                out[j] = float(r[k].replace(",", ".") if decimal_comma else r[k])
        return out

    utc, lat, lon = col("utc"), col("latitude"), col("longitude")
    if idx("utc") is None or idx("latitude") is None:
        raise ValueError(f"{name}: no Utc or Latitude channel")
    fix = np.isfinite(utc) & (utc > 0) & np.isfinite(lat) & np.isfinite(lon) & ((lat != 0) | (lon != 0))
    qual = col("gpsqual")
    fix &= ~(np.isfinite(qual) & (qual == 0))
    if not fix.any():
        raise ValueError(f"{name}: no GPS fixes (Utc is 0 or there is no position on every row)")
    order = np.argsort(np.where(fix, utc, np.inf), kind="stable")[: int(fix.sum())]
    time = utc[order] / 1000.0
    keep = keep_fixes(time)
    sel = order[keep]

    def unit_of(column, default):
        k = idx(column)
        return header[k][1] if k is not None else default

    speed = col("speed")[sel]
    unit = speed_unit(unit_of("speed", "kph")) or "km/h"
    alt = col("altitude")[sel]
    if unit_of("altitude", "m").lower() in ("ft", "feet"):
        alt = alt * 0.3048
    laps_col = col("lapcount")
    laps = []
    if np.isfinite(laps_col).any():
        ok = np.flatnonzero(np.isfinite(laps_col) & np.isfinite(utc) & (utc > 0))
        ok = ok[np.argsort(utc[ok], kind="stable")]
        steps = ok[1:][np.diff(laps_col[ok]) > 0]
        laps = [float(utc[j] / 1000.0) for j in steps]

    telemetry = Telemetry(
        source="racecapture", time=time[keep], lat=lat[sel], lon=lon[sel], alt=alt,
        speed=speed * SPEED_UNITS[unit], heading=col("heading")[sel],
    )
    rate = header[idx("latitude")][2]
    if not np.isfinite(rate):
        rate = telemetry.sample_rate if len(telemetry) > 1 else 0.0
    warnings = [w for w in [low_rate_warning("RaceCapture", rate)] if w]
    return Recording(Kind.RACECAPTURE, telemetry, "RaceCapture", profile=PRECISE, laps=laps, warnings=warnings)


@register(Kind.RACECAPTURE, _sniff)
def read_racecapture(path: Path) -> Recording:
    return parse_racecapture(read_text(path), Path(path).name)


def write_racecapture(fixes: Telemetry, path: str | Path, gps_hz: float = 10.0, row_hz: float = 25.0,
                      speed_unit_name: str = "MPH", european: bool = False, laps: list[float] = ()) -> None:
    """Write `fixes` (on `gps_hz` epochs) as a RaceCapture log with rows at
    `row_hz` where GPS cells are empty between fixes, and a few seconds of
    rows with Utc 0 before the first fix. `european` writes the
    tab-separated, decimal-comma variant. `laps` are lap-start times,
    counted in LapCount."""
    t = fixes
    factor = 1 / SPEED_UNITS[speed_unit(speed_unit_name)]
    head = [("Interval", "ms", 0, 0, 1), ("Utc", "ms", 0, 0, 1), ("AccelX", "G", -3, 3, row_hz),
            ("Latitude", "Degrees", -180, 180, gps_hz), ("Longitude", "Degrees", -180, 180, gps_hz),
            ("Speed", speed_unit_name, 0, 150, gps_hz), ("Altitude", "ft", 0, 4000, gps_hz),
            ("LapCount", "", 0, 0, gps_hz)]
    delim = "\t" if european else ","
    if european:
        cells = [f'"{n}|""{u}""|{lo}|{hi}|{r:g}"' for n, u, lo, hi, r in head]
    else:
        cells = [f'"{n}"|"{u}"|{lo}|{hi}|{r:g}' for n, u, lo, hi, r in head]
    lines = [delim.join(cells)]
    num = (lambda x, d: f"{x:.{d}f}".replace(".", ",")) if european else (lambda x, d: f"{x:.{d}f}")
    start = t.time[0] - 3.0
    rows_t = np.unique(np.round(np.concatenate([np.arange(start, t.time[-1] + 1e-9, 1.0 / row_hz), t.time]), 4))
    fix_at = {round(x, 4): i for i, x in enumerate(t.time)}
    starts = np.asarray(sorted(laps))
    for ts in rows_t:
        i = fix_at.get(round(ts, 4))
        interval = int(round((ts - start) * 1000)) + 600000
        utc = int(round(ts * 1000)) if ts >= t.time[0] - 1e-6 else 0
        gps = ["", "", "", "", ""]
        if i is not None:
            gps = [num(t.lat[i], 7), num(t.lon[i], 7), num(np.nan_to_num(t.speed[i]) * factor, 2),
                   num(np.nan_to_num(t.alt[i]) / 0.3048, 1), str(int(np.searchsorted(starts, ts, side="right")))]
        lines.append(delim.join([str(interval), str(utc), num(0.01, 2), *gps]))
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
