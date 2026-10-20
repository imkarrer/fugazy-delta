# fugazy-delta build plan

A hosted web app for autocross (CVSCC) and track days (RevMatch, Chin Track
Days): a driver drops in the files from a RaceBox Mini and a GoPro and gets
their runs or laps, the time delta between them, an optimal lap, the three
places they lose the most time, and an overlay video. That covers Garmin
Catalyst's post-session features; live audio coaching is out of scope.

The author (Isaac) runs a RaceBox Mini and a GoPro HERO9 in a Miata. The plan
works backwards from the upload, because whether anyone besides the author
uses the app depends on how easy the upload is.

## 1. What the user does

```mermaid
flowchart LR
    A[Open event link<br/>phone or laptop] --> B[Name + car number<br/>no account]
    B --> C[Drop files<br/>any mix, any order]
    C --> D[Results in under a minute<br/>runs/laps, times, deltas]
    D --> E[Optional: send video<br/>later, on wifi]
    E --> F[Overlay and optimal-lap<br/>video to download or post]
```

Rules that keep it simple:

- **No "pick your device" step.** The app identifies each file itself: GoPro
  MP4, RaceBox VBO or CSV, GPX.
- **No manual sync.** GoPro and RaceBox are aligned on GPS time.
- **No course drawing.** Track days match a library of known tracks by GPS.
  At an autocross the organiser sets start and finish once per event, or
  they are inferred from where every driver's runs begin and end.
- **Results do not wait for video.** Times, deltas and the optimal lap come
  from telemetry alone; video is an add-on.
- **Location data:** one consent line at upload, a delete button on every
  session, nothing sold or shared.

### The GoPro file is the hard part

A GoPro chapter is up to 4 GB; nobody uploads that over a cell connection
from a paddock. The telemetry inside it is a few MB. So the browser pulls
only the telemetry track out of the MP4 and uploads that; the video stays
on the user's device until they choose to send it for rendering.

| Device | How the file reaches the app | Friction |
|---|---|---|
| RaceBox Mini | Phone app syncs to racebox.pro; export VBO or CSV from the session page | Low. No public cloud API, so no account linking |
| GoPro HERO9 | SD card into a laptop, or the Quik app on a phone | Highest, so test with real people first |
| Phone only | GPX from any lap-timer app | Low, lower quality |

Before any backend work, a clickable prototype on fake data goes in front
of 3–5 people at an event, to see where the GoPro step loses them. The
prototype is in `prototype/`, and the README says how to run it.

### Three setups, one upload

A RaceBox is optional. Most club members own a GoPro and no RaceBox, so
GoPro only is a full setup, not a fallback. The mix of files decides the
setup (`fugazy_delta/session.py`), and the results page labels each
feature as full, approximate or unavailable, with the reason and what
adding a device would unlock.

| Feature | GoPro only | GoPro + RaceBox | RaceBox only |
|---|---|---|---|
| Lap and run times | Approximate (laps ±0.15 s, autocross runs ±0.7 s) | Full | Full |
| Video overlay | Full | Full | Unavailable |
| Delta, optimal lap | Approximate | Full | Full |
| Racing line comparison | Unavailable | Full | Full |

The ratings rest on the receivers' position error, not their sample rate.
The app interpolates the moment the car crosses a line between two fixes
(`fugazy_delta/segment.py`), so 18 Hz against 25 Hz moves that moment very
little; a position error moves it by the error divided by the car's speed
at the line. GoPro rates the HERO9's receiver at 2 m CEP, meaning half its
fixes fall within 2 m of the true position, while the RaceBox's error is
about 0.6 m. The simulator models both receivers (`HERO9_NOISE`, `RACEBOX_NOISE`
in `fugazy_delta/devices.py`), and `tests/test_device_noise.py` pins each
to its rating.

`tests/test_timing_accuracy.py` measures the timing that results: the
virtual car, recorded by each simulated receiver, split into laps or runs
and compared with the true times. Over 20 seeds (100 laps, 120 runs), the
error in a lap or run time was:

| Where the timing line is | RaceBox: RMS error / worst | GoPro only: RMS error / 19 in 20 within / worst |
|---|---|---|
| Track start/finish, main straight, car at 40 m/s | 0.017 s / 0.05 s | 0.06 s / 0.13 s / 0.15 s |
| Autocross start, 3 m after launch, car at 4.5 m/s | 0.11 s / 0.42 s | 0.37 s / 0.68 s / 0.92 s |

