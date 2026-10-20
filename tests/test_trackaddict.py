"""TrackAddict CSV: written from the simulator and read back, and a real
session timed against TrackAddict's own lap times."""

from datetime import datetime, timezone

import numpy as np
import pytest

from fugazy_delta.devices import NO_NOISE, sample_gnss
from fugazy_delta.importers import COARSE, Kind, identify, read, read_text
from fugazy_delta.segment import Gate, TimingLines, find_laps
from fugazy_delta.sim import example_track, track_session
from fugazy_delta.trackaddict import lap_times, write_trackaddict


@pytest.fixture(scope="module")
def truth():
    return track_session(example_track(), [[1, 1, 1], [1, 1, 1], [0.97, 0.98, 0.99]], in_lap=0.5)


@pytest.mark.parametrize("unit,receiver,gps_hz", [("Km/h", "Android", 1.0), ("MPH", "XGPS160", 10.0)])
def test_round_trip_keeps_only_new_fixes(truth, tmp_path, unit, receiver, gps_hz):
    fixes = sample_gnss(truth, gps_hz, NO_NOISE)
    path = tmp_path / "Log-20260613-090000.csv"
    write_trackaddict(fixes, path, unit=unit, receiver=receiver)
    assert identify(path) is Kind.TRACKADDICT_CSV
    rec = read(path)
    assert rec.profile is COARSE or rec.profile == COARSE
    assert rec.device == ("TrackAddict (phone GPS)" if receiver == "Android" else f"TrackAddict ({receiver})")
    t = rec.telemetry
    np.testing.assert_allclose(t.time, fixes.time, atol=1e-3)  # 50 Hz rows that repeat a fix are gone
    np.testing.assert_allclose(t.lat, fixes.lat, atol=1e-7)
    np.testing.assert_allclose(t.lon, fixes.lon, atol=1e-7)
    np.testing.assert_allclose(t.speed, fixes.speed, atol=0.005)
    # Accel X is written positive when braking; ours is positive forward
    np.testing.assert_allclose(t.accel_long, fixes.accel_long, atol=0.006)
    np.testing.assert_allclose(t.accel_lat, fixes.accel_lat, atol=0.006)
    assert any("about 1 Hz" in w for w in rec.warnings) == (gps_hz < 5)


def test_lap_markers_and_lap_lines(truth, tmp_path):
    fixes = sample_gnss(truth, 10.0, NO_NOISE)
    starts = [truth.utc0 + lap.start for lap in truth.laps[1:]]
    path = tmp_path / "s.csv"
    write_trackaddict(fixes, path, laps=starts, receiver="XGPS160")
    rec = read(path)
    assert rec.laps == pytest.approx(starts, abs=0.03)
    assert list(lap_times(read_text(path)).values()) == pytest.approx(np.diff(starts), abs=0.001)
    found = [lap for lap in find_laps(rec.telemetry, truth.course.timing_lines()) if lap.counted]
    assert [lap.time for lap in found] == pytest.approx([lap.time for lap in truth.laps[1:3]], abs=0.01)


@pytest.mark.fixtures
def test_real_session_times_match_trackaddict(real_dir):
    """TrackAddict 4.3.7, Android phone GPS, Saint-Eustache, 2 September
    2019 (OpenRaceRender). With a start line placed where TrackAddict
    marked its first lap, our times for the 13 laps after the out-lap
    differ from TrackAddict's own by 0.35 s typically and 0.7 s at most:
    the spread 1 Hz phone GPS allows, which is why the file carries a
    low-rate warning."""
    path = real_dir / "trackaddict" / "saint-eustache.csv"
    rec = read(path)
    assert rec.device == "TrackAddict (phone GPS)"
    t = rec.telemetry
    assert datetime.fromtimestamp(t.time[0], timezone.utc).date().isoformat() == "2019-09-02"
    assert t.sample_rate == pytest.approx(1.0, rel=0.05)
    assert any("about 1 Hz" in w for w in rec.warnings)
    marker = rec.laps[0]
    gate = Gate(float(np.interp(marker, t.time, t.lat)), float(np.interp(marker, t.time, t.lon)),
                float(np.interp(marker, t.time, t.heading)), width=40.0)
    ours = [lap.time for lap in find_laps(t, TimingLines(start=gate)) if lap.counted]
    theirs = list(lap_times(read_text(path)).values())
    assert len(theirs) == 15 and len(ours) == 14  # theirs includes the out-lap
    diff = np.abs(np.array(ours[:13]) - np.array(theirs[1:14]))
    assert np.median(diff) < 0.4 and diff.max() < 0.8
