"""AiM XRK / XRZ logger files (Solo 2, Solo 2 DL, MXm, MyChron).

AiM publishes no specification. The layout here is the one libxrk
(github.com/m3rlin45/libxrk, MIT) reverse-engineered and documents in its
`spec/xrk_format.py`:

- The file is a stream of messages. A header message is framed as
  `<h`, a 4-byte token (3-letter tokens padded with a space), an int32
  payload length, a version byte and `>`; then the payload; then `<`,
  the token again, a uint16 checksum (sum of the payload bytes) and `>`.
  A file starts with a `<hCNF` message. XRZ is the same stream
  zlib-compressed.
- `GPS` messages hold 56-byte records: an int32 logger timecode in ms,
  then a u-blox NAV-SOL solution: GPS time of week (ms), a nanosecond
  correction, GPS week, fix type, ECEF position (cm), position accuracy,
  ECEF velocity (cm/s), speed accuracy, PDOP and satellite count.
- `LAP` messages carry the logger's lap number, duration and end time,
  in the same logger ms as the GPS records.

libxrk itself decodes positions but drops the GPS week and time of week,
and so gives no UTC, which syncing with a GoPro needs; it also brings
PyArrow and Cython (about 135 MB installed). So this module reads the
framing, GPS and LAP messages directly, which is all lap timing needs,
and finds the messages by scanning for their frames and checking each
frame's footer and checksum, so it needs none of the channel layout.
libxrk is an optional extra (`pip install fugazy-delta[aim]`) that the
tests use as an independent reference when it is installed.

UTC is GPS time minus the leap seconds in force (18 since 2017).
"""

from __future__ import annotations

import struct
import zlib
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .importers import PRECISE, Head, Kind, Recording, keep_fixes, low_rate_warning, register
from .model import Telemetry

GPS_EPOCH = datetime(1980, 1, 6, tzinfo=timezone.utc).timestamp()
# (GPS time from which it applies, GPS minus UTC in seconds)
LEAP_SECONDS = (
    (datetime(2009, 1, 1, tzinfo=timezone.utc).timestamp() + 14, 15),
    (datetime(2012, 7, 1, tzinfo=timezone.utc).timestamp() + 15, 16),
    (datetime(2015, 7, 1, tzinfo=timezone.utc).timestamp() + 16, 17),
    (datetime(2017, 1, 1, tzinfo=timezone.utc).timestamp() + 17, 18),
)
GPS_RECORD = 56

# WGS84
_A = 6378137.0
_F = 1 / 298.257223563
_E2 = _F * (2 - _F)


def _token(name: str) -> bytes:
    return name.encode("ascii").ljust(4, b" ")


def _is_zlib(b: bytes) -> bool:
    return len(b) >= 2 and b[0] == 0x78 and b[1] in (0x01, 0x5E, 0x9C, 0xDA)


def _sniff(head: Head) -> bool:
    b = head.first
    if b.startswith(b"<hCNF"):
        return True
    if _is_zlib(b):
        try:
            return zlib.decompressobj().decompress(b, 16).startswith(b"<hCNF")
        except zlib.error:
            return False
    return False


def messages(data: bytes, token: str) -> list[tuple[int, bytes]]:
    """(version, payload) of every well-framed `token` header message, in
    file order. A 3-letter token is padded with a space or, in older files
    such as AiM's own sample, a NUL."""
    out = []
    pads = (b" ", b"\0") if len(token) == 3 else (b"",)
    for pad in pads:
        out += _messages(data, token.encode("ascii") + pad)
    return [(v, p) for _, v, p in sorted(out)]


def _messages(data: bytes, tok: bytes) -> list[tuple[int, int, bytes]]:
    opener = b"<h" + tok
    out = []
    pos = data.find(opener)
    while pos >= 0:
        try:
            length, version = struct.unpack_from("<iB", data, pos + 6)
            body = pos + 12
            end = body + length
            if (length >= 0 and data[pos + 11:pos + 12] == b">" and data[end:end + 1] == b"<"
                    and data[end + 1:end + 5] == tok and data[end + 7:end + 8] == b">"
                    and struct.unpack_from("<H", data, end + 5)[0] == sum(data[body:end]) & 0xFFFF):
                out.append((pos, version, data[body:end]))
                pos = data.find(opener, end + 8)
                continue
        except struct.error:
            pass
        pos = data.find(opener, pos + 1)
    return out


