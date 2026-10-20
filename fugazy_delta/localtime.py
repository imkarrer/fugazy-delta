"""Local time to UTC, for files that do not record UTC.

Some formats write a local date and time with no zone: AiM CSV, MoTeC,
and the date line of Racelogic VBO files. The zone is looked up from the
file's first position (tzfpy, the IANA zone polygons) and converted with
the standard library's zoneinfo. Every result from here is an inference,
so importers that use it set `Recording.tz_inferred`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import tzfpy


def zone_at(lat: float, lon: float) -> str:
    """IANA zone name at a position. Over open sea this is a nautical zone
    such as 'Etc/GMT+6', which zoneinfo also understands."""
    return tzfpy.get_tz(float(lon), float(lat))


def utc_offset(local: datetime, lat: float, lon: float) -> tuple[timedelta, str]:
    """The UTC offset in force at naive local time `local` at a position,
    and the zone name."""
    name = zone_at(lat, lon)
    offset = local.replace(tzinfo=ZoneInfo(name)).utcoffset()
    return offset, name


def local_to_utc(local: datetime, lat: float, lon: float) -> tuple[float, str]:
    """Naive local time at a position → (Unix seconds, zone name)."""
    offset, name = utc_offset(local, lat, lon)
    return (local - offset).replace(tzinfo=timezone.utc).timestamp(), name
