"""Virtual car: a ground-truth trajectory with known lap, run and sector times.

Tests generate device files from a `Truth` and check that the pipeline
recovers what the truth says. A course is a chain of straights and constant-
radius arcs, so curvature is exact; the speed profile is the classic
two-pass point-mass model (grip-limited cornering speed, then forward pass
for acceleration and backward pass for braking), which keeps speed
continuous even where pace changes between sectors.

Pace is a multiplier on grip per (lap, sector). A lap with pace 0.9 in
sector 2 is slower there and only there, which is how tests build an
"optimal lap" whose answer is known: the best sector from each lap.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

from .geo import to_latlon
from .model import G, Telemetry

DEFAULT_ORIGIN = (44.8113, -91.4985)  # Chippewa Valley, WI
DEFAULT_UTC0 = datetime(2026, 6, 13, 14, 0, 0, tzinfo=timezone.utc).timestamp()
TRUTH_RATE_HZ = 200.0


@dataclass(frozen=True)
class Straight:
    length: float


@dataclass(frozen=True)
class Arc:
    radius: float
    angle_deg: float  # positive = left turn, negative = right turn


Segment = Straight | Arc


@dataclass
class Course:
    """Course geometry on a fine distance grid.

    `sectors` are fractions of course length where sector boundaries fall,
    excluding 0 and 1. A closed course is a lap (track day); an open course
    runs start line → finish line (autocross).
    """

    segments: list[Segment]
    closed: bool
    sectors: tuple[float, ...] = ()
    origin: tuple[float, float] = DEFAULT_ORIGIN
    start_heading_deg: float = 0.0  # compass, 0 = north
    ds: float = 0.25

    s: np.ndarray = field(init=False)
    curvature: np.ndarray = field(init=False)
    east: np.ndarray = field(init=False)
    north: np.ndarray = field(init=False)
    theta: np.ndarray = field(init=False)  # maths angle, radians CCW from east

    def __post_init__(self) -> None:
        k = []
        for seg in self.segments:
            if isinstance(seg, Straight):
                n = max(1, round(seg.length / self.ds))
                k.extend([0.0] * n)
            else:
                length = seg.radius * math.radians(abs(seg.angle_deg))
                n = max(1, round(length / self.ds))
                k.extend([math.copysign(1.0 / seg.radius, seg.angle_deg)] * n)
        k = np.array(k)
        # Rounding each segment to whole steps makes step length slightly
        # non-uniform; rescale so total length and turn angles stay exact.
        lengths = []
        for seg in self.segments:
            if isinstance(seg, Straight):
                lengths.append(seg.length)
            else:
                lengths.append(seg.radius * math.radians(abs(seg.angle_deg)))
        steps = []
        for seg, length in zip(self.segments, lengths):
            n = max(1, round(length / self.ds))
            steps.extend([length / n] * n)
        steps = np.array(steps)

        theta0 = math.radians(90.0 - self.start_heading_deg)
        theta = theta0 + np.concatenate([[0.0], np.cumsum(k * steps)])
        mid = theta[:-1] + 0.5 * k * steps
        east = np.concatenate([[0.0], np.cumsum(np.cos(mid) * steps)])
        north = np.concatenate([[0.0], np.cumsum(np.sin(mid) * steps)])

        self.s = np.concatenate([[0.0], np.cumsum(steps)])
        # curvature per grid point: the curvature of the step leaving it. On a
        # closed course the last point is the first point, so it leaves into
        # the first step.
        self.curvature = np.append(k, k[0] if self.closed else 0.0)
        self.east, self.north, self.theta = east, north, theta

        if self.closed:
            gap = math.hypot(east[-1], north[-1])
            turn = (theta[-1] - theta0) / (2 * math.pi)
            if gap > 0.5 or abs(abs(turn) - 1.0) > 1e-6:
                raise ValueError(
                    f"closed course does not close: gap {gap:.2f} m, {turn:.4f} turns"
                )

    @property
    def length(self) -> float:
        return float(self.s[-1])

    @property
    def sector_distances(self) -> np.ndarray:
        return np.array([f * self.length for f in self.sectors])

    def latlon_at(self, s: float) -> tuple[float, float]:
        e = float(np.interp(s, self.s, self.east))
        n = float(np.interp(s, self.s, self.north))
        lat, lon = to_latlon(e, n, *self.origin)
        return float(lat), float(lon)


@dataclass
class Lap:
    """One timed lap or run, in seconds relative to the truth's start."""

    start: float
    end: float
    sector_splits: np.ndarray  # times of sector boundaries, same reference

    @property
    def time(self) -> float:
        return self.end - self.start

    @property
    def sector_times(self) -> np.ndarray:
        edges = np.concatenate([[self.start], self.sector_splits, [self.end]])
        return np.diff(edges)


