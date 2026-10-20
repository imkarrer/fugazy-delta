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


# ---------- real-device layouts (docs/formats.md section 6.1) ----------

from datetime import timedelta  # noqa: E402

from fugazy_delta.devices import NO_NOISE as _EXACT  # noqa: E402
from fugazy_delta.importers import Kind, PRECISE, read  # noqa: E402
from fugazy_delta.segment import find_laps  # noqa: E402

# 20:30 on 13 June in Wisconsin (CDT, UTC-5) is 01:30 on 14 June UTC.
EVENING_UTC0 = datetime(2026, 6, 14, 1, 30, tzinfo=timezone.utc).timestamp()


@pytest.fixture(scope="module")
def evening():
    return autocross_session(example_autocross(), [[1, 1, 1]], utc0=EVENING_UTC0)


def test_racebox_layout_with_lng_column_reads(truth, tmp_path):
    rb = racebox_mini(truth, _EXACT)
    path = tmp_path / "racebox.vbo"
    write_vbo(rb, path, style="racebox")
    assert "time lat lng velocity" in path.read_text()
    rec = read(path)
    assert (rec.kind, rec.device, rec.profile, rec.tz_inferred) == (Kind.VBO, "RaceBox", PRECISE, False)
    t = rec.telemetry
    np.testing.assert_allclose(t.time, rb.time, atol=1e-6)
    np.testing.assert_allclose(t.lat, rb.lat, atol=1e-7)
    np.testing.assert_allclose(t.lon, rb.lon, atol=1e-7)  # west negative after the sign flip
    np.testing.assert_allclose(t.speed, rb.speed, atol=0.001 / 3.6 + 1e-9)
    np.testing.assert_allclose(t.accel_long, rb.accel_long, atol=0.0005 + 1e-9)
    np.testing.assert_allclose(t.accel_vert, rb.accel_vert, atol=0.0005 + 1e-9)


def test_racebox_utc_date_started_sets_the_date(evening, tmp_path):
    rb = racebox_mini(evening, _EXACT)
    path = tmp_path / "racebox.vbo"
    write_vbo(rb, path, style="racebox")
    rec = read(path)
    assert rec.telemetry.time[0] == pytest.approx(rb.time[0], abs=1e-6)
    assert not rec.tz_inferred


def test_racelogic_local_date_line_does_not_land_a_day_early(evening, tmp_path):
    rb = racebox_mini(evening, _EXACT)
    path = tmp_path / "vbox.vbo"
    write_vbo(rb, path, style="racelogic", local_zone="America/Chicago")
    assert "File created on 13/06/2026 @ 20:30" in path.read_text()
    rec = read(path)
    assert rec.device == "Racelogic VBOX" and rec.profile == PRECISE
    assert rec.telemetry.time[0] == pytest.approx(rb.time[0], abs=1e-6)
    assert rec.tz_inferred
    assert "America/Chicago" in rec.warnings[0]
    # satellites carry the DGPS bit (128); every row is still a fix
    assert len(rec.telemetry) == len(rb)
    laps = [lap for lap in find_laps(rec.telemetry, evening.course.timing_lines()) if lap.counted]
    assert laps[0].time == pytest.approx(evening.laps[0].time, abs=0.01)


@pytest.mark.parametrize("zone,hour", [("America/Los_Angeles", 17), ("America/New_York", 23), ("Europe/London", 0)])
def test_racelogic_date_inference_across_zones(tmp_path, zone, hour):
    """The created line's date is local; the right UTC date is recovered
    whichever side of midnight UTC the session falls."""
    from dataclasses import replace
    from zoneinfo import ZoneInfo

    origins = {"America/Los_Angeles": (39.54, -122.33), "America/New_York": (40.85, -74.0),
               "Europe/London": (51.21, -1.61)}
    local = datetime(2026, 7, 31, hour, 15, tzinfo=ZoneInfo(zone))
    course = replace(example_autocross(), origin=origins[zone])
    t = autocross_session(course, [[1, 1, 1]], utc0=local.timestamp())
    rb = racebox_mini(t, _EXACT)
    path = tmp_path / "vbox.vbo"
    write_vbo(rb, path, style="racelogic", local_zone=zone)
    assert read(path).telemetry.time[0] == pytest.approx(rb.time[0], abs=1e-6)


