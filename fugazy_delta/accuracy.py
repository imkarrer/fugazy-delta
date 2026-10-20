"""How far each reported time is from the truth, for each way of measuring it.

The app reports four kinds of time: each lap or run, each sector, the best
lap or run, and the optimal lap or run (the best time in each sector,
summed). It measures them from one of two position sources, which is what
decides their accuracy:

- `gopro`: the GoPro HERO9's own GPS (18 Hz, `HERO9_NOISE`). Used when the
  upload is GoPro only.
- `racebox`: the RaceBox Mini (25 Hz, `RACEBOX_NOISE`). Used whenever a
  RaceBox file is in the upload, with or without a GoPro, so GoPro + RaceBox
  and RaceBox only share one set of numbers.

For each event type (track day, autocross), position source and seed, this
module drives a session on the virtual car (`sim.py`), records it with the
simulated receiver, finds the laps with `segment.find_laps` and the best and
optimal times with `segment.best_lap` and `segment.optimal_lap`, the same
code the app uses, and subtracts the simulator's true times. Each seed gets
its own receiver error and its own driving: the pace of every lap in every
sector is drawn between `PACE_RANGE`, so the best and optimal times are
picked from laps a few tenths apart, as a club driver's are.

Per quantity the table holds the bias (mean signed error; negative means
the app reports a time faster than the truth), the RMS error, the 95th
percentile of the absolute error, the largest error and the sample count.
Each statistic carries a 95% confidence interval and a standard error from
a bootstrap that resamples whole seeds, because errors within one recording
are correlated (the receiver's error wanders slowly).

The committed table is `accuracy_table.json`. Regenerate it after changing
the simulator, a noise model or the lap finder:

    python -m fugazy_delta.accuracy

`tests/test_timing_accuracy.py` re-measures a subset of the seeds and fails
when the committed numbers have drifted beyond their standard errors.
"""

from __future__ import annotations

import json
import math
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from functools import cache
from pathlib import Path

import numpy as np

from .devices import HERO9_NOISE, RACEBOX_NOISE, RACEBOX_RATE_HZ, racebox_mini
from .gpmf import decode_gps5, hero9_payloads
from .segment import LapKind, best_lap, find_laps, optimal_lap
from .sim import Truth, autocross_session, example_autocross, example_track, track_session

TABLE_PATH = Path(__file__).with_name("accuracy_table.json")

EVENTS = ("track", "autocross")
METHODS = ("gopro", "racebox")
METHOD_LABELS = {"gopro": "GoPro GPS", "racebox": "RaceBox"}
METHOD_DETAILS = {
    "gopro": "GoPro HERO9 GPS, 18 Hz, about 2 m accuracy",
    "racebox": "RaceBox Mini, 25 Hz, about 0.6 m accuracy",
}
UNITS = {"track": "lap", "autocross": "run"}
SECTORS = 3  # both example courses have three sectors
QUANTITIES = ("lap", "sector", *(f"sector_{k + 1}" for k in range(SECTORS)), "best", "optimal")

SEEDS = 1000
LAPS = 5  # timed laps or runs per session; a track session also has an out-lap
PACE_RANGE = (0.94, 1.0)  # grip multiplier per lap and sector, drawn uniformly
GOPRO_HZ = 18.0
BOOTSTRAP = 2000
CHECK_SEEDS = 40  # how many seeds the test re-measures
MATCH_S = 2.0  # a found lap matches a true one when both ends are this close

# A bias smaller than this is not worth telling a driver about: the app
# shows approximate times to 0.01 s.
BIAS_FLOOR_S = 0.01


# ---------- measuring ----------

def session(event: str, seed: int) -> Truth:
    """The driving for one seed: `LAPS` laps or runs at random pace."""
    rng = np.random.default_rng([0xFD, seed])
    if event == "track":
        pace = rng.uniform(*PACE_RANGE, size=(LAPS + 1, SECTORS))
        return track_session(example_track(), pace.tolist(), in_lap=0.5)
    if event == "autocross":
        pace = rng.uniform(*PACE_RANGE, size=(LAPS, SECTORS))
        return autocross_session(example_autocross(), pace.tolist())
    raise ValueError(f"unknown event {event!r}")


