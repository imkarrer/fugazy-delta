"""The published error table (`fugazy_delta/accuracy_table.json`) is current
and says what the physics says it should.

The table is measured by `python -m fugazy_delta.accuracy`: the virtual car,
recorded by each simulated receiver over many seeds, split into laps by
`find_laps`, compared with the simulator's ground truth.

Staleness check: the table also records the same statistics over its first
`CHECK_SEEDS` seeds alone. Every seed fixes its errors, so re-measuring
those seeds with unchanged code reproduces them exactly. When a change to
the simulator, a noise model or the lap finder moves them, it moves the
full table by about as much, so the test fails once any statistic has moved
by more than the table's own stated uncertainty: twice its bootstrap
standard error, about the half-width of its 95% interval.

The physics, for reading the numbers: a crossing time's error is the
receiver's position error along the course divided by the car's speed at
the line. A track's start/finish line is on the main straight (~40 m/s
here); an autocross start line is crossed at walking pace (~4.5 m/s, three
metres after launch), so the same receiver times an autocross run far less
precisely than a lap.
"""

import math

import numpy as np
import pytest

from fugazy_delta import accuracy
from fugazy_delta.accuracy import BIAS_FLOOR_S, EVENTS, METHODS, QUANTITIES, Error
from fugazy_delta.devices import NO_NOISE, racebox_mini
from fugazy_delta.segment import LapKind, find_laps

STATED = 2.0  # standard errors: about the half-width of the published 95% interval


def committed(event, method, quantity) -> Error:
    return accuracy.error(event, method, quantity)


def test_table_was_made_with_the_current_settings():
    """Noise models, rates, laps per session, pace range and courses."""
    table = accuracy.table()
    assert table["model"] == accuracy.model(), "run python -m fugazy_delta.accuracy"
    assert table["seeds"] == accuracy.SEEDS
    assert table["check"]["seeds"] == accuracy.CHECK_SEEDS
    assert set(table["results"]) == set(EVENTS)
    for event in EVENTS:
        assert set(table["results"][event]) == set(METHODS)


@pytest.fixture(scope="module")
def remeasured():
    return {
        (event, method): accuracy.fingerprint(accuracy.measure(event, method, range(accuracy.CHECK_SEEDS)))
        for event in EVENTS for method in METHODS
    }


@pytest.mark.parametrize("event", EVENTS)
@pytest.mark.parametrize("method", METHODS)
def test_committed_numbers_have_not_drifted(remeasured, event, method):
    fresh = remeasured[(event, method)]
    check = accuracy.table()["check"]["results"][event][method]
    stale = []
    for q in QUANTITIES:
        published = committed(event, method, q)
        for stat in ("bias", "rms", "p95"):
            moved = fresh[q][stat] - check[q][stat]
            allowed = STATED * getattr(published, f"{stat}_se") + 1e-6
            if abs(moved) > allowed:
                stale.append(f"{q} {stat} moved {moved:+.4f} s; the table states ±{allowed:.4f} s")
    p = accuracy.flagged_rate(event, method)
    allowed = STATED * math.sqrt(p * (1 - p) / (accuracy.SEEDS * accuracy.LAPS)) + 1e-6
    if abs(fresh["flagged_rate"] - check["flagged_rate"]) > allowed:
        stale.append(f"flagged rate moved from {check['flagged_rate']} to {fresh['flagged_rate']}")
    assert not stale, "accuracy_table.json is stale; run python -m fugazy_delta.accuracy\n" + "\n".join(stale)


def test_every_quantity_has_enough_samples():
    for event in EVENTS:
        for method in METHODS:
            for q in QUANTITIES:
                e = committed(event, method, q)
                assert e.seeds >= 0.95 * accuracy.SEEDS
                assert e.n >= e.seeds
                # the interval is narrow next to the error it qualifies
                assert e.bias_ci[1] - e.bias_ci[0] < 0.25 * e.rms


