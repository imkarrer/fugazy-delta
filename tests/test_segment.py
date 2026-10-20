"""Laps and runs from line crossings: what counts, what does not, and why."""

import numpy as np
import pytest

from fugazy_delta.devices import NO_NOISE, racebox_mini
from fugazy_delta.geo import to_latlon
from fugazy_delta.model import Telemetry
from fugazy_delta.segment import (
    Gate,
    Lap,
    LapKind,
    TimingLines,
    best_lap,
    find_crossings,
    find_laps,
    optimal_lap,
)
from fugazy_delta.sim import (
    DEFAULT_ORIGIN,
    DEFAULT_UTC0,
    autocross_session,
    example_autocross,
    example_track,
    track_session,
)

EVEN = [1.0, 1.0, 1.0]
ORIGIN = DEFAULT_ORIGIN
# A start line at the origin, driven through heading north, 10 m wide.
START = Gate(*ORIGIN, heading=0.0, width=10.0)


def gate_at(east, north, heading=0.0, width=10.0):
    lat, lon = to_latlon(east, north, *ORIGIN)
    return Gate(float(lat), float(lon), heading, width)


def drive(points, speed, rate=10.0, t0=DEFAULT_UTC0 + 0.037, report_speed=True):
    """Telemetry for a car driving a polyline of (east, north) at constant speed."""
    pts = np.asarray(points, dtype=float)
    seg = np.hypot(*np.diff(pts, axis=0).T)
    s = np.concatenate([[0.0], np.cumsum(seg)])
    t = np.arange(0.0, s[-1] / speed, 1.0 / rate)
    east = np.interp(t * speed, s, pts[:, 0])
    north = np.interp(t * speed, s, pts[:, 1])
    lat, lon = to_latlon(east, north, *ORIGIN)
    return Telemetry("test", t0 + t, lat, lon, speed=np.full(len(t), speed) if report_speed else None)


def concat(*parts):
    return Telemetry("test", *(np.concatenate([getattr(p, c) for p in parts]) for c in ("time", "lat", "lon")),
                     speed=np.concatenate([p.speed for p in parts]))


def without(tel, start, end):
    keep = (tel.time < start) | (tel.time > end)
    return Telemetry(tel.source, tel.time[keep], tel.lat[keep], tel.lon[keep], speed=tel.speed[keep])


# ---------- single line ----------

def test_crossing_time_is_interpolated_between_fixes():
    tel = drive([(0, -50), (0, 50)], speed=20.0, rate=10.0)
    [c] = find_crossings(tel, TimingLines(START))
    # 50 m at 20 m/s, though no fix falls on the line
    assert c.time == pytest.approx(DEFAULT_UTC0 + 0.037 + 2.5, abs=1e-6)


@pytest.mark.parametrize("east, crossings", [(4.0, 1), (-4.0, 1), (6.0, 0), (-6.0, 0)])
def test_only_passing_through_the_line_counts(east, crossings):
    """Driving past within 5 m of a 10 m line is a crossing; 6 m is not."""
    tel = drive([(east, -50), (east, 50)], speed=20.0)
    assert len(find_crossings(tel, TimingLines(START))) == crossings


def test_crossing_backwards_does_not_count():
    tel = drive([(0, 50), (0, -50)], speed=20.0)
    assert find_crossings(tel, TimingLines(START)) == []


def test_standing_on_the_line_does_not_count():
    """GPS wander moves a parked car back and forth across the line."""
    n = 200
    north = 0.3 * np.sin(np.arange(n))
    lat, lon = to_latlon(np.zeros(n), north, *ORIGIN)
    tel = Telemetry("test", DEFAULT_UTC0 + np.arange(n) / 25.0, lat, lon, speed=np.full(n, 0.1))
    assert find_crossings(tel, TimingLines(START)) == []


def test_speed_comes_from_position_when_the_device_reports_none():
    tel = drive([(0, -50), (0, 50)], speed=20.0, report_speed=False)
    [c] = find_crossings(tel, TimingLines(START))
    assert c.speed == pytest.approx(20.0, rel=0.01)


# ---------- track day ----------