def record(truth: Truth, method: str, seed: int):
    """What the position source records of `truth`."""
    if method == "gopro":
        return decode_gps5(hero9_payloads(truth, 0.0, float(truth.t[-1]) - 1.0, gps_hz=GOPRO_HZ, seed=seed))
    if method == "racebox":
        return racebox_mini(truth, seed=seed)
    raise ValueError(f"unknown method {method!r}")


def measure_seed(event: str, method: str, seed: int) -> dict:
    """Signed errors (measured minus true, seconds) for one recorded session.

    Laps the app does not count (flagged) and true laps it did not find
    (missed) are counted, not scored. Best and optimal are compared with the
    truth over the same counted laps, so they measure the timing, not which
    laps happened to be counted.
    """
    truth = session(event, seed)
    true_laps = truth.laps[1:] if truth.course.closed else truth.laps  # track lap 0 is the out-lap
    found = [lap for lap in find_laps(record(truth, method, seed), truth.course.timing_lines())
             if lap.kind is LapKind.TIMED]
    pairs, flagged, missed = [], 0, 0
    for true in true_laps:
        start, end = truth.utc0 + true.start, truth.utc0 + true.end
        match = next((lap for lap in found
                      if abs(lap.start - start) < MATCH_S and abs(lap.end - end) < MATCH_S), None)
        if match is None:
            missed += 1
        elif not match.counted:
            flagged += 1
        else:
            pairs.append((match, true))

    out: dict = {q: [] for q in QUANTITIES}
    out.update(flagged=flagged, missed=missed, expected=len(true_laps))
    for lap, true in pairs:
        out["lap"].append(lap.time - true.time)
        for k, (m, t) in enumerate(zip(lap.sector_times, true.sector_times)):
            if math.isfinite(m):
                out["sector"].append(m - t)
                out[f"sector_{k + 1}"].append(m - t)
    if pairs:
        measured = [lap for lap, _ in pairs]
        true_times = [t.time for _, t in pairs]
        out["best"].append(measured[best_lap(measured)].time - min(true_times))
        opt = optimal_lap(measured)
        if math.isfinite(opt.time):
            true_opt = float(np.sum(np.min([t.sector_times for _, t in pairs], axis=0)))
            out["optimal"].append(opt.time - true_opt)
        out["true_spread"] = max(true_times) - min(true_times)
    return out


def _measure_one(args):
    return measure_seed(*args)


def measure(event: str, method: str, seeds, workers: int | None = 1) -> list[dict]:
    """`measure_seed` for every seed, in seed order."""
    jobs = [(event, method, s) for s in seeds]
    if workers == 1:
        return [_measure_one(j) for j in jobs]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_measure_one, jobs, chunksize=8))


# ---------- statistics ----------

@dataclass(frozen=True)
class Error:
    """One quantity's error for one method and event. Seconds throughout;
    `*_ci` is a 95% bootstrap interval and `*_se` a bootstrap standard
    error. `p95` is the 95th percentile of the absolute error."""

    n: int
    seeds: int
    bias: float
    bias_ci: tuple[float, float]
    bias_se: float
    rms: float
    rms_ci: tuple[float, float]
    rms_se: float
    p95: float
    p95_ci: tuple[float, float]
    p95_se: float
    max: float

    @property
    def biased(self) -> bool:
        """Whether the bias is both real (its interval excludes zero) and
        big enough to show at the app's 0.01 s resolution."""
        lo, hi = self.bias_ci
        return (lo > 0 or hi < 0) and abs(self.bias) >= BIAS_FLOOR_S

    @property
    def bias_halfwidth(self) -> float:
        return max(self.bias - self.bias_ci[0], self.bias_ci[1] - self.bias)

    def summary(self) -> str:
        """Plain words: 'typically 0.30 s too fast (bias −0.30 ± 0.04 s),
        95% within ±0.45 s'."""
        within = f"95% within ±{seconds(self.p95)} s"
        if not self.biased:
            return f"no measurable bias, {within}"
        direction = "fast" if self.bias < 0 else "slow"
        digits = _digits(self.bias)
        return (f"typically {seconds(abs(self.bias))} s too {direction} "
                f"(bias {signed(self.bias)} ± {seconds(self.bias_halfwidth, digits)} s), {within}")


