"""Read any supported device file into a `Recording`, judged by content.

Each file format has one importer: a `sniff` that looks only at the
file's content (never its name) and a `read` that parses it into our
model. `identify` walks the registry, binary signatures first because
they are exact, then text signatures on the decoded head of the file with
any byte-order mark stripped. A file nothing claims raises
`UnsupportedFile` naming what to export instead.

A `Recording` carries what the session needs beyond the samples: which
device made it (and so which row of the accuracy table its times quote),
whether its clock had to be inferred, the lap markers the device itself
recorded, and warnings for the results page.

The helpers at the bottom are the conversions several formats share:
speed-unit inference, deriving speed and heading from positions,
accelerations into our signs, and reducing resampled rows to real fixes.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

import numpy as np

from .geo import to_local
from .model import G, Telemetry

HEAD_BYTES = 64 * 1024
TAIL_BYTES = 128

# Keys of the two receiver error profiles in the accuracy table
# (`accuracy.METHODS`). Every device maps to one of them; see PLAN.md for
# which device maps where and why.
PRECISE = "racebox"  # dedicated motorsport GNSS logger, 10 Hz or more
COARSE = "gopro"  # camera, phone or unknown receiver


class UnsupportedFile(ValueError):
    pass


class Kind(Enum):
    GOPRO = "GoPro video"
    VBO = "VBO"
    RACEBOX_CSV = "RaceBox CSV"
    RACECHRONO_CSV = "RaceChrono CSV"
    AIM_XRK = "AiM XRK"
    TRACKADDICT_CSV = "TrackAddict CSV"
    GPX = "GPX"
    NMEA = "NMEA"
    RACECAPTURE = "RaceCapture log"
    AIM_CSV = "AiM CSV"


@dataclass
class Head:
    """The start and end of a file: enough to identify it without reading
    a multi-GB video whole."""

    first: bytes
    last: bytes
    size: int
    path: Path | None = None

    @classmethod
    def of(cls, path: str | Path) -> Head:
        path = Path(path)
        size = path.stat().st_size
        with open(path, "rb") as f:
            first = f.read(HEAD_BYTES)
            if size > HEAD_BYTES:
                f.seek(max(size - TAIL_BYTES, HEAD_BYTES))
                last = f.read()
            else:
                last = first[-TAIL_BYTES:]
        return cls(first, last, size, path)

    @property
    def text(self) -> str:
        """The head decoded as text, BOM stripped. UTF-16 is decoded when a
        UTF-16 BOM says so; anything else as UTF-8 with Latin-1 fallback."""
        b = self.first
        for bom, enc in ((codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16-le"),
                         (codecs.BOM_UTF16_BE, "utf-16-be")):
            if b.startswith(bom):
                return b[len(bom):].decode(enc, errors="replace")
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError as exc:
            # a 64 KB cut can split a multi-byte character
            if exc.start >= len(b) - 4:
                return b[:exc.start].decode("utf-8")
            return b.decode("latin-1")

    @property
    def binary(self) -> bool:
        """Whether the head looks like binary data rather than text."""
        sample = self.first[:4096]
        if sample.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            return False
        return b"\0" in sample


def read_text(path: str | Path) -> str:
    """A whole text file, BOM stripped, decoded like `Head.text`."""
    raw = Path(path).read_bytes()
    for bom, enc in ((codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16-le"),
                     (codecs.BOM_UTF16_BE, "utf-16-be")):
        if raw.startswith(bom):
            return raw[len(bom):].decode(enc, errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


@dataclass
class Recording:
    """One device file, read into our model.

    telemetry: the GPS fixes in our units, signs and UTC; None when the
        device never got a fix (a GoPro recorded indoors).
    device: who recorded it, in words a driver recognises ("RaceBox",
        "AiM", "TrackAddict").
    profile: the accuracy-table key whose measured errors this device's
        times quote, `PRECISE` or `COARSE`.
    camera: whether the file is a video (it can carry an overlay).
    tz_inferred: True when the file had no UTC and its clock was converted
        from local time using a time zone looked up from its position.
    laps: lap-start times (UTC) the device itself marked, for checking our
        own line crossings against.
    """

    kind: Kind
    telemetry: Telemetry | None
    device: str
    profile: str = COARSE
    camera: bool = False
    tz_inferred: bool = False
    laps: list[float] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    payloads: list | None = None  # a camera's raw telemetry payloads

    @property
    def utc(self) -> bool:
        """Every sample carries a real UTC time."""
        return not self.tz_inferred


@dataclass(frozen=True)
class Importer:
    kind: Kind
    sniff: Callable[[Head], bool]
    read: Callable[[Path], Recording]
    binary: bool = False


_REGISTRY: list[Importer] = []


def register(kind: Kind, sniff: Callable[[Head], bool], *, binary: bool = False):
    """Decorator: register `read` as the importer for `kind`."""

    def wrap(read: Callable[[Path], Recording]) -> Callable[[Path], Recording]:
        _REGISTRY.append(Importer(kind, sniff, read, binary))
        return read

    return wrap


def importers() -> list[Importer]:
    _load()
    return sorted(_REGISTRY, key=lambda i: not i.binary)


def _load() -> None:
    # Importing a format module registers it.
    from . import formats  # noqa: F401


UNSUPPORTED_HELP = (
    "From RaceBox, export the session as VBO or CSV on racebox.pro; from a "
    "phone app such as RaceChrono or TrackAddict, export CSV, VBO or GPX; "
    "from a GoPro, upload the original MP4 from the SD card."
)


def identify(path: str | Path) -> Kind:
    """Which format this file is, judged by its content, not its name."""
    path = Path(path)
    head = Head.of(path)
    for importer in importers():
        if not importer.binary and head.binary:
            continue
        if importer.sniff(head):
            return importer.kind
    raise UnsupportedFile(f"{path.name}: not a file format we can read. {UNSUPPORTED_HELP}")


def read(path: str | Path) -> Recording:
    """Identify `path` and read it."""
    kind = identify(path)
    importer = next(i for i in importers() if i.kind is kind)
    return importer.read(Path(path))


# ---------- shared conversions ----------

# Factors from each unit to m/s.
SPEED_UNITS = {
    "m/s": 1.0,
    "km/h": 1 / 3.6,
    "mph": 0.44704,
    "knots": 1852 / 3600,
}

_UNIT_NAMES = {
    "m/s": "m/s", "ms": "m/s", "mps": "m/s",
    "km/h": "km/h", "kmh": "km/h", "kph": "km/h", "kmph": "km/h",
    "mph": "mph", "mi/h": "mph",
    "knots": "knots", "knot": "knots", "kn": "knots", "kts": "knots", "kt": "knots",
}


def speed_unit(name: str) -> str | None:
    """Canonical unit for a unit string such as 'Km/h', 'MPH', 'kph'."""
    return _UNIT_NAMES.get(name.strip().lower().replace(" ", ""))


def derived_speed(time: np.ndarray, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """Speed (m/s) from consecutive positions, centred on each sample."""
    n = len(time)
    if n < 2:
        return np.full(n, np.nan)
    e, nn = to_local(lat, lon, float(np.nanmean(lat)), float(np.nanmean(lon)))
    return np.hypot(np.gradient(e, time), np.gradient(nn, time))


def derived_heading(time: np.ndarray, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """Course over ground (degrees clockwise from north) from positions."""
    n = len(time)
    if n < 2:
        return np.full(n, np.nan)
    e, nn = to_local(lat, lon, float(np.nanmean(lat)), float(np.nanmean(lon)))
    return np.mod(np.degrees(np.arctan2(np.gradient(e, time), np.gradient(nn, time))), 360.0)


MOVING_MS = 3.0  # below this, position noise swamps derived speed


def infer_speed_unit(time, lat, lon, speed) -> tuple[str | None, float]:
    """Which unit a speed column with no unit is in.

    Compares the column with speed derived from positions while the car is
    moving. Returns (unit, ratio of column to derived speed), or (None,
    ratio) when the car never moved enough to tell or the ratio fits no
    unit within 15%.
    """
    derived = derived_speed(np.asarray(time), np.asarray(lat), np.asarray(lon))
    speed = np.asarray(speed, dtype=np.float64)
    moving = (derived > MOVING_MS) & np.isfinite(speed) & (speed > 0)
    if moving.sum() < 10:
        return None, float("nan")
    ratio = float(np.median(speed[moving] / derived[moving]))
    best = min(SPEED_UNITS, key=lambda u: abs(np.log(ratio * SPEED_UNITS[u])))
    if abs(np.log(ratio * SPEED_UNITS[best])) > np.log(1.15):
        return None, ratio
    return best, ratio


def accel_to_model(long, lat, *, long_forward: bool, lat_left: bool, unit_g: float = 1.0):
    """Longitudinal and lateral acceleration into our convention (g, long
    positive forward, lat positive left). `long_forward` / `lat_left` state
    the source's convention; `unit_g` is the source unit in g (1 for g,
    1/G for m/s²)."""
    long = None if long is None else np.asarray(long, dtype=np.float64) * unit_g * (1 if long_forward else -1)
    lat = None if lat is None else np.asarray(lat, dtype=np.float64) * unit_g * (1 if lat_left else -1)
    return long, lat


MS2_IN_G = 1 / G


def keep_fixes(time: np.ndarray, *, changed: np.ndarray | None = None) -> np.ndarray:
    """Mask of rows to keep: strictly increasing time, and (when given) only
    rows where the device reported a new fix."""
    time = np.asarray(time, dtype=np.float64)
    keep = np.isfinite(time)
    if changed is not None:
        keep &= changed
    idx = np.flatnonzero(keep)
    out = np.zeros(len(time), dtype=bool)
    last = -np.inf
    for i in idx:
        if time[i] > last:
            out[i] = True
            last = time[i]
    return out


def position_changed(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """True where the position differs from the previous row's: rows that
    repeat the last fix carry no new information."""
    lat = np.asarray(lat)
    lon = np.asarray(lon)
    out = np.ones(len(lat), dtype=bool)
    out[1:] = (lat[1:] != lat[:-1]) | (lon[1:] != lon[:-1])
    return out


def epoch_rows(time: np.ndarray, rate_hz: float) -> np.ndarray:
    """Mask of the rows nearest each receiver epoch (a multiple of 1/rate
    in UTC), for files that resample fixes onto a faster row clock."""
    time = np.asarray(time, dtype=np.float64)
    epoch = np.round(time * rate_hz)
    dist = np.abs(time - epoch / rate_hz)
    out = np.zeros(len(time), dtype=bool)
    order = np.lexsort((dist, epoch))
    seen = set()
    for i in order:
        if epoch[i] in seen:
            continue
        seen.add(epoch[i])
        if dist[i] < 0.25 / rate_hz:
            out[i] = True
    return out


def low_rate_warning(device: str, rate_hz: float) -> str | None:
    """A warning when a recording's GPS rate is too low for the quoted error."""
    if rate_hz >= 5.0:
        return None
    return (
        f"{device} recorded GPS at about {rate_hz:.0f} Hz. The errors quoted were "
        "measured for an 18 Hz receiver, so times from this file are less "
        "accurate than they say. An external GPS receiver at 10 Hz or more fixes this."
    )