Working from the rating alone gives ±0.1 s for the GoPro: a 2 m error at
30 m/s shifts a crossing by about 0.07 s, and a time takes two crossings.
That holds for about 9 laps in 10, so the app claims ±0.15 s for laps. For
autocross runs ±0.1 s is about seven times too optimistic, because the car
crosses the start line at walking pace. The app therefore claims ±0.7 s for
runs. In about 1 GoPro run in 17, GPS error also puts the staged car past the start line, so there is no
crossing: the run is timed from when the car moves off and flagged, not
counted. The RaceBox does better at the same start but still only to about
±0.2 s for 19 runs in 20, so "Full" for its autocross run times means as
good as GPS gets, not the event timing lights' 0.001 s. Optimal times from
GoPro-only data come out too fast: each sector time carries the error, and
taking the best of each picks the errors that flatter.

## 2. The system

```mermaid
flowchart TB
    subgraph Browser
        U[Drop zone] --> X[Copy the gpmd track out of the MP4<br/>video stays local]
        U --> R[RaceBox / GPX files<br/>small, sent as-is]
    end
    subgraph Backend
        P[Parsers to one Telemetry type] --> S[Sync on GPS time<br/>fallback: match motion data]
        S --> SEG[Split into runs/laps<br/>start/finish lines, sectors]
        SEG --> AN[Analysis<br/>delta, optimal lap, top 3 losses]
        AN --> RES[Results page]
        AN --> REN[Render jobs<br/>overlay, optimal-lap composite]
    end
    X --> P
    R --> P
    REN --> PUB[fugazy-publish<br/>YouTube, TikTok, Instagram]
```

The browser does one thing — copy the telemetry bytes out of the MP4 — and
all parsing happens in Python on the backend, so there is one parser and
one test suite.

| Component | Job | Build or reuse |
|---|---|---|
| Browser extraction | Copy the `gpmd` track out of a multi-GB MP4 without reading the whole file | Reuse mp4box.js; **spike** on a 4 GB file |
| Parsers | GoPro GPMF (GPS5, ACCL, GYRO), RaceBox VBO and CSV, GPX → `Telemetry` | GPMF and VBO done in phase 0; CSV and GPX to do |
| Sync | Align devices on GPS time; handle chaptered GoPro files; fall back to matching motion data | Build |
| Fusion | Prefer RaceBox 25 Hz position; use GoPro GPS when there is no RaceBox | Build |
| Segmentation | Start/finish crossings, sectors, staging, out- and in-laps | Started in `segment.py`: crossings, sectors, out- and in-laps, staging, DNFs, dropouts; tested on the virtual car only |
| Track library | Start/finish and sector lines for the tracks the user runs | Build, starting with the user's tracks |
| Analysis | Speed against distance, delta-t, minimum corner speeds, braking points, distance-based mini-sectors | Build |
| Optimal lap | Best mini-sector from each lap, **only from lines driven more than once**, so the result is achievable | Build |
| Top 3 losses | The sectors losing the most against your own best, with run-vs-run clips | Build |
| Overlay render | Speed, g-circle, map, delta burned into video | Reuse `gopro-overlay` as a base (check licence first) |
| Composite video | Cut the best segments from several laps together with a running combined time | Build on ffmpeg |
| Publish | Hand the finished video to `fugazy-publish` | Exists; its Flox environment is in progress |
| Environment | One Flox environment for development, CI and the render worker; `flox containerize` for deploy | Flox |

### HERO9 specifics

The author's camera is a HERO9 Black. It writes GPS5 at ~18 Hz (lat, lon,
alt, 2D speed, 3D speed), ACCL and GYRO at ~200 Hz, and **one** GPSU UTC
timestamp per ~1 s payload. The 18 GPS samples in a payload have no
timestamps of their own: readers spread them evenly over the payload's
duration from the MP4 sample table. Sync therefore depends on

1. GPSU being accurate to the payload, and
2. the even-spread assumption holding within the payload.

