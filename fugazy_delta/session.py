"""Turn whatever files a driver drops in into one session.

The driver never says which devices they used. Each file is identified by
its content (`importers.identify`), and the mix of files decides the
setup:

- GoPro only: video plus the camera's own GPS
- GoPro + logger: video, positioned by the logger (a RaceBox, AiM, phone
  app or any other device `importers` reads)
- logger only: data, no video

When several loggers are in the upload, the one with UTC on every sample,
a dedicated GNSS receiver and the highest rate times the session.

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
from .gpmf import Payload
from .importers import COARSE, PRECISE, Kind, Recording, UnsupportedFile, identify, importers
from .model import Telemetry

__all__ = ["Kind", "UnsupportedFile", "identify", "Session", "Setup", "Feature", "Support", "Availability"]


class Setup(Enum):
    """Which kinds of device the upload has. "RaceBox" stands for any
    data logger or lap-timer app; `Session.setup_label` names the actual
    devices."""

    GOPRO_ONLY = "GoPro"
    GOPRO_RACEBOX = "GoPro + RaceBox"
    RACEBOX_ONLY = "RaceBox"


# Which position source times each setup when the logger is a RaceBox:
# the RaceBox whenever there is one. Other loggers quote their own
# profile (`Session.method`).
METHOD = {
    Setup.GOPRO_ONLY: COARSE,
    Setup.GOPRO_RACEBOX: PRECISE,
    Setup.RACEBOX_ONLY: PRECISE,
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
    device: str = "GoPro"


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
class Logger:
    """All the files one non-camera device produced, joined in time order."""

    device: str
    kind: Kind
    profile: str
    telemetry: Telemetry
    recordings: list[Recording]

    @property
    def utc(self) -> bool:
        return all(r.utc for r in self.recordings)

    @property
    def laps(self) -> list[float]:
        return sorted(t for r in self.recordings for t in r.laps)


def _rank(logger: Logger) -> tuple:
    """Which logger times the session: one with UTC on every sample first,
    then a dedicated GNSS logger over a phone, then the highest rate."""
    return (logger.utc, logger.profile == PRECISE, logger.telemetry.sample_rate)


@dataclass
class Session:
    videos: list[Video] = field(default_factory=list)
    loggers: list[Logger] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_files(cls, paths: list[str | Path]) -> Session:
        if not paths:
            raise UnsupportedFile("no files uploaded")
        session = cls()
        readers = {i.kind: i.read for i in importers()}
        groups: dict[tuple[str, Kind], list[Recording]] = {}
        for path in sorted(map(Path, paths), key=_recording_order):
            recording = readers[identify(path)](path)
            if recording.camera:
                session.videos.append(Video(path, recording.payloads or [], recording.telemetry, recording.device))
            elif recording.telemetry is None or not len(recording.telemetry):
                session.warnings.append(f"{path.name}: {recording.device} file has no GPS fixes.")
            else:
                groups.setdefault((recording.device, recording.kind), []).append(recording)
            for w in recording.warnings:
                if w not in session.warnings:
                    session.warnings.append(w)
        for (device, kind), recs in groups.items():
            source = recs[0].telemetry.source
            session.loggers.append(Logger(device, kind, recs[0].profile,
                                          _concat([r.telemetry for r in recs], source), recs))
        session.loggers.sort(key=_rank, reverse=True)
        session._check()
        return session

    @property
    def primary(self) -> Logger | None:
        """The logger whose positions time the session."""
        return self.loggers[0] if self.loggers else None

    @property
    def logger(self) -> Telemetry | None:
        return self.primary.telemetry if self.primary else None

    def _check(self) -> None:
        if self.videos and self.logger is None and self.camera_gps is None:
            self.warnings.append(
                "The GoPro never got a GPS fix, so there is no position data. "
                "Turn GPS on and wait for a lock before recording, or add a "
                "RaceBox file."
            )
        if self.logger is not None and self.camera_gps is not None and not _overlaps(self.logger, self.camera_gps):
            self.warnings.append(
                f"The {self.primary.device} and GoPro recordings don't overlap in time. "
                "Are they from the same session?"
            )
        for other in self.loggers[1:]:
            self.warnings.append(
                f"Timed from the {self.primary.device} file; the {other.device} file "
                "was read but not used for timing."
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
    def setup_label(self) -> str:
        """The setup in device names: 'GoPro + AiM', 'RaceChrono'."""
        names = []
        if self.videos:
            names.append("GoPro")
        if self.primary:
            names.append(self.primary.device)
        return " + ".join(names)

    @property
    def position(self) -> Telemetry | None:
        """The best position source: a logger when present, else the camera."""
        return self.logger if self.logger is not None else self.camera_gps

    @property
    def method(self) -> str:
        """The position source's key in the accuracy table."""
        return self.primary.profile if self.primary else COARSE

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

        overlay = Availability(Support.FULL) if self.videos else Availability(
            Support.UNAVAILABLE, "Add your GoPro video for an overlay.")

        if method == COARSE:
            add_racebox = "Add a RaceBox file for full accuracy."
            if self.primary is None:
                gps = "GoPro GPS is accurate to about 2 m."
            else:
                gps = (f"{self.primary.device} GPS is a phone or unknown receiver, "
                       "quoted with the GoPro's measured error (about 2 m).")
            slow_start = {
                None: " Autocross runs fare worst, because the car crosses the start line at walking pace.",
                "autocross": " Most of that error is at the start line, which the car crosses at walking pace.",
                "track": "",
            }[event]
            return {
                Feature.LAP_TIMES: Availability(Support.APPROXIMATE, f"{gps} {quote('lap')}{slow_start} {add_racebox}"),
                Feature.VIDEO_OVERLAY: overlay,
                Feature.DELTA: Availability(Support.APPROXIMATE, (
                    f"{gps} {quote('sector')} Small gains and losses may not be real. {add_racebox}"
                )),
                Feature.OPTIMAL: Availability(Support.APPROXIMATE, (
                    f"{gps} {quote('optimal')} It comes out too fast because taking the best of each "
                    f"sector also picks the sectors whose GPS error flattered them. {add_racebox}"
                )),
                Feature.LINE_COMPARISON: Availability(Support.UNAVAILABLE, "Comparing lines needs sub-metre accuracy. Add a RaceBox file."),
            }

        device = self.primary.device
        return {
            Feature.LAP_TIMES: Availability(Support.FULL, f"{device} GPS. {quote('lap')}"),
            Feature.VIDEO_OVERLAY: overlay,
            Feature.DELTA: Availability(Support.FULL, f"{device} GPS. {quote('sector')}"),
            Feature.OPTIMAL: Availability(Support.FULL, f"{device} GPS. {quote('optimal')}"),
            Feature.LINE_COMPARISON: Availability(Support.FULL),
        }
