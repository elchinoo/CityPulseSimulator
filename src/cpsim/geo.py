"""Where a simulated city is on the map, so CityPulse can draw it (`lat`/`lon` per reading, WGS 84, 6 decimals).

Everything is a pure function of names, never of a seed or the clock, so the same city name gives the same map after a restart, a
recreate or a changed seed, and sensors never jump:

* the city's ORIGIN is a built-in anchor for known names (`gotham` is New York, `london` is London) and otherwise comes from a hash of
  its name: latitude -50..58, longitude -165..165 (clear of the poles and the antimeridian);
* its districts take cells of CELL_M metres on a square spiral around the origin (cell 0 is the origin). The order is by the district's
  KIND (read from its generated name, `alder-quay` is a harbour; ids such as `d01` have no kind and sort as a middle kind): centres and old
  towns first (so they sit near the origin), then markets, campuses, housing, parks, rivers, harbours, industry, suburbs, airports and
  farmland towards the edge; ties break by id. Cells never overlap. Adding a district can move the ones after it in that order;
  existing districts keep their cell as long as the set of ids up to them is unchanged;
* a sensor sits at a hash-of-its-id position inside its district's cell (10 % margin, so neighbouring districts do not touch).

`CityRunner` sends the position with a sensor's first reading and then every POSITION_EVERY readings: CityPulse keeps the last position
it was sent (`coalesce` on the sensor row), so a few repeats are enough to restore it after a database reset, at a small payload cost.
"""
import hashlib
import math

from .districts import NOUNS

CELL_M = 1500.0                  # side of a district cell
MARGIN = 0.10                    # share of a cell's side kept free on each edge
M_PER_DEG = 111_320.0            # metres per degree of latitude
POSITION_EVERY = 30              # a sensor repeats its position on every 30th reading (the first one carries it)
LAT_RANGE, LON_RANGE = (-50.0, 58.0), (-165.0, 165.0)

KIND_ORDER = ("centre", "historic", "market", "campus", "residential", "green", "riverside", "harbour", "industrial", "suburb",
              "airport", "rural")
_UNKNOWN_RANK = KIND_ORDER.index("residential")
assert set(KIND_ORDER) == set(NOUNS)
_NOUN_KIND: dict[str, str] = {}
for _kind in KIND_ORDER:                       # a noun that several kinds share belongs to the one placed nearest the centre
    for _noun in NOUNS[_kind]:
        _NOUN_KIND.setdefault(_noun, _kind)


def _unit(*parts: str) -> float:
    """A stable number in [0, 1) from the parts."""
    h = hashlib.sha256("\x1f".join(("cpsim-geo", *parts)).encode()).digest()
    return int.from_bytes(h[:8], "big") / 2**64


def kind_of(district: str) -> str | None:
    """The kind a generated district id (`<qualifier>-<noun>`) belongs to, else None (`d01`, hand-made names)."""
    parts = district.split("-")
    for i in range(1, len(parts)):             # the qualifier may itself contain a dash (`st-anne`), nouns may too (`old-town`)
        kind = _NOUN_KIND.get("-".join(parts[i:]))
        if kind:
            return kind
    return None


# Anchors by city name (lowercase, spaces or dashes). A fictional name stands in for a real place, so distances and bearings are physical.
ANCHORS: dict[str, tuple[float, float]] = {
    "gotham": (40.7128, -74.0060), "metropolis": (41.8781, -87.6298),
    "london": (51.5074, -0.1278), "paris": (48.8566, 2.3522), "berlin": (52.5200, 13.4050), "madrid": (40.4168, -3.7038),
    "lisbon": (38.7223, -9.1393), "rome": (41.9028, 12.4964), "tokyo": (35.6762, 139.6503), "sydney": (-33.8688, 151.2093),
    "new york": (40.7128, -74.0060), "chicago": (41.8781, -87.6298), "san francisco": (37.7749, -122.4194),
    "boston": (42.3601, -71.0589), "toronto": (43.6532, -79.3832), "sao paulo": (-23.5505, -46.6333),
    "mexico city": (19.4326, -99.1332), "mumbai": (19.0760, 72.8777), "singapore": (1.3521, 103.8198),
    "cape town": (-33.9249, 18.4241), "dubai": (25.2048, 55.2708), "amsterdam": (52.3676, 4.9041),
}


def origin(city: str) -> tuple[float, float]:
    """A known name gets its anchor; any other name a deterministic hash-derived spot (cities may be real or invented)."""
    key = city.lower().replace("-", " ").replace("_", " ").strip()
    if key in ANCHORS:
        return ANCHORS[key]
    lat = LAT_RANGE[0] + _unit(city, "lat") * (LAT_RANGE[1] - LAT_RANGE[0])
    lon = LON_RANGE[0] + _unit(city, "lon") * (LON_RANGE[1] - LON_RANGE[0])
    return lat, lon


def spiral(n: int) -> tuple[int, int]:
    """The n-th cell (0 = origin) of a square spiral: (0,0), (1,0), (1,1), (0,1), (-1,1), (-1,0), ..."""
    x = y = 0
    dx, dy, run, turned = 1, 0, 1, 0
    for _ in range(n):
        x, y = x + dx, y + dy
        run -= 1
        if run == 0:
            dx, dy, turned = -dy, dx, turned + 1
            run = turned // 2 + 1
    return x, y


def _rank(district: str) -> int:
    kind = kind_of(district)
    return KIND_ORDER.index(kind) if kind else _UNKNOWN_RANK


def cells(districts) -> dict[str, tuple[int, int]]:
    """Grid cell of each district id (a collection of ids, any order): kind order first, then id."""
    ordered = sorted(set(districts), key=lambda d: (_rank(d), d))
    return {d: spiral(i) for i, d in enumerate(ordered)}


def _offset(lat0: float, east_m: float, north_m: float) -> tuple[float, float]:
    """Degrees of latitude and longitude for a move of metres from latitude lat0."""
    return north_m / M_PER_DEG, east_m / (M_PER_DEG * math.cos(math.radians(lat0)))


class CityMap:
    """The positions of one city: `CityMap(name, district ids).position(sensor id, district id)`."""

    def __init__(self, city: str, districts):
        self.city, self.cells = city, cells(districts)
        self.lat0, self.lon0 = origin(city)

    def district_centre(self, district: str) -> tuple[float, float]:
        cx, cy = self.cells[district]
        dlat, dlon = _offset(self.lat0, cx * CELL_M, cy * CELL_M)
        return round(self.lat0 + dlat, 6), round(self.lon0 + dlon, 6)

    def position(self, sensor: str, district: str) -> tuple[float, float]:
        cx, cy = self.cells[district]
        span = 1 - 2 * MARGIN
        east = (cx + (MARGIN + _unit(self.city, sensor, "x") * span) - 0.5) * CELL_M
        north = (cy + (MARGIN + _unit(self.city, sensor, "y") * span) - 0.5) * CELL_M
        dlat, dlon = _offset(self.lat0, east, north)
        return round(self.lat0 + dlat, 6), round(self.lon0 + dlon, 6)
