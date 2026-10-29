"""Real cities: their district polygons and base-map lines, and where a sensor goes inside a district.

The files in `geodata/` are copies of CityPulse's (`CityPulse/src/citypulse/geodata/`, copied by `scripts/sync_geodata.py`; the same
real districts are stored in CityPulse's PostGIS as source 'boundary', so a position computed here is inside the polygon CityPulse
draws). Brussels (19 communes) and Vienna (23 districts) are bundled; the module needs no network and no third-party package.

A sensor's place is a pure function of the city name and the sensor id (sha256, never the seed or the clock):

* ROAD_SHARE of the sensors sit beside a main road (a point along a road segment that lies inside the district, a few metres off the
  line): traffic and parking sensors belong at roads and so do most others in a real city;
* the rest sit anywhere in the district; every candidate point (road or open) is kept only with a probability that falls off with the
  distance from the city centre (floor DENSITY_FLOOR, scale DENSITY_SCALE of the city's half diagonal): the centre is denser than the edge;
* never in a lake or a river polygon of the base map; always inside the district (even-odd test with holes, MultiPolygons included).
"""
from __future__ import annotations

import hashlib
import json
import math
from functools import cache
from pathlib import Path

DATA = Path(__file__).parent / "geodata"
M_PER_DEG = 111_320.0
ROAD_SHARE = 0.45                 # share of sensors placed beside a main road
ROAD_OFFSET_M = 20.0              # how far from the line, at most
DENSITY_FLOOR = 0.25              # acceptance probability at the far edge of the city (1.0 at the centre)
DENSITY_SCALE = 0.35              # Gaussian scale as a share of the city's half diagonal
TRIES = 40

# What each real district is like (a kind from districts.KIND_ORDER), so the generator gives it plausible sensors: the historic core
# gets pedestrians and noise, a park district soil moisture, an airport or industrial one air quality and noise. General knowledge of
# the cities, not data from the files.
KINDS: dict[str, dict[str, str]] = {
    "brussels": {
        "bruxelles": "centre", "saint-gilles": "residential", "ixelles": "campus", "etterbeek": "campus", "saint-josse-ten-noode": "market",
        "schaerbeek": "residential", "molenbeek-saint-jean": "riverside", "anderlecht": "industrial", "koekelberg": "residential",
        "jette": "residential", "ganshoren": "suburb", "berchem-sainte-agathe": "suburb", "evere": "industrial", "forest": "industrial",
        "uccle": "green", "watermael-boitsfort": "green", "auderghem": "green", "woluwe-saint-pierre": "green",
        "woluwe-saint-lambert": "residential"},
    "vienna": {
        "innere-stadt": "historic", "leopoldstadt": "green", "landstrasse": "centre", "wieden": "campus", "margareten": "residential",
        "mariahilf": "market", "neubau": "market", "josefstadt": "residential", "alsergrund": "campus", "favoriten": "residential",
        "simmering": "industrial", "meidling": "residential", "hietzing": "green", "penzing": "suburb",
        "rudolfsheim-fuenfhaus": "residential", "ottakring": "residential", "hernals": "suburb", "waehring": "residential",
        "doebling": "suburb", "brigittenau": "riverside", "floridsdorf": "suburb", "donaustadt": "riverside", "liesing": "industrial"},
}


def _unit(*parts: str) -> float:
    h = hashlib.sha256("\x1f".join(("cpsim-real", *parts)).encode()).digest()
    return int.from_bytes(h[:8], "big") / 2**64


def _rings(geometry: dict) -> list[list[list[tuple[float, float]]]]:
    """GeoJSON Polygon or MultiPolygon as a list of polygons, each a list of rings (outer first, then holes)."""
    polys = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"] if geometry["type"] == "MultiPolygon" else []
    return [[[(x, y) for x, y in ring] for ring in poly] for poly in polys]


def _in_ring(ring, lon: float, lat: float) -> bool:
    inside = False
    x1, y1 = ring[-1]
    for x2, y2 in ring:
        if (y2 > lat) != (y1 > lat) and lon < (x1 - x2) * (lat - y2) / (y1 - y2) + x2:
            inside = not inside
        x1, y1 = x2, y2
    return inside


class Area:
    """A polygon or multipolygon with a bounding box; `contains` is the even-odd test with holes."""

    def __init__(self, geometry: dict):
        self.polys = _rings(geometry)
        xs = [x for p in self.polys for x, _ in p[0]]
        ys = [y for p in self.polys for _, y in p[0]]
        self.bbox = (min(xs), min(ys), max(xs), max(ys)) if xs else (0.0, 0.0, 0.0, 0.0)
        self.boxes = [(min(x for x, _ in p[0]), min(y for _, y in p[0]), max(x for x, _ in p[0]), max(y for _, y in p[0])) for p in self.polys]

    def contains(self, lon: float, lat: float) -> bool:
        x0, y0, x1, y1 = self.bbox
        if not (x0 <= lon <= x1 and y0 <= lat <= y1):
            return False
        for poly, (a, b, c, d) in zip(self.polys, self.boxes):
            if a <= lon <= c and b <= lat <= d and _in_ring(poly[0], lon, lat) and not any(_in_ring(h, lon, lat) for h in poly[1:]):
                return True
        return False


def _dist_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    k = math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot((lon1 - lon2) * k, lat1 - lat2) * M_PER_DEG


