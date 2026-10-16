"""GoPro GPMF telemetry: encode and decode.

GPMF is a key-length-value format (github.com/gopro/gpmf-parser,
docs/README.md). Each KLV is an 8-byte header — FourCC key, one-byte type
char, one-byte struct size, two-byte big-endian repeat count — then
`size * repeat` bytes of big-endian data padded to a 4-byte boundary. Type 0
means the data is itself a list of KLVs (DEVC and STRM are nested).

A GoPro MP4 carries one GPMF payload per ~1 s of video in a 'gpmd' track.
The HERO9 writes, per payload:

- GPS5: lat, lon, alt, 2D speed, 3D speed at ~18 Hz, scaled integers (SCAL)
- GPSU: one UTC timestamp string, 'yymmddhhmmss.sss'
- GPSF fix type, GPSP precision (DOP x 100)
- ACCL / GYRO at ~200 Hz

Only one GPSU per payload means the ~18 GPS5 samples in it have no
timestamps of their own. Readers spread them evenly across the payload's
duration; `decode_gps5` does the same, using the MP4 sample times.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

from .devices import HERO9_NOISE, GnssNoise, gnss_fixes
from .model import G, Telemetry
from .sim import Truth

# struct format per GPMF type char; sizes are per element
_FORMATS = {
    "b": "b", "B": "B", "c": "c", "d": "d", "f": "f", "j": "q", "J": "Q",
    "l": "i", "L": "I", "q": "i", "Q": "I", "s": "h", "S": "H", "U": "c",
}
_SIZES = {"b": 1, "B": 1, "c": 1, "d": 8, "f": 4, "j": 8, "J": 8, "l": 4, "L": 4,
          "q": 4, "Q": 4, "s": 2, "S": 2, "U": 1}


@dataclass
class KLV:
    key: str
    type: str  # "\0" for nested
    size: int
    repeat: int
    data: bytes  # unpadded
    children: list[KLV] | None = None

    def find(self, key: str) -> KLV | None:
        for child in self.children or []:
            if child.key == key:
                return child
        return None

    def find_all(self, key: str) -> list[KLV]:
        return [c for c in self.children or [] if c.key == key]

    def values(self) -> np.ndarray:
        """Numeric data as an array of shape (repeat, size // elem_size)."""
        fmt = _FORMATS[self.type]
        elem = _SIZES[self.type]
        per = self.size // elem
        flat = struct.unpack(f">{per * self.repeat}{fmt}", self.data)
        return np.array(flat, dtype=np.float64).reshape(self.repeat, per)

    def text(self) -> str:
        return self.data.decode("latin-1").rstrip("\0")


def _pad(n: int) -> int:
    return (n + 3) & ~3


def klv(key: str, type_: str, size: int, repeat: int, data: bytes) -> bytes:
    if len(key) != 4:
        raise ValueError(f"key must be 4 chars: {key!r}")
    if len(data) != size * repeat:
        raise ValueError(f"{key}: {len(data)} bytes, expected {size}x{repeat}")
    header = key.encode("ascii") + struct.pack(">cBH", type_.encode("latin-1"), size, repeat)
    return header + data + b"\0" * (_pad(len(data)) - len(data))


def nested(key: str, *children: bytes) -> bytes:
    body = b"".join(children)
    # nested KLVs use size 1 and repeat = byte length when it fits; GoPro
    # writes size 4 for long bodies, so do the same
    if len(body) < 0x10000:
        return klv(key, "\0", 1, len(body), body)
    return klv(key, "\0", 4, len(body) // 4, body)


def numbers(key: str, type_: str, rows) -> bytes:
    arr = np.atleast_2d(np.asarray(rows))
    fmt = _FORMATS[type_]
    data = struct.pack(f">{arr.size}{fmt}", *[int(x) if fmt not in "fd" else float(x) for x in arr.ravel()])
    return klv(key, type_, _SIZES[type_] * arr.shape[1], arr.shape[0], data)


def string(key: str, value: str) -> bytes:
    """A single string: one sample whose struct size is the string length."""
    raw = value.encode("latin-1")
    return klv(key, "c", len(raw), 1, raw)


def decode(buf: bytes) -> list[KLV]:
    out = []
    pos = 0
    while pos + 8 <= len(buf):
        key = buf[pos:pos + 4].decode("latin-1")
        type_ = chr(buf[pos + 4])
        size = buf[pos + 5]
        repeat = struct.unpack(">H", buf[pos + 6:pos + 8])[0]
        length = size * repeat
        data = buf[pos + 8:pos + 8 + length]
        if key == "\0\0\0\0":
            break
        item = KLV(key, type_, size, repeat, data)
        if type_ == "\0":
            item.children = decode(data)
        out.append(item)
        pos += 8 + _pad(length)
    return out


def format_gpsu(utc: float) -> str:
    dt = datetime.fromtimestamp(utc, tz=timezone.utc)
    return f"{dt:%y%m%d%H%M%S}.{dt.microsecond // 1000:03d}"


def parse_gpsu(value: str) -> float:
    base = datetime.strptime(value[:12], "%y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    return base.timestamp() + float(value[12:] or 0)


GPS5_SCAL = (10_000_000, 10_000_000, 1000, 1000, 100)
ACCL_SCAL = 418


@dataclass
class Payload:
    """One GPMF payload and where it sits on the video timeline (seconds)."""

    data: bytes
    start: float
    duration: float


@dataclass(frozen=True)
class CameraClock:
    """How a simulated camera's timing departs from truth.

    gpsu_latency: GPSU minus the true UTC of the payload's first GPS sample.
    camera_offset: camera RTC (written as MP4 creation_time) minus true UTC.
    """

    gpsu_latency: float = 0.0
    camera_offset: float = 37.4


def hero9_payloads(
    truth: Truth,
    record_start: float,
    record_seconds: float,
    clock: CameraClock = CameraClock(),
    gps_hz: float = 18.0,
    accl_hz: float = 200.0,
    payload_seconds: float = 1.001,
    seed: int = 0,
    noise: GnssNoise = HERO9_NOISE,
    gps_fix: int = 3,
) -> list[Payload]:
    """GPMF payloads a HERO9 would write while recording.

    `record_start` is seconds after `truth.utc0` when recording began.

    Accelerometer axis convention (an assumption to verify against a real
    mounted HERO9 file): ORIN "ZXY", camera facing forward and level, so the
    three stored components are (vertical incl. gravity, longitudinal,
    lateral) in m/s².

    GPS fixes carry `noise` (the HERO9 profile unless a test asks otherwise),
    as one continuous error series across the whole recording. `gps_fix` is
    written as GPSF: 0 means no fix, 2 a 2D fix, 3 a 3D fix.
    """
    rng = np.random.default_rng(seed)
    payloads = []
    n_payloads = int(record_seconds // payload_seconds)
    n_gps = round(gps_hz * payload_seconds)
    n_accl = round(accl_hz * payload_seconds)
    t_all = record_start + np.arange(n_payloads * n_gps) * payload_seconds / n_gps
    fixes = gnss_fixes(truth, truth.utc0 + t_all, noise, rng)
    gps_total = accl_total = 0
    for p in range(n_payloads):
        start = p * payload_seconds
        t_gps = t_all[p * n_gps:(p + 1) * n_gps]
        t_accl = record_start + start + np.arange(n_accl) * payload_seconds / n_accl
        g = {k: fixes[k][p * n_gps:(p + 1) * n_gps] for k in ("lat", "lon", "speed")}
        a = truth.at(t_accl)
        speed3d = g["speed"]
        gps5 = np.column_stack([
            np.round(g["lat"] * GPS5_SCAL[0]),
            np.round(g["lon"] * GPS5_SCAL[1]),
            np.full(n_gps, round(250.0 * GPS5_SCAL[2])),
            np.round(g["speed"] * GPS5_SCAL[3]),
            np.round(speed3d * GPS5_SCAL[4]),
        ])
        accl = np.column_stack([
            np.round((G + rng.normal(0, 0.05, n_accl)) * ACCL_SCAL),
            np.round(a["accel_long"] * G * ACCL_SCAL),
            np.round(a["accel_lat"] * G * ACCL_SCAL),
        ])
        stmp_us = round(start * 1e6)
        gps_strm = nested(
            "STRM",
            numbers("STMP", "J", [[stmp_us]]),
            numbers("TSMP", "L", [[gps_total + n_gps]]),
            string("STNM", "GPS (Lat., Long., Alt., 2D speed, 3D speed)"),
            numbers("GPSF", "L", [[gps_fix]]),
            klv("GPSU", "U", 16, 1, format_gpsu(truth.utc0 + t_gps[0] + clock.gpsu_latency).encode()),
            numbers("GPSP", "S", [[150]]),
            klv("UNIT", "c", 3, 5, b"degdegm\0\0m/sm/s"),
            numbers("SCAL", "l", [[s] for s in GPS5_SCAL]),
            numbers("GPS5", "l", gps5),
        )
        accl_strm = nested(
            "STRM",
            numbers("STMP", "J", [[stmp_us]]),
            numbers("TSMP", "L", [[accl_total + n_accl]]),
            string("STNM", "Accelerometer"),
            string("ORIN", "ZXY"),
            numbers("SCAL", "s", [[ACCL_SCAL]]),
            string("SIUN", "m/s\xb2"),
            numbers("ACCL", "s", accl),
        )
        devc = nested(
            "DEVC",
            numbers("DVID", "L", [[1]]),
            string("DVNM", "HERO9 Black"),
            accl_strm,
            gps_strm,
        )
        gps_total += n_gps
        accl_total += n_accl
        payloads.append(Payload(devc, start, payload_seconds))
    return payloads


def _streams(payload: bytes):
    for devc in decode(payload):
        if devc.key != "DEVC":
            continue
        for strm in devc.find_all("STRM"):
            yield strm


def decode_gps5(payloads: list[Payload], source: str = "gopro") -> Telemetry:
    """GPS5 samples from a run of payloads, timed by each payload's GPSU.

    Within a payload the samples are spread evenly over its duration,
    starting at GPSU. Samples with no fix (GPSF < 2) are dropped.
    """
    times, rows = [], []
    for payload in payloads:
        for strm in _streams(payload.data):
            gps5 = strm.find("GPS5")
            if gps5 is None:
                continue
            gpsu = strm.find("GPSU")
            fix = strm.find("GPSF")
            if gpsu is None or (fix is not None and fix.values()[0, 0] < 2):
                continue
            scal = strm.find("SCAL").values().ravel()
            values = gps5.values() / scal
            t0 = parse_gpsu(gpsu.text())
            n = len(values)
            times.append(t0 + np.arange(n) * payload.duration / n)
            rows.append(values)
    if not rows:
        raise ValueError("no GPS5 samples with a fix")
    t = np.concatenate(times)
    v = np.concatenate(rows)
    keep = np.concatenate([[True], np.diff(t) > 0])
    t, v = t[keep], v[keep]
    return Telemetry(source=source, time=t, lat=v[:, 0], lon=v[:, 1], alt=v[:, 2], speed=v[:, 3])
