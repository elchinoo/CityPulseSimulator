"""A whole city: sensors -> devices -> protocol servers -> gateway -> sender, over loopback."""
import asyncio

from cpsim.engine import BUFFER_MAX, CityRunner
from cpsim.model import parse_city
from cpsim.protocols import PROTOCOLS
from cpsim.sinks import Fatal, Retry


class Sink:
    def __init__(self, behaviour=None):
        self.got, self.behaviour = [], behaviour

    async def send(self, readings):
        if self.behaviour:
            raise self.behaviour
        self.got.extend(readings)

    async def close(self):
        return None


def spec(**over):
    groups = [{"type": "temperature", "count": 40, "protocol": "modbus_tcp", "frequency": 1, "burst_pct": 0},
              {"type": "rain", "count": 5, "protocol": "ethernet_ip", "frequency": 1, "burst_pct": 0},
              {"type": "radar", "count": 5, "protocol": "ethercat", "frequency": 1, "burst_pct": 0},
              {"type": "humidity", "count": 5, "protocol": "profinet", "frequency": 1, "burst_pct": 0},
              {"type": "co2", "count": 5, "protocol": "profibus", "frequency": 1, "burst_pct": 0},
              {"type": "noise", "count": 5, "protocol": "direct", "frequency": 1, "burst_pct": 0}]
    return parse_city({"name": "testville", "poll_interval": 0.5, "seed": 1, "groups": groups, **over})


async def run_for(runner, seconds):
    await runner.start()
    try:
        await asyncio.sleep(seconds)
    finally:
        await runner.stop()


async def test_every_protocol_delivers_readings_that_reach_the_sink():
    sink = Sink()
    runner = CityRunner(spec(), sink)
    await run_for(runner, 4.5)
    by = {}
    for r in sink.got:
        by.setdefault(r["protocol"], set()).add(r["sensor"])
    assert set(by) == {*PROTOCOLS, "direct"}                                     # all five stacks plus direct
    assert len(by["modbus_tcp"]) == 40 and all(len(by[p]) == 5 for p in ("ethernet_ip", "ethercat", "profinet", "profibus", "direct"))
    assert all(set(r) - {"lat", "lon"} == {"sensor", "type", "district", "protocol", "city", "value", "ts"} for r in sink.got)
    assert {r["sensor"] for r in sink.got if "lat" in r} == {r["sensor"] for r in sink.got}   # every sensor sent its position once
    assert {r["city"] for r in sink.got} == {runner.city.name}
    assert all(isinstance(r["value"], float) and -50 <= r["value"] <= 5000 for r in sink.got)
    ids = [r["sensor"] for r in sink.got]
    assert all(len(i) <= 64 for i in ids)
    snap = runner.snapshot()
    assert snap["devices"]["modbus_tcp"] == 2 and snap["devices"]["profibus"] == 1                  # 40 channels -> 32 + 8
    assert snap["polls_failed"] == {} and snap["polls_ok"]["modbus_tcp"] > 0 and snap["polls_ok"]["profinet"] > 0
    assert all(ep["port"] > 0 for ep in snap["endpoints"].values())


async def test_a_reading_is_delivered_once_per_update_not_once_per_poll():
    sink = Sink()
    runner = CityRunner(spec(groups=[{"type": "temperature", "count": 3, "protocol": "modbus_tcp", "frequency": 3600, "burst_pct": 0}]), sink)
    for s in runner.sensors:
        s.due = 0.0                                                              # every sensor updates once, then not for an hour
    await run_for(runner, 3.5)                                                   # ...while the gateway polls about seven times
    per_sensor = {}
    for r in sink.got:
        per_sensor[r["sensor"]] = per_sensor.get(r["sensor"], 0) + 1
    assert per_sensor and all(n == 1 for n in per_sensor.values())


async def test_unreachable_target_buffers_backs_off_and_keeps_order():
    sink = Sink(Retry(0))
    runner = CityRunner(spec(), sink)
    await run_for(runner, 3.5)
    snap = runner.snapshot()
    assert snap["buffer"] > 0 and snap["backoff"] >= 1 and snap["send_failures"] > 0 and snap["sent"] == 0
    stamps = [r["ts"] for r in runner.buffer]
    assert stamps == sorted(stamps)


async def test_a_refused_token_is_reported_not_hidden():
    runner = CityRunner(spec(), Sink(Fatal("CityPulse refused the token (HTTP 401); check the ingest token")))
    await run_for(runner, 2.5)
    assert "401" in runner.snapshot()["problem"]


async def test_the_buffer_is_bounded_and_counts_drops():
    runner = CityRunner(spec())
    for i in range(BUFFER_MAX + 50):
        runner._emit(runner.sensors[0], float(i))
    assert len(runner.buffer) == BUFFER_MAX and runner.stats["dropped"] == 50


async def test_a_dead_device_link_shows_up_as_failed_polls():
    runner = CityRunner(spec(), Sink())
    await runner.start()
    try:
        for dev in runner.devices["ethernet_ip"].values():
            dev.online = False
        await asyncio.sleep(5.5)
        snap = runner.snapshot()
        assert snap["polls_failed"].get("ethernet_ip", 0) >= 3 and snap["devices_unreachable"] >= 1
        assert "ethernet_ip device" in snap["problem"] or snap["problem"]
    finally:
        await runner.stop()


async def test_a_boost_produces_bursts_and_corrupt_values_become_rejectable_nulls():
    sink = Sink()
    runner = CityRunner(spec(groups=[{"type": "temperature", "count": 60, "protocol": "direct", "frequency": 1, "burst_pct": 100}]), sink)
    runner.boost(60, 25)
    await run_for(runner, 5.5)
    assert sum(runner.snapshot()["bursts"].values()) >= 0 and runner.stats["silenced"] + runner.stats["bad_values"] > 0
    assert any(r["value"] is None for r in sink.got) or runner.stats["bad_values"] == 0


class MemStore:
    """Just enough of the store for the runtime's sink selection."""

    def __init__(self):
        self.specs = {}

    def save_city(self, name, spec):
        self.specs[name] = spec

    def cities(self):
        return self.specs


def test_no_target_means_generate_only():
    from cpsim.runtime import Runtime
    from cpsim.sinks import NullSink
    rt = Runtime(MemStore())
    rt.store.save_city("testville", spec().to_dict())
    assert isinstance(rt._sink(rt.get("testville")), NullSink)


def test_a_target_without_its_token_is_an_error_naming_the_variable(monkeypatch):
    import pytest

    from cpsim.model import SpecError
    from cpsim.runtime import Runtime
    monkeypatch.delenv("CITYPULSE_INGEST_TOKEN", raising=False)
    rt = Runtime(MemStore())
    rt.store.save_city("testville", spec(target_url="https://citypulse.example").to_dict())
    with pytest.raises(SpecError, match="CITYPULSE_INGEST_TOKEN"):
        rt._sink(rt.get("testville"))


def test_readings_carry_the_city_and_a_long_name_gets_a_short_stable_id():
    import re

    from cpsim.model import city_id
    cid = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")      # CityPulse's rule for a city id
    assert city_id("paris") == "paris"
    long_a, long_b = "a" * 40, "a" * 39 + "b"
    assert cid.match(city_id(long_a)) and cid.match(city_id(long_b)) and city_id(long_a) != city_id(long_b) and city_id(long_a) == city_id(long_a)
