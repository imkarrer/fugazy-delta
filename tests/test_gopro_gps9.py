"""GoPro GPS9 (HERO11, HERO13): every sample timed by its own UTC fields."""

import re
import struct
import subprocess
from datetime import datetime, timezone

import numpy as np
import pytest

from fugazy_delta.devices import NO_NOISE
from fugazy_delta.fixtures import gopro_mp4
from fugazy_delta.gpmf import (
    GPS9_EPOCH,
    HERO11_GPSU_LAG,
    Payload,
    decode,
    decode_gps,
    decode_gps5,
    decode_gps9,
    hero11_payloads,
    parse_gpsu,
)
from fugazy_delta.importers import Kind, identify, read
from fugazy_delta.session import Session, Setup
from fugazy_delta.sim import autocross_session, example_autocross


@pytest.fixture(scope="module")
def truth():
    return autocross_session(example_autocross(), [[1, 1, 1]])


def test_gps9_round_trip_is_exact_on_receiver_epochs(truth):
    payloads = hero11_payloads(truth, 5.0, 20.0, noise=NO_NOISE)
    gps = decode_gps9(payloads)
    assert gps.sample_rate == pytest.approx(10.0)
    np.testing.assert_allclose(np.mod(gps.time * 10, 1.0), 0.0, atol=1e-6)
    exact = truth.at(gps.time - truth.utc0)
    np.testing.assert_allclose(gps.lat, exact["lat"], atol=1e-7)
    np.testing.assert_allclose(gps.lon, exact["lon"], atol=1e-7)
    np.testing.assert_allclose(gps.speed, exact["speed"], atol=1e-3)


def test_decode_gps_prefers_gps9_over_late_gpsu(truth):
    """GPS5 timed from GPSU lands HERO11_GPSU_LAG late; GPS9 does not."""
    payloads = hero11_payloads(truth, 5.0, 20.0, noise=NO_NOISE)
    gps9, gps5 = decode_gps(payloads), decode_gps5(payloads)
    np.testing.assert_allclose(gps5.lat[:20], gps9.lat[:20])
    assert gps5.time[0] - gps9.time[0] == pytest.approx(HERO11_GPSU_LAG, abs=1e-3)


def test_samples_without_fix_are_dropped(truth):
    with pytest.raises(ValueError, match="fix"):
        decode_gps9(hero11_payloads(truth, 5.0, 5.0, gps_fix=0))


@pytest.mark.ffmpeg
@pytest.mark.parametrize("camera", ["HERO11", "HERO13"])
def test_hero11_and_hero13_videos(truth, tmp_path, camera):
    path = tmp_path / "GX010001.MP4"
    gopro_mp4(truth, path, 15.0, 10.0, camera=camera, noise=NO_NOISE)
    assert identify(path) is Kind.GOPRO
    rec = read(path)
    assert rec.device == f"GoPro {camera} Black" and rec.camera
    session = Session.from_files([path])
    assert session.setup is Setup.GOPRO_ONLY
    t = session.position
    assert t.time[0] == pytest.approx(np.ceil((truth.utc0 + 15.0) * 10) / 10, abs=1e-6)
    exact = truth.at(t.time - truth.utc0)
    np.testing.assert_allclose(t.lat, exact["lat"], atol=1e-7)


@pytest.mark.fixtures
@pytest.mark.ffmpeg
def test_gopro_reference_parser_reads_our_gps9(real_dir, truth, tmp_path):
    demo = real_dir / "gpmf_demo"
    path = tmp_path / "GX010001.MP4"
    gopro_mp4(truth, path, 15.0, 10.0, camera="HERO13")
    out = subprocess.run([str(demo), str(path)], capture_output=True, text=True, errors="replace")
    rates = dict(re.findall(r"(\w{4}) sampling rate = ([\d.]+)Hz", out.stdout))
    assert float(rates["GPS9"]) == pytest.approx(10.0, rel=0.02)


def test_real_hero11_payloads(samples_dir):
    """GoPro HERO11 Black, 20 September 2022 (gopro-telemetry, MIT, committed)."""
    raw = (samples_dir / "gopro-telemetry" / "hero11.raw").read_bytes()
    gps = decode_gps9([Payload(raw, 0.0, 0.0)])
    assert datetime.fromtimestamp(gps.time[0], timezone.utc).date().isoformat() == "2022-09-20"
    assert gps.sample_rate == pytest.approx(10.0, rel=0.02)
    assert np.median(gps.lat) == pytest.approx(42.426, abs=0.01)
    assert np.median(gps.lon) == pytest.approx(-8.650, abs=0.01)
    # each payload's GPSU is 1.4 s after its first GPS9 sample
    for devc in decode(raw)[:5]:
        streams = devc.find_all("STRM")
        gpsu = next(s.find("GPSU") for s in streams if s.find("GPSU") is not None)
        gps9 = next(s for s in streams if s.find("GPS9") is not None)
        days, secs = struct.unpack_from(">7i2H", gps9.find("GPS9").data, 0)[5:7]
        first = GPS9_EPOCH + days * 86400 + secs / 1000
        assert parse_gpsu(gpsu.text()) - first == pytest.approx(HERO11_GPSU_LAG, abs=0.05)
