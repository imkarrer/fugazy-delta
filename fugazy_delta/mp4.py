"""Just enough ISO-BMFF (MP4) to read and write a GoPro 'gpmd' track.

Reading: find the track whose sample description is 'gpmd', then turn its
sample tables (stts, stsz, stsc, stco/co64) into payload bytes plus their
start time and duration on the video timeline. The file is memory-mapped,
so a 4 GB GoPro chapter costs only the telemetry bytes actually read.

Writing (`add_gpmd_track`) is the test-fixture path: take an MP4 that
ffmpeg wrote with its moov at the end, grow its mdat to hold the GPMF
payloads, then rewrite moov with one more trak pointing into them. The
video's chunk offsets do not move, so the video track is untouched.
"""

from __future__ import annotations

import mmap
import struct
from datetime import datetime, timezone
from pathlib import Path

from .gpmf import Payload

MP4_EPOCH = datetime(1904, 1, 1, tzinfo=timezone.utc).timestamp()
_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"dinf", b"edts", b"udta"}


def _boxes(buf, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack(">I4s", buf[pos:pos + 8])
        header = 8
        if size == 1:
            size = struct.unpack(">Q", buf[pos + 8:pos + 16])[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header:
            raise ValueError(f"bad box size {size} at {pos}")
        yield kind, pos, size, header
        pos += size


def _child(buf, parent: tuple, kind: bytes):
    _, pos, size, header = parent
    for box in _boxes(buf, pos + header, pos + size):
        if box[0] == kind:
            return box
    return None


def _path(buf, parent: tuple, *kinds: bytes):
    box = parent
    for kind in kinds:
        box = _child(buf, box, kind)
        if box is None:
            return None
    return box


def _payload(buf, box) -> bytes:
    _, pos, size, header = box
    return bytes(buf[pos + header:pos + size])


def _top(buf, kind: bytes):
    for box in _boxes(buf, 0, len(buf)):
        if box[0] == kind:
            return box
    raise ValueError(f"no top-level {kind!r} box")


def _full_box_body(buf, box) -> bytes:
    return _payload(buf, box)[4:]  # skip version + flags


def _gpmd_trak(buf, moov):
    for trak in _boxes(buf, moov[1] + moov[3], moov[1] + moov[2]):
        if trak[0] != b"trak":
            continue
        stsd = _path(buf, trak, b"mdia", b"minf", b"stbl", b"stsd")
        if stsd is None:
            continue
        body = _full_box_body(buf, stsd)
        if bytes(body[8:12]) == b"gpmd":
            return trak
    return None


def read_gpmd(path: str | Path) -> list[Payload]:
    """All GPMF payloads in the file, timed on the video timeline."""
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as buf:
        moov = _top(buf, b"moov")
        trak = _gpmd_trak(buf, moov)
        if trak is None:
            raise ValueError(f"{path}: no gpmd telemetry track")
        mdhd = _full_box_body(buf, _path(buf, trak, b"mdia", b"mdhd"))
        version = buf[_path(buf, trak, b"mdia", b"mdhd")[1] + 8]
        timescale = struct.unpack(">I", mdhd[16:20] if version == 1 else mdhd[8:12])[0]
        stbl = _path(buf, trak, b"mdia", b"minf", b"stbl")

        stts = _full_box_body(buf, _child(buf, stbl, b"stts"))
        deltas = []
        for i in range(struct.unpack(">I", stts[:4])[0]):
            count, delta = struct.unpack(">II", stts[4 + 8 * i:12 + 8 * i])
            deltas.extend([delta] * count)

        stsz = _full_box_body(buf, _child(buf, stbl, b"stsz"))
        uniform, count = struct.unpack(">II", stsz[:8])
        sizes = [uniform] * count if uniform else list(struct.unpack(f">{count}I", stsz[8:8 + 4 * count]))

        co = _child(buf, stbl, b"stco")
        if co is not None:
            body = _full_box_body(buf, co)
            n = struct.unpack(">I", body[:4])[0]
            chunks = list(struct.unpack(f">{n}I", body[4:4 + 4 * n]))
        else:
            body = _full_box_body(buf, _child(buf, stbl, b"co64"))
            n = struct.unpack(">I", body[:4])[0]
            chunks = list(struct.unpack(f">{n}Q", body[4:4 + 8 * n]))

        stsc = _full_box_body(buf, _child(buf, stbl, b"stsc"))
        entries = [struct.unpack(">III", stsc[4 + 12 * i:16 + 12 * i]) for i in range(struct.unpack(">I", stsc[:4])[0])]

        offsets = []
        sample = 0
        for e, (first_chunk, per_chunk, _) in enumerate(entries):
            last_chunk = entries[e + 1][0] - 1 if e + 1 < len(entries) else len(chunks)
            for chunk in range(first_chunk, last_chunk + 1):
                pos = chunks[chunk - 1]
                for _ in range(per_chunk):
                    if sample >= len(sizes):
                        break
                    offsets.append(pos)
                    pos += sizes[sample]
                    sample += 1

        payloads = []
        t = 0
        for offset, size, delta in zip(offsets, sizes, deltas):
            payloads.append(Payload(bytes(buf[offset:offset + size]), t / timescale, delta / timescale))
            t += delta
        return payloads


def creation_time(path: str | Path) -> float:
    """mvhd creation_time as Unix seconds (the camera's own clock)."""
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as buf:
        mvhd = _child(buf, _top(buf, b"moov"), b"mvhd")
        body = _full_box_body(buf, mvhd)
        version = buf[mvhd[1] + 8]
        secs = struct.unpack(">Q", body[:8])[0] if version == 1 else struct.unpack(">I", body[:4])[0]
        return MP4_EPOCH + secs


# --- writing -----------------------------------------------------------------

def _box(kind: bytes, *parts: bytes) -> bytes:
    body = b"".join(parts)
    return struct.pack(">I4s", 8 + len(body), kind) + body


def _full(kind: bytes, version: int, flags: int, *parts: bytes) -> bytes:
    return _box(kind, struct.pack(">I", (version << 24) | flags), *parts)


def _gpmd_trak_box(track_id: int, payloads: list[Payload], data_offset: int, movie_timescale: int) -> bytes:
    timescale = 1000
    deltas = [round(p.duration * timescale) for p in payloads]
    duration = sum(deltas)
    movie_duration = round(duration * movie_timescale / timescale)
    identity = struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)

    tkhd = _full(b"tkhd", 0, 0x3,
                 struct.pack(">IIII", 0, 0, track_id, 0),
                 struct.pack(">I", movie_duration),
                 b"\0" * 8, struct.pack(">hhhH", 0, 0, 0, 0), identity, struct.pack(">II", 0, 0))
    mdhd = _full(b"mdhd", 0, 0, struct.pack(">IIIIHH", 0, 0, timescale, duration, 0x55C4, 0))
    hdlr = _full(b"hdlr", 0, 0, struct.pack(">I4s12x", 0, b"meta"), b"GoPro MET\0")
    gpmd_entry = _box(b"gpmd", b"\0" * 6, struct.pack(">H", 1), struct.pack(">I", 0))
    stsd = _full(b"stsd", 0, 0, struct.pack(">I", 1), gpmd_entry)

    runs = []
    for d in deltas:
        if runs and runs[-1][1] == d:
            runs[-1][0] += 1
        else:
            runs.append([1, d])
    stts = _full(b"stts", 0, 0, struct.pack(">I", len(runs)), b"".join(struct.pack(">II", c, d) for c, d in runs))
    stsc = _full(b"stsc", 0, 0, struct.pack(">I", 1), struct.pack(">III", 1, 1, 1))
    sizes = [len(p.data) for p in payloads]
    stsz = _full(b"stsz", 0, 0, struct.pack(">II", 0, len(sizes)), struct.pack(f">{len(sizes)}I", *sizes))
    offsets, pos = [], data_offset
    for s in sizes:
        offsets.append(pos)
        pos += s
    if pos < 2**32:
        stco = _full(b"stco", 0, 0, struct.pack(">I", len(offsets)), struct.pack(f">{len(offsets)}I", *offsets))
    else:
        stco = _full(b"co64", 0, 0, struct.pack(">I", len(offsets)), struct.pack(f">{len(offsets)}Q", *offsets))

    dinf = _box(b"dinf", _full(b"dref", 0, 0, struct.pack(">I", 1), _full(b"url ", 0, 1)))
    minf = _box(b"minf", _full(b"nmhd", 0, 0), dinf, _box(b"stbl", stsd, stts, stsc, stsz, stco))
    return _box(b"trak", tkhd, _box(b"mdia", mdhd, hdlr, minf))


def add_gpmd_track(src: str | Path, payloads: list[Payload], dst: str | Path, creation_utc: float | None = None) -> None:
    """Copy `src` to `dst` with a GoPro telemetry track holding `payloads`.

    `src` must have its moov after its mdat (ffmpeg's default without
    `-movflags +faststart`). If `creation_utc` is given, mvhd creation_time
    is set to it, as a camera writes its own clock there.
    """
    data = Path(src).read_bytes()
    boxes = list(_boxes(data, 0, len(data)))
    kinds = [b[0] for b in boxes]
    moov_at = kinds.index(b"moov")
    if moov_at == 0 or kinds[moov_at - 1] != b"mdat":
        raise ValueError("expected mdat immediately before moov; write the source without faststart")
    mdat = boxes[moov_at - 1]
    moov = boxes[moov_at]
    if mdat[3] != 8:
        raise ValueError("64-bit mdat not supported")

    # GoPro's reference reader stops after the first mdat and moov it sees,
    # so the payloads go into the existing mdat (grown in place) rather than
    # a second one. Video chunk offsets are unchanged.
    gpmf = b"".join(p.data for p in payloads)
    grown = mdat[2] + len(gpmf)
    if grown >= 2**32:
        raise ValueError("telemetry would push mdat past 4 GiB")
    head = bytearray(data[:moov[1]])
    head[mdat[1]:mdat[1] + 4] = struct.pack(">I", grown)
    data_offset = len(head)
    head += gpmf
    after = data[moov[1] + moov[2]:]

    children = []
    next_id = 1
    movie_timescale = 1000
    for kind, pos, size, header in _boxes(data, moov[1] + moov[3], moov[1] + moov[2]):
        raw = bytearray(data[pos:pos + size])
        if kind == b"mvhd":
            version = raw[8]
            if version != 0:
                raise ValueError("only version-0 mvhd supported")
            movie_timescale = struct.unpack(">I", raw[20:24])[0]
            next_id = struct.unpack(">I", raw[-4:])[0]
            raw[-4:] = struct.pack(">I", next_id + 1)
            if creation_utc is not None:
                secs = int(round(creation_utc - MP4_EPOCH))
                raw[12:20] = struct.pack(">II", secs, secs)
        children.append(bytes(raw))
    track = _gpmd_trak_box(next_id, payloads, data_offset, movie_timescale)
    # GoPro's reference reader stops at moov/udta, so the new trak goes after
    # the last existing trak rather than at the end of moov
    kinds = [c[4:8] for c in children]
    last_trak = max(i for i, k in enumerate(kinds) if k == b"trak")
    children.insert(last_trak + 1, track)
    new_moov = _box(b"moov", *children)
    Path(dst).write_bytes(bytes(head) + new_moov + after)