GoPro's GPS timestamps may also lag the position they label (community
reports; unverified). So the sync stage measures the GoPro's
offset against the RaceBox (whose 25 Hz samples each carry their own UTC
time) rather than trusting GPSU, and the virtual-car tests inject a known
GPSU latency (`CameraClock.gpsu_latency`) so that stage can be checked
against a known answer. The camera's own clock, written as MP4
`creation_time`, is not trusted for anything.

GoPro's public sample files stop at the HERO8, which writes the same GPS5
stream. One real HERO9 clip from the author's camera becomes a fixture as
soon as it exists.

## 3. Testing without the hardware

```mermaid
flowchart LR
    T[Course<br/>straights + arcs] --> V[Speed profile<br/>grip and power limits]
    V --> GT[Truth at 200 Hz<br/>known lap and sector times]
    GT --> RBX[Simulated RaceBox<br/>25 Hz on UTC epochs + GNSS error<br/>→ VBO]
    GT --> GPX2[Simulated HERO9<br/>GPS5 18 Hz + ACCL 200 Hz<br/>+ known GPSU latency and camera clock]
    GPX2 --> MUX[MP4: test pattern with true UTC<br/>printed on every frame + gpmd track]
```

Four layers:

1. **Real files.** GoPro's `gpmf-parser` samples (HERO5, HERO6, HERO8),
   fetched and checksum-pinned by `scripts/fetch-fixtures.sh`. For the
   RaceBox, one real VBO and CSV export from a club member gives the true
   column layout.
2. **Virtual car with known answers.** Because the truth is known, tests
   assert exact outcomes:
   - sync recovers the injected GoPro offset to within one video frame
   - with perfect GPS, lap and run times within 1 ms of truth; with the
     simulated receivers, within the errors in section 1
   - optimal lap equals the sum of the best sectors, built in on purpose:
     for example, lap 2 has the best sector 1 and lap 3 the best sector 2
   - the overlay on the frame stamped *t* shows the truth at *t*
3. **Messy data.** GPS dropouts, no fix at the start, a recording split
   across two chapters, truncated files, drifting clocks, the wrong file
   type, a red-flagged lap, an autocross DNF or rerun.
4. **Bluetooth emulator (later).** An open-source ESP32 RaceBox Mini
   emulator speaks RaceBox's published BLE protocol, if live import is ever
   added. Not in v1.

## 4. Phases

| Phase | Delivers | Hardware needed |
|---|---|---|
| 0. Foundations | Flox environment, `Telemetry` model, virtual car, VBO and GPMF codecs, synthetic GoPro MP4 (works; see `docs/spikes/gpmf-mux.md`), CI, clickable prototype tested on real people. Prototype built in `prototype/` but not yet tested on anyone. CI runs every test, real GoPro files included, in the Flox environment on each push to main (`.github/workflows/ci.yml`) | None |
| 1. Autocross results | Upload → runs, times, run-vs-run delta, from telemetry only. **The demo for the sponsorship proposal** | None |
| 2. Track days | Track library, laps, sectors, optimal lap time | None |
| 3. Video | Overlay render, optimal-lap composite video, top 3 losses | RaceBox and GoPro (arriving next year) |
| 4. Event mode | Organiser page for CVSCC with every driver's results; hand-off to `fugazy-publish` | — |

Phases 0–2 need no hardware, so they can be done before the 2027 season.

## 5. Decisions

| Decision | Choice |
|---|---|
| Hosted or local | Hosted, with telemetry extraction in the browser |
| Repository | New repo `fugazy-delta`. `fugazy-publish` stays the final step and gets its own Flox environment, while keeping its native Windows path |
| Camera | GoPro HERO9 Black (GPS5). The HERO12 has no GPS; avoid it |
| Location data | Consent line and delete button from the first version |

## 6. Open risks

- **Browser extraction from a 4 GB file** — mp4box.js must read the `moov`
  without loading the whole file. Spike before phase 1 depends on it.
- **Getting files off the GoPro** — the prototype test decides whether phone upload
  is viable or the laptop is the path.
- **RaceBox export columns** — the VBO reader matches columns by name; the
  names RaceBox actually uses are unconfirmed until a real export is in hand.
- **GoPro accelerometer axes** — assumed `ORIN "ZXY"` with a forward-facing
  level camera. Verify on a real mounted clip before the fallback that syncs
  devices by matching motion data relies on it.
