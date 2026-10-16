"""Simulated receivers must be as good, and as bad, as the real ones.

If the simulated GoPro were as accurate as the RaceBox, every analysis test
would pass on GoPro-only data and the app would promise more than a camera
can deliver. These tests pin each profile to its device's stated accuracy.
"""

import numpy as np
import pytest

from fugazy_delta.devices import HERO9_NOISE, NO_NOISE, GnssNoise, racebox_mini
from fugazy_delta.geo import to_local
from fugazy_delta.gpmf import decode_gps5, hero9_payloads
from fugazy_delta.sim import example_track, track_session

SEEDS = range(4)


@pytest.fixture(scope="module")
def truth():
    return track_session(example_track(), [[1, 1, 1]] * 6)


def horizontal_error(truth, telemetry, latency_s=0.0):
    """Metres between each fix and where the car truly was when it was taken."""
    exact = truth.at(telemetry.time - latency_s - truth.utc0)
    east, north = to_local(telemetry.lat, telemetry.lon, *truth.course.origin)
    return np.hypot(east - exact["east"], north - exact["north"])


def gopro_gps(truth, seed, **kwargs):
    return decode_gps5(hero9_payloads(truth, 0.0, truth.t[-1] - 1.0, seed=seed, **kwargs))


@pytest.fixture(scope="module")
def median_errors(truth):
    racebox = [np.median(horizontal_error(truth, racebox_mini(truth, seed=s))) for s in SEEDS]
    hero9 = [
        np.median(horizontal_error(truth, gopro_gps(truth, s), HERO9_NOISE.latency_s))
        for s in SEEDS
    ]
    return float(np.mean(racebox)), float(np.mean(hero9))


def test_hero9_matches_its_rated_2m_cep(median_errors):
    _, hero9 = median_errors
    assert 1.5 < hero9 < 2.6


def test_racebox_is_sub_metre(median_errors):
    racebox, _ = median_errors
    assert racebox < 1.0


def test_hero9_is_much_worse_than_racebox(median_errors):
    racebox, hero9 = median_errors
    assert hero9 > 2.5 * racebox


def test_latency_reports_where_the_car_was(truth):
    late = GnssNoise(0.0, 1.0, 0.0, 0.0, 0.0, 0.0, latency_s=0.2)
    gps = gopro_gps(truth, 0, noise=late)
    assert np.max(horizontal_error(truth, gps, latency_s=0.2)) < 0.01
    assert np.median(horizontal_error(truth, gps)) > 1.0  # stale by ~0.2 s of travel


def test_hero9_error_is_continuous_across_payloads(truth):
    """One receiver, one error series: no jumps where 1 s payloads meet.

    Sample-to-sample wander stays well under a metre; restarting the error
    at each payload would jump by about the full 2.4 m error at most of the
    ~300 boundaries.
    """
    gps = gopro_gps(truth, 0)
    exact = truth.at(gps.time - HERO9_NOISE.latency_s - truth.utc0)
    east, north = to_local(gps.lat, gps.lon, *truth.course.origin)
    step = np.hypot(np.diff(east - exact["east"]), np.diff(north - exact["north"]))
    assert np.max(step) < 1.0


def test_codec_tests_can_still_ask_for_a_perfect_receiver(truth):
    gps = gopro_gps(truth, 0, noise=NO_NOISE)
    assert np.max(horizontal_error(truth, gps)) < 0.05


def test_no_fix_yields_no_positions(truth):
    with pytest.raises(ValueError, match="no GPS5 samples with a fix"):
        decode_gps5(hero9_payloads(truth, 0.0, 5.0, gps_fix=0))
