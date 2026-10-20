"""GPX and NMEA 0183: written from the simulator and read back, and real
files: a RaceBox GPX against the CSV of the same session, and GPSBabel's
NMEA track against its own GPX conversion."""

import numpy as np
import pytest

from fugazy_delta.devices import NO_NOISE, sample_gnss
from fugazy_delta.gpx import write_gpx
from fugazy_delta.importers import COARSE, PRECISE, Kind, identify, read
from fugazy_delta.nmea import checksum, write_nmea
from fugazy_delta.segment import find_laps
from fugazy_delta.sim import example_track, track_session


@pytest.fixture(scope="module")
def truth():
    return track_session(example_track(), [[1, 1, 1], [1, 1, 1], [0.97, 0.98, 0.99]], in_lap=0.5)


def _counted(t, truth):
    return [lap.time for lap in find_laps(t, truth.course.timing_lines()) if lap.counted]


@pytest.mark.parametrize("rate", [25.0, 1.0])
def test_gpx_11_round_trip_derives_speed(truth, tmp_path, rate):
    fixes = sample_gnss(truth, rate, NO_NOISE)
    path = tmp_path / "track.gpx"
    write_gpx(fixes, path, creator="RaceChrono Pro", segments=3)
    assert identify(path) is Kind.GPX
    rec = read(path)
    assert (rec.device, rec.profile) == ("RaceChrono", COARSE)
    t = rec.telemetry
    np.testing.assert_allclose(t.time, fixes.time, atol=5e-4)
    np.testing.assert_allclose(t.lat, fixes.lat, atol=1e-8)
    np.testing.assert_allclose(t.lon, fixes.lon, atol=1e-8)
    assert any("GPX has no speed" in w for w in rec.warnings)
    # speed from positions: close to the receiver's at 25 Hz, rough at 1 Hz
    moving = fixes.speed > 5
    err = np.abs(t.speed - fixes.speed)[moving]
    assert np.median(err) < (0.05 if rate == 25 else 1.0)
    assert _counted(t, truth) == pytest.approx([lap.time for lap in truth.laps[1:3]], abs=0.01 if rate == 25 else 0.06)
    assert any("about 1 Hz" in w for w in rec.warnings) == (rate < 5)


def test_gpx_10_speed_and_course_are_used(truth, tmp_path):
    fixes = sample_gnss(truth, 10.0, NO_NOISE)
    path = tmp_path / "track.gpx"
    write_gpx(fixes, path, creator="GPSBabel", version="1.0")
    rec = read(path)
    assert rec.device == "GPX file"
    assert not any("no speed" in w for w in rec.warnings)
    np.testing.assert_allclose(rec.telemetry.speed, fixes.speed, atol=5e-4)
    np.testing.assert_allclose(rec.telemetry.heading, fixes.heading, atol=0.005)


def test_racebox_gpx_is_racebox(truth, tmp_path):
    path = tmp_path / "track.gpx"
    write_gpx(sample_gnss(truth, 25.0, NO_NOISE), path, creator="RaceBox")
    rec = read(path)
    assert (rec.device, rec.profile) == ("RaceBox", PRECISE)
    assert any("up to 1 s early" in w for w in rec.warnings)


def test_gpx_without_track_times_is_refused(tmp_path):
    path = tmp_path / "route.gpx"
    path.write_text('<?xml version="1.0"?><gpx version="1.1"><rte><rtept lat="1" lon="2"/></rte></gpx>')
    with pytest.raises(ValueError, match="cannot be timed"):
        read(path)


@pytest.mark.parametrize("rate", [10.0, 1.0])
def test_nmea_round_trip(truth, tmp_path, rate):
    fixes = sample_gnss(truth, rate, NO_NOISE)
    path = tmp_path / "log.nmea"
    write_nmea(fixes, path)
    assert identify(path) is Kind.NMEA
    rec = read(path)
    assert (rec.device, rec.profile) == ("NMEA log", COARSE)
    t = rec.telemetry
    np.testing.assert_allclose(t.time, fixes.time, atol=5e-3)
    np.testing.assert_allclose(t.lat, fixes.lat, atol=2e-6)  # 0.0001 minute
    np.testing.assert_allclose(t.lon, fixes.lon, atol=2e-6)  # west is W, read as negative
    np.testing.assert_allclose(t.speed, fixes.speed, atol=0.001)  # knots
    np.testing.assert_allclose(t.alt, fixes.alt, atol=0.06)  # from GGA
    assert _counted(t, truth) == pytest.approx([lap.time for lap in truth.laps[1:3]], abs=0.06)


def test_nmea_bad_checksums_and_void_fixes_are_skipped(tmp_path):
    good = "GPRMC,120000.00,A,4448.6780,N,09129.9100,W,10.0,90.0,130626,,,A"
    void = "GPRMC,120001.00,V,4448.6780,N,09129.9100,W,10.0,90.0,130626,,,A"
    nxt = "GPRMC,120002.00,A,4448.6790,N,09129.9000,W,10.0,90.0,130626,,,A"
    path = tmp_path / "log.nmea"
    path.write_text(f"${good}*{checksum(good)}\n${void}*{checksum(void)}\n${nxt}*00\n${nxt}*{checksum(nxt)}\n")
    rec = read(path)
    assert len(rec.telemetry) == 2
    assert "1 sentences with a bad checksum" in rec.warnings[0]


def test_nmea_without_rmc_is_refused(tmp_path):
    body = "GPGGA,120000.00,4448.6780,N,09129.9100,W,1,08,0.9,250.0,M,,M,,"
    path = tmp_path / "log.nmea"
    path.write_text(("$GPRMC,1*00\n" * 2) + f"${body}*{checksum(body)}\n")
    with pytest.raises(ValueError, match="RMC"):
        read(path)


@pytest.mark.fixtures
def test_real_racebox_gpx_matches_its_csv(real_dir):
    """RaceBox's GPX and CSV exports of one session (RacePlex, GPL-3.0):
    identical positions, GPX times 0.16 s earlier throughout."""
    gpx = read(real_dir / "racebox" / "session.gpx")
    csv = read(real_dir / "racebox" / "session.csv").telemetry
    t = gpx.telemetry
    assert gpx.device == "RaceBox" and len(t) == len(csv)
    np.testing.assert_array_equal(t.lat, csv.lat)
    np.testing.assert_array_equal(t.lon, csv.lon)
    np.testing.assert_allclose(t.time - csv.time, -0.16, atol=0.002)
    moving = csv.speed > 5
    assert np.median(np.abs(t.speed - csv.speed)[moving]) < 0.3


@pytest.mark.fixtures
def test_real_nmea_matches_gpsbabel_conversion(real_dir):
    """GPSBabel's reference NMEA track and the GPX GPSBabel made from it
    (GPL-2.0) read to the same fixes."""
    nmea = read(real_dir / "nmea" / "gpsbabel-track.nmea").telemetry
    gpx = read(real_dir / "nmea" / "gpsbabel-track.gpx").telemetry
    assert len(nmea) == len(gpx) == 154
    np.testing.assert_allclose(nmea.time, gpx.time, atol=1e-6)
    np.testing.assert_allclose(nmea.lat, gpx.lat, atol=1e-8)
    np.testing.assert_allclose(nmea.lon, gpx.lon, atol=1e-8)
    np.testing.assert_allclose(nmea.speed, gpx.speed, atol=0.02)
    assert nmea.lon[0] == pytest.approx(-88.1217, abs=1e-4)  # W is negative
