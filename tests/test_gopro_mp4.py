"""The synthetic GoPro MP4 must look like a real one to real tools."""

import json
import re
import subprocess

import numpy as np
import pytest

from fugazy_delta.devices import NO_NOISE
from fugazy_delta.fixtures import gopro_mp4, render_test_video
from fugazy_delta.gpmf import CameraClock, decode_gps5
from fugazy_delta.mp4 import creation_time, read_gpmd
from fugazy_delta.sim import autocross_session, example_autocross

pytestmark = pytest.mark.ffmpeg

RECORD_START = 15.0
CLOCK = CameraClock(gpsu_latency=0.0, camera_offset=37.4)


@pytest.fixture(scope="module")
def truth():
    return autocross_session(example_autocross(), [[1, 1, 1]])


@pytest.fixture(scope="module")
def clip(truth, tmp_path_factory):
    path = tmp_path_factory.mktemp("gopro") / "GX010001.MP4"
    payloads = gopro_mp4(truth, path, RECORD_START, 12.0, CLOCK, noise=NO_NOISE)
    return path, payloads


def _streams(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )
    return json.loads(out.stdout)["streams"]


def test_ffprobe_sees_a_gopro_telemetry_stream(clip):
    path, _ = clip
    data = [s for s in _streams(path) if s["codec_type"] == "data"]
    assert len(data) == 1
    assert data[0]["codec_tag_string"] == "gpmd"
    assert data[0]["tags"]["handler_name"] == "GoPro MET"


def test_ffmpeg_extracts_exactly_the_payloads(clip, tmp_path):
    path, payloads = clip
    index = next(s["index"] for s in _streams(path) if s["codec_type"] == "data")
    out = tmp_path / "gpmf.bin"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(path), "-map", f"0:{index}",
         "-codec", "copy", "-f", "rawvideo", str(out)],
        check=True,
    )
    assert out.read_bytes() == b"".join(p.data for p in payloads)


def test_read_gpmd_round_trip(clip):
    path, payloads = clip
    back = read_gpmd(path)
    assert [p.data for p in back] == [p.data for p in payloads]
    np.testing.assert_allclose([p.start for p in back], [p.start for p in payloads], atol=1e-3)


def test_creation_time_is_camera_clock(clip, truth):
    path, _ = clip
    # MP4 stores whole seconds
    assert creation_time(path) == pytest.approx(truth.utc0 + RECORD_START + CLOCK.camera_offset, abs=0.5)


def test_telemetry_from_file_matches_truth(clip, truth):
    path, _ = clip
    gps = decode_gps5(read_gpmd(path))
    assert gps.time[0] == pytest.approx(truth.utc0 + RECORD_START, abs=1e-3)
    exact = truth.at(gps.time - truth.utc0)
    np.testing.assert_allclose(gps.lat, exact["lat"], atol=2e-7)


def test_test_video_frames_are_at_gopro_rate(truth, tmp_path):
    """Frame n sits at n * 1001/30000 s; its burned-in stamp is utc_start plus that.

    This checks the timing the stamp is computed from, not the pixels; reading
    the stamp back (OCR) is left to the stage that renders overlays.
    """
    path = tmp_path / "v.mp4"
    render_test_video(path, 2.0, truth.utc0 + RECORD_START)
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-vf", "showinfo", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    )
    pts = [float(m) for m in re.findall(r"pts_time:([\d.]+)", out.stderr)]
    assert len(pts) == 60
    assert pts[30] == pytest.approx(30 * 1001 / 30000, abs=1e-3)


def test_file_without_telemetry_is_rejected(tmp_path, truth):
    path = tmp_path / "plain.mp4"
    render_test_video(path, 1.0, truth.utc0)
    with pytest.raises(ValueError, match="no gpmd"):
        read_gpmd(path)
