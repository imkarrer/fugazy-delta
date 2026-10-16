"""Real GoPro files: our reader against them, and GoPro's reader against ours."""

import re
import subprocess

import numpy as np
import pytest

from fugazy_delta.fixtures import gopro_mp4
from fugazy_delta.gpmf import decode_gps5
from fugazy_delta.mp4 import read_gpmd
from fugazy_delta.sim import autocross_session, example_autocross

pytestmark = pytest.mark.fixtures


@pytest.mark.parametrize("name", ["hero5.mp4", "hero6.mp4"])
def test_reads_real_gps5(real_dir, name):
    gps = decode_gps5(read_gpmd(real_dir / name))
    assert gps.sample_rate == pytest.approx(18.0, rel=0.02)
    # the samples were shot in Carlsbad, California
    assert np.nanmedian(gps.lat) == pytest.approx(33.13, abs=0.05)
    assert np.nanmedian(gps.lon) == pytest.approx(-117.33, abs=0.05)


def test_real_file_without_gps_fix_is_reported(real_dir):
    with pytest.raises(ValueError, match="fix"):
        decode_gps5(read_gpmd(real_dir / "hero8.mp4"))


@pytest.mark.ffmpeg
def test_gopro_reference_parser_reads_synthetic_file(real_dir, tmp_path):
    demo = real_dir / "gpmf_demo"
    if not demo.exists():
        pytest.skip("gpmf_demo not built")
    truth = autocross_session(example_autocross(), [[1, 1, 1]])
    path = tmp_path / "GX010001.MP4"
    gopro_mp4(truth, path, 15.0, 10.0)
    out = subprocess.run([str(demo), str(path)], capture_output=True, text=True, errors="replace")
    assert "invalid" not in out.stdout
    rates = dict(re.findall(r"(\w{4}) sampling rate = ([\d.]+)Hz", out.stdout))
    assert float(rates["GPS5"]) == pytest.approx(18 / 1.001, rel=1e-3)
    assert float(rates["ACCL"]) == pytest.approx(200 / 1.001, rel=1e-3)
