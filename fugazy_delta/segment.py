"""Split a recording into laps or runs by finding where the car crosses
timing lines.

A timing line (`Gate`) is a short segment across the course: a centre point,
the direction of travel through it, and a width. The car crosses it when the
path between two consecutive fixes passes through the segment in the
direction of travel. The crossing time is interpolated between the two
fixes, so it does not depend on the sample rate; its error is the position
error along the course divided by the car's speed.

What does not count as a crossing:

- passing the line outside its width (a pit lane alongside the start/finish,
  another part of the course nearby)
- crossing it backwards (driving back to the autocross grid)
- crossing it below `min_speed`: a car standing on or near a line, whose GPS
  position wanders back and forth across it
- a crossing inside a GPS dropout longer than `max_gap`. Its time is still
  estimated, so the laps on either side keep their boundaries, but the laps
  it bounds are flagged rather than counted (a sector line in a dropout
  leaves just that split unknown)

A closed course (track day) has one start/finish line: laps run from one
crossing to the next. The part of the recording before the first crossing
is the out-lap, as is a lap that starts from a standstill; the part after
the last crossing is the in-lap. An open course (autocross) has separate
start and finish lines: a run is a start crossing followed by a finish
crossing. Staging, the drive back and anything else between runs is not a
run; a start with no finish before the next start is a DNF.

At an autocross the car launches from a few metres behind the start line,
so it crosses at walking pace and GPS error moves the start time most. When
that error puts the staged car past the line, there is no crossing to find;
the run then starts when the car moves off, and is flagged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import numpy as np

from .geo import to_local
from .model import Telemetry


@dataclass(frozen=True)
class Gate:
    """A timing line across the course.

    heading: direction of travel through the line, compass degrees. The line
    itself runs across it, `width` metres long, centred on (lat, lon).
    """

    lat: float
    lon: float
    heading: float
    width: float = 20.0


@dataclass(frozen=True)
class TimingLines:
    """Where a course is timed. `finish` is None on a closed course, where
    the start line is also the finish line."""

    start: Gate
    finish: Gate | None = None
    sectors: tuple[Gate, ...] = ()

    @property
    def closed(self) -> bool:
        return self.finish is None


class LapKind(Enum):
    TIMED = "timed"
    OUT = "out"
    IN = "in"
    DNF = "dnf"


@dataclass
class Lap:
    """One lap or run, times in UTC seconds.

    `splits` holds the sector-line crossing times, NaN where the line was not
    crossed. `issues` says why a lap is not counted even though it was timed.
    """

    kind: LapKind
    start: float
    end: float
    splits: np.ndarray
    issues: list[str] = field(default_factory=list)

    @property
    def time(self) -> float:
        return self.end - self.start

    @property
    def sector_times(self) -> np.ndarray:
        """Time in each sector, NaN where a sector line was missed."""
        edges = np.concatenate([[self.start], self.splits, [self.end]])
        return np.diff(edges)

    @property
    def counted(self) -> bool:
        """Whether the lap time can be compared with others."""
        return self.kind is LapKind.TIMED and not self.issues


@dataclass(frozen=True)
class Crossing:
    time: float
    gate: int  # 0 start, 1 finish, 2+ sector lines in order
    speed: float
    issue: str = ""  # set when the time is an estimate the lap should not be counted on


START, FINISH, SECTOR0 = 0, 1, 2


def _speed(t: np.ndarray, e: np.ndarray, n: np.ndarray, reported: np.ndarray, window: float = 0.5) -> np.ndarray:
    """Speed per fix: the receiver's own where it gave one, else from position.

    Position-derived speed differences fixes `window` seconds either side,
    since fix-to-fix GPS wander alone looks like a few m/s at 18–25 Hz.
    """
    if np.all(np.isfinite(reported)):
        return reported
    lo = np.searchsorted(t, t - window)
    hi = np.minimum(np.searchsorted(t, t + window, side="right") - 1, len(t) - 1)
    dt = np.maximum(t[hi] - t[lo], 1e-9)
    derived = np.hypot(e[hi] - e[lo], n[hi] - n[lo]) / dt
    return np.where(np.isfinite(reported), reported, derived)


def _crossings(t, e, n, speed, gate: Gate, origin, index: int, name: str, max_gap: float) -> list[Crossing]:
    ce, cn = to_local(gate.lat, gate.lon, *origin)
    h = np.radians(gate.heading)
    ux, uy = np.sin(h), np.cos(h)  # along the direction of travel
    along = (e - ce) * ux + (n - cn) * uy
    across = (e - ce) * uy - (n - cn) * ux
    i = np.flatnonzero((along[:-1] < 0.0) & (along[1:] >= 0.0))
    f = -along[i] / (along[i + 1] - along[i])
    lateral = across[i] + f * (across[i + 1] - across[i])
    keep = np.abs(lateral) <= gate.width / 2
    i, f = i[keep], f[keep]
    times = t[i] + f * (t[i + 1] - t[i])
    v = speed[i] + f * (speed[i + 1] - speed[i])
    gaps = t[i + 1] - t[i]
    return [Crossing(float(a), index, float(b), _DROPOUT.format(name) if g > max_gap else "")
            for a, b, g in zip(times, v, gaps)]


_DROPOUT = "GPS dropout at the {} line"
_PAST_START = "GPS placed the staged car past the start line; start time is when it moved off"


def _launches_past(t, e, n, speed, gate: Gate, origin, min_speed: float) -> list[Crossing]:
    """Moments the car moves off from standing already past `gate` (by GPS)."""
    ce, cn = to_local(gate.lat, gate.lon, *origin)
    h = np.radians(gate.heading)
    along = (e - ce) * np.sin(h) + (n - cn) * np.cos(h)
    across = (e - ce) * np.cos(h) - (n - cn) * np.sin(h)
    i = np.flatnonzero((speed[:-1] < min_speed) & (speed[1:] >= min_speed))
    i = i[(along[i] >= 0.0) & (along[i] <= gate.width / 2) & (np.abs(across[i]) <= gate.width / 2)]
    f = (min_speed - speed[i]) / (speed[i + 1] - speed[i])
    return [Crossing(float(a), START, min_speed, _PAST_START) for a in t[i] + f * (t[i + 1] - t[i])]


def _track(telemetry: Telemetry, origin) -> tuple[np.ndarray, ...]:
    """Time, east, north and speed of every fix that has a position."""
    ok = np.isfinite(telemetry.lat) & np.isfinite(telemetry.lon)
    t = telemetry.time[ok]
    e, n = to_local(telemetry.lat[ok], telemetry.lon[ok], *origin)
    return t, e, n, _speed(t, e, n, telemetry.speed[ok])


def find_crossings(
    telemetry: Telemetry, lines: TimingLines, min_speed: float = 1.0, max_gap: float = 1.0,
) -> list[Crossing]:
    """Every forward crossing of every line at `min_speed` or faster, in time order."""
    origin = (lines.start.lat, lines.start.lon)
    t, e, n, speed = _track(telemetry, origin)
    gates = [(START, lines.start, "start/finish" if lines.closed else "start")]
    if lines.finish is not None:
        gates.append((FINISH, lines.finish, "finish"))
    gates += [(SECTOR0 + k, g, f"sector {k + 2}") for k, g in enumerate(lines.sectors)]
    found = []
    for index, gate, name in gates:
        found += _crossings(t, e, n, speed, gate, origin, index, name, max_gap)
    if lines.finish is not None:
        found += _launches_past(t, e, n, speed, lines.start, origin, min_speed)
    return sorted((c for c in found if c.speed >= min_speed), key=lambda c: c.time)


def _fill_splits(lap: Lap, crossings: list[Crossing], until: float) -> None:
    """The first crossing of each sector line, in order, before `until`."""
    after = lap.start
    for k in range(len(lap.splits)):
        hit = next((c for c in crossings if c.gate == SECTOR0 + k and after < c.time < until), None)
        if hit is None:
            continue
        after = hit.time
        if not hit.issue:
            lap.splits[k] = hit.time


def find_laps(
    telemetry: Telemetry,
    lines: TimingLines,
    min_speed: float = 1.0,
    max_gap: float = 1.0,
    min_lap_time: float = 10.0,
) -> list[Lap]:
    """Laps (closed course) or runs (open course) in one recording.

    min_speed: crossings slower than this (m/s) are ignored.
    max_gap: a crossing between fixes further apart than this (s) is a
        dropout: estimated, and the laps it bounds are not counted.
    min_lap_time: a start/finish crossing this soon after the last one is
        ignored, and a partial out- or in-lap shorter than this is dropped.
    """
    crossings = find_crossings(telemetry, lines, min_speed, max_gap)
    empty = np.full(len(lines.sectors), np.nan)
    if lines.closed:
        t, _, _, speed = _track(telemetry, (lines.start.lat, lines.start.lon))
        laps = _laps(t, speed, crossings, empty, min_speed, min_lap_time)
    else:
        laps = _runs(float(telemetry.time[-1]), crossings, empty, min_lap_time)
    return laps


def _laps(t, speed, crossings, empty, min_speed, min_lap_time) -> list[Lap]:
    line: list[Crossing] = []
    for c in crossings:
        if c.gate == START and (not line or c.time - line[-1].time >= min_lap_time):
            line.append(c)
    if not line:
        return []

    laps = []
    if line[0].time - t[0] >= min_lap_time:
        laps.append(Lap(LapKind.OUT, float(t[0]), line[0].time, empty.copy()))
    for a, b in zip(line, line[1:]):
        before = (t >= a.time - min_lap_time) & (t <= a.time)
        standing = bool(np.any(speed[before] < min_speed))
        lap = Lap(LapKind.OUT if standing else LapKind.TIMED, a.time, b.time, empty.copy())
        lap.issues += [c.issue for c in (a, b) if c.issue]
        laps.append(lap)
    if t[-1] - line[-1].time >= min_lap_time:
        laps.append(Lap(LapKind.IN, line[-1].time, float(t[-1]), empty.copy()))
    for lap in laps:
        _fill_splits(lap, crossings, lap.end)
    return laps


def _runs(t_end, crossings, empty, min_lap_time) -> list[Lap]:
    """Start, then finish, is a run. Start, then start again, is a DNF,
    unless the car has not reached a sector line within `min_lap_time`:
    then it is the same start, re-triggered."""
    runs: list[Lap] = []
    for c in crossings:
        current = runs[-1] if runs and np.isnan(runs[-1].end) else None
        if c.gate == START:
            if current is not None and c.time - current.start < min_lap_time and not any(
                    x.gate >= SECTOR0 and current.start < x.time < c.time for x in crossings):
                runs.pop()
            elif current is not None:
                current.end = c.time
            runs.append(Lap(LapKind.DNF, c.time, np.nan, empty.copy(), [c.issue] if c.issue else []))
        elif c.gate == FINISH and current is not None:
            current.kind, current.end = LapKind.TIMED, c.time
            if c.issue:
                current.issues.append(c.issue)
    if runs and np.isnan(runs[-1].end):
        runs[-1].end = t_end
    for run in runs:
        _fill_splits(run, crossings, run.end)
    return runs
