"""Simulated devices: turn a `Truth` into what a real device would record.

Each device samples the truth on its own clock and adds its own errors. The
errors are seeded, so a test that fails fails the same way every run.

GNSS position error is modelled as a first-order Gauss–Markov process per
axis (slowly wandering, as real receiver error does) rather than white
noise, because white noise averages out and would flatter the analysis.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .geo import to_latlon
from .model import Telemetry
from .sim import Truth


@dataclass(frozen=True)
class GnssNoise:
    position_sigma_m: float = 0.5
    position_tau_s: float = 5.0
    speed_sigma_ms: float = 0.05
    heading_sigma_deg: float = 0.3
    alt_sigma_m: float = 1.5
    accel_sigma_g: float = 0.02


NO_NOISE = GnssNoise(0.0, 1.0, 0.0, 0.0, 0.0, 0.0)


def _gauss_markov(rng: np.random.Generator, n: int, dt: float, sigma: float, tau: float) -> np.ndarray:
    if sigma == 0.0:
        return np.zeros(n)
    phi = np.exp(-dt / tau)
    drive = sigma * np.sqrt(1.0 - phi**2)
    out = np.empty(n)
    out[0] = rng.normal(0.0, sigma)
    w = rng.normal(0.0, drive, n)
    for i in range(1, n):
        out[i] = phi * out[i - 1] + w[i]
    return out


def sample_gnss(
    truth: Truth,
    rate_hz: float,
    noise: GnssNoise = GnssNoise(),
    seed: int = 0,
    source: str = "gnss",
    alt: float = 250.0,
) -> Telemetry:
    """Sample the truth on a UTC-aligned grid at `rate_hz`, with GNSS errors.

    Receivers report on whole-epoch UTC boundaries (a 25 Hz receiver reports
    at .00, .04, .08 …), so sample times are aligned to UTC, not to the
    truth's start.
    """
    rng = np.random.default_rng(seed)
    dt = 1.0 / rate_hz
    first = np.ceil(truth.utc0 * rate_hz) / rate_hz
    utc = np.arange(first, truth.utc0 + truth.t[-1], dt)
    # rebuild from integer epochs so times are exact multiples of dt
    utc = np.round(utc * rate_hz) / rate_hz
    v = truth.at(utc - truth.utc0)
    n = len(utc)

    east = v["east"] + _gauss_markov(rng, n, dt, noise.position_sigma_m, noise.position_tau_s)
    north = v["north"] + _gauss_markov(rng, n, dt, noise.position_sigma_m, noise.position_tau_s)
    lat, lon = to_latlon(east, north, *truth.course.origin)
    speed = np.maximum(0.0, v["speed"] + rng.normal(0.0, noise.speed_sigma_ms, n))
    heading = np.mod(v["heading"] + rng.normal(0.0, noise.heading_sigma_deg, n), 360.0)
    return Telemetry(
        source=source,
        time=utc,
        lat=lat,
        lon=lon,
        alt=alt + _gauss_markov(rng, n, dt, noise.alt_sigma_m, 30.0),
        speed=speed,
        heading=heading,
        accel_long=v["accel_long"] + rng.normal(0.0, noise.accel_sigma_g, n),
        accel_lat=v["accel_lat"] + rng.normal(0.0, noise.accel_sigma_g, n),
        accel_vert=rng.normal(0.0, noise.accel_sigma_g, n),
    )


RACEBOX_RATE_HZ = 25.0


def racebox_mini(truth: Truth, noise: GnssNoise = GnssNoise(), seed: int = 0) -> Telemetry:
    """What a RaceBox Mini records: 25 Hz fused GNSS + IMU."""
    return sample_gnss(truth, RACEBOX_RATE_HZ, noise, seed, source="racebox")