def ecef_to_lla(x, y, z):
    """ECEF metres → (lat, lon degrees, height m), Bowring's method."""
    x, y, z = (np.asarray(v, dtype=np.float64) for v in (x, y, z))
    b = _A * (1 - _F)
    ep2 = (_A**2 - b**2) / b**2
    p = np.hypot(x, y)
    th = np.arctan2(z * _A, p * b)
    lat = np.arctan2(z + ep2 * b * np.sin(th) ** 3, p - _E2 * _A * np.cos(th) ** 3)
    lon = np.arctan2(y, x)
    n = _A / np.sqrt(1 - _E2 * np.sin(lat) ** 2)
    alt = p / np.cos(lat) - n
    return np.degrees(lat), np.degrees(lon), alt


def lla_to_ecef(lat, lon, alt):
    lat, lon = np.radians(lat), np.radians(lon)
    n = _A / np.sqrt(1 - _E2 * np.sin(lat) ** 2)
    return ((n + alt) * np.cos(lat) * np.cos(lon), (n + alt) * np.cos(lat) * np.sin(lon),
            (n * (1 - _E2) + alt) * np.sin(lat))


def _enu(vx, vy, vz, lat, lon):
    lat, lon = np.radians(lat), np.radians(lon)
    east = -np.sin(lon) * vx + np.cos(lon) * vy
    north = -np.sin(lat) * np.cos(lon) * vx - np.sin(lat) * np.sin(lon) * vy + np.cos(lat) * vz
    return east, north


def leap_seconds(gps_seconds: float) -> int:
    """GPS minus UTC at a GPS time given as seconds since the Unix epoch."""
    leap = 14
    for start, value in LEAP_SECONDS:
        if gps_seconds >= start:
            leap = value
    return leap


def load(path: str | Path) -> bytes:
    data = Path(path).read_bytes()
    if _is_zlib(data):
        d = zlib.decompressobj()
        try:
            data = d.decompress(data)
        except zlib.error:
            data = d.flush()
    return data


def parse_xrk(data: bytes, name: str = "XRK") -> Recording:
    raw = b"".join(p for _, p in messages(data, "GPS") + messages(data, "GPS1"))
    if not raw or len(raw) % GPS_RECORD:
        raise ValueError(f"{name}: no GPS records")
    n = len(raw) // GPS_RECORD
    rec = np.frombuffer(raw, dtype=np.dtype([
        ("tc", "<i4"), ("itow", "<u4"), ("ftow", "<i4"), ("week", "<u2"), ("fix", "u1"), ("flags", "u1"),
        ("x", "<i4"), ("y", "<i4"), ("z", "<i4"), ("pacc", "<u4"),
        ("vx", "<i4"), ("vy", "<i4"), ("vz", "<i4"), ("sacc", "<u4"),
        ("pdop", "<u2"), ("res", "u1"), ("nsv", "u1"), ("res2", "<u4"),
    ]), count=n)
    good = (rec["fix"] >= 2) & (rec["week"] > 0) & ((rec["x"] != 0) | (rec["y"] != 0))
    rec = rec[good]
    if not len(rec):
        raise ValueError(f"{name}: no GPS records with a fix")
    gps = GPS_EPOCH + rec["week"].astype(np.float64) * 604800 + rec["itow"] / 1000.0 + rec["ftow"] * 1e-9
    time = gps - np.array([leap_seconds(g) for g in gps])
    order = np.argsort(time, kind="stable")
    rec, time = rec[order], time[order]
    keep = keep_fixes(time)
    rec, time = rec[keep], time[keep]

    lat, lon, alt = ecef_to_lla(rec["x"] / 100.0, rec["y"] / 100.0, rec["z"] / 100.0)
    east, north = _enu(rec["vx"] / 100.0, rec["vy"] / 100.0, rec["vz"] / 100.0, lat, lon)
    telemetry = Telemetry(
        source="aim", time=time, lat=lat, lon=lon, alt=alt,
        speed=np.hypot(east, north), heading=np.mod(np.degrees(np.arctan2(east, north)), 360.0),
    )

    # Lap end times are logger ms, the clock of the GPS records'
    # timecodes. Some firmware corrupts the timecodes' upper 16 bits, so
    # each marker is placed by a straight-line fit of GPS time against
    # timecode over the records within 30 s of it, after dropping records
    # whose offset is more than 0.5 s from the median there.
    laps = []
    tc = rec["tc"].astype(np.float64) / 1000.0
    offset = time - tc
    for version, payload in messages(data, "LAP"):
        if len(payload) < 20:
            continue
        segment, number, duration = struct.unpack_from("<xBHI", payload, 0)
        end = struct.unpack_from("<I", payload, 28 if len(payload) >= 32 else 16)[0]
        if segment:
            continue
        start = (end - duration) / 1000.0
        near = np.abs(tc - start) < 30.0
        if near.sum() < 5:
            continue
        near &= np.abs(offset - np.median(offset[near])) < 0.5
        slope, intercept = np.polyfit(tc[near] - start, time[near], 1)
        laps.append(round(float(intercept), 3))
    laps = sorted(set(laps))

    rate = telemetry.sample_rate if len(telemetry) > 1 else 0.0
    warnings = [w for w in [low_rate_warning("AiM", rate)] if w]
    return Recording(Kind.AIM_XRK, telemetry, "AiM", profile=PRECISE, laps=laps, warnings=warnings)


