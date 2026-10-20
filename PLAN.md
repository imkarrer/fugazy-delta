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
| Lap and run times | Approximate (19 laps in 20 within ±0.11 s, autocross runs ±0.70 s) | Full | Full |
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

Working from the rating alone gives ±0.1 s for the GoPro: a 2 m error at
30 m/s shifts a crossing by about 0.07 s, and a time takes two crossings.
That holds for laps, where 19 in 20 GoPro lap times fall within ±0.11 s.
For autocross runs it is about seven times too optimistic, because the car
crosses the start line at walking pace, so the app quotes ±0.70 s. The
RaceBox does better at the same start but still only to ±0.22 s for 19 runs
in 20, so "Full" for its autocross run times means as good as GPS gets, not
the event timing lights' 0.001 s.

### How accurate each time is

Every time the app shows carries the measured error of the position source
that produced it. GoPro only is timed from the camera's GPS; GoPro +
RaceBox and RaceBox only are both timed from the RaceBox, so they share the RaceBox
rows. The results page shows the rows for the current setup and event
under "How accurate is this?", the time columns carry their "95% within"
figure, and `Session.features()` quotes the same numbers in its reasons. In
the table, bias is the mean of measured minus true time, so a negative bias
means the app reports a time faster than the truth. "95% within" is the
95th percentile of the absolute error. n counts laps or runs, sectors, or
sessions (best and optimal: one per session).

| Event | Measured with | Time | n | Bias (95% CI) | RMS | 95% within (95% CI) | Worst |
|---|---|---|---|---|---|---|---|
| track | GoPro GPS | Lap times | 5000 | 0.000 (−0.001 to +0.001) | 0.056 | ±0.11 (0.106 to 0.112) | 0.22 |
| track | GoPro GPS | Sector times | 15000 | 0.000 (0.000 to 0.000) | 0.12 | ±0.24 (0.239 to 0.247) | 0.49 |
| track | GoPro GPS | Sector 1 times | 5000 | −0.001 (−0.005 to +0.003) | 0.13 | ±0.25 (0.243 to 0.256) | 0.49 |
| track | GoPro GPS | Sector 2 times | 5000 | −0.001 (−0.005 to +0.003) | 0.13 | ±0.26 (0.253 to 0.267) | 0.49 |
| track | GoPro GPS | Sector 3 times | 5000 | +0.002 (−0.001 to +0.005) | 0.11 | ±0.21 (0.206 to 0.216) | 0.43 |
| track | GoPro GPS | Best lap | 1000 | −0.010 (−0.014 to −0.007) | 0.052 | ±0.099 (0.092 to 0.105) | 0.19 |
| track | GoPro GPS | Optimal lap | 1000 | −0.21 (−0.22 to −0.20) | 0.25 | ±0.45 (0.442 to 0.465) | 0.68 |
| track | RaceBox | Lap times | 5000 | 0.000 (0.000 to 0.000) | 0.018 | ±0.034 (0.034 to 0.035) | 0.060 |
| track | RaceBox | Sector times | 15000 | 0.000 (0.000 to 0.000) | 0.036 | ±0.071 (0.069 to 0.072) | 0.15 |
| track | RaceBox | Sector 1 times | 5000 | −0.001 (−0.002 to 0.000) | 0.035 | ±0.070 (0.068 to 0.072) | 0.14 |
| track | RaceBox | Sector 2 times | 5000 | 0.000 (−0.001 to +0.002) | 0.041 | ±0.082 (0.079 to 0.084) | 0.15 |
| track | RaceBox | Sector 3 times | 5000 | 0.000 (−0.001 to +0.001) | 0.029 | ±0.057 (0.056 to 0.059) | 0.13 |
| track | RaceBox | Best lap | 1000 | −0.002 (−0.003 to −0.001) | 0.018 | ±0.034 (0.032 to 0.037) | 0.060 |
| track | RaceBox | Optimal lap | 1000 | −0.029 (−0.032 to −0.026) | 0.054 | ±0.11 (0.099 to 0.111) | 0.17 |
| autocross | GoPro GPS | Run times | 4778 | 0.000 (−0.009 to +0.010) | 0.35 | ±0.70 (0.682 to 0.724) | 1.06 |
| autocross | GoPro GPS | Sector times | 14334 | 0.000 (−0.003 to +0.003) | 0.23 | ±0.47 (0.461 to 0.481) | 1.12 |
| autocross | GoPro GPS | Sector 1 times | 4778 | +0.009 (−0.001 to +0.017) | 0.32 | ±0.63 (0.606 to 0.648) | 1.12 |
| autocross | GoPro GPS | Sector 2 times | 4778 | −0.013 (−0.018 to −0.007) | 0.19 | ±0.36 (0.356 to 0.377) | 0.77 |
| autocross | GoPro GPS | Sector 3 times | 4778 | +0.004 (+0.001 to +0.008) | 0.13 | ±0.25 (0.240 to 0.254) | 0.51 |
| autocross | GoPro GPS | Best run | 1000 | −0.27 (−0.29 to −0.26) | 0.34 | ±0.60 (0.586 to 0.646) | 0.96 |
| autocross | GoPro GPS | Optimal run | 1000 | −0.52 (−0.53 to −0.50) | 0.57 | ±0.90 (0.878 to 0.946) | 1.53 |
| autocross | RaceBox | Run times | 5000 | +0.004 (0.000 to +0.007) | 0.11 | ±0.22 (0.212 to 0.226) | 0.48 |
| autocross | RaceBox | Sector times | 15000 | +0.001 (0.000 to +0.002) | 0.075 | ±0.17 (0.162 to 0.170) | 0.45 |
| autocross | RaceBox | Sector 1 times | 5000 | +0.003 (0.000 to +0.007) | 0.12 | ±0.23 (0.219 to 0.233) | 0.45 |
| autocross | RaceBox | Sector 2 times | 5000 | +0.001 (0.000 to +0.003) | 0.047 | ±0.090 (0.088 to 0.094) | 0.18 |
| autocross | RaceBox | Sector 3 times | 5000 | −0.001 (−0.002 to 0.000) | 0.032 | ±0.062 (0.061 to 0.064) | 0.11 |
| autocross | RaceBox | Best run | 1000 | −0.047 (−0.052 to −0.041) | 0.097 | ±0.19 (0.185 to 0.200) | 0.29 |
| autocross | RaceBox | Optimal run | 1000 | −0.099 (−0.10 to −0.094) | 0.13 | ±0.25 (0.236 to 0.260) | 0.38 |

