import shutil
from pathlib import Path

import pytest

REAL = Path(__file__).parent / "fixtures" / "real"


def pytest_collection_modifyitems(config, items):
    no_fixtures = pytest.mark.skip(reason="run scripts/fetch-fixtures.sh first")
    no_ffmpeg = pytest.mark.skip(reason="ffmpeg not on PATH; run inside flox activate")
    have_fixtures = (REAL / "hero5.mp4").exists()
    have_ffmpeg = shutil.which("ffmpeg") is not None
    for item in items:
        if "fixtures" in item.keywords and not have_fixtures:
            item.add_marker(no_fixtures)
        if "ffmpeg" in item.keywords and not have_ffmpeg:
            item.add_marker(no_ffmpeg)


@pytest.fixture
def real_dir() -> Path:
    return REAL
