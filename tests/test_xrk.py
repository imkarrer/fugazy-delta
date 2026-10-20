"""AiM XRK / XRZ: written from the simulator and read back, real AiM files,
and libxrk as an independent reference when it is installed."""

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from fugazy_delta import xrk
from fugazy_delta.devices import NO_NOISE, sample_gnss
from fugazy_delta.importers import PRECISE, Kind, identify, read
from fugazy_delta.segment import find_laps
from fugazy_delta.sim import example_track, track_session


@pytest.fixture(scope="module")
def truth():
    return track_session(example_track(), [[1, 1, 1], [1, 1, 1], [0.97, 0.98, 0.99]], in_lap=0.5)


@pytest.fixture(scope="module")
def fixes(truth):
    return sample_gnss(truth, 25.0, NO_NOISE)


def _laps(truth):
    return [(truth.utc0 + lap.start, truth.utc0 + lap.end) for lap in truth.laps]


@pytest.mark.parametrize("compress,name", [(False, "session.xrk"), (True, "session.xrz")])
def test_round_trip(truth, fixes, tmp_path, compress, name):
    path = tmp_path / name
    xrk.write_xrk(fixes, path, laps=_laps(truth), compress=compress)
    assert identify(path) is Kind.AIM_XRK
    rec = read(path)
    assert (rec.device, rec.profile, rec.tz_inferred) == ("AiM", PRECISE, False)
    t = rec.telemetry
    exact = truth.at(t.time - truth.utc0)
    np.testing.assert_allclose(t.time, fixes.time, atol=1e-6)  # UTC from GPS week and time of week
    np.testing.assert_allclose(t.lat, exact["lat"], atol=2e-7)  # ECEF is in cm
    np.testing.assert_allclose(t.lon, exact["lon"], atol=2e-7)
    np.testing.assert_allclose(t.alt, fixes.alt, atol=0.02)
    np.testing.assert_allclose(t.speed, exact["speed"], atol=0.02)
    moving = t.speed > 1
    diff = (t.heading[moving] - exact["heading"][moving] + 180) % 360 - 180
    assert np.abs(diff).max() < 0.2
    found = [lap for lap in find_laps(t, truth.course.timing_lines()) if lap.counted]
    assert [lap.time for lap in found] == pytest.approx([lap.time for lap in truth.laps[1:3]], abs=0.005)
    assert rec.laps == pytest.approx([s for s, _ in _laps(truth)], abs=0.002)


def test_leap_seconds_follow_the_date():
    assert xrk.leap_seconds(datetime(2016, 1, 23, tzinfo=timezone.utc).timestamp()) == 17
    assert xrk.leap_seconds(datetime(2026, 5, 23, tzinfo=timezone.utc).timestamp()) == 18


def test_old_session_uses_the_leap_seconds_of_its_day(tmp_path):
    utc0 = datetime(2016, 1, 23, 11, 0, tzinfo=timezone.utc).timestamp()
    t = sample_gnss(track_session(example_track(), [[1, 1, 1]], utc0=utc0), 10.0, NO_NOISE)
    path = tmp_path / "old.xrk"
    xrk.write_xrk(t, path)
    np.testing.assert_allclose(read(path).telemetry.time, t.time, atol=1e-6)


def test_ecef_round_trip():
    lat, lon, alt = np.array([44.81, -33.9, 89.0]), np.array([-91.5, 151.2, 0.0]), np.array([250.0, 10.0, -5.0])
    back = xrk.ecef_to_lla(*xrk.lla_to_ecef(lat, lon, alt))
    np.testing.assert_allclose(back[0], lat, atol=1e-9)
    np.testing.assert_allclose(back[1], lon, atol=1e-9)
    np.testing.assert_allclose(back[2], alt, atol=1e-3)


def test_corrupt_frames_are_skipped(fixes, tmp_path):
    good = xrk.frame("GPS", xrk.gps_records(fixes.slice_time(fixes.time[0], fixes.time[9]), fixes.time[0]))
    bad = bytearray(good)
    bad[20] ^= 0xFF  # checksum no longer matches
    path = tmp_path / "x.xrk"
    path.write_bytes(xrk.frame("CNF", b"") + bytes(bad) + good)
    assert len(read(path).telemetry) == 10


# ---------- real AiM files (fetched by scripts/fetch-fixtures.sh) ----------

REAL = {
    # file: (logger's local start, its zone's UTC offset, rate, lat, lon)
    "adria-kart.xrk": (datetime(2016, 1, 23, 12, 9, 4), 1, 8.0, 45.045, 12.149),
    "tsukuba.xrz": (datetime(2026, 5, 23, 13, 19, 33), 9, 25.0, 36.150, 139.920),
}


@pytest.mark.fixtures
@pytest.mark.parametrize("name", REAL)
def test_real_aim_files(real_dir, name):
    local, offset, rate, lat, lon = REAL[name]
    rec = read(real_dir / "aim" / name)
    t = rec.telemetry
    start = datetime.fromtimestamp(t.time[0], timezone.utc).replace(tzinfo=None)
    # the first fix is within minutes of the logger's own local start time
    assert abs(start - (local - timedelta(hours=offset))) < timedelta(minutes=5)
    assert t.sample_rate == pytest.approx(rate, rel=0.02)
    assert t.lat[0] == pytest.approx(lat, abs=0.01) and t.lon[0] == pytest.approx(lon, abs=0.01)
    assert len(rec.laps) >= 3


@pytest.mark.fixtures
@pytest.mark.parametrize("name", REAL)
def test_real_aim_files_agree_with_libxrk(real_dir, name):
    libxrk = pytest.importorskip("libxrk")
    path = real_dir / "aim" / name
    ours = read(path)
    ref = libxrk.aim_xrk(str(path))
    lat = ref.channels["GPS Latitude"].column("GPS Latitude").to_numpy()
    lon = ref.channels["GPS Longitude"].column("GPS Longitude").to_numpy()
    # libxrk keeps records without a fix; every fix we keep is among its positions
    theirs = set(zip(np.round(lat, 7), np.round(lon, 7)))
    mine = list(zip(np.round(ours.telemetry.lat, 7), np.round(ours.telemetry.lon, 7)))
    assert sum(p in theirs for p in mine) >= 0.99 * len(mine)
    # lap lengths agree to within the logger's GPS timecode jitter
    laps = ref.laps.to_pydict()
    durations = (np.array(laps["end_time"]) - np.array(laps["start_time"])) / 1000.0
    ours_durations = np.diff(ours.laps)
    k = min(len(ours_durations), len(durations)) - 1
    np.testing.assert_allclose(ours_durations[1:k], durations[1:k], atol=0.1)
