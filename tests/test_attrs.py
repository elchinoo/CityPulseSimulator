"""Sensor attrs: deterministic metadata in the shape CityPulse accepts, when it travels, the switches, and the synthetic firmware/fault link."""
import datetime as dt
import re
import sys
from pathlib import Path

import pytest

from cpsim import attrs
from cpsim.catalog import TYPES
from cpsim.engine import CityRunner
from cpsim.generator import SensorSim
from cpsim.model import SpecError, parse_city

KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def _sample(n=300):
    return [(f"{k}-d01-x-{i:05d}", k, "alder-quay", "gotham") for i in range(n) for k in list(TYPES)[:: 1 + i % 5][:3]]


def _city(**over):
    return parse_city({"name": "attrville", "seed": 3, "groups": [{"type": "temperature", "count": 3, "district": "alder-quay", "frequency": 1}], **over})


def test_attrs_are_deterministic_and_depend_on_the_sensor_only():
    a = attrs.sensor_attrs("temperature-d01-x-00001", "temperature", "d01", "gotham")
    assert a == attrs.sensor_attrs("temperature-d01-x-00001", "temperature", "d01", "gotham")
    assert a != attrs.sensor_attrs("temperature-d01-x-00002", "temperature", "d01", "gotham")
    assert attrs.sensor_attrs("temperature-d01-x-00001", "temperature", "d01", "gotham") != attrs.sensor_attrs("temperature-d01-x-00001", "temperature", "d01", "paris")
    # pinned values: a change here changes every sensor's metadata for everybody, so it must be deliberate
    assert a == {"vendor": "Aerolab", "firmware": "2.1.4", "install_date": "2022-10-07",
                 "calibration": {"offset": -0.061, "scale": 0.9939, "date": "2023-07-28"}, "tags": ["north", "pole", "roof"]}


def test_every_type_has_three_or_four_vendors_and_every_vendor_a_firmware_suffix():
    assert set(attrs.VENDORS) == set(TYPES)
    for kind, vendors in attrs.VENDORS.items():
        assert 3 <= len(vendors) <= 4 and len(set(vendors)) == len(vendors), kind
        assert all(v in attrs._FW_SUFFIX for v in vendors)


def test_shape_ranges_and_size():
    end = attrs.INSTALL_END
    for sid, kind, district, city in _sample():
        a = attrs.sensor_attrs(sid, kind, district, city)
        assert set(a) == {"vendor", "firmware", "install_date", "calibration", "tags"} and all(KEY.match(k) for k in a)
        assert set(a["calibration"]) == {"offset", "scale", "date"}
        assert -0.2 <= a["calibration"]["offset"] <= 0.2 and 0.99 <= a["calibration"]["scale"] <= 1.01
        install, cal = dt.date.fromisoformat(a["install_date"]), dt.date.fromisoformat(a["calibration"]["date"])
        assert end - dt.timedelta(days=5 * 365) <= install <= cal < end
        assert 1 <= len(a["tags"]) <= 3 and a["tags"] == sorted(set(a["tags"])) and set(a["tags"]) <= set(attrs.TAGS)
        assert a["firmware"] == attrs.OLD_FIRMWARE or a["firmware"].split("-")[0] in ("2.0.1", "2.1.4", "2.2.0")
        assert attrs.size(a) <= attrs.MAX_BYTES


def test_old_firmware_is_rare_and_every_vendor_has_it():
    ids = [f"s-{i:05d}" for i in range(6000)]
    fw = [attrs.sensor_attrs(i, "temperature", "d01", "gotham")["firmware"] for i in ids]
    share = fw.count(attrs.OLD_FIRMWARE) / len(fw)
    assert 0.06 <= share <= 0.10
    old_vendors = {attrs.vendor_of(i, "temperature", "gotham") for i, f in zip(ids, fw) if f == attrs.OLD_FIRMWARE}
    assert old_vendors == set(attrs.VENDORS["temperature"])


def test_shape_is_accepted_by_citypulse_validator_when_importable():
    root = Path(__file__).resolve().parents[2] / "CityPulse" / "src"
    if not root.is_dir():
        pytest.skip("../CityPulse is not next to this repository")
    sys.path.insert(0, str(root))
    try:
        gateway = pytest.importorskip("citypulse.gateway")
    finally:
        sys.path.remove(str(root))
    for sid, kind, district, city in _sample():
        a = attrs.sensor_attrs(sid, kind, district, city)
        assert gateway.check_attrs(a) == a


def test_engine_sends_attrs_first_then_every_nth(monkeypatch):
    monkeypatch.delenv("CPSIM_ATTRS", raising=False)
    r = CityRunner(_city())
    s = r.sensors[0]
    for _ in range(attrs.ATTRS_EVERY + 1):
        r._emit(s, 1.0)
    with_attrs = [i for i, x in enumerate(r.buffer) if "attrs" in x]
    assert with_attrs == [0, attrs.ATTRS_EVERY]
    assert r.buffer[0]["attrs"] == attrs.sensor_attrs(s.id, s.type, s.district, "attrville")
    assert CityRunner(_city()).sensors[0].fault_factor == s.fault_factor


def test_attrs_switches(monkeypatch):
    monkeypatch.delenv("CPSIM_ATTRS", raising=False)
    off = CityRunner(_city(attrs=False))
    off._emit(off.sensors[0], 1.0)
    assert "attrs" not in off.buffer[0] and "lat" in off.buffer[0]                    # positions are independent
    nopos = CityRunner(_city(positions=False))
    nopos._emit(nopos.sensors[0], 1.0)
    assert "attrs" in nopos.buffer[0] and "lat" not in nopos.buffer[0]
    for value in ("off", "0", "false", "No"):
        monkeypatch.setenv("CPSIM_ATTRS", value)
        env_off = CityRunner(_city())
        env_off._emit(env_off.sensors[0], 1.0)
        assert "attrs" not in env_off.buffer[0]
    monkeypatch.setenv("CPSIM_ATTRS", "on")
    on = CityRunner(_city())
    on._emit(on.sensors[0], 1.0)
    assert "attrs" in on.buffer[0]


def test_city_attrs_field_is_validated_and_defaults_true():
    assert parse_city({"name": "a"}).attrs is True and parse_city({"name": "a", "attrs": False}).attrs is False
    assert parse_city({"name": "a", "attrs": False}).to_dict()["attrs"] is False
    for bad in ("no", 0, None):
        with pytest.raises(SpecError):
            parse_city({"name": "a", "attrs": bad})


def _episodes(old: bool, n_sensors=400, readings=400):
    """Fault episodes per reading over sensors whose firmware is (not) the old one, found by hash, simulated with seeds."""
    found, i = [], 0
    while len(found) < n_sensors:
        sid = f"temperature-d01-x-{i:05d}"
        if (attrs.firmware_of(sid, "temperature", "gotham") == attrs.OLD_FIRMWARE) == old:
            found.append(sid)
        i += 1
    started = 0
    for sid in found:
        s = SensorSim(sid, "temperature", "d01", "direct", 10, 30, 10.0, 10.0, 7, 0.0, "gotham")
        t = 1_700_000_000.0
        for _ in range(readings):
            was = s.episode
            s.step(t, 1.0)
            started += 1 if was is None and s.episode is not None else 0
            t += 10
    return started / (n_sensors * readings)


def test_old_firmware_faults_about_three_times_as_often():
    old, new = _episodes(True), _episodes(False)
    assert 2.4 <= old / new <= 3.6, (old, new)
    assert 0.003 <= new <= 0.006                                       # the normal rate (10 % x 0.04 per reading, less time spent inside episodes) is untouched
