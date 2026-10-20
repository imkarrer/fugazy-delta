"""Racelogic VBO files: read and write.

VBO is a text format: bracketed sections, a `[column names]` line, then
whitespace-separated rows under `[data]`. Its conventions differ from ours
and are converted at this boundary only:

- latitude and longitude are in *minutes*, and longitude is positive WEST
- time is UTC `HHMMSS.SS` with no date, and rows roll over at midnight
- velocity is km/h
- the satellite count has 64 added while the brake trigger is active and
  128 while DGPS is in use, so the count is `sats & 0x3F`

The UTC date has to come from somewhere else, and devices differ:

- RaceBox writes `UTC Date Started : dd/mm/yyyy HH:MM` in `[comments]`,
  which is the UTC date and time of the start of the session.
- Racelogic's `File created on dd/mm/yyyy @ HH:MM:SS` line is the device
  clock's *local* time. A US session after 17:00 Pacific is already the
  next day in UTC, so taking that date as UTC puts every sample a day
  early. Without RaceBox's comment, the reader looks up the time zone at
  the first position and picks the UTC date (the line's date, the day
  before or the day after) whose implied offset between the line's time
  and the first sample's UTC time is closest to that zone's offset. This
  holds even when the line's time is not the session's start, as in a
  trimmed copy, as long as it is within about 12 hours of it.

Columns are matched by name, case-insensitively, so files from different
exporters (Racelogic, RaceBox, RaceChrono) load as long as they carry the
core channels. Unknown columns are ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from .importers import COARSE, PRECISE, Kind, Recording, register
from .localtime import utc_offset
from .model import Telemetry

_ALIASES = {
    "time": {"time"},
    "lat": {"lat", "latitude"},
    "lon": {"long", "lon", "lng", "longitude"},
    "speed": {"velocity", "speed", "velocity_kmh"},
    "heading": {"heading"},
    "alt": {"height", "altitude", "alt"},
    "accel_long": {"longacc", "long_acc", "longaccel", "longitudinal_acc", "accel_long", "gps_longacc"},
    "accel_lat": {"latacc", "lat_acc", "lataccel", "lateral_acc", "accel_lat", "gps_latacc"},
    "accel_vert": {"vertacc", "vert_acc", "accel_vert"},
    "sats": {"sats", "satellites"},
}

_DATE = r"(\d{1,2})/(\d{1,2})/(\d{4})\s*(?:@|at)?\s*(?:(\d{1,2}):(\d{2})(?::(\d{2}(?:\.\d*)?))?)?"
_CREATED = re.compile(r"File created on\s+" + _DATE, re.IGNORECASE)
_UTC_STARTED = re.compile(r"UTC Date Started\s*:\s*" + _DATE, re.IGNORECASE)


def _hms_to_seconds(value: str) -> float:
    v = float(value)
    hh = int(v // 10000)
    mm = int((v % 10000) // 100)
    ss = v - hh * 10000 - mm * 100
    return hh * 3600 + mm * 60 + ss


def _stamp(m: re.Match) -> datetime:
    """A naive datetime from a `_DATE` match; time 00:00 when absent."""
    day, month, year = int(m[1]), int(m[2]), int(m[3])
    hh, mm, ss = int(m[4] or 0), int(m[5] or 0), float(m[6] or 0)
    return datetime(year, month, day) + timedelta(hours=hh, minutes=mm, seconds=ss)


@dataclass
class VboFile:
    telemetry: Telemetry
    tz_inferred: bool = False
    warnings: list[str] = field(default_factory=list)


def _pick_day(candidates_from: datetime, first_utc_s: float, target: datetime, offset: timedelta) -> datetime:
    """The UTC midnight, within a day of `candidates_from`'s date, that
    makes `target - (midnight + first_utc_s)` closest to `offset`."""
    day = candidates_from.replace(hour=0, minute=0, second=0, microsecond=0)
    options = [day + timedelta(days=k) for k in (-1, 0, 1)]
    return min(options, key=lambda d: abs((target - (d + timedelta(seconds=first_utc_s))) - offset))


def parse_vbo(text: str, date: datetime | None = None, source: str = "vbo", name: str = "VBO") -> VboFile:
    section = None
    columns: list[str] = []
    rows: list[list[str]] = []
    created = utc_started = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if created is None and (m := _CREATED.search(line)):
            created = m
        if utc_started is None and (m := _UTC_STARTED.search(line)):
            utc_started = m
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if section == "column names":
            columns = [c.lower() for c in line.split()]
        elif section == "data":
            rows.append(line.split())
    if not columns:
        raise ValueError(f"{name}: no [column names] section")
    if not rows:
        raise ValueError(f"{name}: no data rows")

    index: dict[str, int] = {}
    for field_, names in _ALIASES.items():
        for i, col in enumerate(columns):
            if col in names:
                index[field_] = i
                break
    for required in ("time", "lat", "lon"):
        if required not in index:
            raise ValueError(f"{name}: missing {required!r} column (have {columns})")

    width = len(columns)
    rows = [r for r in rows if len(r) == width]

    def col(field_):
        if field_ not in index:
            return None
        return np.array([float(r[index[field_]]) for r in rows])

    lat, lon = col("lat") / 60.0, -col("lon") / 60.0
    keep = (lat != 0) | (lon != 0)
    if "sats" in index:
        keep &= (col("sats").astype(np.int64) & 0x3F) > 0
    seconds = np.array([_hms_to_seconds(r[index["time"]]) for r in rows])
    rollover = np.concatenate([[0], np.cumsum(np.diff(seconds) < -43200)]) * 86400.0
    seconds = seconds + rollover
    if not keep.any():
        raise ValueError(f"{name}: no rows with a GPS fix")
    first = int(np.flatnonzero(keep)[0])

    out = VboFile(telemetry=None)  # type: ignore[arg-type]
    if date is not None:
        midnight = date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    elif utc_started is not None:
        started = _stamp(utc_started)
        midnight = _pick_day(started, seconds[first], started, timedelta(0))
    elif created is not None:
        local = _stamp(created)
        offset, zone = utc_offset(local, lat[first], lon[first])
        midnight = _pick_day(local, seconds[first], local, offset)
        out.tz_inferred = True
        out.warnings.append(
            f"{name}: the file's date is in the logger's local time, so its UTC date "
            f"was worked out from the time zone at the track ({zone})."
        )
    else:
        raise ValueError(f"{name}: no 'File created on' date; pass date=")
    time = midnight.replace(tzinfo=timezone.utc).timestamp() + seconds

    speed = col("speed")
    m = keep
    pick = lambda a: None if a is None else a[m]  # noqa: E731
    out.telemetry = Telemetry(
        source=source,
        time=time[m],
        lat=lat[m],
        lon=lon[m],
        alt=pick(col("alt")),
        speed=None if speed is None else speed[m] / 3.6,
        heading=pick(col("heading")),
        accel_long=pick(col("accel_long")),
        accel_lat=pick(col("accel_lat")),
        accel_vert=pick(col("accel_vert")),
    )
    return out


def read_vbo(path: str | Path, date: datetime | None = None, source: str = "vbo") -> Telemetry:
    """The samples of a VBO file. `date` overrides the file's UTC date."""
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    return parse_vbo(text, date, source, str(path)).telemetry


