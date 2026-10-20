import hashlib
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
REAL = Path(__file__).parent / "fixtures" / "real"
SAMPLES = Path(__file__).parent / "fixtures" / "samples"  # committed real files


def _fetched() -> bool:
    """Whether scripts/fetch-fixtures.sh has run in its current version."""
    stamp = REAL / ".fetched"
    script = hashlib.sha256((ROOT / "scripts" / "fetch-fixtures.sh").read_bytes()).hexdigest()
    return stamp.exists() and stamp.read_text().strip() == script


def pytest_collection_modifyitems(config, items):
    no_fixtures = pytest.mark.skip(reason="run scripts/fetch-fixtures.sh (again: it has changed)")
    no_ffmpeg = pytest.mark.skip(reason="ffmpeg not on PATH; run inside flox activate")
    have_fixtures = _fetched()
    have_ffmpeg = shutil.which("ffmpeg") is not None
    for item in items:
        if "fixtures" in item.keywords and not have_fixtures:
            item.add_marker(no_fixtures)
        if "ffmpeg" in item.keywords and not have_ffmpeg:
            item.add_marker(no_ffmpeg)


@pytest.fixture
def real_dir() -> Path:
    return REAL


@pytest.fixture
def samples_dir() -> Path:
    return SAMPLES