def _digits(x: float) -> int:
    return 3 if abs(x) < 0.1 else 2


def seconds(x: float, digits: int | None = None) -> str:
    """Two decimals, or three below 0.1 s, so small errors keep a digit."""
    return f"{x:.{_digits(x) if digits is None else digits}f}"


def signed(x: float) -> str:
    """'+0.12', '−0.012', or '0.000' for anything that rounds to zero."""
    text = seconds(abs(x))
    if float(text) == 0.0:
        return text
    return ("+" if x > 0 else "−") + text


def _stats(errors: np.ndarray) -> tuple[float, float, float]:
    return float(np.mean(errors)), float(np.sqrt(np.mean(errors**2))), float(np.percentile(np.abs(errors), 95))


def summarize(per_seed: list[list[float]], rng: np.random.Generator, resamples: int = BOOTSTRAP) -> Error | None:
    """Statistics of errors grouped by seed, with a bootstrap over seeds."""
    groups = [np.asarray(g, dtype=np.float64) for g in per_seed]
    errors = np.concatenate(groups) if groups else np.array([])
    if len(errors) == 0:
        return None
    point = _stats(errors)
    boot = np.empty((resamples, 3))
    for b in range(resamples):
        pick = rng.integers(0, len(groups), len(groups))
        sample = np.concatenate([groups[i] for i in pick])
        boot[b] = _stats(sample) if len(sample) else point
    lo, hi = np.percentile(boot, [2.5, 97.5], axis=0)
    se = boot.std(axis=0, ddof=1)
    return Error(
        n=len(errors), seeds=sum(1 for g in groups if len(g)),
        bias=point[0], bias_ci=(float(lo[0]), float(hi[0])), bias_se=float(se[0]),
        rms=point[1], rms_ci=(float(lo[1]), float(hi[1])), rms_se=float(se[1]),
        p95=point[2], p95_ci=(float(lo[2]), float(hi[2])), p95_se=float(se[2]),
        max=float(np.max(np.abs(errors))),
    )


def tabulate(results: list[dict], resamples: int = BOOTSTRAP, seed: int = 0) -> dict:
    """The table entry for one event and method, from `measure`'s output."""
    rng = np.random.default_rng(seed)
    entry = {}
    for q in QUANTITIES:
        err = summarize([r[q] for r in results], rng, resamples)
        entry[q] = None if err is None else _round(asdict(err))
    expected = sum(r["expected"] for r in results)
    entry["flagged_rate"] = round(sum(r["flagged"] for r in results) / expected, 4)
    entry["missed_rate"] = round(sum(r["missed"] for r in results) / expected, 4)
    entry["median_true_spread_s"] = round(float(np.median([r["true_spread"] for r in results if "true_spread" in r])), 3)
    return entry


def _round(d: dict) -> dict:
    def r(v):
        if isinstance(v, float):
            return round(v, 5)
        if isinstance(v, (tuple, list)):
            return [r(x) for x in v]
        return v
    return {k: r(v) for k, v in d.items()}


def model() -> dict:
    """Everything the measurement depends on that is a setting, not code, so
    the test can tell when the committed table was made under other settings."""
    return {
        "gopro_noise": asdict(HERO9_NOISE),
        "racebox_noise": asdict(RACEBOX_NOISE),
        "gopro_hz": GOPRO_HZ,
        "racebox_hz": RACEBOX_RATE_HZ,
        "laps_per_session": LAPS,
        "pace_range": list(PACE_RANGE),
        "track_length_m": round(example_track().length, 1),
        "autocross_length_m": round(example_autocross().length, 1),
    }


