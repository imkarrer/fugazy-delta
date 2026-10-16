"""Common telemetry data model.

Every parser produces a `Telemetry`, whatever device the file came from, and
everything downstream (sync, segmentation, analysis, rendering) consumes only
this type. Units are SI and fixed here so no consumer has to guess:

- time: seconds since the Unix epoch, UTC
- lat/lon: decimal degrees (WGS84), east and north positive
- alt: metres above mean sea level
- speed: metres per second over ground
- heading: degrees clockwise from true north, [0, 360)
- accel_long / accel_lat / accel_vert: g, vehicle frame
  (long: + forward, lat: + left, vert: + up)
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

G = 9.80665

_CHANNELS = (
    "lat",
    "lon",
    "alt",
    "speed",
    "heading",
    "accel_long",
    "accel_lat",
    "accel_vert",
)


@dataclass
class Telemetry:
    """A time series of position and motion samples from one device.

    `time`, `lat`, `lon` are required. Any other channel the device did not
    record is NaN-filled, so every array has the same length.
    """

    source: str
    time: np.ndarray
    lat: np.ndarray
    lon: np.ndarray
    alt: np.ndarray = field(default=None)  # type: ignore[assignment]
    speed: np.ndarray = field(default=None)  # type: ignore[assignment]
    heading: np.ndarray = field(default=None)  # type: ignore[assignment]
    accel_long: np.ndarray = field(default=None)  # type: ignore[assignment]
    accel_lat: np.ndarray = field(default=None)  # type: ignore[assignment]
    accel_vert: np.ndarray = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.time = np.asarray(self.time, dtype=np.float64)
        n = len(self.time)
        for name in _CHANNELS:
            value = getattr(self, name)
            if value is None:
                value = np.full(n, np.nan)
            value = np.asarray(value, dtype=np.float64)
            if value.shape != (n,):
                raise ValueError(f"{name} has shape {value.shape}, expected ({n},)")
            setattr(self, name, value)
        if n > 1 and np.any(np.diff(self.time) <= 0):
            raise ValueError("time must be strictly increasing")

    def __len__(self) -> int:
        return len(self.time)

    @property
    def duration(self) -> float:
        return float(self.time[-1] - self.time[0]) if len(self) else 0.0

    @property
    def sample_rate(self) -> float:
        """Median sample rate in Hz."""
        return float(1.0 / np.median(np.diff(self.time)))

    def slice_time(self, start: float, end: float) -> Telemetry:
        mask = (self.time >= start) & (self.time <= end)
        return Telemetry(
            source=self.source,
            time=self.time[mask],
            **{name: getattr(self, name)[mask] for name in _CHANNELS},
        )
