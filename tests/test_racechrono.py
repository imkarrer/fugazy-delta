"""RaceChrono CSV v3: written from the simulator and read back, and a real export."""

from datetime import datetime, timezone

import numpy as np
import pytest

from fugazy_delta.devices import NO_NOISE, sample_gnss
from fugazy_delta.importers import COARSE, Kind, UnsupportedFile, identify, read
from fugazy_delta.racechrono import write_racechrono
from fugazy_delta.segment import find_laps
from fugazy_delta.session import Feature, Session, Support
from fugazy_delta.sim import example_track, track_session


@pytest.fixture(scope="module")
def truth():
    return track_session(example_track(), [[1, 1, 1], [1, 1, 1], [0.97, 0.98, 0.99]], in_lap=0.5)


@pytest.mark.parametrize("gps_hz", [25.0, 10.0, 1.0])
def test_round_trip_keeps_only_the_fixes(truth, tmp_path, gps_hz):
    fixes = sample_gnss(truth, gps_hz, NO_NOISE)
    path = tmp_path / "session.csv"
    write_racechrono(fixes, path, gps_hz=gps_hz)
    assert identify(path) is Kind.RACECHRONO_CSV
    rec = read(path)
    assert (rec.device, rec.profile, rec.tz_inferred) == ("RaceChrono", COARSE, False)
    t = rec.telemetry
    # the rows RaceChrono interpolated are gone; the receiver's fixes remain
    np.testing.assert_allclose(t.time, fixes.time, atol=1e-6)
    np.testing.assert_allclose(t.lat, fixes.lat, atol=1e-9)
    np.testing.assert_allclose(t.lon, fixes.lon, atol=1e-9)
    # GPS speed, not the CAN speed column of the same name
    np.testing.assert_allclose(t.speed, fixes.speed, atol=1e-5)
    np.testing.assert_allclose(t.heading, fixes.heading, atol=0.01)
    assert np.all(np.isnan(t.accel_long))  # sign convention unverified: dropped
    assert any("about 1 Hz" in w for w in rec.warnings) == (gps_hz < 5)


def test_lap_times_and_device_markers(truth, tmp_path):
    fixes = sample_gnss(truth, 25.0, NO_NOISE)
    starts = [truth.utc0 + lap.start for lap in truth.laps[1:]]
    path = tmp_path / "s.csv"
    write_racechrono(fixes, path, gps_hz=25.0, laps=starts)
    rec = read(path)
    laps = [lap for lap in find_laps(rec.telemetry, truth.course.timing_lines()) if lap.counted]
    assert [lap.time for lap in laps] == pytest.approx([lap.time for lap in truth.laps[1:3]], abs=0.005)
    assert rec.laps == pytest.approx(starts, abs=0.02)


def test_phone_rate_session_is_approximate(truth, tmp_path):
    path = tmp_path / "s.csv"
    write_racechrono(sample_gnss(truth, 1.0, NO_NOISE), path, gps_hz=1.0)
    session = Session.from_files([path])
    assert session.setup_label == "RaceChrono"
    assert session.features("track")[Feature.LAP_TIMES].support is Support.APPROXIMATE
    assert any("about 1 Hz" in w for w in session.warnings)


def test_other_csv_versions_are_refused_with_advice(truth, tmp_path):
    path = tmp_path / "s.csv"
    write_racechrono(sample_gnss(truth, 10.0, NO_NOISE), path, gps_hz=10.0, fmt=2)
    with pytest.raises(UnsupportedFile, match="CSV v3"):
        read(path)


@pytest.mark.fixtures
def test_real_racechrono_v3_excerpt(real_dir):
    """RaceChrono Pro v10.1.3, Massa Finalese, 4 August 2026 (UltraLog, AGPL-3.0).

    The excerpt is four short pieces of a session. Its GPS updates at 1 Hz
    on whole UTC seconds, and the rows between are interpolated."""
    rec = read(real_dir / "racechrono" / "massa-finalese-v3.csv")
    t = rec.telemetry
    assert datetime.fromtimestamp(t.time[0], timezone.utc).date().isoformat() == "2026-08-04"
    assert 44.82 < np.median(t.lat) < 44.84 and 11.22 < np.median(t.lon) < 11.23
    whole = np.abs(t.time - np.round(t.time)) < 0.01
    assert whole.sum() >= len(t) - 2  # chunk starts are the only non-fix rows kept
    assert len(t) < 20  # of 400 rows
    assert any("about 1 Hz" in w for w in rec.warnings)
    assert rec.laps  # lap_number steps from 1 to 2 in the excerpt
