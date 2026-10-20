"""How close detected lap and run times come to the truth, per setup.

These pin the accuracy the app claims (PLAN.md's setup table and the
reasons in `session.py`) to a measurement: the virtual car, sampled by each
simulated receiver over several seeds, segmented by `find_laps`, compared
with the simulator's ground truth.

The error is the receiver's position error along the course divided by the
car's speed at the line. A track's start/finish line is on the main
straight (~40 m/s here); an autocross start line is crossed at walking pace
(~4.5 m/s, three metres after launch), so the same receiver times an
autocross run far less precisely than a lap.
"""

import numpy as np
import pytest

from fugazy_delta.devices import NO_NOISE, racebox_mini
from fugazy_delta.gpmf import decode_gps5, hero9_payloads
from fugazy_delta.segment import LapKind, find_laps
from fugazy_delta.session import GOPRO_LAP_ERROR_S, GOPRO_RUN_ERROR_S
from fugazy_delta.sim import autocross_session, example_autocross, example_track, track_session

SEEDS = range(8)
EVEN = [1.0, 1.0, 1.0]


@pytest.fixture(scope="module")
def track():
    return track_session(example_track(), [EVEN] * 6, in_lap=0.5)


@pytest.fixture(scope="module")
def autox():
    return autocross_session(example_autocross(), [EVEN] * 6)


def receiver(truth, device, seed):
    if device == "perfect":
        return racebox_mini(truth, NO_NOISE, seed)
    if device == "racebox":
        return racebox_mini(truth, seed=seed)
    return decode_gps5(hero9_payloads(truth, 0.0, truth.t[-1] - 1.0, seed=seed))


def measure(truth, device):
    """Errors of the counted laps or runs, and how many were timed but flagged."""
    expected = truth.laps[1:] if truth.course.closed else truth.laps  # track lap 0 is the out-lap
    errors, flagged = [], 0
    for seed in SEEDS:
        found = [lap for lap in find_laps(receiver(truth, device, seed), truth.course.timing_lines())
                 if lap.kind is LapKind.TIMED]
        assert len(found) == len(expected), f"{device} seed {seed}: {len(found)} laps"
        for lap, true in zip(found, expected):
            if lap.counted:
                errors.append(lap.time - true.time)
            else:
                flagged += 1
    return np.abs(errors), flagged / (len(expected) * len(SEEDS))


@pytest.fixture(scope="module")
def errors(track, autox):
    return {
        (event, device): measure(truth, device)
        for event, truth in (("track", track), ("autocross", autox))
        for device in ("perfect", "racebox", "gopro")
    }


@pytest.mark.parametrize("event", ["track", "autocross"])
def test_perfect_gps_times_to_the_millisecond(errors, event):
    """With no position error, interpolating between 25 Hz fixes is exact."""
    err, flagged = errors[(event, "perfect")]
    assert err.max() < 0.001
    assert flagged == 0


def test_racebox_lap_times(errors):
    """0.5 m per axis at ~40 m/s: about 0.02 s RMS. The bound is ~4 sigma."""
    err, flagged = errors[("track", "racebox")]
    assert np.sqrt(np.mean(err**2)) < 0.025
    assert err.max() < 0.075
    assert flagged == 0


def test_gopro_lap_times(errors):
    """1.7 m per axis at ~40 m/s: about 0.06 s RMS. Most laps fall within
    ±0.1 s, but not all; 19 in 20 fall within the claimed figure."""
    err, flagged = errors[("track", "gopro")]
    assert np.sqrt(np.mean(err**2)) < 0.075
    assert np.mean(err <= 0.1) >= 0.75
    assert np.percentile(err, 95) <= GOPRO_LAP_ERROR_S
    assert err.max() < 0.2
    assert flagged == 0


def test_racebox_run_times(errors):
    """At a 4.5 m/s start even the RaceBox is only good to about 0.1 s RMS."""
    err, flagged = errors[("autocross", "racebox")]
    assert np.sqrt(np.mean(err**2)) < 0.15
    assert err.max() < 0.5
    assert flagged == 0


def test_gopro_run_times(errors):
    """At a 4.5 m/s start the GoPro is good to about 0.4 s RMS, and now and
    then its error puts the staged car past the line, which flags the run."""
    err, flagged = errors[("autocross", "gopro")]
    assert np.sqrt(np.mean(err**2)) < 0.45
    assert np.percentile(err, 95) <= GOPRO_RUN_ERROR_S
    assert err.max() < 1.2
    assert flagged < 0.15


def test_gopro_is_much_worse_than_racebox(errors):
    for event in ("track", "autocross"):
        racebox = np.sqrt(np.mean(errors[(event, "racebox")][0] ** 2))
        gopro = np.sqrt(np.mean(errors[(event, "gopro")][0] ** 2))
        assert gopro > 2.0 * racebox
