"""Local flat-earth projection.

A track or autocross course spans a few kilometres at most, so an
equirectangular projection about a reference point is accurate to well under
a centimetre — far below GNSS noise — and keeps the maths in metres.
"""

from __future__ import annotations

import numpy as np

EARTH_RADIUS_M = 6_371_008.8


def to_local(lat, lon, origin_lat: float, origin_lon: float):
    """Degrees → (east, north) metres from the origin."""
    k = np.radians(1.0) * EARTH_RADIUS_M
    east = (np.asarray(lon) - origin_lon) * k * np.cos(np.radians(origin_lat))
    north = (np.asarray(lat) - origin_lat) * k
    return east, north


def to_latlon(east, north, origin_lat: float, origin_lon: float):
    """(east, north) metres from the origin → degrees."""
    k = np.radians(1.0) * EARTH_RADIUS_M
    lat = origin_lat + np.asarray(north) / k
    lon = origin_lon + np.asarray(east) / (k * np.cos(np.radians(origin_lat)))
    return lat, lon