@dataclass
class Truth:
    """Ground-truth trajectory sampled at `TRUTH_RATE_HZ`.

    `t` is seconds from `utc0`. Accelerations are in g in the vehicle frame.
    """

    course: Course
    utc0: float
    t: np.ndarray
    east: np.ndarray
    north: np.ndarray
    speed: np.ndarray
    heading: np.ndarray  # compass degrees
    accel_long: np.ndarray
    accel_lat: np.ndarray
    laps: list[Lap]

    @property
    def lap_times(self) -> np.ndarray:
        return np.array([lap.time for lap in self.laps])

    @property
    def optimal_time(self) -> float:
        """Sum of the best time for each sector across all laps."""
        return float(np.sum(np.min([lap.sector_times for lap in self.laps], axis=0)))

    def at(self, t: np.ndarray) -> dict[str, np.ndarray]:
        """Truth channels linearly interpolated at relative times `t`."""
        t = np.asarray(t, dtype=np.float64)
        heading = np.degrees(np.unwrap(np.radians(self.heading)))
        east = np.interp(t, self.t, self.east)
        north = np.interp(t, self.t, self.north)
        lat, lon = to_latlon(east, north, *self.course.origin)
        return {
            "east": east,
            "north": north,
            "lat": lat,
            "lon": lon,
            "speed": np.interp(t, self.t, self.speed),
            "heading": np.mod(np.interp(t, self.t, heading), 360.0),
            "accel_long": np.interp(t, self.t, self.accel_long),
            "accel_lat": np.interp(t, self.t, self.accel_lat),
        }

    def telemetry(self, rate_hz: float = TRUTH_RATE_HZ, alt: float = 250.0) -> Telemetry:
        """Noise-free telemetry at `rate_hz`, in absolute UTC."""
        t = np.arange(0.0, self.t[-1], 1.0 / rate_hz)
        v = self.at(t)
        return Telemetry(
            source="truth",
            time=self.utc0 + t,
            lat=v["lat"],
            lon=v["lon"],
            alt=np.full(len(t), alt),
            speed=v["speed"],
            heading=v["heading"],
            accel_long=v["accel_long"],
            accel_lat=v["accel_lat"],
            accel_vert=np.zeros(len(t)),
        )


@dataclass(frozen=True)
class Car:
    grip: float = 1.0  # lateral/braking limit in g
    drive: float = 0.35  # acceleration limit in g
    top_speed: float = 45.0  # m/s


def _speed_profile(k: np.ndarray, ds: np.ndarray, grip: np.ndarray, car: Car, v_start: float, v_end: float | None):
    with np.errstate(divide="ignore"):
        v = np.minimum(car.top_speed, np.sqrt(grip * G / np.abs(k)))
    v[0] = min(v[0], v_start)
    if v_end is not None:
        v[-1] = min(v[-1], v_end)
    for i in range(len(v) - 1):
        v[i + 1] = min(v[i + 1], math.sqrt(v[i] ** 2 + 2 * car.drive * G * ds[i]))
    for i in range(len(v) - 2, -1, -1):
        v[i] = min(v[i], math.sqrt(v[i + 1] ** 2 + 2 * grip[i] * G * ds[i]))
    return v


def _time_along(s: np.ndarray, v: np.ndarray) -> np.ndarray:
    dt = 2.0 * np.diff(s) / (v[:-1] + v[1:])
    return np.concatenate([[0.0], np.cumsum(dt)])


