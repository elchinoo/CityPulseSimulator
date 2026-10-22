"""The demo-city generator: seeded variability within the limits, and the headless `cpsim cities generate`."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cpsim import citygen
from cpsim.catalog import DISTRICTS, TYPES
from cpsim.model import MAX_CITY_SENSORS, MAX_GROUPS, NAME, SpecError, city_id, parse_city
from cpsim.protocols.image import MAX_CHANNELS

ROOT = Path(__file__).resolve().parent.parent


def batch(n=10, **kw):
    return citygen.generate_cities(n, kw.pop("size", "medium"), kw.pop("profile", "mixed"), kw.pop("seed", 100), **kw)


def test_same_seed_same_city_and_another_seed_another_city():
    a = citygen.generate_city(5, "medium", "mixed")
    b = citygen.generate_city(5, "medium", "mixed")
    c = citygen.generate_city(6, "medium", "mixed")
    assert a.city == b.city and a.districts == b.districts
    assert a.city["groups"] != c.city["groups"] and a.name != c.name
    assert citygen.generate_city(5, "small", "mixed").city != a.city
    assert [g.city for g in batch(5, seed=9)] == [g.city for g in batch(5, seed=9)]
    assert batch(3, seed=9)[1].city["groups"] == citygen.generate_city(10, "medium", "mixed").city["groups"]   # city i uses seed + i


def test_ten_cities_are_not_ten_copies():
    cities = batch(10)
    district_sets = {tuple(d["id"] for d in c.districts) for c in cities}
    type_sets = {frozenset(g["type"] for g in c.city["groups"]) for c in cities}
    group_lists = {json.dumps(c.city["groups"], sort_keys=True) for c in cities}
    shapes = {(d["id"], tuple(sorted(d["types"]))) for c in cities for d in c.districts}
    assert len(district_sets) >= 8 and len(type_sets) >= 9 and len(group_lists) == 10
    assert len(shapes) >= 40                                                       # the same district differs from city to city
    counts = [len(c.districts) for c in cities]
    assert max(counts) - min(counts) >= 3
    sensors = [c.sensors for c in cities]
    assert max(sensors) > 1.4 * min(sensors)
    assert len({tuple(sorted({g["protocol"] for g in c.city["groups"]})) for c in cities}) >= 4


def test_districts_range_from_three_to_twelve_across_many_cities():
    sizes = {len(c.districts) for c in citygen.generate_cities(50, "medium", "mixed", seed=1)}
    assert min(sizes) == 3 and max(sizes) >= 11
    small = [len(c.districts) for c in citygen.generate_cities(30, "small", "balanced", seed=2)]
    large = [len(c.districts) for c in citygen.generate_cities(30, "large", "balanced", seed=2)]
    assert sum(large) / 30 > sum(small) / 30 and min(large) >= 5 and min(small) >= 3


@pytest.mark.parametrize("size,low,high", [("small", 60, 700), ("medium", 700, 6000), ("large", 6000, MAX_CITY_SENSORS)])
def test_size_sets_the_scale_and_is_clamped(size, low, high):
    for c in citygen.generate_cities(12, size, "balanced", seed=3):
        assert low <= c.sensors <= high, (size, c.sensors)
    assert max(c.sensors for c in citygen.generate_cities(20, "large", "mixed", seed=4)) <= MAX_CITY_SENSORS


def test_a_numeric_size_is_accepted_and_bad_ones_refused():
    assert 8 <= citygen.generate_city(1, 30, "balanced").sensors <= 30
    assert citygen.generate_city(1, "50", "balanced").city["groups"]
    for bad in ("huge", 5, MAX_CITY_SENSORS + 1, True, 2.5, None):
        with pytest.raises(SpecError, match="size"):
            citygen.generate_city(1, bad, "balanced")


def test_limits_hold_for_every_size_and_profile():
    for size in ("small", "medium", "large"):
        for profile in citygen.PROFILES:
            for g in citygen.generate_cities(8, size, profile, seed=11):
                city = parse_city(g.city)                                         # validity, including every protocol's device limit
                assert city.sensor_count() <= MAX_CITY_SENSORS and len(city.groups) <= MAX_GROUPS
                assert all(x.district in DISTRICTS and x.type in TYPES for x in city.groups)
                assert 3 <= len({x.district for x in city.groups}) <= 12
                seen = [(x.district, x.type) for x in city.groups]
                assert len(seen) == len(set(seen))                                 # a type once per district
                assert city.autostart is False and city.target_url == "" and city.token_env == "CITYPULSE_INGEST_TOKEN"


def test_group_sizes_are_skewed_a_few_big_many_small():
    groups = [x["count"] for c in citygen.generate_cities(20, "medium", "mixed", seed=21) for x in c.city["groups"]]
    groups.sort(reverse=True)
    top = sum(groups[: len(groups) // 10])
    assert top > 0.4 * sum(groups)                                                 # the largest tenth holds a big share
    assert sorted(groups)[len(groups) // 2] < sum(groups) / len(groups)           # the median is below the mean


def test_every_protocol_and_wide_ranges_of_frequency_burst_and_range_appear():
    gs = [x for c in citygen.generate_cities(30, "medium", "mixed", seed=31) for x in c.city["groups"]]
    assert {x["protocol"] for x in gs} == {"direct", "modbus_tcp", "ethernet_ip", "ethercat", "profinet", "profibus"}
    assert len({x["frequency"] for x in gs}) >= 8 and len({x["burst_pct"] for x in gs}) >= 8
    narrowed = [x for x in gs if "lo" in x]
    assert 0.1 * len(gs) < len(narrowed) < 0.45 * len(gs)
    for x in narrowed:
        t = TYPES[x["type"]]
        assert t.lo <= x["lo"] < x["hi"] <= t.hi


def test_types_follow_the_kind_of_district():
    from cpsim.catalog import DISTRICT_KINDS

    cities = citygen.generate_cities(50, "medium", "balanced", seed=41)

    def mean_share(kind, types):
        vals = []
        for c in cities:
            for d in c.districts:
                if d["kind"] == kind:
                    vals.append(sum(1 for t in d["types"] if t in types) / len(d["types"]))
        return sum(vals) / len(vals)

    assert DISTRICT_KINDS["d01"] == "harbour"
    assert mean_share("harbour", {"water_level", "bridge_vibration", "wind_speed"}) > 0.3
    assert mean_share("industrial", {"air_quality", "noise", "smart_meter", "gas_leak"}) > 0.35
    assert mean_share("residential", {"noise", "parking", "waste_bin", "street_light"}) > 0.4
    assert mean_share("centre", {"traffic_camera", "parking", "air_quality", "traffic_light", "pedestrian_counter"}) > 0.45
    assert mean_share("harbour", {"soil_moisture", "bike_share"}) < 0.1


def test_profiles_change_the_kind_of_city():
    def included(profile, kind):                       # the share of small cities that have a district of this kind
        cs = citygen.generate_cities(50, "small", profile, seed=51)
        return sum(any(d["kind"] == kind for d in c.districts) for c in cs) / 50

    for profile, kind in (("industrial", "industrial"), ("coastal", "harbour"), ("tourist", "historic"), ("residential", "residential"),
                          ("residential", "suburb")):
        assert included(profile, kind) > included("balanced", kind) + 0.2, (profile, kind)
    with pytest.raises(SpecError, match="profile"):
        citygen.generate_city(1, "small", "lunar")


def test_names_are_unique_valid_varied_and_avoid_taken_ones():
    cities = citygen.generate_cities(50, "small", "mixed", seed=61)
    names = [c.name for c in cities]
    assert len(set(names)) == 50 and all(NAME.match(n) for n in names) and all(len(city_id(n)) <= 32 for n in names)
    assert len({n.split("-")[0] for n in names}) > 30                              # not one stem with numbers
    taken = {c.name for c in citygen.generate_cities(5, "small", "mixed", seed=61)}
    again = citygen.generate_cities(5, "small", "mixed", seed=61, taken=taken)
    assert not taken & {c.name for c in again} and [c.city["groups"] for c in again] == [c.city["groups"] for c in cities[:5]]
    assert [c.name for c in citygen.generate_cities(3, "small", "mixed", seed=1, name="demo")] == ["demo-1", "demo-2", "demo-3"]
    assert [c.name for c in citygen.generate_cities(2, "small", "mixed", seed=1, name="demo", taken={"demo-1"})] == ["demo-2", "demo-3"]
    assert citygen.generate_cities(1, "small", name="mine")[0].name == "mine"
    with pytest.raises(SpecError, match="already exists"):
        citygen.generate_cities(1, "small", name="mine", taken={"mine"})
    for bad in ("Bad Name", "", "x" * 41):
        with pytest.raises(SpecError, match="name"):
            citygen.generate_cities(1, "small", name=bad)


def test_count_and_seed_bounds():
    for bad in (0, 51, True, "3"):
        with pytest.raises(SpecError, match="count"):
            citygen.generate_cities(bad)
    for bad in (-1, 2**32, True):
        with pytest.raises(SpecError, match="seed"):
            citygen.generate_city(bad)
    assert len(citygen.generate_cities(50, "small", seed=2**32 - 5)) == 50          # the seed wraps instead of overflowing
    assert len(citygen.generate_cities(2)) == 2 and citygen.generate_cities(2)[0].city["seed"] is not None


def test_target_url_token_and_autostart_pass_through():
    g = citygen.generate_city(3, "small", target_url="https://citypulse.example", token_env="MY_TOKEN", autostart=True)
    c = parse_city(g.city)
    assert (c.target_url, c.token_env, c.autostart, c.seed) == ("https://citypulse.example", "MY_TOKEN", True, 3)


def test_rendered_files_parse_back_as_the_same_city():
    import yaml

    g = citygen.generate_city(8, "medium", "mixed")
    for fmt, load in (("yaml", yaml.safe_load), ("json", json.loads)):
        doc = load(citygen.render(g, fmt))
        assert parse_city(doc).to_dict() == parse_city(g.city).to_dict()
    assert citygen.render(g).startswith("# Generated demo city")
    assert MAX_CHANNELS == 32


# ---- the CLI, as a real process with no database ---------------------------------------------------
def cpsim(*args, cwd=None):
    env = {**os.environ, "DATABASE_URL": "postgresql://nobody:nothing@127.0.0.1:1/none", "PYTHONPATH": str(ROOT / "src")}
    for k in ("DB_EXTERNAL_DSN", "PSQL_CONNECTION_STRING"):
        env.pop(k, None)
    return subprocess.run([sys.executable, "-m", "cpsim.cli", *args], capture_output=True, text=True, timeout=120, env=env, cwd=cwd)


def test_cli_generates_files_without_postgres(tmp_path):
    r = cpsim("cities", "generate", "--count", "4", "--size", "small", "--profile", "coastal", "--seed", "7", "--out", str(tmp_path / "out"))
    assert r.returncode == 0, r.stderr
    files = sorted((tmp_path / "out").glob("*.yaml"))
    assert len(files) == 4 and "4 cities written" in r.stdout
    for f in files:
        v = cpsim("validate", str(f))
        assert v.returncode == 0 and "sensors" in v.stdout, v.stderr
    assert len({f.read_text() for f in files}) == 4
    again = cpsim("cities", "generate", "--count", "4", "--size", "small", "--profile", "coastal", "--seed", "7", "--out", str(tmp_path / "out2"))
    assert again.returncode == 0
    assert sorted(f.name for f in (tmp_path / "out2").glob("*.yaml")) == [f.name for f in files]
    assert [(tmp_path / "out2" / f.name).read_text() for f in files] == [f.read_text() for f in files]
    more = cpsim("cities", "generate", "--count", "2", "--seed", "7", "--size", "small", "--profile", "coastal", "--out", str(tmp_path / "out"))
    assert more.returncode == 0 and len(list((tmp_path / "out").glob("*.yaml"))) == 6      # existing names are never reused or overwritten


def test_cli_json_verbose_and_errors(tmp_path):
    r = cpsim("cities", "generate", "--count", "2", "--size", "300", "--seed", "1", "--format", "json", "-v", "--name", "demo",
              "--out", str(tmp_path))
    assert r.returncode == 0 and (tmp_path / "demo-1.json").exists() and "    d" in r.stdout and "(" in r.stdout
    assert parse_city(json.loads((tmp_path / "demo-2.json").read_text())).name == "demo-2"
    no_out = cpsim("cities", "generate", "--count", "2", "--seed", "1")
    assert no_out.returncode == 0 and "use --out" in no_out.stdout
    for args in (["--count", "51"], ["--size", "huge"], ["--profile", "lunar"], ["--name", "Bad Name"]):
        bad = cpsim("cities", "generate", *args)
        assert bad.returncode == 2 and bad.stderr.startswith("invalid:"), (args, bad.stderr)