What the table shows:

- **Lap, run and sector times are essentially unbiased.** Both ends of a
  lap move with the same slowly wandering position error, so the error
  does not push them one way. The only bias above the 0.01 s the app
  shows is 0.013 s, in the GoPro's autocross sector 2: it is real, because
  its interval excludes zero, but smaller than a tenth of that sector's
  RMS error.
- **The optimal time is biased fast, for both receivers.** Taking the
  fastest of five noisy times for each sector also picks the sector whose
  error happened to flatter it. The GoPro's optimal lap reads 0.21 s fast
  and its optimal run 0.52 s fast, against 0.03 s and 0.10 s for the
  RaceBox. The
  best lap or run is biased the same way, less, because it is one pick
  from five times rather than one pick per sector.
- **Autocross sector 1 carries the slow start.** The GoPro's sector 1 times
  are 95% within ±0.63 s against ±0.25 s for sector 3.
- **About 1 GoPro run in 23 is flagged, not timed** (4.4%): GPS error put
  the staged car past the start line, so there is no crossing to time. One
  run in 1250 was not found at all. Neither kind is in the run-time row.
  The RaceBox flagged none.

The app reports the optimal time as measured and states its bias; it does
not subtract it. The bias depends on how close together the laps are: the
measurement draws each lap's pace in each sector between 94% and 100% of
the car's grip, which spreads the five true lap times by 0.38 s (track) and
0.26 s (autocross, medians). A driver whose laps are closer than that
gets a larger bias, and one whose laps are further apart a smaller one, so
subtracting one fixed number would correct some sessions too far and
others not enough.

#### How the table is measured

`fugazy_delta/accuracy.py` builds the table and `python -m
fugazy_delta.accuracy` regenerates `fugazy_delta/accuracy_table.json`.

- **Ground truth** is the virtual car's own lap, run and sector times
  (`sim.py`), the moments its true path crosses each timing line.
- **Sessions:** 1000 seeds per event and receiver. Each seed drives five
  timed laps (plus an out-lap) of the example track or five runs of the
  example autocross, at random pace as described above, and records it
  with its own receiver error.
- **Receivers:** `HERO9_NOISE` (1.7 m per axis, wandering over 20 s, 0.2 s
  latency, 18 Hz, written as GPMF and decoded back) and `RACEBOX_NOISE`
  (0.5 m per axis, wandering over 5 s, 25 Hz).
- **Timing:** `segment.find_laps`, `best_lap` and `optimal_lap`, the code
  the app runs. Flagged and missed laps are counted, not scored. Best and
  optimal are compared with the true best and optimal over the same
  counted laps.
- **Uncertainty:** every bias, RMS and percentile has a 95% interval and a
  standard error from 2000 bootstrap resamples of whole sessions, because
  errors within one recording are correlated.
- **Staying current:** the JSON also records the statistics of the first
  40 seeds alone. `tests/test_timing_accuracy.py` re-measures those seeds
  and fails when any statistic has moved by more than twice the table's
  standard error, or when a noise model, rate, course or pace setting no
  longer matches the one the table was made with.

Limits: everything here is simulated. The noise models come from the
receivers' ratings plus assumptions (the HERO9's error wander time and
latency, the RaceBox's 0.5 m), and none of it has been checked against a
real RaceBox or HERO9 recording yet. Two courses, one car and one pace
spread are measured; a course whose start line is crossed even slower, or
a driver more consistent than the simulated one, will see larger errors.
The first real session with both devices running should be timed against
the event's timing lights to calibrate both models.

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
