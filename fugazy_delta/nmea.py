"""NMEA 0183 logs, as RaceChrono and Harry's LapTimer export them.

Each line is a sentence: `$`, a talker and type (`GPRMC`, `GNRMC`, ...),
comma-separated fields, `*` and a two-digit hex checksum, the XOR of the
characters between `$` and `*`. Lines whose checksum does not match are
skipped. The official specification is sold by the NMEA, so the field
layouts here are the ones every open parser uses (pynmea2, GPSBabel):

- RMC: UTC time `hhmmss.ss`, status (`A` valid, `V` void), latitude
  `ddmm.mmmm` and N/S, longitude `dddmm.mmmm` and E/W, speed in knots,
  course over ground, date `ddmmyy`. It is the only common sentence with
  a date, so a log needs RMC.
- GGA: UTC time, position, fix quality, satellites, HDOP, altitude. Only
  its altitude and satellite count are used, matched to RMC by time.

Two-digit years are taken as 2000 to 2099 (GPSBabel's 1990s samples
aside, no lap timer predates 2000). The receiver is unknown, so NMEA logs
quote the GoPro's measured error.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from functools import reduce
from pathlib import Path

import numpy as np

from .importers import COARSE, Head, Kind, Recording, keep_fixes, low_rate_warning, read_text, register
from .model import Telemetry

_SENTENCE = re.compile(r"\$(G[A-Z])(RMC|GGA),([^*\r\n]*)\*([0-9A-Fa-f]{2})")
KNOT = 1852 / 3600


def _sniff(head: Head) -> bool:
    return len(re.findall(r"^\$G[A-Z]RMC,", head.text, re.MULTILINE)) >= 2


def checksum(body: str) -> str:
    return f"{reduce(lambda a, c: a ^ ord(c), body, 0):02X}"


def _degrees(value: str, hemi: str) -> float:
    if not value:
        return float("nan")
    head, minutes = divmod(float(value), 100)
    deg = head + minutes / 60
    return -deg if hemi in ("S", "W") else deg


def _seconds(hms: str) -> float:
    return int(hms[0:2]) * 3600 + int(hms[2:4]) * 60 + float(hms[4:])


def parse_nmea(text: str, name: str = "NMEA") -> Recording:
    rmc, gga = [], {}
    bad = 0
    for m in _SENTENCE.finditer(text):
        talker, kind, body, cs = m.groups()
        if checksum(f"{talker}{kind},{body}") != cs.upper():
            bad += 1
            continue
        f = body.split(",")
        if kind == "RMC" and len(f) >= 9 and f[1] == "A" and f[0] and f[8]:
            day, month, year = int(f[8][0:2]), int(f[8][2:4]), 2000 + int(f[8][4:6])
            midnight = datetime(year, month, day, tzinfo=timezone.utc).timestamp()
            rmc.append((midnight + _seconds(f[0]), _degrees(f[2], f[3]), _degrees(f[4], f[5]),
                        float(f[6] or "nan") * KNOT, float(f[7] or "nan")))
        elif kind == "GGA" and len(f) >= 9 and f[0]:
            gga[round(_seconds(f[0]), 2)] = float(f[8]) if f[8] else float("nan")
    if not rmc:
        raise ValueError(f"{name}: no valid RMC sentences, which carry the date. Log RMC "
                         "sentences, or export the session as GPX or CSV instead.")
    a = np.array(sorted(rmc))
    a = a[keep_fixes(a[:, 0])]
    time, lat, lon, speed, course = a.T
    alt = np.array([gga.get(round(ts % 86400, 2), np.nan) for ts in time])
    telemetry = Telemetry(source="nmea", time=time, lat=lat, lon=lon, alt=alt, speed=speed, heading=course)
    warnings = []
    if bad:
        warnings.append(f"{name}: {bad} sentences with a bad checksum were skipped.")
    rate = telemetry.sample_rate if len(telemetry) > 1 else 0.0
    if low := low_rate_warning("The NMEA log", rate):
        warnings.append(low)
    return Recording(Kind.NMEA, telemetry, "NMEA log", profile=COARSE, warnings=warnings)


@register(Kind.NMEA, _sniff)
def read_nmea(path: Path) -> Recording:
    return parse_nmea(read_text(path), Path(path).name)


def _ddmm(value: float, width: int) -> str:
    deg = int(abs(value))
    return f"{deg:0{width}d}{(abs(value) - deg) * 60:07.4f}"


def write_nmea(telemetry: Telemetry, path: str | Path, talker: str = "GN", gga: bool = True) -> None:
    """Write `telemetry` as an NMEA log: one RMC (and GGA) per fix."""
    t = telemetry
    lines = []
    for i in range(len(t)):
        dt = datetime.fromtimestamp(round(t.time[i], 2), timezone.utc)
        hms = f"{dt:%H%M%S}.{dt.microsecond // 10000:02d}"
        lat = f"{_ddmm(t.lat[i], 2)},{'N' if t.lat[i] >= 0 else 'S'}"
        lon = f"{_ddmm(t.lon[i], 3)},{'E' if t.lon[i] >= 0 else 'W'}"
        bodies = []
        if gga:
            bodies.append(f"{talker}GGA,{hms},{lat},{lon},1,12,0.8,{np.nan_to_num(t.alt[i]):.1f},M,-34.2,M,,")
        bodies.append(f"{talker}RMC,{hms},A,{lat},{lon},{np.nan_to_num(t.speed[i]) / KNOT:.3f},"
                      f"{np.nan_to_num(t.heading[i]):.2f},{dt:%d%m%y},,,A")
        lines += [f"${b}*{checksum(b)}" for b in bodies]
    Path(path).write_text("\r\n".join(lines) + "\r\n", encoding="ascii")
