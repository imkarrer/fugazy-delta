# Spike: a synthetic GoPro MP4 that real tools accept

**Outcome: works.** `fugazy_delta.fixtures.gopro_mp4` writes an MP4 that
ffprobe, ffmpeg and GoPro's own reference parser (`gpmf-parser`'s
`GPMF_demo`) all read as a GoPro telemetry file. Tests in
`tests/test_gopro_mp4.py` and `tests/test_real_gopro.py` keep it that way.

## Question

Can we produce a HERO9-like MP4 — video plus a GPMF telemetry track — with no
camera, so that every later stage (sync, overlay rendering) can be tested
against a file whose true timing is known?

## Approach

1. **Video:** ffmpeg `testsrc2` at GoPro's 30000/1001 fps, with `drawtext`
   printing each frame's true UTC time (`%{pts:hms:<seconds-of-day>}`) and
   frame number. The font comes from the Flox environment (`dejavu_fonts`) so
   the render is identical on every machine.
2. **Telemetry:** `gpmf.hero9_payloads` encodes one GPMF payload per 1.001 s:
   DEVC → STRM(ACCL, 200 Hz) + STRM(GPS5, 18 Hz, with GPSU, GPSF, GPSP, UNIT,
   SCAL, STMP, TSMP), from the `Truth`.
3. **Mux:** ffmpeg cannot build a timed data track from raw bytes, so
   `mp4.add_gpmd_track` edits the MP4 directly: grow the existing `mdat` to
   hold the payloads, then append a `trak` to `moov` with handler `meta`
   ("GoPro MET") and sample entry `gpmd`, one sample per payload. The video's
   chunk offsets do not move. `mvhd.creation_time` is set to the simulated
   camera clock.

## What it took — two findings worth keeping

- **One `mdat` only.** The first attempt appended a second `mdat`. ffprobe
  and ffmpeg were happy; GoPro's reader was not: it counts `mdat` and `moov`
  as "required tags" and stops reading after the second one it sees, so it
  never reached `moov`. Real GoPro files have a single `mdat`; ours now do
  too. Any reader of ours should not depend on this, but our fixtures must
  not either.
- **Strings are one sample.** GPMF `c` strings like `SIUN` are encoded with
  struct size = string length and repeat = 1. Encoding them as N one-byte
  samples parses, but GoPro's reader then prints garbage units. Fixed-width
  unit lists (`UNIT`, size 3 × 5) pad with trailing NULs (`m\0\0`), not
  leading.

## Verified

| Check | Tool | Result |
|---|---|---|
| Stream is seen as GoPro telemetry | ffprobe | `codec_tag_string=gpmd`, handler `GoPro MET` |
| Bytes extract unchanged | `ffmpeg -map 0:1 -codec copy` | identical to payloads written |
| Rates | `GPMF_demo` | GPS5 17.98 Hz, ACCL 199.8 Hz (= 18 and 200 per 1.001 s) |
| Values | `GPMF_demo -fGPS5` | lat/lon/speed match truth |
| Frame timing | ffmpeg `showinfo` | frame n at n × 1001/30000 s |
| Our reader on real files | `read_gpmd` + `decode_gps5` | hero5/hero6 samples decode at ~18 Hz in Carlsbad, CA |

## Not covered yet

- **No real HERO9 sample.** GoPro's public samples stop at the HERO8 (and
  `hero8.mp4` has no GPS fix). The HERO9 writes the same GPS5 stream as the
  HERO5–8, so the format risk is low, but one real HERO9 clip from the
  user's camera should be added as a fixture as soon as it exists.
- **Accelerometer axes.** `hero9_payloads` assumes `ORIN "ZXY"` with a
  forward-facing, level camera. Verify against a real mounted clip before
  the sync fallback (matching motion data) relies on it.
- **The burned-in stamp is not read back.** Tests check frame timing, not
  the pixels. Reading the stamp (OCR) belongs to the overlay stage.
- **Chaptered recordings** (GX01…, GX02…) and files over 4 GiB are not
  generated yet.
