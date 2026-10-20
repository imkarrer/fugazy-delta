"""GPX track files, the fallback any lap-timer app can export.

GPX 1.1 (topografix.com/GPX/1/1) gives each `trkpt` a latitude,
longitude, optional `ele` and an ISO 8601 UTC `time`. It has no speed or
course element, so unless the file carries one anyway (GPX 1.0's
`speed` and `course`, which GPSBabel writes, or a `speed` in an
extension) speed and heading are derived from consecutive positions.
That speed is noisier than a receiver's Doppler speed; lap timing only
uses it to ignore crossings slower than walking pace, and the crossing
time itself is interpolated from positions. A warning says when speed
was derived.

Elements are matched by local name, so GPX 1.0, 1.1 and vendor
namespaces read alike. RaceBox splits one session over several `trkseg`
elements and writes `siv` for satellites; segments are joined in time
order.

Who wrote the file comes from the `creator` attribute. RaceBox's GPX is
from its own receiver; anything else is a phone or unknown receiver and
quotes the GoPro's measured error. RaceBox's GPX has the same positions
as its CSV export of the same session, but every time is earlier by the
fraction of a second at which the session started (0.16 s in RacePlex's
sample): the GPX counts elapsed time from the whole second.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .importers import (
    COARSE,
    PRECISE,
    Head,
    Kind,
    Recording,
    derived_heading,
    derived_speed,
    keep_fixes,
    low_rate_warning,
    register,
)
from .model import Telemetry

_CREATORS = (
    ("racebox", "RaceBox", True),
    ("racechrono", "RaceChrono", False),
    ("harry", "Harry's LapTimer", False),
    ("trackaddict", "TrackAddict", False),
    ("qstarz", "Qstarz", False),
)


def _sniff(head: Head) -> bool:
    text = head.text[:4096].lstrip().lower()
    return (text.startswith("<?xml") or text.startswith("<gpx")) and "<gpx" in text


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_time(value: str) -> float:
    value = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # GPX times are UTC
    return dt.timestamp()


def parse_gpx(source, name: str = "GPX") -> Recording:
    root = ET.parse(source).getroot()
    creator = root.get("creator", "")
    device, precise = next(((n, p) for w, n, p in _CREATORS if w in creator.lower()),
                           ("GPX file", False))
    rows = []
    for el in root.iter():
        if _local(el.tag) != "trkpt":
            continue
        fields = {}
        for child in el.iter():
            tag = _local(child.tag)
            if tag in ("ele", "time", "speed", "course") and child.text and tag not in fields:
                fields[tag] = child.text
        if "time" not in fields:
            continue
        rows.append((parse_time(fields["time"]), float(el.get("lat")), float(el.get("lon")),
                     float(fields.get("ele", "nan")), float(fields.get("speed", "nan")),
                     float(fields.get("course", "nan"))))
    if not rows:
        raise ValueError(f"{name}: no track points with a time. A GPX route or waypoint "
                         "file cannot be timed; export the recorded track.")
    a = np.array(sorted(rows, key=lambda r: r[0]))
    keep = keep_fixes(a[:, 0])
    a = a[keep]
    time, lat, lon, alt, speed, course = a.T
    warnings = []
    if not np.isfinite(speed).all():
        speed = derived_speed(time, lat, lon)
        warnings.append(f"{name}: GPX has no speed, so speed was worked out from the positions, "
                        "which is less accurate than the receiver's own.")
    if not np.isfinite(course).all():
        course = derived_heading(time, lat, lon)
    telemetry = Telemetry(source="racebox" if device == "RaceBox" else "gpx", time=time, lat=lat, lon=lon, alt=alt, speed=speed, heading=course)
    if device == "RaceBox":
        warnings.append(f"{name}: RaceBox's GPX times run up to 1 s early (they count from the whole "
                        "second before the session started), which does not change lap times. To sync "
                        "with a GoPro, export the session as CSV or VBO instead.")
    rate = telemetry.sample_rate if len(telemetry) > 1 else 0.0
    low = low_rate_warning(device, rate)
    if low:
        warnings.append(low)
    return Recording(Kind.GPX, telemetry, device, profile=PRECISE if precise else COARSE, warnings=warnings)


@register(Kind.GPX, _sniff)
def read_gpx(path: Path) -> Recording:
    return parse_gpx(str(path), Path(path).name)


def write_gpx(telemetry: Telemetry, path: str | Path, creator: str = "fugazy-delta", version: str = "1.1",
              segments: int = 1) -> None:
    """Write `telemetry` as a GPX track. Version 1.0 adds `speed` and
    `course` to each point as GPSBabel does; 1.1 has neither. `segments`
    splits the track as RaceBox does."""
    t = telemetry
    ns = f"http://www.topografix.com/GPX/{version.replace('.', '/')}"
    out = [f'<?xml version="1.0" encoding="UTF-8"?>\n<gpx xmlns="{ns}" version="{version}" creator="{creator}">',
           "<trk><name>Session Data</name>"]
    bounds = np.linspace(0, len(t), segments + 1).astype(int)
    for a, b in zip(bounds, bounds[1:]):
        out.append("<trkseg>")
        for i in range(a, b):
            ts = datetime.fromtimestamp(round(t.time[i], 3), timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            extra = ""
            if version == "1.0":
                extra = f"<course>{np.nan_to_num(t.heading[i]):.2f}</course><speed>{np.nan_to_num(t.speed[i]):.3f}</speed>"
            out.append(f'<trkpt lat="{t.lat[i]:.8f}" lon="{t.lon[i]:.8f}"><ele>{np.nan_to_num(t.alt[i]):.1f}</ele>'
                       f"<time>{ts}</time>{extra}<siv>12</siv></trkpt>")
        out.append("</trkseg>")
    out.append("</trk></gpx>")
    Path(path).write_text("\n".join(out) + "\n", encoding="utf-8")
