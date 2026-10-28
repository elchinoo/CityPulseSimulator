"""Deterministic positions: origin, district cells, sensor jitter, and what the engine sends."""
import math

from cpsim import geo
from cpsim.engine import CityRunner
from cpsim.model import parse_city


def test_anchor_table_and_fallback_are_deterministic():
    assert geo.origin("gotham") == (40.7128, -74.0060) and geo.origin("Metropolis") == (41.8781, -87.6298)
    assert geo.origin("san-francisco") == geo.ANCHORS["san francisco"] and len(geo.ANCHORS) >= 22
    for name in ("quietvale", "x", "a-very-long-invented-city-name"):
        lat, lon = geo.origin(name)
        assert geo.origin(name) == (lat, lon) and -50 <= lat <= 58 and -165 <= lon <= 165
    assert len({geo.origin(f"town-{i}") for i in range(50)}) == 50


def test_spiral_cells_never_overlap_and_centre_sits_near_origin():
    assert len({geo.spiral(i) for i in range(300)}) == 300 and geo.spiral(0) == (0, 0)
    cells = geo.cells(["alder-quay", "d01", "east-central", "oak-farm", "pine-airfield"])
    assert len(set(cells.values())) == 5 and cells["east-central"] == (0, 0)
    assert geo.kind_of("alder-quay") == "harbour" and geo.kind_of("st-anne-old-town") == "historic" and geo.kind_of("d01") is None


def test_positions_are_stable_inside_their_cell_and_true_metres():
    ds = ["alder-quay", "east-central", "d01"]
    m1, m2 = geo.CityMap("gotham", ds), geo.CityMap("gotham", list(reversed(ds)))
    for sid, d in (("s-1", ds[0]), ("s-2", ds[1]), ("s-3", ds[2])):
        p = m1.position(sid, d)
        assert p == m2.position(sid, d) and p == (round(p[0], 6), round(p[1], 6))
        centre = m1.district_centre(d)
        north = (p[0] - centre[0]) * geo.M_PER_DEG
        east = (p[1] - centre[1]) * geo.M_PER_DEG * math.cos(math.radians(centre[0]))
        assert abs(north) <= geo.CELL_M * 0.4 + 1 and abs(east) <= geo.CELL_M * 0.4 + 1
    assert m1.position("s-1", ds[0]) != m1.position("s-2", ds[0])


def _city(**over):
    return parse_city({"name": "geoville", "seed": 3, "groups": [{"type": "temperature", "count": 3, "district": "alder-quay", "frequency": 1}], **over})


def test_engine_sends_position_first_then_every_nth_and_can_turn_it_off(monkeypatch):
    r = CityRunner(_city())
    s = r.sensors[0]
    for _ in range(geo.POSITION_EVERY + 1):
        r._emit(s, 1.0)
    with_pos = [i for i, x in enumerate(r.buffer) if "lat" in x]
    assert with_pos == [0, geo.POSITION_EVERY] and r.buffer[0]["lat"] == r.coords[s.id][0] and r.buffer[0]["lon"] == r.coords[s.id][1]
    assert CityRunner(_city()).coords == r.coords                                    # a new runner: same places
    off = CityRunner(_city(positions=False))
    off._emit(off.sensors[0], 1.0)
    assert "lat" not in off.buffer[0]
    monkeypatch.setenv("CPSIM_POSITIONS", "off")
    env_off = CityRunner(_city())
    env_off._emit(env_off.sensors[0], 1.0)
    assert "lat" not in env_off.buffer[0]
