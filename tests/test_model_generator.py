"""City validation and the sensor generator, including how burst_pct shapes the burst probability."""
import math
import time

import pytest

from cpsim.catalog import TYPES
from cpsim.generator import SensorSim
from cpsim.model import SpecError, check_target, parse_city, parse_group


def city(**over):
    return {"name": "metropolis", "groups": [{"type": "temperature", "count": 10}], **over}


def test_a_valid_city_round_trips_and_applies_defaults():
    c = parse_city(city(target_url="https://citypulse.example/", seed=7))
    g = c.groups[0]
    assert (c.target_url, c.token_env, c.seed, c.poll_interval, g.protocol, g.frequency, g.burst_pct) == \
        ("https://citypulse.example", "CITYPULSE_INGEST_TOKEN", 7, 2.0, "direct", 30.0, 10.0)
    assert parse_city(c.to_dict()).to_dict() == c.to_dict()


@pytest.mark.parametrize("bad,why", [
    ({"name": "Bad Name"}, "name"), ({"name": ""}, "name"), ({"seed": -1}, "seed"), ({"seed": True}, "seed"),
    ({"token_env": "my-secret-token-value"}, "NAME of an environment variable"), ({"token_env": "lowercase"}, "NAME"),
    ({"poll_interval": 0.1}, "poll_interval"), ({"poll_interval": float("nan")}, "poll_interval"),
    ({"surprise": 1}, "unknown city field"), ({"groups": "x"}, "groups"),
    ({"target_url": "http://example.com"}, "https"), ({"target_url": "https://user:pw@example.com"}, "credentials"),
    ({"target_url": "https://example.com/?x=1"}, "query"), ({"target_url": "ftp://example.com"}, "http\\(s\\)"),
    ({"target_url": "https://169.254.169.254"}, "link-local"),
])
def test_invalid_cities_are_refused_with_a_reason(bad, why):
    with pytest.raises(SpecError, match=why):
        parse_city(city(**bad))


@pytest.mark.parametrize("bad,why", [
    ({"type": "teleporter"}, "unknown sensor type"), ({"count": 0}, "count"), ({"count": 5001}, "count"), ({"count": True}, "count"),
    ({"protocol": "zigbee"}, "protocol"), ({"frequency": 0.5}, "frequency"), ({"frequency": 99999}, "frequency"),
    ({"burst_pct": 101}, "burst_pct"), ({"burst_pct": -1}, "burst_pct"), ({"lo": 5, "hi": 5}, "smaller"),
    ({"lo": "a"}, "lo must"), ({"district": "NOPE!"}, "district"), ({"colour": "red"}, "unknown group field"),
])
def test_invalid_groups_are_refused(bad, why):
    with pytest.raises(SpecError, match=why):
        parse_group({"type": "temperature", "count": 5, **bad})


def test_limits_on_city_size_and_device_address_spaces():
    with pytest.raises(SpecError, match="at most 10000"):
        parse_city(city(groups=[{"type": "rain", "count": 5000}] * 3))
    with pytest.raises(SpecError, match="profibus supports at most 125"):
        parse_city(city(groups=[{"type": "rain", "count": 4001, "protocol": "profibus"}]))
    assert parse_city(city(groups=[{"type": "rain", "count": 4000, "protocol": "profibus"}])).sensor_count() == 4000
    assert check_target("http://localhost:8002/") == "http://localhost:8002"
    assert check_target("") == ""


def test_there_are_at_least_twenty_sensor_types_each_usable_with_every_protocol():
    assert len(TYPES) >= 20
    from cpsim.protocols import PROTOCOLS
    for proto in PROTOCOLS:
        for kind in TYPES:
            parse_group({"type": kind, "count": 1, "protocol": proto})


# ---- generator -------------------------------------------------------------------------------------
def sensor(kind="temperature", pct=0.0, seed=1, lo=10.0, hi=30.0, freq=10.0):
    return SensorSim(f"{kind}-d01-x-00001", kind, "d01", "direct", lo, hi, freq, pct, seed, 0.0)


def run(s, n=3000, boost=1.0):
    t, out = 1_700_000_000.0, []
    for _ in range(n):
        out.append(s.step(t, boost))
        t += s.frequency
    return out


def test_no_bursts_when_the_percentage_is_zero_and_values_stay_in_range():
    ticks = run(sensor(pct=0.0))
    assert all(t.kind is None and 10 <= t.value <= 30 for t in ticks)
    assert len({round(t.value, 6) for t in ticks}) > 1000                          # it is not frozen


def test_a_higher_percentage_means_more_burst_readings():
    share = lambda pct: sum(t.kind is not None for t in run(sensor(pct=pct, seed=5), 6000)) / 6000  # noqa: E731
    low, mid, high = share(5), share(30), share(100)
    assert 0 < low < mid < high
    assert high > 3 * low


def test_the_same_seed_gives_the_same_data_and_a_different_seed_does_not():
    a, b, c = run(sensor(pct=50, seed=3), 500), run(sensor(pct=50, seed=3), 500), run(sensor(pct=50, seed=4), 500)
    key = lambda ts: [(t.kind, None if t.value is None or math.isnan(t.value) else round(t.value, 6)) for t in ts]  # noqa: E731
    assert key(a) == key(b) and key(a) != key(c)


def test_every_burst_kind_happens_and_behaves_as_described():
    ticks = run(sensor(pct=100, seed=11), 20000)
    kinds = {t.kind for t in ticks}
    assert {"spike", "noise", "stuck", "dropout", "flood", "corrupt"} <= kinds
    spec = TYPES["temperature"]
    assert any(t.kind == "spike" and (t.value > 30 or t.value < 10) for t in ticks)
    assert any(t.kind == "spike" and (t.value > spec.hi or t.value < spec.lo) for t in ticks)         # past physical limits
    assert all(t.value is None for t in ticks if t.kind == "dropout")
    assert all(math.isnan(t.value) for t in ticks if t.kind == "corrupt")
    runs, current = [], []
    for t in ticks:                                                                  # stuck: a run of identical values
        if t.kind == "stuck":
            current.append(t.value)
        elif current:
            runs.append(current)
            current = []
    assert runs and all(len(set(r)) == 1 for r in runs) and max(len(r) for r in runs) >= 3


def test_flood_episodes_report_five_times_as_often():
    s = sensor(pct=0)
    base = sum(s.interval() for _ in range(200)) / 200
    s.episode = "flood"
    assert sum(s.interval() for _ in range(200)) / 200 < base / 4


def test_a_city_wide_boost_makes_bursts_far_more_likely():
    calm = sum(t.kind is not None for t in run(sensor(pct=10, seed=2), 3000))
    storm = sum(t.kind is not None for t in run(sensor(pct=10, seed=2), 3000, boost=25))
    assert storm > 4 * max(calm, 1)


def test_discrete_types_ignore_the_analogue_curve():
    light = run(sensor("traffic_light", lo=0, hi=2), 400)
    assert {t.value for t in light} == {0.0, 1.0, 2.0}
    parking = run(sensor("parking", lo=0, hi=1), 400)
    assert {t.value for t in parking} <= {0.0, 1.0}


def test_timestamps_are_not_involved_in_values_so_wall_clock_is_irrelevant():
    assert sensor().step(time.time()).value is not None
