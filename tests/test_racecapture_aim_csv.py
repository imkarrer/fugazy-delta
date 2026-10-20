"""RaceCapture logs and AiM CSV: written from the simulator and read back,
and real files."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from fugazy_delta.aim_csv import write_aim_csv
from fugazy_delta.devices import NO_NOISE, sample_gnss
from fugazy_delta.importers import PRECISE, Kind, identify, read
from fugazy_delta.racecapture import write_racecapture
from fugazy_delta.segment import find_laps
from fugazy_delta.sim import autocross_session, example_autocross, example_track, track_session


@pytest.fixture(scope="module")
def truth():
    return track_session(example_track(), [[1, 1, 1], [1, 1, 1], [0.97, 0.98, 0.99]], in_lap=0.5)


def _counted(t, truth):
    return [lap.time for lap in find_laps(t, truth.course.timing_lines()) if lap.counted]


# ---------- RaceCapture ----------

@pytest.mark.parametrize("european,unit", [(False, "MPH"), (True, "kph")])
def test_racecapture_round_trip(truth, tmp_path, european, unit):
    fixes = sample_gnss(truth, 10.0, NO_NOISE)
    starts = [truth.utc0 + lap.start for lap in truth.laps[1:]]
    path = tmp_path / "rc_3.log"
    write_racecapture(fixes, path, speed_unit_name=unit, european=european, laps=starts)
    assert identify(path) is Kind.RACECAPTURE
    rec = read(path)
    assert (rec.device, rec.profile, rec.tz_inferred) == ("RaceCapture", PRECISE, False)
    t = rec.telemetry
    # rows with Utc 0 (before the fix) and rows with empty GPS cells are dropped
    np.testing.assert_allclose(t.time, fixes.time, atol=1e-3)
    np.testing.assert_allclose(t.lat, fixes.lat, atol=1e-7)
    np.testing.assert_allclose(t.lon, fixes.lon, atol=1e-7)
    np.testing.assert_allclose(t.speed, fixes.speed, atol=0.005)
    np.testing.assert_allclose(t.alt, fixes.alt, atol=0.05)  # written in ft
    assert _counted(t, truth) == pytest.approx([lap.time for lap in truth.laps[1:3]], abs=0.01)
    assert rec.laps == pytest.approx(starts, abs=0.1)


def test_racecapture_rows_are_ordered_by_utc(truth, tmp_path):
    fixes = sample_gnss(truth, 10.0, NO_NOISE).slice_time(truth.utc0 + 5, truth.utc0 + 15)
    path = tmp_path / "rc.log"
    write_racecapture(fixes, path)
    lines = path.read_text().splitlines()
    body = lines[1:]
    half = len(body) // 2
    path.write_text("\n".join([lines[0]] + body[half:] + body[:half]) + "\n")  # Interval goes backwards
    np.testing.assert_allclose(read(path).telemetry.time, fixes.time, atol=1e-3)


@pytest.mark.fixtures
def test_real_racecapture_autocross(real_dir):
    """Formula Student Germany autocross, Hockenheim, 20 August 2021
    (EOSCogniton/Test-Data, no licence): 10 Hz GPS, speed in kph."""
    rec = read(real_dir / "racecapture" / "fsg21-autocross.log")
    t = rec.telemetry
    assert datetime.fromtimestamp(t.time[0], timezone.utc).date().isoformat() == "2021-08-20"
    assert t.sample_rate == pytest.approx(10.0, rel=0.01)
    assert np.median(t.lat) == pytest.approx(49.33, abs=0.01) and np.median(t.lon) == pytest.approx(8.57, abs=0.01)
    assert 15 < np.nanmax(t.speed) < 30  # m/s, a Formula Student car on an autocross
    assert len(t) > 1500


@pytest.mark.fixtures
def test_real_racecapture_european_log_without_fix(real_dir):
    """Tab-separated with decimal commas; recorded before the GPS had a fix."""
    with pytest.raises(ValueError, match="no GPS fixes"):
        read(real_dir / "racecapture" / "no-fix-tabs.log")


# ---------- AiM CSV ----------

@pytest.fixture(scope="module")
def evening():
    """An autocross at 18:45 Chicago time (23:45 UTC), starting on a whole
    minute because AiM CSV gives the start only to the minute."""
    local = datetime(2026, 6, 13, 18, 45, tzinfo=ZoneInfo("America/Chicago"))
    return autocross_session(example_autocross(), [[1, 1, 1], [0.98, 0.98, 0.98]], utc0=local.timestamp())


def test_aim_csv_round_trip_converts_local_time(evening, tmp_path):
    fixes = sample_gnss(evening, 25.0, NO_NOISE)
    assert fixes.time[0] == evening.utc0
    path = tmp_path / "aim.csv"
    write_aim_csv(fixes, path, datetime(2026, 6, 13, 18, 45), rate_hz=20.0, bom=True)
    assert identify(path) is Kind.AIM_CSV
    rec = read(path)
    assert (rec.device, rec.profile, rec.tz_inferred) == ("AiM", PRECISE, True)
    assert "America/Chicago" in rec.warnings[0]
    t = rec.telemetry
    # the local 18:45 becomes 23:45 UTC on the same day, not a day off
    assert t.time[0] == pytest.approx(fixes.time[0], abs=1e-6)
    # 20 Hz rows hold each 25 Hz fix; every kept row is a real fix
    exact = evening.at(t.time - evening.utc0)
    held = np.interp(t.time, fixes.time, fixes.time)
    assert np.all(np.abs(held - t.time) < 0.05)
    np.testing.assert_allclose(t.speed, np.interp(np.searchsorted(fixes.time, t.time + 1e-9, "right") - 1,
                                                  np.arange(len(fixes)), fixes.speed), atol=1e-4)
    assert np.all(np.abs(t.lat - exact["lat"]) < 1e-5)
    runs = [lap.time for lap in find_laps(t, evening.course.timing_lines()) if lap.counted]
    assert runs == pytest.approx([lap.time for lap in evening.laps], abs=0.1)


@pytest.mark.parametrize("date,clock", [("13/06/2026", "18:45"), ("2026-06-13", "6:45 PM")])
def test_aim_csv_date_formats(evening, tmp_path, date, clock):
    fixes = sample_gnss(evening, 25.0, NO_NOISE).slice_time(evening.utc0, evening.utc0 + 20)
    path = tmp_path / "aim.csv"
    write_aim_csv(fixes, path, datetime(2026, 6, 13, 18, 45))
    lines = path.read_text().splitlines()
    lines = [f'"Date","{date}"' if ln.startswith('"Date"') else f'"Time","{clock}"' if ln.startswith('"Time","') and "PM" in ln else ln
             for ln in lines]
    path.write_text("\n".join(lines) + "\n")
    assert read(path).telemetry.time[0] == pytest.approx(fixes.time[0], abs=1e-6)


def test_aim_csv_beacon_markers(evening, tmp_path):
    fixes = sample_gnss(evening, 25.0, NO_NOISE)
    starts = [evening.utc0 + lap.start for lap in evening.laps]
    path = tmp_path / "aim.csv"
    write_aim_csv(fixes, path, datetime(2026, 6, 13, 18, 45), laps=starts)
    assert read(path).laps == pytest.approx(starts, abs=1e-3)


def test_real_aim_csv_pittrace(samples_dir):
    """Race Studio 3 export, Pittsburgh International Race Complex,
    1 August 2025, 11:24 AM local (uOpenFormula, MIT, committed)."""
    rec = read(samples_dir / "uopenformula" / "pittrace-90.csv")
    assert rec.kind is Kind.AIM_CSV and rec.tz_inferred
    assert "America/New_York" in rec.warnings[0]
    t = rec.telemetry
    # 11:24 EDT is 15:24 UTC
    assert datetime.fromtimestamp(t.time[0], timezone.utc) == datetime(2025, 8, 1, 15, 24, tzinfo=timezone.utc)
    assert t.lat[0] == pytest.approx(40.848, abs=1e-3) and t.lon[0] == pytest.approx(-80.346, abs=1e-3)
    assert np.all((t.heading >= 0) & (t.heading < 360))
    assert len(t) < 360  # of 360 rows, the held repeats are gone
    assert rec.laps == pytest.approx([t.time[0] + 18.0], abs=0.05)