class District:
    def __init__(self, ident: str, name: str, geometry: dict):
        self.id, self.name = ident, name
        self.area = Area(geometry)
        self.segments: list[tuple[float, float, float, float, float]] = []      # road segments inside: x1, y1, x2, y2, length (m)
        self.interior = self._interior()

    def _interior(self) -> tuple[float, float]:
        """A point certainly inside (the fallback): the first of a grid over the largest polygon's box that the area contains."""
        a, b, c, d = self.area.bbox
        best = None
        for i in range(1, 20):
            for j in range(1, 20):
                lon, lat = a + (c - a) * i / 20, b + (d - b) * j / 20
                if self.area.contains(lon, lat):
                    mid = (a + c) / 2, (b + d) / 2
                    dist = _dist_m(lon, lat, *mid)
                    if best is None or dist < best[0]:
                        best = (dist, lon, lat)
        if best:
            return best[1], best[2]
        x, y = self.area.polys[0][0][0]
        return x, y


class RealCity:
    def __init__(self, key: str, districts: list[District], water: list[Area], centre: tuple[float, float]):
        self.key, self.districts, self.water, self.centre = key, districts, water, centre
        self.by_id = {d.id: d for d in districts}
        xs = [v for d in districts for v in (d.area.bbox[0], d.area.bbox[2])]
        ys = [v for d in districts for v in (d.area.bbox[1], d.area.bbox[3])]
        self.half_diag_m = _dist_m(min(xs), min(ys), max(xs), max(ys)) / 2

    def district_of(self, lon: float, lat: float) -> str | None:
        for d in self.districts:
            if d.area.contains(lon, lat):
                return d.id
        return None


CENTRES = {"brussels": (50.8467, 4.3525), "vienna": (48.2082, 16.3738)}


def available() -> list[str]:
    return sorted(k for k in KINDS if (DATA / f"{k}.districts.geojson").exists())


@cache
def load(key: str) -> RealCity | None:
    """The real city called `key` (exact, lowercase) or None when it is not bundled."""
    path = DATA / f"{key}.districts.geojson"
    if key not in KINDS or not path.exists():
        return None
    districts = [District(f["properties"]["id"], f["properties"]["name"], f["geometry"]) for f in json.loads(path.read_text())["features"]]
    ctx = DATA / f"{key}.context.geojson"
    context = json.loads(ctx.read_text()) if ctx.exists() else {"features": []}
    water = [Area(f["geometry"]) for f in context["features"] if f["properties"]["kind"] == "water"]
    city = RealCity(key, districts, water, CENTRES[key])
    _assign_roads(city, context)
    return city


def _assign_roads(city: RealCity, ctx: dict) -> None:
    """Give each district the road segments (any kind) whose midpoint lies inside it."""
    for f in ctx["features"]:
        if f["properties"]["kind"] not in ("road_major", "road_main"):
            continue
        g = f["geometry"]
        lines = [g["coordinates"]] if g["type"] == "LineString" else g["coordinates"] if g["type"] == "MultiLineString" else []
        for line in lines:
            for (x1, y1), (x2, y2) in zip(line, line[1:]):
                mx, my = (x1 + x2) / 2, (y1 + y2) / 2
                length = _dist_m(x1, y1, x2, y2)
                if length < 1:
                    continue
                for d in city.districts:
                    if d.area.contains(mx, my):
                        d.segments.append((x1, y1, x2, y2, length))
                        break


def stand_in(city: RealCity, district: str) -> District:
    """The real district a district id of a spec stands for: itself when it is a real id, otherwise one picked from a hash of the id
    (a hand-made `d01` in a Brussels city still lands on a real commune, the same one every run)."""
    d = city.by_id.get(district)
    if d is not None:
        return d
    return city.districts[int(_unit(city.key, "stand-in", district) * len(city.districts))]


def _pick_segment(segments, pick: float):
    """The segment where the running length passes `pick` metres (segments are (x1, y1, x2, y2, length))."""
    acc = 0.0
    for seg in segments:
        acc += seg[4]
        if acc >= pick:
            return seg
    return segments[-1]


def place(city: RealCity, sensor: str, district: str) -> tuple[float, float]:
    """(lat, lon) of a sensor inside its real district (see the module docstring). Deterministic."""
    d = stand_in(city, district)
    x0, y0, x1, y1 = d.area.bbox
    total = sum(s[4] for s in d.segments)
    clat, clon = city.centre
    scale = max(city.half_diag_m * DENSITY_SCALE, 1.0)
    for k in range(TRIES):
        def u(tag: str, k: int = k) -> float:
            return _unit(city.key, sensor, tag, str(k))
        if total and u("mode") < ROAD_SHARE:
            ax, ay, bx, by, _length = _pick_segment(d.segments, u("seg") * total)
            t = u("t")
            lon, lat = ax + (bx - ax) * t, ay + (by - ay) * t
            off = (u("off") - 0.5) * 2 * ROAD_OFFSET_M
            cos = math.cos(math.radians(lat))
            east, north = -(by - ay), (bx - ax) * cos               # a normal to the segment, in metric proportions
            norm = math.hypot(east, north) or 1.0
            lon += off * east / norm / (M_PER_DEG * cos)
            lat += off * north / norm / M_PER_DEG
        else:
            lon, lat = x0 + u("x") * (x1 - x0), y0 + u("y") * (y1 - y0)
        if u("acc") > DENSITY_FLOOR + (1 - DENSITY_FLOOR) * math.exp(-((_dist_m(lon, lat, clon, clat) / scale) ** 2)):
            continue                                                 # thinner towards the edge, for road and open-ground points alike
        lat, lon = round(lat, 6), round(lon, 6)                     # test the numbers that will be sent, not the unrounded ones
        if d.area.contains(lon, lat) and not any(w.contains(lon, lat) for w in city.water):
            return lat, lon
    lon, lat = d.interior
    return round(lat, 6), round(lon, 6)
