"""Drop in any mix of files; get the right setup and honest feature support."""

import pytest

from fugazy_delta.fixtures import gopro_mp4, racebox_vbo, render_test_video
from fugazy_delta import accuracy
from fugazy_delta.session import (
    Feature,
    Kind,
    Session,
    Setup,
    Support,
    UnsupportedFile,
    identify,
)
from fugazy_delta.sim import DEFAULT_UTC0, autocross_session, example_autocross

pytestmark = pytest.mark.ffmpeg

RECORD_START = 15.0
CHAPTER = 12.0


@pytest.fixture(scope="module")
def truth():
    return autocross_session(example_autocross(), [[1, 1, 1], [1, 1, 1]])


@pytest.fixture(scope="module")
def files(truth, tmp_path_factory):
    d = tmp_path_factory.mktemp("upload")
    # One recording split into two chapters, as GoPro does for long files.
    gopro_mp4(truth, d / "GX010042.MP4", RECORD_START, CHAPTER)
    gopro_mp4(truth, d / "GX020042.MP4", RECORD_START + CHAPTER, CHAPTER, seed=1)
    gopro_mp4(truth, d / "GX010043.MP4", RECORD_START, CHAPTER, gps_fix=0)
    racebox_vbo(truth, d / "session.vbo")

    next_day = autocross_session(example_autocross(), [[1, 1, 1]], utc0=DEFAULT_UTC0 + 86_400)
    racebox_vbo(next_day, d / "other-day.vbo")

    render_test_video(d / "exported.mp4", 2.0, truth.utc0)
    (d / "notes.txt").write_text("cone 14 is tight")
    return d


def test_identifies_files_by_content_not_name(files, tmp_path):
    renamed = tmp_path / "whatever.dat"
    renamed.write_bytes((files / "session.vbo").read_bytes())
    assert identify(files / "GX010042.MP4") is Kind.GOPRO
    assert identify(renamed) is Kind.VBO


def test_rejects_video_without_telemetry(files):
    with pytest.raises(UnsupportedFile, match="original file from the camera"):
        identify(files / "exported.mp4")


def test_rejects_unknown_files(files):
    with pytest.raises(UnsupportedFile, match="export the session as VBO"):
        identify(files / "notes.txt")


def test_gopro_only(files):
    session = Session.from_files([files / "GX010042.MP4"])
    assert session.setup is Setup.GOPRO_ONLY
    assert session.position.source == "gopro"
    features = session.features()
    assert features[Feature.VIDEO_OVERLAY].support is Support.FULL
    assert features[Feature.LAP_TIMES].support is Support.APPROXIMATE
    assert features[Feature.LINE_COMPARISON].support is Support.UNAVAILABLE
    assert "RaceBox" in features[Feature.DELTA].reason


def test_gopro_and_racebox_in_any_order(files):
    session = Session.from_files([files / "session.vbo", files / "GX010042.MP4"])
    assert session.setup is Setup.GOPRO_RACEBOX
    assert session.position.source == "racebox"
    assert all(a.support is Support.FULL for a in session.features().values())
    assert session.warnings == []


def test_racebox_only(files):
    session = Session.from_files([files / "session.vbo"])
    assert session.setup is Setup.RACEBOX_ONLY
    features = session.features()
    assert features[Feature.VIDEO_OVERLAY].support is Support.UNAVAILABLE
    assert features[Feature.LINE_COMPARISON].support is Support.FULL


def test_chapters_join_in_recording_order(files, truth):
    session = Session.from_files([files / "GX020042.MP4", files / "GX010042.MP4"])
    assert [v.path.name for v in session.videos] == ["GX010042.MP4", "GX020042.MP4"]
    gps = session.camera_gps
    assert gps.time[0] == pytest.approx(truth.utc0 + RECORD_START, abs=0.01)
    assert gps.duration == pytest.approx(2 * CHAPTER, abs=1.5)


def test_gopro_without_fix_explains_itself(files):
    session = Session.from_files([files / "GX010043.MP4"])
    assert session.position is None
    assert all(a.support is Support.UNAVAILABLE for a in session.features().values())
    assert "never got a GPS fix" in session.warnings[0]


def test_racebox_rescues_gopro_without_fix(files):
    session = Session.from_files([files / "GX010043.MP4", files / "session.vbo"])
    assert session.setup is Setup.GOPRO_RACEBOX
    assert session.features()[Feature.LAP_TIMES].support is Support.FULL


def test_files_from_different_sessions_are_flagged(files):
    session = Session.from_files([files / "GX010042.MP4", files / "other-day.vbo"])
    assert any("don't overlap" in w for w in session.warnings)


def test_empty_upload_is_rejected():
    with pytest.raises(UnsupportedFile):
        Session.from_files([])


@pytest.mark.fixtures
def test_real_hero8_without_fix_explains_itself(real_dir):
    """GoPro's own HERO8 sample was recorded without a GPS lock."""
    session = Session.from_files([real_dir / "hero8.mp4"])
    assert session.setup is Setup.GOPRO_ONLY
    assert session.position is None
    assert "never got a GPS fix" in session.warnings[0]


@pytest.mark.fixtures
def test_real_hero5_with_fix_is_gopro_only(real_dir):
    session = Session.from_files([real_dir / "hero5.mp4"])
    assert session.position is not None and len(session.position) > 100
    assert session.features()[Feature.VIDEO_OVERLAY].support is Support.FULL


@pytest.mark.parametrize("event", ["track", "autocross"])
def test_gopro_only_reasons_quote_the_measured_error(files, event):
    features = Session.from_files([files / "GX010042.MP4"]).features(event)
    lap = accuracy.error(event, "gopro", "lap")
    optimal = accuracy.error(event, "gopro", "optimal")
    assert f"95% within ±{accuracy.seconds(lap.p95)} s" in features[Feature.LAP_TIMES].reason
    assert features[Feature.LAP_TIMES].support is Support.APPROXIMATE
    assert optimal.summary() in features[Feature.OPTIMAL].reason
    assert "too fast" in features[Feature.OPTIMAL].reason
    assert accuracy.error(event, "gopro", "sector").summary() in features[Feature.DELTA].reason
    other = {"track": "autocross", "autocross": "track"}[event]
    assert f"({other})" not in features[Feature.LAP_TIMES].reason


def test_racebox_reasons_quote_the_racebox_error(files):
    features = Session.from_files([files / "session.vbo"]).features("autocross")
    assert features[Feature.LAP_TIMES].support is Support.FULL
    assert accuracy.error("autocross", "racebox", "lap").summary() in features[Feature.LAP_TIMES].reason
    assert accuracy.error("autocross", "racebox", "optimal").summary() in features[Feature.OPTIMAL].reason


def test_reasons_without_an_event_quote_both(files):
    reason = Session.from_files([files / "GX010042.MP4"]).features()[Feature.LAP_TIMES].reason
    assert "(track)" in reason and "(autocross)" in reason