def _hms(utc: float, midnight: float) -> str:
    secs = (utc - midnight) % 86400.0
    hh, rem = divmod(secs, 3600)
    mm, ss = divmod(rem, 60)
    # round seconds to the file's precision before carrying, so 59.9999
    # never prints as "60.000"
    ss = round(ss, 3)
    if ss >= 60.0:
        ss -= 60.0
        mm += 1
        if mm >= 60:
            mm -= 60
            hh = (hh + 1) % 24
    return f"{int(hh):02d}{int(mm):02d}{ss:06.3f}"


def write_vbo(telemetry: Telemetry, path: str | Path, satellites: int = 12, comment: str = "",
              style: str = "fugazy", local_zone: str | None = None) -> None:
    """Write `telemetry` as a VBO file with the core channels plus g-forces.

    `style` copies a real device's layout:

    - "fugazy": our own layout, UTC date line plus RaceBox's `UTC Date
      Started` comment.
    - "racebox": a RaceBox export (Thruxton file in docs/formats.md): a
      `lng` column, a date line without seconds, `Generated by RaceBox`.
    - "racelogic": a VBOX: the date line in local time in `local_zone`, no
      UTC comment, and the DGPS bit (128) set in the satellite count.
    """
    from zoneinfo import ZoneInfo

    t = telemetry
    created = datetime.fromtimestamp(t.time[0], tz=timezone.utc)
    midnight = created.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    if style == "fugazy":
        lines = [
            f"File created on {created:%d/%m/%Y} @ {created:%H:%M:%S}", "",
            "[header]", "satellites", "time", "latitude", "longitude", "velocity kmh", "heading",
            "height", "longacc", "latacc", "",
            "[comments]", comment or "Generated by fugazy-delta",
            f"UTC Date Started : {created:%d/%m/%Y %H:%M}", "",
            "[column names]", "sats time lat long velocity heading height longacc latacc", "",
            "[data]",
        ]
        for i in range(len(t)):
            lines.append(
                f"{satellites:03d} {_hms(t.time[i], midnight)} "
                f"{t.lat[i] * 60.0:+012.6f} {-t.lon[i] * 60.0:+012.6f} "
                f"{_num(t.speed[i] * 3.6, '07.3f')} {_num(t.heading[i], '06.2f')} "
                f"{_num(t.alt[i], '+09.2f')} {_num(t.accel_long[i], '+06.3f')} {_num(t.accel_lat[i], '+06.3f')}"
            )
    elif style == "racebox":
        lines = [
            f"File created on {created:%d/%m/%Y} @ {created:%H:%M}", "",
            "[header]", "time", "latitude", "longitude", "velocity kmh", "heading", "height",
            "LongAcc", "LatAcc", "VertAcc", "satellites", "",
            "[comments]", "Generated by RaceBox", f"UTC Date Started : {created:%d/%m/%Y %H:%M}",
            "Venue : fugazy-delta test course", "",
            "[column names]", "time lat lng velocity heading height LongAcc LatAcc VertAcc sats", "",
            "[data]",
        ]
        for i in range(len(t)):
            lines.append(
                f"{_hms(t.time[i], midnight)[:9]} {t.lat[i] * 60.0:+011.5f} {-t.lon[i] * 60.0:+012.5f} "
                f"{_num(t.speed[i] * 3.6, '07.3f')} {_num(t.heading[i], '06.2f')} {_num(t.alt[i], '+09.2f')} "
                f"{_num(t.accel_long[i], '+06.3f')} {_num(t.accel_lat[i], '+06.3f')} "
                f"{_num(t.accel_vert[i], '+06.3f')} {satellites}"
            )
    elif style == "racelogic":
        local = created.astimezone(ZoneInfo(local_zone or "UTC"))
        lines = [
            f"File created on {local:%d/%m/%Y} @ {local:%H:%M:%S}", "",
            "[header]", "satellites", "time", "latitude", "longitude", "velocity kmh", "heading",
            "height", "long accel g", "lat accel g", "",
            "[channel units]", "", "", "", "",
            "[comments]", "(c) Racelogic", "<Unit Info>", "Type   : VBOX Sport", "",
            "[column names]", "sats time lat long velocity heading height longacc latacc", "",
            "[data]",
        ]
        for i in range(len(t)):
            lines.append(
                f"{satellites + 128:03d} {_hms(t.time[i], midnight)} "
                f"{t.lat[i] * 60.0:+012.6f} {-t.lon[i] * 60.0:+012.6f} "
                f"{_num(t.speed[i] * 3.6, '07.3f')} {_num(t.heading[i], '06.2f')} "
                f"{_num(t.alt[i], '+09.2f')} {_num(t.accel_long[i], '+08.4f')} {_num(t.accel_lat[i], '+08.4f')}"
            )
    else:
        raise ValueError(f"unknown VBO style {style!r}")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _num(value: float, fmt: str) -> str:
    return format(0.0 if np.isnan(value) else value, fmt)


