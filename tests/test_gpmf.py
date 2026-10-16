import numpy as np
import pytest

from fugazy_delta.gpmf import (
    GPS5_SCAL,
    CameraClock,
    decode,
    decode_gps5,
    format_gpsu,
    hero9_payloads,
    klv,
    nested,
    numbers,
    parse_gpsu,
    string,
)
from fugazy_delta.sim import autocross_session, example_autocross


@pytest.fixture(scope="module")
def truth():
    return autocross_session(example_autocross(), [[1, 1, 1]])


def test_klv_header_and_padding():
    raw = klv("TEST", "c", 1, 5, b"hello")
    assert raw[:4] == b"TEST" and raw[4:5] == b"c" and raw[5] == 1
    assert int.from_bytes(raw[6:8], "big") == 5
    assert len(raw) == 8 + 8  # 5 bytes padded to 8


def test_nested_round_trip():
    raw = nested("DEVC", numbers("DVID", "L", [[1]]), string("DVNM", "HERO9 Black"),
                 nested("STRM", numbers("SCAL", "l", [[10], [20]])))
    (devc,) = decode(raw)
    assert devc.find("DVNM").text() == "HERO9 Black"
    strm = devc.find("STRM")
    np.testing.assert_array_equal(strm.find("SCAL").values().ravel(), [10, 20])


def test_gpsu_round_trip():
    utc = 1781359215.123
    assert format_gpsu(utc) == "260613140015.123"
    assert parse_gpsu(format_gpsu(utc)) == pytest.approx(utc, abs=1e-3)


def test_hero9_payload_layout(truth):
    payloads = hero9_payloads(truth, record_start=5.0, record_seconds=5.0)
    assert len(payloads) == 4  # whole 1.001 s payloads only
    (devc,) = decode(payloads[0].data)
    keys = {s.find("STNM").text(): s for s in devc.find_all("STRM")}
    gps = keys["GPS (Lat., Long., Alt., 2D speed, 3D speed)"]
    assert gps.find("GPS5").repeat == 18
    assert keys["Accelerometer"].find("ACCL").repeat == 200
    np.testing.assert_array_equal(gps.find("SCAL").values().ravel(), GPS5_SCAL)


def test_decoded_gps_matches_truth(truth):
    payloads = hero9_payloads(truth, record_start=20.0, record_seconds=40.0)
    gps = decode_gps5(payloads)
    exact = truth.at(gps.time - truth.utc0)
    np.testing.assert_allclose(gps.lat, exact["lat"], atol=2e-7)
    np.testing.assert_allclose(gps.lon, exact["lon"], atol=2e-7)
    np.testing.assert_allclose(gps.speed, exact["speed"], atol=1e-3)
    assert gps.sample_rate == pytest.approx(18 / 1.001, rel=1e-3)


def test_gpsu_latency_shifts_decoded_time(truth):
    late = hero9_payloads(truth, 20.0, 5.0, CameraClock(gpsu_latency=0.25))
    on_time = hero9_payloads(truth, 20.0, 5.0, CameraClock(gpsu_latency=0.0))
    # GPSU only has millisecond precision, so allow that much
    assert decode_gps5(late).time[0] - decode_gps5(on_time).time[0] == pytest.approx(0.25, abs=1e-3)


def test_payload_without_fix_is_skipped(truth):
    payloads = hero9_payloads(truth, 20.0, 4.0)  # three payloads
    no_fix = payloads[0].data.replace(numbers("GPSF", "L", [[3]]), numbers("GPSF", "L", [[0]]))
    payloads[0].data = no_fix
    gps = decode_gps5(payloads)
    assert len(gps) == 2 * 18
