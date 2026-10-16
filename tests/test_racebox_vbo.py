from datetime import datetime, timezone

import numpy as np
import pytest

from fugazy_delta.devices import NO_NOISE, GnssNoise, racebox_mini
from fugazy_delta.fixtures import racebox_vbo
from fugazy_delta.geo import to_local
from fugazy_delta.sim import example_autocross, autocross_session
from fugazy_delta.vbo import read_vbo, write_vbo


@pytest.fixture(scope="module")
def truth():
    return autocross_session(example_autocross(), [[1, 1, 1]])


def test_racebox_samples_on_utc_epochs_at_25hz(truth):
    rb = racebox_mini(truth, NO_NOISE)
    assert rb.sample_rate == pytest.approx(25.0)
    np.testing.assert_allclose(np.mod(rb.time * 25, 1.0), 0.0, atol=1e-6)


def test_noise_free_racebox_matches_truth(truth):
    rb = racebox_mini(truth, NO_NOISE)
    exact = truth.at(rb.time - truth.utc0)
    np.testing.assert_allclose(rb.lat, exact["lat"], atol=1e-9)
    np.testing.assert_allclose(rb.speed, exact["speed"], atol=1e-9)


def test_position_noise_has_requested_size(truth):
    rb = racebox_mini(truth, GnssNoise(position_sigma_m=0.5), seed=3)
    exact = truth.at(rb.time - truth.utc0)
    e, n = to_local(rb.lat, rb.lon, *truth.course.origin)
    err = np.hypot(e - exact["east"], n - exact["north"])
    assert 0.3 < np.sqrt(np.mean(err**2)) / np.sqrt(2) < 0.8


def test_noise_is_reproducible(truth):
    a = racebox_mini(truth, seed=7)
    b = racebox_mini(truth, seed=7)
    np.testing.assert_array_equal(a.lat, b.lat)


def test_vbo_round_trip(truth, tmp_path):
    rb = racebox_mini(truth, seed=1)
    path = tmp_path / "session.vbo"
    write_vbo(rb, path)
    back = read_vbo(path)
    np.testing.assert_allclose(back.time, rb.time, atol=1e-3)
    np.testing.assert_allclose(back.lat, rb.lat, atol=1e-8)
    np.testing.assert_allclose(back.lon, rb.lon, atol=1e-8)
    np.testing.assert_allclose(back.speed, rb.speed, atol=0.001 / 3.6 + 1e-9)
    np.testing.assert_allclose(back.accel_lat, rb.accel_lat, atol=0.0005 + 1e-9)


def test_racebox_fixture_writes_readable_vbo(truth, tmp_path):
    path = tmp_path / "rb.vbo"
    racebox_vbo(truth, path)
    assert read_vbo(path).sample_rate == pytest.approx(25.0)


VBO_SAMPLE = """File created on 13/06/2026 @ 23:59:59

[header]
satellites
time
latitude
longitude
velocity kmh

[column names]
sats time lat long velocity heading height LatAcc LongAcc

[data]
011 235959.96 +02688.67800 +05489.91000 036.000 090.00 +00250.00 +0.50 -0.20
011 000000.00 +02688.67810 +05489.90000 036.000 090.00 +00250.00 +0.51 -0.21
"""


def test_vbo_conventions_minutes_west_positive_and_midnight(tmp_path):
    path = tmp_path / "sample.vbo"
    path.write_text(VBO_SAMPLE)
    t = read_vbo(path)
    assert t.lat[0] == pytest.approx(44.8113)
    assert t.lon[0] == pytest.approx(-91.4985)  # positive minutes = west
    assert t.speed[0] == pytest.approx(10.0)  # 36 km/h
    assert t.accel_lat[0] == pytest.approx(0.5) and t.accel_long[0] == pytest.approx(-0.2)
    midnight = datetime(2026, 6, 14, tzinfo=timezone.utc).timestamp()
    assert t.time[1] == pytest.approx(midnight)
    assert t.time[1] - t.time[0] == pytest.approx(0.04)


def test_vbo_without_position_columns_is_rejected(tmp_path):
    path = tmp_path / "bad.vbo"
    path.write_text("File created on 13/06/2026\n[column names]\nsats time velocity\n[data]\n011 120000.00 010.0\n")
    with pytest.raises(ValueError, match="lat"):
        read_vbo(path)