def _pace_grid(course: Course, s_in_lap: np.ndarray, pace: list[float]) -> np.ndarray:
    n_sectors = len(course.sectors) + 1
    if len(pace) != n_sectors:
        raise ValueError(f"pace has {len(pace)} entries, course has {n_sectors} sectors")
    idx = np.searchsorted(course.sector_distances, s_in_lap, side="right")
    return np.asarray(pace)[idx]


def _sample_path(t_grid, s_grid, v, course_s, course_e, course_n, course_theta, course_k, s_mod):
    """Resample a distance-grid path onto the fixed truth time grid."""
    t = np.arange(0.0, t_grid[-1], 1.0 / TRUTH_RATE_HZ)
    s = np.interp(t, t_grid, s_grid)
    sm = s_mod(s)
    speed = np.interp(t, t_grid, v)
    east = np.interp(sm, course_s, course_e)
    north = np.interp(sm, course_s, course_n)
    theta = np.interp(sm, course_s, course_theta)
    k = np.interp(sm, course_s, course_k)
    accel_long = np.gradient(speed, t) / G
    accel_lat = speed**2 * k / G
    heading = np.mod(90.0 - np.degrees(theta), 360.0)
    return t, east, north, speed, heading, accel_long, accel_lat


def _crossing_time(t_grid, s_grid, s_target) -> float:
    return float(np.interp(s_target, s_grid, t_grid))