@register(Kind.AIM_XRK, _sniff, binary=True)
def read_xrk(path: Path) -> Recording:
    return parse_xrk(load(path), Path(path).name)


# ---------- writing (test fixtures) ----------

def frame(token: str, payload: bytes, version: int = 0) -> bytes:
    tok = _token(token)
    return (b"<h" + tok + struct.pack("<iB", len(payload), version) + b">" + payload
            + b"<" + tok + struct.pack("<H", sum(payload) & 0xFFFF) + b">")


def gps_records(telemetry: Telemetry, logger_t0: float, leap: int | None = None) -> bytes:
    """56-byte GPS records for every fix in `telemetry`; the logger clock
    started at UTC `logger_t0`."""
    t = telemetry
    out = bytearray()
    alt = np.nan_to_num(t.alt)
    x, y, z = lla_to_ecef(t.lat, t.lon, alt)
    hdg = np.radians(np.nan_to_num(t.heading))
    ve, vn = np.nan_to_num(t.speed) * np.sin(hdg), np.nan_to_num(t.speed) * np.cos(hdg)
    lat, lon = np.radians(t.lat), np.radians(t.lon)
    vx = -np.sin(lon) * ve - np.sin(lat) * np.cos(lon) * vn
    vy = np.cos(lon) * ve - np.sin(lat) * np.sin(lon) * vn
    vz = np.cos(lat) * vn
    for i in range(len(t)):
        gps = t.time[i] + (leap if leap is not None else leap_seconds(t.time[i] + 18))
        week, tow = divmod(gps - GPS_EPOCH, 604800)
        itow = int(round(tow * 1000))
        ftow = int(round((tow * 1000 - itow) * 1e6))
        out += struct.pack(
            "<iIiHBBiiiIiiiIHBBI",
            int(round((t.time[i] - logger_t0) * 1000)), itow, ftow, int(week), 3, 0x0D,
            int(round(x[i] * 100)), int(round(y[i] * 100)), int(round(z[i] * 100)), 80,
            int(round(vx[i] * 100)), int(round(vy[i] * 100)), int(round(vz[i] * 100)), 30,
            120, 0, 12, 0,
        )
    return bytes(out)


def write_xrk(telemetry: Telemetry, path: str | Path, laps: list[tuple[float, float]] = (),
              compress: bool = False, per_message: int = 25) -> None:
    """Write `telemetry` as an AiM XRK (or XRZ when `compress`) holding a
    CNF message, GPS messages of `per_message` fixes, and a LAP message
    per (start, end) UTC pair in `laps`."""
    t0 = float(telemetry.time[0]) - 2.0
    racer = frame("RCR", b"fugazy-delta\0")
    body = bytearray(frame("CNF", racer))
    records = gps_records(telemetry, t0)
    for k in range(0, len(records), GPS_RECORD * per_message):
        body += frame("GPS", records[k:k + GPS_RECORD * per_message])
        # a data message between header messages, as a real stream has
        body += b"(S" + struct.pack("<iH", k, 3) + b"\x01\x02)"
    for number, (start, end) in enumerate(laps, 1):
        ms = int(round((end - t0) * 1000))
        payload = struct.pack("<xBHI8xI", 0, number, int(round((end - start) * 1000)), ms)
        body += frame("LAP", payload, version=1)
    data = bytes(body)
    Path(path).write_bytes(zlib.compress(data) if compress else data)