def fingerprint(results: list[dict]) -> dict:
    """Point statistics only: what a re-measurement of the same seeds must
    reproduce. Deterministic, because every seed fixes its errors."""
    out = {}
    for q in QUANTITIES:
        errors = np.concatenate([np.asarray(r[q], dtype=np.float64) for r in results])
        bias, rms, p95 = _stats(errors) if len(errors) else (math.nan,) * 3
        out[q] = {"n": len(errors), "bias": round(bias, 6), "rms": round(rms, 6), "p95": round(p95, 6)}
    out["flagged_rate"] = round(sum(r["flagged"] for r in results) / sum(r["expected"] for r in results), 6)
    return out


def build_table(seeds: int = SEEDS, workers: int | None = None) -> dict:
    measured = {(e, m): measure(e, m, range(seeds), workers) for e in EVENTS for m in METHODS}
    return {
        "generated_by": "python -m fugazy_delta.accuracy",
        "seeds": seeds,
        "bootstrap_resamples": BOOTSTRAP,
        "model": model(),
        "results": {e: {m: tabulate(measured[(e, m)]) for m in METHODS} for e in EVENTS},
        # The same statistics over the first CHECK_SEEDS seeds alone, which
        # the test re-measures to tell whether the code still produces
        # this table (see tests/test_timing_accuracy.py).
        "check": {
            "seeds": CHECK_SEEDS,
            "results": {e: {m: fingerprint(measured[(e, m)][:CHECK_SEEDS]) for m in METHODS} for e in EVENTS},
        },
    }


# ---------- reading the committed table ----------

@cache
def table() -> dict:
    return json.loads(TABLE_PATH.read_text())


def error(event: str, method: str, quantity: str) -> Error:
    d = table()["results"][event][method][quantity]
    return Error(**{k: tuple(v) if isinstance(v, list) else v for k, v in d.items()})


def flagged_rate(event: str, method: str) -> float:
    return table()["results"][event][method]["flagged_rate"]


def label(quantity: str, event: str) -> str:
    unit = UNITS[event]
    if quantity.startswith("sector_"):
        return f"Sector {quantity.split('_')[1]} times"
    return {"lap": f"{unit.capitalize()} times", "sector": "Sector times",
            "best": f"Best {unit}", "optimal": f"Optimal {unit}"}[quantity]


# ---------- regenerating ----------

def markdown(t: dict) -> str:
    """The full table as Markdown, for PLAN.md."""
    lines = [
        "| Event | Measured with | Time | n | Bias (95% CI) | RMS | 95% within (95% CI) | Worst |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for event in EVENTS:
        for method in METHODS:
            for q in QUANTITIES:
                d = t["results"][event][method][q]
                if d is None:
                    continue
                e = Error(**{k: tuple(v) if isinstance(v, list) else v for k, v in d.items()})
                lines.append(
                    f"| {event} | {METHOD_LABELS[method]} | {label(q, event)} | {e.n} | "
                    f"{signed(e.bias)} ({signed(e.bias_ci[0])} to {signed(e.bias_ci[1])}) | "
                    f"{seconds(e.rms)} | ±{seconds(e.p95)} ({seconds(e.p95_ci[0], 3)} to {seconds(e.p95_ci[1], 3)}) | "
                    f"{seconds(e.max)} |"
                )
    return "\n".join(lines)


def main(argv: list[str]) -> None:
    seeds = int(argv[0]) if argv else SEEDS
    t = build_table(seeds)
    TABLE_PATH.write_text(json.dumps(t, indent=1, allow_nan=False) + "\n")
    print(f"wrote {TABLE_PATH}")
    print(markdown(t))
    for event in EVENTS:
        for method in METHODS:
            r = t["results"][event][method]
            print(f"{event} {method}: flagged {r['flagged_rate']:.1%}, missed {r['missed_rate']:.1%}, "
                  f"median true spread {r['median_true_spread_s']} s")


if __name__ == "__main__":
    main(sys.argv[1:])