def track_session(course: Course, pace: list[list[float]], car: Car = Car(), utc0: float = DEFAULT_UTC0) -> Truth:
    """Laps of a closed course, starting from rest on the start/finish line.

    Lap 0 is the out-lap from standstill; every entry in `pace` is a full lap,
    so `laps` in the result has `len(pace)` entries.
    """
    if not course.closed:
        raise ValueError("track_session needs a closed course")
    L = course.length
    n_laps = len(pace)
    s = np.concatenate([course.s[:-1] + i * L for i in range(n_laps)] + [[n_laps * L]])
    s_in_lap = np.mod(s, L)
    s_in_lap[-1] = L
    k = np.interp(s_in_lap, course.s, course.curvature)
    lap_idx = np.minimum((s // L).astype(int), n_laps - 1)
    grip = car.grip * np.concatenate(
        [_pace_grid(course, s_in_lap[lap_idx == i], pace[i]) for i in range(n_laps)]
    )
    v = _speed_profile(k, np.diff(s), grip, car, v_start=0.0, v_end=None)
    v[0] = max(v[0], 1e-3)
    t_grid = _time_along(s, v)

    laps = []
    for i in range(n_laps):
        start = _crossing_time(t_grid, s, i * L)
        end = _crossing_time(t_grid, s, (i + 1) * L)
        splits = np.array([_crossing_time(t_grid, s, i * L + d) for d in course.sector_distances])
        laps.append(Lap(start, end, splits))

    cols = _sample_path(
        t_grid, s, v, course.s, course.east, course.north, course.theta, course.curvature,
        lambda x: np.mod(x, L),
    )
    return Truth(course, utc0, *cols, laps=laps)


def autocross_session(
    course: Course,
    pace: list[list[float]],
    car: Car = Car(),
    utc0: float = DEFAULT_UTC0,
    staging: float = 3.0,
    idle: float = 20.0,
    return_speed: float = 5.0,
) -> Truth:
    """Several runs of an open course in one continuous recording.

    Each run starts from rest `staging` metres behind the start line (so the
    car is rolling when it trips the timer, as at a real event), runs to the
    finish, brakes to a stop on a straight run-off, idles, then drives back to
    the staging spot in a straight line and idles again before the next run.
    """
    if course.closed:
        raise ValueError("autocross_session needs an open course")
    L = course.length
    runoff = car.top_speed**2 / (2 * car.grip * G) + 10.0

    # Extend the course geometry with staging before and run-off after.
    e0, n0, th0 = course.east[0], course.north[0], course.theta[0]
    e1, n1, th1 = course.east[-1], course.north[-1], course.theta[-1]
    stage_s = np.arange(-staging, 0.0, course.ds)
    run_s = np.arange(course.ds, runoff + course.ds, course.ds)
    full_s = np.concatenate([stage_s, course.s, L + run_s])
    full_e = np.concatenate([e0 + stage_s * math.cos(th0), course.east, e1 + run_s * math.cos(th1)])
    full_n = np.concatenate([n0 + stage_s * math.sin(th0), course.north, n1 + run_s * math.sin(th1)])
    full_th = np.concatenate([np.full(len(stage_s), th0), course.theta, np.full(len(run_s), th1)])
    full_k = np.concatenate([np.zeros(len(stage_s)), course.curvature, np.zeros(len(run_s))])

    pieces: list[tuple] = []
    laps: list[Lap] = []
    t_offset = 0.0

    def idle_piece(duration, e, n, heading):
        t = np.arange(0.0, duration, 1.0 / TRUTH_RATE_HZ)
        z = np.zeros(len(t))
        return t, e + z, n + z, z, heading + z, z, z

    def append(piece):
        nonlocal t_offset
        t, *rest = piece
        pieces.append((t + t_offset, *rest))
        t_offset += len(t) / TRUTH_RATE_HZ

    stage_heading = float(np.mod(90.0 - math.degrees(th0), 360.0))
    append(idle_piece(idle, full_e[0], full_n[0], stage_heading))
    for run_pace in pace:
        in_course = np.clip(full_s, 0.0, L)
        grip = car.grip * _pace_grid(course, in_course, run_pace)
        v = _speed_profile(full_k, np.diff(full_s), grip, car, v_start=0.0, v_end=0.0)
        v[0] = v[-1] = 1e-3
        t_grid = _time_along(full_s, v)
        start = t_offset + _crossing_time(t_grid, full_s, 0.0)
        end = t_offset + _crossing_time(t_grid, full_s, L)
        splits = t_offset + np.array([_crossing_time(t_grid, full_s, d) for d in course.sector_distances])
        laps.append(Lap(start, end, splits))
        append(_sample_path(t_grid, full_s, v, full_s, full_e, full_n, full_th, full_k, lambda x: x))

        stop_heading = float(np.mod(90.0 - math.degrees(th1), 360.0))
        append(idle_piece(idle, full_e[-1], full_n[-1], stop_heading))
        de, dn = full_e[0] - full_e[-1], full_n[0] - full_n[-1]
        dist = math.hypot(de, dn)
        t = np.arange(0.0, dist / return_speed, 1.0 / TRUTH_RATE_HZ)
        frac = t * return_speed / dist
        back_heading = float(np.mod(90.0 - math.degrees(math.atan2(dn, de)), 360.0))
        z = np.zeros(len(t))
        append((t, full_e[-1] + frac * de, full_n[-1] + frac * dn, z + return_speed, z + back_heading, z, z))
        append(idle_piece(idle, full_e[0], full_n[0], stage_heading))

    cols = [np.concatenate([p[i] for p in pieces]) for i in range(7)]
    return Truth(course, utc0, *cols, laps=laps)


def example_track() -> Course:
    """A ~1.4 km rounded-rectangle road course with three sectors."""
    return Course(
        segments=[
            Straight(400), Arc(60, 90), Straight(150), Arc(25, 90),
            Straight(420), Arc(40, 90), Straight(170), Arc(25, 90),
        ],
        closed=True,
        sectors=(1 / 3, 2 / 3),
    )


def example_autocross() -> Course:
    """A ~470 m autocross course: launch, slalom, sweeper, hairpin, finish."""
    slalom = []
    for i in range(6):
        slalom.append(Arc(18, 35 if i % 2 == 0 else -35))
    return Course(
        segments=[
            Straight(60), Arc(30, 45), Arc(30, -45), *slalom,
            Straight(40), Arc(35, -150), Straight(50), Arc(12, 170), Straight(80),
        ],
        closed=False,
        sectors=(0.35, 0.7),
    )
