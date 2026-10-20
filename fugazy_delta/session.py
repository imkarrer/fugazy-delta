"""Turn whatever files a driver drops in into one session.

The driver never says which devices they used. Each file is identified by
its content, and the mix of files decides the setup:

- GoPro only: video plus the camera's own GPS
- GoPro + RaceBox: video, positioned by the RaceBox
- RaceBox only: data, no video

`Session.features()` reports what each setup can deliver, with the reason,
so the results page can show what is available and what adding a device
would unlock, instead of quietly showing numbers the data cannot support.
Every reason that concerns a time quotes that time's measured error for the
setup's position source, from `accuracy.py`'s committed table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import numpy as np

from . import accuracy
from .gpmf import Payload, decode_gps5
from .model import Telemetry
from .mp4 import read_gpmd
from .vbo import read_vbo


class UnsupportedFile(ValueError):
    pass


class Kind(Enum):
    GOPRO = "gopro"
    VBO = "vbo"


class Setup(Enum):
    GOPRO_ONLY = "GoPro"
    GOPRO_RACEBOX = "GoPro + RaceBox"
    RACEBOX_ONLY = "RaceBox"


# Which position source times each setup: the RaceBox whenever there is one.
METHOD = {
    Setup.GOPRO_ONLY: "gopro",
    Setup.GOPRO_RACEBOX: "racebox",
    Setup.RACEBOX_ONLY: "racebox",
}


class Feature(Enum):
    LAP_TIMES = "Lap and run times"
    VIDEO_OVERLAY = "Video with data overlay"
    DELTA = "Delta to your best lap or run"
    OPTIMAL = "Optimal lap or run"
    LINE_COMPARISON = "Racing line comparison"


class Support(Enum):
    FULL = "full"
    APPROXIMATE = "approximate"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Availability:
    support: Support
    reason: str = ""


def identify(path: str | Path) -> Kind:
    """What produced this file, judged by its content, not its name."""
    path = Path(path)
    with open(path, "rb") as f:
        head = f.read(4096)
    if head[4:8] == b"ftyp":
        try:
            read_gpmd(path)
        except ValueError as exc:
            raise UnsupportedFile(
                f"{path.name}: video has no GoPro telemetry. Upload the original "
                "file from the camera's SD card, not an edited or exported copy. "
                "(The HERO12 records no GPS.)"
            ) from exc
        return Kind.GOPRO
    text = head.decode("latin-1").lower()
    if "[column names]" in text or "file created on" in text:
        return Kind.VBO
    raise UnsupportedFile(
        f"{path.name}: not a GoPro video or a VBO export. From RaceBox, export "
        "the session as VBO on racebox.pro."
    )


_GOPRO_NAME = re.compile(r"^G[HXL](\d{2})(\d{4})\.MP4$", re.IGNORECASE)


def _recording_order(path: Path) -> tuple:
    """GoPro splits long recordings into chapters: GX010042, GX020042, ...

    Order by recording number, then chapter, so chapters stay together.
    """
    m = _GOPRO_NAME.match(path.name)
    return (int(m.group(2)), int(m.group(1)), "") if m else (10_000, 0, path.name)


@dataclass
class Video:
    path: Path
    payloads: list[Payload]
    gps: Telemetry | None  # None when the camera never got a fix


def _concat(parts: list[Telemetry], source: str) -> Telemetry:
    parts = sorted(parts, key=lambda t: t.time[0])
    for a, b in zip(parts, parts[1:]):
        if b.time[0] <= a.time[-1]:
            raise ValueError(f"overlapping {source} recordings; upload each session once")
    return Telemetry(
        source=source,
        time=np.concatenate([p.time for p in parts]),
        **{c: np.concatenate([getattr(p, c) for p in parts])
           for c in ("lat", "lon", "alt", "speed", "heading", "accel_long", "accel_lat", "accel_vert")},
    )


def _overlaps(a: Telemetry, b: Telemetry) -> bool:
    return a.time[0] <= b.time[-1] and b.time[0] <= a.time[-1]


@dataclass
class Session:
    videos: list[Video] = field(default_factory=list)
    logger: Telemetry | None = None
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_files(cls, paths: list[str | Path]) -> Session:
        if not paths:
            raise UnsupportedFile("no files uploaded")
        session = cls()
        logs = []
        for path in sorted(map(Path, paths), key=_recording_order):
            kind = identify(path)
            if kind is Kind.GOPRO:
                payloads = read_gpmd(path)
                try:
                    gps = decode_gps5(payloads)
                except ValueError:
                    gps = None
                session.videos.append(Video(path, payloads, gps))
            else:
                logs.append(read_vbo(path, source="racebox"))
        if logs:
            session.logger = _concat(logs, "racebox")
        session._check()
        return session

    def _check(self) -> None:
        if self.videos and self.logger is None and self.camera_gps is None:
            self.warnings.append(
                "The GoPro never got a GPS fix, so there is no position data. "
                "Turn GPS on and wait for a lock before recording, or add a "
                "RaceBox file."
            )
        if self.logger is not None and self.camera_gps is not None and not _overlaps(self.logger, self.camera_gps):
            self.warnings.append(
                "The RaceBox and GoPro recordings don't overlap in time. "
                "Are they from the same session?"
            )

    @property
    def camera_gps(self) -> Telemetry | None:
        parts = [v.gps for v in self.videos if v.gps is not None and len(v.gps)]
        return _concat(parts, "gopro") if parts else None

    @property
    def setup(self) -> Setup:
        if self.videos and self.logger is not None:
            return Setup.GOPRO_RACEBOX
        return Setup.GOPRO_ONLY if self.videos else Setup.RACEBOX_ONLY

    @property
    def position(self) -> Telemetry | None:
        """The best position source: the RaceBox when present, else the camera."""
        return self.logger if self.logger is not None else self.camera_gps

    @property
    def method(self) -> str:
        """The position source's key in the accuracy table."""
        return METHOD[self.setup]

    def features(self, event: str | None = None) -> dict[Feature, Availability]:
        """What this setup delivers. `event` ("track" or "autocross") picks
        which measured errors the reasons quote; without it they quote both."""
        if self.position is None:
            none = Availability(Support.UNAVAILABLE, "no GPS data")
            return {f: none for f in Feature}

        method = self.method
        events = [event] if event else list(accuracy.EVENTS)

        def quote(quantity: str) -> str:
            parts = []
            for e in events:
                err = accuracy.error(e, method, quantity)
                parts.append(f"{accuracy.label(quantity, e)}{'' if event else f' ({e})'}: {err.summary()}.")
            return " ".join(parts)

        if self.setup is Setup.GOPRO_ONLY:
            add_racebox = "Add a RaceBox file for full accuracy."
            camera = "GoPro GPS is accurate to about 2 m."
            slow_start = {
                None: " Autocross runs fare worst, because the car crosses the start line at walking pace.",
                "autocross": " Most of that error is at the start line, which the car crosses at walking pace.",
                "track": "",
            }[event]
            return {
                Feature.LAP_TIMES: Availability(Support.APPROXIMATE, f"{camera} {quote('lap')}{slow_start} {add_racebox}"),
                Feature.VIDEO_OVERLAY: Availability(Support.FULL),
                Feature.DELTA: Availability(Support.APPROXIMATE, (
                    f"{camera} {quote('sector')} Small gains and losses may not be real. {add_racebox}"
                )),
                Feature.OPTIMAL: Availability(Support.APPROXIMATE, (
                    f"{camera} {quote('optimal')} It comes out too fast because taking the best of each "
                    f"sector also picks the sectors whose GPS error flattered them. {add_racebox}"
                )),
                Feature.LINE_COMPARISON: Availability(Support.UNAVAILABLE, "Comparing lines needs sub-metre accuracy. Add a RaceBox file."),
            }

        overlay = Availability(Support.FULL) if self.videos else Availability(
            Support.UNAVAILABLE, "Add your GoPro video for an overlay.")
        return {
            Feature.LAP_TIMES: Availability(Support.FULL, f"RaceBox GPS. {quote('lap')}"),
            Feature.VIDEO_OVERLAY: overlay,
            Feature.DELTA: Availability(Support.FULL, f"RaceBox GPS. {quote('sector')}"),
            Feature.OPTIMAL: Availability(Support.FULL, f"RaceBox GPS. {quote('optimal')}"),
            Feature.LINE_COMPARISON: Availability(Support.FULL),
        }
