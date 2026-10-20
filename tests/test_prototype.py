"""The prototype's drop zone identifies files by content, like the backend."""

import shutil
import subprocess
from pathlib import Path

import pytest

from fugazy_delta.devices import NO_NOISE, sample_gnss
from fugazy_delta.gpx import write_gpx
from fugazy_delta.importers import UnsupportedFile, identify
from fugazy_delta.nmea import write_nmea
from fugazy_delta.racebox_csv import write_racebox_csv
from fugazy_delta.racecapture import write_racecapture
from fugazy_delta.racechrono import write_racechrono
from fugazy_delta.sim import autocross_session, example_autocross
from fugazy_delta.trackaddict import write_trackaddict
from fugazy_delta.vbo import write_vbo
from fugazy_delta.xrk import write_xrk

ROOT = Path(__file__).parent.parent
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH; run inside flox activate")


def classify(paths):
    out = subprocess.run(["node", str(ROOT / "tests/js/sniff.mjs"), str(ROOT / "prototype/app.js"), *map(str, paths)],
                         capture_output=True, text=True, check=True)
    return dict(line.split("\t") for line in out.stdout.splitlines())


def backend(path):
    try:
        return identify(path).value
    except UnsupportedFile:
        return "-"


@pytest.fixture(scope="module")
def files(tmp_path_factory):
    d = tmp_path_factory.mktemp("drop")
    fixes = sample_gnss(autocross_session(example_autocross(), [[1, 1, 1]]), 10.0, NO_NOISE)
    write_vbo(fixes, d / "a.dat", style="racebox")
    write_racebox_csv(fixes, d / "b.dat", metadata=True)
    write_racechrono(fixes, d / "c.dat", gps_hz=10.0)
    write_trackaddict(fixes, d / "d.dat")
    write_gpx(fixes, d / "e.dat")
    write_nmea(fixes, d / "f.dat")
    write_racecapture(fixes, d / "g.dat", european=True)
    write_xrk(fixes, d / "h.dat")
    write_xrk(fixes, d / "i.dat", compress=True)
    (d / "j.dat").write_text("cone 14 is tight\n")
    return sorted(d.iterdir())


LABELS = {  # the prototype's label for each backend kind
    "VBO": "VBO", "RaceBox CSV": "RaceBox CSV", "RaceChrono CSV": "RaceChrono CSV",
    "TrackAddict CSV": "TrackAddict CSV", "GPX": "GPX", "NMEA": "NMEA", "RaceCapture log": "RaceCapture log",
    "AiM XRK": ("AiM XRK", "AiM XRZ"), "AiM CSV": "AiM CSV", "GoPro video": "GoPro video", "-": "-",
}


def _agree(paths):
    seen = classify(paths)
    for p in paths:
        want = LABELS[backend(p)]
        assert seen[str(p)] in (want if isinstance(want, tuple) else (want,)), p


def test_prototype_agrees_with_backend_on_synthetic_files(files):
    _agree(files)


def test_prototype_agrees_with_backend_on_committed_samples():
    _agree([ROOT / "tests/fixtures/samples/uopenformula/pittrace-90.csv"])


@pytest.mark.fixtures
def test_prototype_agrees_with_backend_on_real_files(real_dir):
    _agree(sorted(p for p in real_dir.rglob("*") if p.is_file() and p.suffix not in ("",) and p.name != "gpmf_demo"))
