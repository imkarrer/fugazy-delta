"""GoPro MP4 importer: the GPMF telemetry track's GPS, read by content.

Any ISO-BMFF file (an `ftyp` box at byte 4) is claimed here, because a
video is never anything else we read. One without a GoPro telemetry track
is rejected with advice, since that is almost always an edited or
re-exported copy, or a HERO12, which records no GPS.
"""

from __future__ import annotations

from pathlib import Path

from .gpmf import decode, decode_gps
from .importers import COARSE, Head, Kind, Recording, UnsupportedFile, register
from .mp4 import read_gpmd


def sniff(head: Head) -> bool:
    if head.first[4:8] != b"ftyp":
        return False
    try:
        read_gpmd(head.path)
    except ValueError as exc:
        raise UnsupportedFile(
            f"{head.path.name}: video has no GoPro telemetry. Upload the original "
            "file from the camera's SD card, not an edited or exported copy. "
            "(The HERO12 records no GPS.)"
        ) from exc
    return True


def camera_name(payloads) -> str:
    """'GoPro HERO9 Black', from the first payload's device name."""
    for payload in payloads[:3]:
        for devc in decode(payload.data):
            name = devc.find("DVNM") if devc.key == "DEVC" else None
            if name is not None:
                text = name.text()
                return text if text.lower().startswith("gopro") else f"GoPro {text}"
    return "GoPro"


@register(Kind.GOPRO, sniff, binary=True)
def read_gopro(path: Path) -> Recording:
    payloads = read_gpmd(path)
    try:
        gps = decode_gps(payloads)
    except ValueError:
        gps = None
    return Recording(Kind.GOPRO, gps, camera_name(payloads), profile=COARSE, camera=True, payloads=payloads)
