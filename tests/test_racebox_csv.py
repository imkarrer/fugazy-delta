"""RaceBox CSV: written from the simulator and read back, and a real export."""

from datetime import datetime, timezone

import numpy as np
import pytest

from fugazy_delta.devices import NO_NOISE, racebox_mini
from fugazy_delta.importers import PRECISE, Kind, identify, read
from fugazy_delta.racebox_csv import write_racebox_csv
from fugazy_delta.segment import find_laps
from fugazy_delta.sim import example_track, track_session


@pytest.fixture(scope="module")
def truth():
    return track_session(example_track(), [[1, 1, 1], [1, 1, 1], [0.97, 0.98, 0.99]], in_lap=0.5)


@pytest.fixture(scope="module")
def rb(truth):
    return racebox_mini(truth, NO_NOISE)


@pytest.mark.parametrize("unit", ["km/h", "mph", "m/s"])
def test_round_trip_with_inferred_speed_unit(truth, rb, tmp_path, unit):
    path = tmp_path / "RaceBox Track Session.csv"
    write_racebox_csv(rb, path, unit=unit)
    assert identify(path) is Kind.RACEBOX_CSV
    rec = read(path)
    assert (rec.device, rec.profile, rec.tz_inferred) == ("RaceBox", PRECISE, False)
    assert f"speed unit inferred as {unit}" in rec.warnings[0]
    t = rec.telemetry
    exact = truth.at(t.time - truth.utc0)
    np.testing.assert_allclose(t.time, rb.time, atol=5e-4)
    np.testing.assert_allclose(t.lat, exact["lat"], atol=1e-7)
    np.testing.assert_allclose(t.lon, exact["lon"], atol=1e-7)  # west negative, as the course is
    np.testing.assert_allclose(t.speed, exact["speed"], atol=0.006)
    # GForceX forward-positive, GForceY left-positive, GForceZ minus gravity
    np.testing.assert_allclose(t.accel_long, exact["accel_long"], atol=6e-4)
    np.testing.assert_allclose(t.accel_lat, exact["accel_lat"], atol=6e-4)
    np.testing.assert_allclose(t.accel_vert, 0.0, atol=6e-4)


def test_lap_times_match_truth(truth, rb, tmp_path):
    path = tmp_path / "s.csv"
    write_racebox_csv(rb, path)
    laps = [lap for lap in find_laps(read(path).telemetry, truth.course.timing_lines()) if lap.counted]
    true = [lap.time for lap in truth.laps[1:3]]
    assert [lap.time for lap in laps] == pytest.approx(true, abs=0.005)


def test_unit_in_column_name_and_metadata_block(rb, tmp_path):
    path = tmp_path / "s.csv"
    write_racebox_csv(rb, path, unit="m/s", unit_in_name=True, metadata=True)
    assert path.read_text().startswith("Format,RaceBox")
    rec = read(path)
    assert rec.warnings == []
    np.testing.assert_allclose(rec.telemetry.speed, rb.speed, atol=0.006)


def test_lap_counter_becomes_device_lap_markers(truth, rb, tmp_path):
    starts = [truth.utc0 + lap.start for lap in truth.laps[1:]]
    path = tmp_path / "s.csv"
    write_racebox_csv(rb, path, laps=starts)
    assert read(path).laps == pytest.approx(starts, abs=0.04)


def test_speed_unit_falls_back_to_kmh_when_parked(rb, tmp_path):
    from fugazy_delta.model import Telemetry
    still = Telemetry("racebox", rb.time[:50], np.full(50, rb.lat[0]), np.full(50, rb.lon[0]), speed=np.zeros(50))
    path = tmp_path / "s.csv"
    write_racebox_csv(still, path)
    rec = read(path)
    assert "km/h, RaceBox's default, was assumed" in rec.warnings[0]


@pytest.mark.fixtures
def test_real_racebox_mini_export(real_dir):
    """RaceBox Mini, Lake Elsinore, 21 June 2026 (RacePlex sample, GPL-3.0)."""
    rec = read(real_dir / "racebox" / "session.csv")
    assert rec.kind is Kind.RACEBOX_CSV
    assert "speed unit inferred as km/h" in rec.warnings[0]
    t = rec.telemetry
    assert datetime.fromtimestamp(t.time[0], timezone.utc) == datetime(2026, 6, 21, 20, 43, 33, 160000, tzinfo=timezone.utc)
    assert t.sample_rate == pytest.approx(25.0, rel=0.01)
    assert t.lat[0] == pytest.approx(33.6528145) and t.lon[0] == pytest.approx(-117.3042013)
    assert np.nanmedian(t.accel_vert) == pytest.approx(0.0, abs=0.05)
    # the axes match our model: long tracks the change of speed, lat the turn
    moving = t.speed > 5
    smooth = lambda a: np.convolve(a, np.ones(11) / 11, "same")  # noqa: E731
    dv = smooth(np.gradient(t.speed, t.time))
    yaw = np.gradient(np.unwrap(np.radians(np.nan_to_num(np.degrees(np.arctan2(
        np.gradient(t.lon, t.time), np.gradient(t.lat, t.time)))))), t.time)
    assert np.corrcoef(t.accel_long[moving], dv[moving])[0, 1] > 0.9
    assert np.corrcoef(t.accel_lat[moving], smooth(-t.speed * yaw)[moving])[0, 1] > 0.9
    assert [x - t.time[0] for x in rec.laps] == pytest.approx([42.92], abs=0.05)
