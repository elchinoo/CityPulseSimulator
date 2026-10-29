"""Real cities (Brussels, Vienna): every sensor lies inside the real polygon of its district, near roads and denser towards the centre,
the same one every run; the generator names real cities after real ones and gives them their real districts."""
import json
import math
import re
from collections import Counter
from pathlib import Path

import pytest

from cpsim import citygen, geo, realcities
from cpsim.engine import CityRunner
from cpsim.model import parse_city

CITIES = ("brussels", "vienna")
DISTRICT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")


def test_bundled_files_match_the_district_kinds_table():
    assert realcities.available() == list(CITIES)
    for key in CITIES:
        city = realcities.load(key)
        assert {d.id for d in city.districts} == set(realcities.KINDS[key]), key
        assert all(DISTRICT_ID.match(d.id) for d in city.districts)
    assert realcities.load("paris") is None and realcities.load("gotham") is None


def test_district_kinds_are_known_kinds():
    from cpsim.districts import NOUNS
    for table in realcities.KINDS.values():
        assert set(table.values()) <= set(NOUNS)


def test_files_are_identical_to_citypulses_when_the_sibling_checkout_exists():
    sibling = Path(__file__).resolve().parents[2] / "CityPulse" / "src" / "citypulse" / "geodata"
    if not sibling.is_dir():
        pytest.skip("no sibling CityPulse checkout (CI): the copy is checked on a developer machine")
    for f in sibling.glob("*.geojson"):
        assert (realcities.DATA / f.name).read_bytes() == f.read_bytes(), f"{f.name}: run scripts/sync_geodata.py"


@pytest.mark.parametrize("key", CITIES)
def test_every_sensor_lies_inside_its_real_district_and_not_in_water(key):
    city = realcities.load(key)
    for d in city.districts:
        for i in range(60):
            lat, lon = realcities.place(city, f"temperature-{d.id}-{i:05d}", d.id)
            assert d.area.contains(lon, lat), (key, d.id, i)
            assert not any(w.contains(lon, lat) for w in city.water)
            assert city.district_of(lon, lat) == d.id, "and in no other district"


def test_positions_are_deterministic_and_do_not_depend_on_other_sensors_or_the_clock():
    c = realcities.load("brussels")
    a = realcities.place(c, "rain-ixelles-00001", "ixelles")
    assert a == realcities.place(c, "rain-ixelles-00001", "ixelles") == geo.CityMap("brussels", ["ixelles"]).position("rain-ixelles-00001", "ixelles")
    assert a == geo.CityMap("brussels", ["ixelles", "uccle", "d01"]).position("rain-ixelles-00001", "ixelles")
    assert a != realcities.place(c, "rain-ixelles-00002", "ixelles")
    assert a == (round(a[0], 6), round(a[1], 6))


def _dist_to_road_m(district, lat, lon):
    best = math.inf
    k = math.cos(math.radians(lat))
    for x1, y1, x2, y2, _ in district.segments:
        dx, dy = (x2 - x1) * k, y2 - y1
        t = max(0.0, min(1.0, (((lon - x1) * k) * dx + (lat - y1) * dy) / (dx * dx + dy * dy or 1e-18)))
        best = min(best, math.hypot((lon - x1) * k - t * dx, lat - y1 - t * dy) * realcities.M_PER_DEG)
    return best


def test_many_sensors_sit_beside_a_main_road():
    city = realcities.load("brussels")
    d = city.by_id["ixelles"]
    assert d.segments, "the district has main roads"
    near = sum(_dist_to_road_m(d, *realcities.place(city, f"s-{i}", "ixelles")) <= realcities.ROAD_OFFSET_M + 2 for i in range(300))
    assert near >= 0.3 * 300, near                      # about ROAD_SHARE of them; a uniform spread would give a few percent


def test_the_centre_is_denser_than_the_edge():
    """Within the same districts and in equal numbers, sensors lie closer to the city centre than a uniform spread would put them."""
    import random
    city = realcities.load("vienna")
    clat, clon = city.centre
    dist = lambda lat, lon: realcities._dist_m(lon, lat, clon, clat)  # noqa: E731
    rng, placed, uniform = random.Random(1), [], []
    for d in city.districts:
        placed += [realcities.place(city, f"s-{i}", d.id) for i in range(60)]
        x0, y0, x1, y1 = d.area.bbox
        n = 0
        while n < 60:
            lon, lat = rng.uniform(x0, x1), rng.uniform(y0, y1)
            if d.area.contains(lon, lat):
                uniform.append((lat, lon))
                n += 1
    got, base = sum(dist(*p) for p in placed) / len(placed), sum(dist(*p) for p in uniform) / len(uniform)
    assert got < 0.93 * base, (got, base)


