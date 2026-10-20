import numpy as np
import pytest

from fugazy_delta.sim import (
    Arc,
    Car,
    Course,
    Straight,
    autocross_session,
    example_autocross,
    example_track,
    track_session,
)

EVEN = [1.0, 1.0, 1.0]


@pytest.fixture(scope="module")
def track():
    # lap 0 out-lap, then: baseline, slow S1, slow S2, slow S3
    pace = [EVEN, EVEN, [0.85, 1, 1], [1, 0.85, 1], [1, 1, 0.85]]
    return track_session(example_track(), pace)


@pytest.fixture(scope="module")
def autox():
    return autocross_session(example_autocross(), [EVEN, [0.9, 1, 1], [1, 1, 0.9]])


def test_course_that_does_not_close_is_rejected():
    with pytest.raises(ValueError, match="does not close"):
        Course([Straight(100), Arc(20, 180), Straight(90), Arc(20, 180)], closed=True)


def test_course_length_is_exact():
    c = Course([Straight(100.1), Arc(20, 90)], closed=False)
    assert c.length == pytest.approx(100.1 + 20 * np.pi / 2)


def test_one_lap_per_pace_entry(track):
    assert len(track.laps) == 5


def test_out_lap_is_slowest(track):
    assert track.lap_times[0] > track.lap_times[1:].max()


def test_identical_pace_gives_identical_laps():
    t = track_session(example_track(), [EVEN, EVEN, EVEN, EVEN])
    # the flying laps (after the out-lap) are all the same car on the same grip
    assert np.ptp(t.lap_times[2:]) < 1e-6


def test_slower_sector_costs_time_mainly_in_that_sector(track):
    base = track.laps[1].sector_times
    for lap, slow in ((2, 0), (3, 1), (4, 2)):
        loss = track.laps[lap].sector_times - base
        assert loss[slow] > 0.1
        assert loss[slow] == loss.max()


def test_optimal_lap_is_sum_of_best_sectors(track):
    flying = track.laps[1:]
    best = np.min([lap.sector_times for lap in flying], axis=0)
    # optimal over flying laps can be no slower than the best real lap
    assert best.sum() <= min(lap.time for lap in flying) + 1e-9
    assert track.optimal_time <= best.sum() + 1e-9


def test_speed_respects_grip_and_power(track):
    car = Car()
    # resampling onto the time grid blends curvature across arc entries,
    # which overshoots the grip limit by a fraction of a percent
    assert np.abs(track.accel_lat).max() <= car.grip * 1.01
    assert track.accel_long[50:-50].max() <= car.drive + 0.01
    assert track.speed.max() <= car.top_speed + 1e-9


def test_distance_travelled_matches_course_length(track):
    L = track.course.length
    for lap in track.laps[1:]:
        m = (track.t >= lap.start) & (track.t < lap.end)
        travelled = np.sum(track.speed[m]) / 200.0
        assert travelled == pytest.approx(L, rel=0.005)


def test_lap_starts_on_the_start_line(track):
    for lap in track.laps:
        pos = track.at(np.array([lap.start]))
        assert np.hypot(pos["east"][0], pos["north"][0]) < 0.5


def test_autocross_runs_are_rolling_starts(autox):
    for run in autox.laps:
        assert autox.at(np.array([run.start]))["speed"][0] > 2.0


def test_autocross_car_stops_between_runs(autox):
    for prev, nxt in zip(autox.laps, autox.laps[1:]):
        gap = (autox.t > prev.end) & (autox.t < nxt.start)
        assert autox.speed[gap].min() == 0.0


def test_autocross_slower_first_sector_is_slower_run(autox):
    assert autox.laps[1].time > autox.laps[0].time
    assert autox.laps[1].sector_times[0] > autox.laps[0].sector_times[0]


def test_truth_telemetry_is_absolute_utc(autox):
    tel = autox.telemetry(rate_hz=10)
    assert tel.time[0] == autox.utc0
    assert tel.sample_rate == pytest.approx(10.0)


def test_line_offset_moves_the_car_sideways_without_changing_times():
    base = track_session(example_track(), [EVEN, EVEN])
    wide = track_session(example_track(), [EVEN, EVEN], line_offset=[0.0, 5.0])
    np.testing.assert_allclose(wide.lap_times, base.lap_times)
    lap = base.laps[1]
    at = lambda truth, t: np.array([truth.at(np.array([t]))[k][0] for k in ("east", "north")])
    # on the centre line at the start/finish, 5 m off it half-way round
    assert np.linalg.norm(at(wide, lap.start) - at(base, lap.start)) < 0.01
    assert np.linalg.norm(at(wide, lap.start + lap.time / 2) - at(base, lap.start + lap.time / 2)) == pytest.approx(5.0, abs=0.5)


def test_line_offset_needs_one_entry_per_lap():
    with pytest.raises(ValueError, match="line_offset"):
        track_session(example_track(), [EVEN, EVEN], line_offset=[1.0])


def test_in_lap_stops_short_of_the_line():
    t = track_session(example_track(), [EVEN, EVEN], in_lap=0.5)
    assert len(t.laps) == 2
    assert t.speed[-1] < 0.1
    assert t.t[-1] > t.laps[-1].end + 10.0
    end = t.at(np.array([t.t[-1]]))
    start = t.at(np.array([0.0]))
    assert np.hypot(end["east"][0] - start["east"][0], end["north"][0] - start["north"][0]) > 100.0


def test_autocross_line_offset_keeps_run_times():
    base = autocross_session(example_autocross(), [EVEN])
    wide = autocross_session(example_autocross(), [EVEN], line_offset=[-3.0])
    np.testing.assert_allclose(wide.lap_times, base.lap_times)
