"""Demo data for the prototype: honest shape, honest numbers, kept current."""

import json
from pathlib import Path

import pytest

from fugazy_delta import demo

DATA = Path(__file__).parent.parent / "prototype" / "data"
SCENARIOS = [(event, setup) for event in demo.EVENTS for setup in demo.SETUPS]


def load(event, setup):
    return json.loads((DATA / f"{event}-{setup}.json").read_text())


@pytest.fixture(params=SCENARIOS, ids=lambda s: f"{s[0]}-{s[1]}")
def data(request):
    return load(*request.param)


def counted(data):
    return [lap for lap in data["laps"] if lap["counted"]]


def test_is_labelled_demo_data(data):
    assert data["demo"] is True


def test_shape(data):
    n = len(data["distance"])
    assert n == demo.GRID_POINTS
    assert data["distance"][0] == 0
    assert data["distance"][-1] == pytest.approx(data["event"]["course_length_m"], abs=0.1)
    assert [lap["number"] for lap in data["laps"]] == list(range(1, len(data["laps"]) + 1))
    for lap in data["laps"]:
        assert len(lap["sectors"]) == len(data["course"]["sector_marks"]) + 1
        assert len(lap["trace"]["x"]) == len(lap["trace"]["y"]) == n
        assert len(lap["delta_to_best"]) == n
    assert {f["key"] for f in data["features"]} == {"LAP_TIMES", "VIDEO_OVERLAY", "DELTA", "OPTIMAL", "LINE_COMPARISON"}
    assert all(f["support"] in ("full", "approximate", "unavailable") for f in data["features"])


def test_sectors_add_up_to_lap_time(data):
    for lap in data["laps"]:
        assert sum(lap["sectors"]) == pytest.approx(lap["time"], abs=0.003)


def test_best_is_fastest_counted(data):
    best = min(counted(data), key=lambda lap: lap["time"])
    assert data["best"] == best["number"]
    assert [lap["number"] for lap in data["laps"] if lap["best"]] == [best["number"]]


def test_optimal_is_best_sectors_and_beats_best(data):
    laps = counted(data)
    best_sectors = [min(lap["sectors"][s] for lap in laps) for s in range(len(laps[0]["sectors"]))]
    assert data["optimal"]["time"] == pytest.approx(sum(best_sectors), abs=0.003)
    assert data["optimal"]["time"] <= data["laps"][data["best"] - 1]["time"]
    # the demo is built so the optimal time draws on more than one lap
    assert len(set(data["optimal"]["sector_laps"])) > 1


def test_delta_runs_from_zero_to_the_time_difference(data):
    best = data["laps"][data["best"] - 1]
    assert set(best["delta_to_best"]) == {0}
    for lap in data["laps"]:
        assert lap["delta_to_best"][0] == pytest.approx(0, abs=0.001)
        assert lap["delta_to_best"][-1] == pytest.approx(lap["time"] - best["time"], abs=0.002)


def test_track_out_lap_is_shown_but_not_counted():
    data = load("track", "racebox")
    assert data["laps"][0]["out_lap"] and not data["laps"][0]["counted"]
    assert all(lap["counted"] for lap in data["laps"][1:])
    assert all(lap["counted"] for lap in load("autocross", "racebox")["laps"])


def features(data):
    return {f["key"]: f for f in data["features"]}


@pytest.mark.parametrize("event", demo.EVENTS)
def test_gopro_only_cannot_compare_lines(event):
    data = load(event, "gopro")
    assert data["setup"]["label"] == "GoPro"
    assert data["position_source"] == "gopro"
    f = features(data)
    assert f["LINE_COMPARISON"]["support"] == "unavailable"
    assert "RaceBox" in f["LINE_COMPARISON"]["reason"]
    assert f["LAP_TIMES"]["support"] == "approximate"
    assert f["VIDEO_OVERLAY"]["support"] == "full"


@pytest.mark.parametrize("event", demo.EVENTS)
def test_gopro_and_racebox_is_full(event):
    data = load(event, "gopro-racebox")
    assert data["position_source"] == "racebox"
    assert all(f["support"] == "full" for f in data["features"])


@pytest.mark.parametrize("event", demo.EVENTS)
def test_racebox_only_has_no_overlay(event):
    data = load(event, "racebox")
    f = features(data)
    assert f["VIDEO_OVERLAY"]["support"] == "unavailable"
    assert "GoPro" in f["VIDEO_OVERLAY"]["reason"]
    assert f["LINE_COMPARISON"]["support"] == "full"


def test_same_laps_whatever_the_setup():
    """The setup changes how precisely the driving is timed, not the driving."""
    for event in demo.EVENTS:
        laps = {setup: load(event, setup)["laps"] for setup in demo.SETUPS}
        assert len({len(v) for v in laps.values()}) == 1
        # both RaceBox setups are timed from the same RaceBox file
        assert [lap["time"] for lap in laps["racebox"]] == [lap["time"] for lap in laps["gopro-racebox"]]


@pytest.mark.parametrize("event", demo.EVENTS)
def test_times_are_detected_not_copied_from_the_simulator(event):
    """Counted times are within each setup's measured error of the truth, and
    the GoPro's differ from the RaceBox's, as detected times must."""
    truth = demo.demo_truth(event)
    tolerance = {"racebox": 0.5 if event == "autocross" else 0.075,
                 "gopro": 1.2 if event == "autocross" else 0.2}
    for setup, tol in tolerance.items():
        laps = load(event, setup)["laps"]
        assert len(laps) == len(truth.laps)  # the track in-lap is not shown
        for lap, true in zip(laps, truth.laps):
            if lap["counted"]:
                assert lap["time"] == pytest.approx(true.time, abs=tol)
    gopro = [lap["time"] for lap in load(event, "gopro")["laps"]]
    racebox = [lap["time"] for lap in load(event, "racebox")["laps"]]
    assert gopro != racebox


@pytest.mark.ffmpeg
def test_committed_data_is_current(tmp_path):
    """Regenerating from real device files reproduces the committed JSON."""
    demo.main([str(tmp_path)])
    for event, setup in SCENARIOS:
        name = f"{event}-{setup}.json"
        assert json.loads((tmp_path / name).read_text()) == load(event, setup), (
            f"{name} is stale: run python -m fugazy_delta.demo prototype/data"
        )