def test_an_unknown_district_id_stands_in_for_one_real_district():
    city = realcities.load("brussels")
    d = realcities.stand_in(city, "d01")
    assert d.id in city.by_id and realcities.stand_in(city, "d01") is d
    lat, lon = geo.CityMap("brussels", ["d01"]).position("temperature-d01-1", "d01")
    assert d.area.contains(lon, lat)
    spread = Counter(realcities.stand_in(city, f"d{i:02d}").id for i in range(1, 40))
    assert len(spread) > 8, "different ids land on different communes"


def test_other_cities_keep_the_old_spiral_and_old_names_keep_their_anchor():
    assert geo.CityMap("gotham", ["d01"]).real is None
    assert geo.origin("gotham") == (40.7128, -74.0060)
    assert geo.origin("brussels") == (50.8467, 4.3525) and geo.origin("vienna") == (48.2082, 16.3738)


def _spec(city, **over):
    return parse_city({"name": city, "seed": 3, "groups": [
        {"type": "temperature", "count": 4, "district": "ixelles" if city == "brussels" else "d01", "frequency": 1},
        {"type": "rain", "count": 3, "district": "uccle" if city == "brussels" else "d02", "frequency": 1}], **over})


def test_the_engine_sends_real_positions_for_a_real_city_and_a_non_real_one_as_before():
    r = CityRunner(_spec("brussels"))
    city = realcities.load("brussels")
    for s in r.sensors:
        lat, lon = r.coords[s.id]
        assert city.by_id[s.district].area.contains(lon, lat)
    s = r.sensors[0]
    r._emit(s, 1.0)
    assert r.buffer[0]["lat"] == r.coords[s.id][0] and r.buffer[0]["city"] == "brussels"
    other = CityRunner(_spec("gotham"))
    assert all(abs(lat - 40.7) < 0.1 for lat, _lon in other.coords.values())
    assert CityRunner(_spec("brussels")).coords == r.coords


# ---- the generator ---------------------------------------------------------------------------------------------------------------
def test_a_city_named_brussels_gets_its_real_districts_with_real_kinds():
    g = citygen.generate_city(1, "medium", "mixed", name="brussels")
    ids = {d["id"] for d in g.districts}
    assert ids == set(realcities.KINDS["brussels"]), "a medium city uses all 19 communes"
    kinds = {d["id"]: d["kind"] for d in g.districts}
    assert kinds["ixelles"] == "campus" and kinds["anderlecht"] == "industrial" and kinds["bruxelles"] == "centre"
    assert {d["name"] for d in g.districts} >= {"Molenbeek-Saint-Jean", "Ixelles"}
    assert {x["district"] for x in g.city["groups"]} <= ids
    parse_city(g.city)
    assert g.sensors > 0 and sum(d["sensors"] for d in g.districts) == g.sensors


def test_a_small_real_city_uses_a_seeded_handful_of_districts():
    a, b = citygen.generate_city(1, "small", "mixed", name="vienna"), citygen.generate_city(2, "small", "mixed", name="vienna")
    for g in (a, b):
        assert 3 <= len(g.districts) <= 7 and {d["id"] for d in g.districts} <= set(realcities.KINDS["vienna"])
    assert [d["id"] for d in a.districts] == [d["id"] for d in citygen.generate_city(1, "small", "mixed", name="vienna").districts]
    assert {d["id"] for d in a.districts} != {d["id"] for d in b.districts}


def test_real_names_come_first_when_asked_and_invented_ones_only_when_they_are_taken():
    one = citygen.generate_cities(1, "small", "mixed", seed=5, real=True)[0]
    assert one.name == "brussels"
    both = citygen.generate_cities(2, "small", "mixed", seed=5, real=True)
    assert [c.name for c in both] == ["brussels", "vienna"]
    three = citygen.generate_cities(3, "small", "mixed", seed=5, real=True)
    assert [c.name for c in three][:2] == ["brussels", "vienna"] and three[2].name not in CITIES
    assert citygen.generate_cities(1, "small", "mixed", seed=5, real=True, taken={"brussels"})[0].name == "vienna"
    assert citygen.generate_cities(1, "small", "mixed", seed=5)[0].name not in CITIES, "the library default is unchanged"


def test_the_yaml_of_a_real_city_round_trips():
    g = citygen.generate_city(4, "small", "balanced", name="brussels")
    spec = parse_city(json.loads(citygen.render(g, "json")))
    assert spec.name == "brussels" and {x.district for x in spec.groups} <= set(realcities.KINDS["brussels"])