@pytest.fixture(scope="module")
def track():
    return track_session(example_track(), [EVEN] * 5, line_offset=[0, 4, -4, 3, -3], in_lap=0.5)


@pytest.fixture(scope="module")
def track_laps(track):
    return find_laps(racebox_mini(track, NO_NOISE), track.course.timing_lines())


def test_track_out_lap_timed_laps_then_in_lap(track_laps):
    assert [lap.kind for lap in track_laps] == [LapKind.OUT] + [LapKind.TIMED] * 4 + [LapKind.IN]
    assert [lap.counted for lap in track_laps] == [False] + [True] * 4 + [False]


def test_perfect_gps_recovers_lap_and_sector_times(track, track_laps):
    """Interpolation alone, at 25 Hz: well under a millisecond, whatever the line."""
    for lap, truth in zip(track_laps[1:5], track.laps[1:]):
        assert lap.time == pytest.approx(truth.time, abs=1e-3)
        np.testing.assert_allclose(lap.sector_times, truth.sector_times, atol=1e-3)


def test_dropout_across_the_line_flags_both_laps(track):
    tel = racebox_mini(track, NO_NOISE)
    lines = track.course.timing_lines()
    line_at = track.utc0 + track.laps[2].end
    laps = find_laps(without(tel, line_at - 1.0, line_at + 1.0), lines)
    assert [lap.counted for lap in laps] == [False, True, False, False, True, False]
    for lap in laps[2:4]:
        assert lap.issues == ["GPS dropout at the start/finish line"]
    # the estimate still splits the two laps in about the right place;
    # bridging 2 s in a straight line while accelerating is off by ~0.05 s
    assert laps[2].end == pytest.approx(line_at, abs=0.1)


def test_short_dropout_is_bridged(track):
    tel = racebox_mini(track, NO_NOISE)
    line_at = track.utc0 + track.laps[2].end
    laps = find_laps(without(tel, line_at - 0.3, line_at + 0.3), track.course.timing_lines())
    assert all(lap.counted for lap in laps[1:5])
    # straight-line interpolation over 0.6 s while the car accelerates
    assert laps[2].time == pytest.approx(track.laps[2].time, abs=0.01)


def test_dropout_at_a_sector_line_loses_only_that_split(track):
    tel = racebox_mini(track, NO_NOISE)
    split_at = track.utc0 + track.laps[3].sector_splits[0]
    laps = find_laps(without(tel, split_at - 1.0, split_at + 1.0), track.course.timing_lines())
    assert laps[3].counted
    assert laps[3].time == pytest.approx(track.laps[3].time, abs=1e-3)
    assert np.isnan(laps[3].sector_times[:2]).all()
    assert laps[3].sector_times[2] == pytest.approx(track.laps[3].sector_times[2], abs=1e-3)


def test_lap_after_a_stop_is_an_out_lap(track):
    """Stopping on track (a red flag, a spin) makes the next lap an out-lap."""
    tel = racebox_mini(track, NO_NOISE)
    stop = track.utc0 + track.laps[2].end - 2.0
    slow = (tel.time > stop) & (tel.time < stop + 1.0)
    tel.speed[slow] = 0.0
    laps = find_laps(tel, track.course.timing_lines())
    assert laps[3].kind is LapKind.OUT and not laps[3].counted


# ---------- autocross ----------

@pytest.fixture(scope="module")
def autox():
    return autocross_session(example_autocross(), [EVEN, [0.9, 1, 1], [1, 1, 0.9]], line_offset=[0, 2, -2])


def test_staging_and_the_drive_back_are_not_runs(autox):
    runs = find_laps(racebox_mini(autox, NO_NOISE), autox.course.timing_lines())
    assert [r.kind for r in runs] == [LapKind.TIMED] * 3
    for run, truth in zip(runs, autox.laps):
        assert run.time == pytest.approx(truth.time, abs=1e-3)
        np.testing.assert_allclose(run.sector_times, truth.sector_times, atol=1e-3)


def test_recording_that_ends_mid_run_is_a_dnf(autox):
    tel = racebox_mini(autox, NO_NOISE)
    cut = autox.utc0 + autox.laps[2].start + 10.0
    runs = find_laps(tel.slice_time(tel.time[0], cut), autox.course.timing_lines())
    assert [r.kind for r in runs] == [LapKind.TIMED, LapKind.TIMED, LapKind.DNF]
    assert not runs[2].counted
    assert runs[2].end == pytest.approx(cut, abs=0.05)