def test_vbo_date_line_variants(tmp_path):
    body = "\n[column names]\ntime lat long velocity\n[data]\n120000.00 +02688.67800 +05489.91000 036.000\n"
    for line in ("File created on 13/06/2026 at 12:00", "File created on 13/06/2026 @ 12:00:00",
                 "File created on 13/06/2026"):
        path = tmp_path / "v.vbo"
        path.write_text(line + "\n" + body)
        t = read_vbo(path)
        assert datetime.fromtimestamp(t.time[0], timezone.utc) == datetime(2026, 6, 13, 12, tzinfo=timezone.utc) + timedelta(0)


def test_vbo_rows_without_a_fix_are_dropped(tmp_path):
    path = tmp_path / "v.vbo"
    path.write_text(
        "File created on 13/06/2026 @ 12:00\n[comments]\nUTC Date Started : 13/06/2026 12:00\n"
        "[column names]\nsats time lat long velocity\n[data]\n"
        "000 120000.00 +00000.00000 +00000.00000 000.000\n"
        "140 120000.10 +02688.67800 +05489.91000 036.000\n"
        "064 120000.20 +02688.67900 +05489.91000 036.000\n"
    )
    t = read_vbo(path)
    assert len(t) == 1  # 64 is the brake bit with no satellites; 140 is DGPS + 12


# ---------- real files (fetched by scripts/fetch-fixtures.sh) ----------

@pytest.mark.fixtures
def test_real_racebox_thruxton(real_dir):
    """RaceBox Mini export, Thruxton, 28 May 2026 (mt3uk test fixture)."""
    rec = read(real_dir / "vbo" / "thruxton-trimmed.vbo")
    assert (rec.device, rec.profile, rec.tz_inferred) == ("RaceBox", PRECISE, False)
    t = rec.telemetry
    assert datetime.fromtimestamp(t.time[0], timezone.utc) == datetime(2026, 5, 28, 13, 34, 41, 360000, tzinfo=timezone.utc)
    assert t.lat[0] == pytest.approx(51.2063, abs=1e-4) and t.lon[0] == pytest.approx(-1.6073, abs=1e-4)
    assert t.sample_rate == pytest.approx(12.5, rel=0.01)
    # LatAcc is positive left, as in our model: it tracks the turn rate
    yaw = np.gradient(np.unwrap(np.radians(t.heading)), t.time)
    moving = t.speed > 5
    assert np.corrcoef(t.accel_lat[moving], -(t.speed * yaw)[moving])[0, 1] > 0.8


@pytest.mark.fixtures
def test_real_vbox_sport_shelsley(real_dir):
    """VBOX Sport hill climb, Shelsley Walsh, 25 July 2021 (mt3uk)."""
    rec = read(real_dir / "vbo" / "shelsley-climb-and-descent.vbo")
    assert rec.device == "Racelogic VBOX" and rec.tz_inferred
    t = rec.telemetry
    assert datetime.fromtimestamp(t.time[0], timezone.utc).date().isoformat() == "2021-07-25"
    assert t.sample_rate == pytest.approx(10.0, rel=0.01)
    assert np.corrcoef(t.accel_long, np.gradient(t.speed, t.time))[0, 1] > 0.9


@pytest.mark.fixtures
def test_real_vbox_video_hd2_thunderhill(real_dir):
    """Video HD2, Thunderhill, 31 July 2025, 11:33 local = 18:33 UTC (apexai)."""
    rec = read(real_dir / "vbo" / "VBOX0133.vbo")
    t = rec.telemetry
    assert datetime.fromtimestamp(t.time[0], timezone.utc) == datetime(2025, 7, 31, 18, 33, 3, 800000, tzinfo=timezone.utc)
    assert t.lat[0] == pytest.approx(39.5367, abs=1e-3) and t.lon[0] == pytest.approx(-122.332, abs=1e-3)
    assert "America/Los_Angeles" in rec.warnings[0]
