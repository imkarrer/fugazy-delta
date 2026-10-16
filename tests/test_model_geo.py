import numpy as np
import pytest

from fugazy_delta.geo import to_latlon, to_local
from fugazy_delta.model import Telemetry


def test_missing_channels_are_nan_filled():
    t = Telemetry("x", time=[0.0, 1.0], lat=[1.0, 2.0], lon=[3.0, 4.0])
    assert np.isnan(t.speed).all() and len(t.accel_lat) == 2


def test_rejects_mismatched_lengths():
    with pytest.raises(ValueError, match="speed"):
        Telemetry("x", time=[0.0, 1.0], lat=[1.0, 2.0], lon=[3.0, 4.0], speed=[1.0])


def test_rejects_time_going_backwards():
    with pytest.raises(ValueError, match="increasing"):
        Telemetry("x", time=[0.0, 0.0], lat=[1.0, 2.0], lon=[3.0, 4.0])


def test_slice_and_rate():
    t = Telemetry("x", time=np.arange(0, 10, 0.04), lat=np.zeros(250), lon=np.zeros(250))
    assert t.sample_rate == pytest.approx(25.0)
    part = t.slice_time(2.0, 3.0)
    assert part.time[0] >= 2.0 and part.time[-1] <= 3.0 and len(part) == 26


def test_local_projection_round_trip():
    lat, lon = to_latlon([120.0, -35.5], [-80.0, 400.25], 44.8, -91.5)
    east, north = to_local(lat, lon, 44.8, -91.5)
    np.testing.assert_allclose(east, [120.0, -35.5], atol=1e-6)
    np.testing.assert_allclose(north, [-80.0, 400.25], atol=1e-6)


def test_local_projection_scale():
    _, north = to_local(45.8, -91.5, 44.8, -91.5)
    assert north == pytest.approx(111_195, rel=1e-3)  # one degree of latitude