FINISH = gate_at(0, 200)
SECTOR = gate_at(0, 100)
AUTOX = TimingLines(START, FINISH, (SECTOR,))


def test_start_without_finish_then_a_full_run():
    """Start, turn back before the sector line, return outside the gates, go again."""
    tel = drive([(0, -20), (0, 60), (30, 60), (30, -20), (0, -20), (0, 250)], speed=10.0)
    runs = find_laps(tel, AUTOX)
    assert [r.kind for r in runs] == [LapKind.DNF, LapKind.TIMED]
    assert runs[0].end == runs[1].start
    assert runs[1].time == pytest.approx(20.0, abs=1e-3)
    assert runs[1].sector_times == pytest.approx([10.0, 10.0], abs=1e-3)


def test_crossing_the_start_twice_quickly_is_one_run():
    """Roll over the line, back up behind it, then go: the run starts on the go."""
    tel = drive([(0, -10), (0, 5), (0, -10), (0, 250)], speed=5.0)
    runs = find_laps(tel, AUTOX)
    assert [r.kind for r in runs] == [LapKind.TIMED]
    assert runs[0].time == pytest.approx(40.0, abs=1e-3)


def test_staged_past_the_start_line_starts_when_the_car_moves():
    """GPS error can put a staged car past the line; then there is no crossing."""
    lat, lon = to_latlon(np.zeros(50), np.full(50, 1.5), *ORIGIN)
    parked = Telemetry("test", DEFAULT_UTC0 + np.arange(50) / 10.0, lat, lon, speed=np.zeros(50))
    go = drive([(0, 1.5), (0, 250)], speed=10.0, t0=parked.time[-1] + 0.1)
    runs = find_laps(concat(parked, go), AUTOX)
    assert [r.kind for r in runs] == [LapKind.TIMED]
    assert not runs[0].counted
    assert "past the start line" in runs[0].issues[0]
    assert runs[0].start == pytest.approx(go.time[0], abs=0.1)


def test_no_crossings_no_laps():
    tel = drive([(100, -50), (100, 50)], speed=20.0)
    assert find_laps(tel, TimingLines(START)) == []
    assert find_laps(tel, AUTOX) == []


def lap(start, splits, end, kind=LapKind.TIMED, issues=()):
    return Lap(kind, start, end, np.array(splits, dtype=float), list(issues))


def test_best_and_optimal_use_counted_laps_only():
    laps = [
        lap(0, [10, 20], 30, kind=LapKind.OUT),                  # out-lap: fastest sectors, never counted
        lap(100, [111, 121], 131),                               # 11, 10, 10 = 31
        lap(200, [210, 221], 232),                               # 10, 11, 11 = 32
        lap(300, [309, 318], 327, issues=["GPS dropout"]),       # flagged
    ]
    assert best_lap(laps) == 1
    opt = optimal_lap(laps)
    assert opt.sector_laps == [2, 1, 1]
    assert opt.time == pytest.approx(30.0)


def test_optimal_skips_a_missed_sector_line():
    """A sector line one lap missed (NaN split) leaves the other laps to
    supply both sectors it bounds."""
    laps = [lap(0, [10, np.nan], 30), lap(100, [111, 121], 132)]
    opt = optimal_lap(laps)
    assert opt.sector_laps == [0, 1, 1]
    assert opt.time == pytest.approx(10 + 10 + 11)


def test_optimal_is_nan_when_no_lap_times_a_sector():
    """Leaving a sector out would make the optimal faster than any lap."""
    laps = [lap(0, [10, np.nan], 30), lap(100, [111, np.nan], 132)]
    opt = optimal_lap(laps)
    assert opt.sector_laps == [0, None, None]
    assert np.isnan(opt.time)


def test_no_counted_laps():
    laps = [lap(0, [10, 20], 30, kind=LapKind.OUT)]
    assert best_lap(laps) is None
    assert np.isnan(optimal_lap(laps).time)
