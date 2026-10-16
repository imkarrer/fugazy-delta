"""Build synthetic device files from a `Truth`.

`gopro_mp4` makes a HERO9-like MP4: an ffmpeg test pattern with the true UTC
time of every frame printed on it, plus a GPMF telemetry track. Because the
true time is in the pixels, any later stage that claims to have aligned
telemetry to video can be checked frame by frame.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .devices import HERO9_NOISE, RACEBOX_NOISE, GnssNoise, racebox_mini
from .gpmf import CameraClock, hero9_payloads
from .mp4 import add_gpmd_track
from .sim import Truth
from .vbo import write_vbo

GOPRO_FPS = "30000/1001"


def _font() -> str:
    env = os.environ.get("FLOX_ENV", "")
    candidate = Path(env) / "share/fonts/truetype/DejaVuSansMono.ttf"
    if candidate.exists():
        return str(candidate)
    raise FileNotFoundError("DejaVuSansMono.ttf not found; run inside `flox activate`")


def render_test_video(path: str | Path, seconds: float, utc_start: float, size: str = "320x180") -> None:
    """Encode a test-pattern video whose frames show their true UTC time."""
    if shutil.which("ffmpeg") is None:
        raise FileNotFoundError("ffmpeg not on PATH")
    start = datetime.fromtimestamp(utc_start, tz=timezone.utc)
    of_day = utc_start - start.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as text:
        # hms with an offset prints HH:MM:SS.mmm of (pts + offset): the frame's
        # true UTC time of day, to the millisecond
        text.write(f"UTC %{{pts:hms:{of_day:.3f}}}\nframe %{{n}}")
    try:
        draw = (
            f"drawtext=fontfile={_font()}:textfile={text.name}:expansion=normal:"
            "fontsize=18:fontcolor=white:box=1:boxcolor=black@0.7:x=8:y=8"
        )
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={GOPRO_FPS}:duration={seconds}",
                "-vf", draw,
                "-c:v", "mpeg4", "-q:v", "5",
                str(path),
            ],
            check=True,
        )
    finally:
        os.unlink(text.name)


def gopro_mp4(
    truth: Truth,
    path: str | Path,
    record_start: float,
    record_seconds: float,
    clock: CameraClock = CameraClock(),
    seed: int = 0,
    noise: GnssNoise = HERO9_NOISE,
    gps_fix: int = 3,
) -> list:
    """Write a HERO9-like MP4 recorded from `record_start` (s after utc0).

    Returns the payloads written, for tests to compare against.
    """
    payloads = hero9_payloads(truth, record_start, record_seconds, clock, seed=seed, noise=noise, gps_fix=gps_fix)
    utc_start = truth.utc0 + record_start
    with tempfile.TemporaryDirectory() as tmp:
        video = Path(tmp) / "video.mp4"
        render_test_video(video, len(payloads) * payloads[0].duration, utc_start)
        add_gpmd_track(video, payloads, path, creation_utc=utc_start + clock.camera_offset)
    return payloads


def racebox_vbo(truth: Truth, path: str | Path, noise: GnssNoise = RACEBOX_NOISE, seed: int = 0) -> None:
    """Write what a RaceBox Mini session exports as VBO."""
    write_vbo(racebox_mini(truth, noise, seed), path, comment="Simulated RaceBox Mini")