# ---------- importer ----------

# Who wrote a VBO file, from words in its comments or header, and whether
# it is a dedicated GNSS logger (see importers.PRECISE).
_WRITERS = (
    ("racebox", "RaceBox", True),
    ("racelogic", "Racelogic VBOX", True),
    ("vbox", "Racelogic VBOX", True),
    ("racechrono", "RaceChrono", False),
    ("trackaddict", "TrackAddict", False),
    ("harry", "Harry's LapTimer", False),
    ("dragy", "dragy", False),
)


def _sniff(head) -> bool:
    text = head.text[:8192].lower()
    return "[column names]" in text or "file created on" in text


def vbo_writer(text: str) -> tuple[str, bool]:
    """(device name, dedicated logger?) from a VBO file's text."""
    preamble = text.split("[data]", 1)[0].lower()
    for word, name, precise in _WRITERS:
        if word in preamble:
            return name, precise
    return "VBO logger", False


@register(Kind.VBO, _sniff)
def read_vbo_recording(path: Path) -> Recording:
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    device, precise = vbo_writer(text)
    vbo = parse_vbo(text, source=_slug(device), name=Path(path).name)
    return Recording(Kind.VBO, vbo.telemetry, device, profile=PRECISE if precise else COARSE,
                     tz_inferred=vbo.tz_inferred, warnings=vbo.warnings)


def _slug(device: str) -> str:
    return "".join(c for c in device.split()[0].lower() if c.isalnum())