@pytest.mark.parametrize("event", EVENTS)
@pytest.mark.parametrize("method", METHODS)
def test_lap_and_run_times_are_unbiased(event, method):
    """Both ends of a lap move with the same slowly wandering error, so it
    does not push lap times one way."""
    assert abs(committed(event, method, "lap").bias) < BIAS_FLOOR_S
    assert not committed(event, method, "lap").biased


@pytest.mark.parametrize("event", EVENTS)
@pytest.mark.parametrize("method", METHODS)
def test_optimal_is_biased_fast(event, method):
    """Taking the best of noisy sectors picks the errors that flatter."""
    e = committed(event, method, "optimal")
    assert e.bias_ci[1] < 0
    assert e.biased
    assert "too fast" in e.summary()
    # and the best lap, one pick from noisy laps, less so
    assert committed(event, method, "best").bias > e.bias


def test_racebox_lap_times():
    """0.5 m per axis at ~40 m/s: about 0.02 s RMS."""
    e = committed("track", "racebox", "lap")
    assert e.rms < 0.025
    assert e.max < 0.075


def test_gopro_lap_times():
    """1.7 m per axis at ~40 m/s: about 0.06 s RMS."""
    e = committed("track", "gopro", "lap")
    assert e.rms < 0.075
    assert e.p95 < 0.15
    assert e.max < 0.3


def test_racebox_run_times():
    """At a 4.5 m/s start even the RaceBox is only good to about 0.1 s RMS."""
    e = committed("autocross", "racebox", "lap")
    assert e.rms < 0.15
    assert e.max < 0.6
    assert accuracy.flagged_rate("autocross", "racebox") == 0


def test_gopro_run_times():
    """At a 4.5 m/s start the GoPro is good to about 0.4 s RMS, and now and
    then its error puts the staged car past the line, which flags the run."""
    e = committed("autocross", "gopro", "lap")
    assert e.rms < 0.45
    assert e.p95 < 1.0
    assert 0 < accuracy.flagged_rate("autocross", "gopro") < 0.15


def test_autocross_first_sector_carries_the_slow_start():
    for method in METHODS:
        s1, s3 = committed("autocross", method, "sector_1"), committed("autocross", method, "sector_3")
        assert s1.rms > 2 * s3.rms


def test_gopro_is_much_worse_than_racebox():
    for event in EVENTS:
        for q in QUANTITIES:
            assert committed(event, "gopro", q).rms > 2.0 * committed(event, "racebox", q).rms, (event, q)


@pytest.mark.parametrize("event", EVENTS)
def test_perfect_gps_times_to_the_millisecond(event):
    """With no position error, interpolating between 25 Hz fixes is exact,
    so every error in the table comes from the receivers' noise."""
    truth = accuracy.session(event, 0)
    expected = truth.laps[1:] if truth.course.closed else truth.laps
    found = [lap for lap in find_laps(racebox_mini(truth, NO_NOISE), truth.course.timing_lines())
             if lap.kind is LapKind.TIMED]
    assert len(found) == len(expected)
    for lap, true in zip(found, expected):
        assert lap.counted
        assert abs(lap.time - true.time) < 0.001
        assert np.max(np.abs(lap.sector_times - true.sector_times)) < 0.001


def test_summary_wording():
    biased = Error(n=100, seeds=100, bias=-0.3, bias_ci=(-0.34, -0.26), bias_se=0.02,
                   rms=0.35, rms_ci=(0.3, 0.4), rms_se=0.02, p95=0.45, p95_ci=(0.4, 0.5), p95_se=0.02, max=0.6)
    assert biased.summary() == "typically 0.30 s too fast (bias −0.30 ± 0.04 s), 95% within ±0.45 s"
    tiny = Error(n=100, seeds=100, bias=0.004, bias_ci=(0.001, 0.007), bias_se=0.0015,
                 rms=0.06, rms_ci=(0.05, 0.07), rms_se=0.005, p95=0.11, p95_ci=(0.1, 0.12), p95_se=0.005, max=0.2)
    assert not tiny.biased  # real, but below the 0.01 s the app shows
    assert tiny.summary() == "no measurable bias, 95% within ±0.11 s"
    assert accuracy.signed(-0.0001) == "0.000"
    assert accuracy.signed(0.034) == "+0.034"
