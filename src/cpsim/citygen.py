"""Demo-city generator: seeded variability, so ten cities are never ten copies of one.

Same seed, size and profile give the same city; another seed gives another one. A city makes up 3 to 12 districts of its own (a kind
such as harbour or industrial and a fictional name such as `alder-quay`, see districts.py: two cities almost never share one),
gives each district the sensor types that plausibly belong there (a harbour has water levels and vibration, an industrial park has
air quality, noise and power, a residential district has noise, parking and waste), draws group sizes from a skewed distribution
(a few big groups, many small), and gives each group its own protocol, frequency, burst percentage and sometimes a narrower range.
Everything produced is checked by `parse_city`, so it stays inside the existing limits.
"""
import hashlib
import math
import random
import re
from dataclasses import dataclass

from . import fleet
from .catalog import TYPES
from .districts import KINDS, district_id, pretty
from .model import (
    DEVICE_LIMITS,
    MAX_CITY_SENSORS,
    MAX_GROUP_COUNT,
    MAX_GROUPS,
    NAME,
    SpecError,
    parse_city,
)
from .protocols.image import MAX_CHANNELS

SIZES = {"small": 200, "medium": 2_000, "large": 20_000}      # target sensors; clamped to MAX_CITY_SENSORS
PROFILES = ("balanced", "industrial", "coastal", "tourist", "residential", "mixed")
MIN_SENSORS = 8
MAX_COUNT = 50
MAX_DISTRICTS = 12
BASE = 0.25               # affinity of a type that is not listed for a kind: unlikely, never impossible

# Affinity (0 to 10) of each sensor type for each kind of district; unlisted types get BASE.
_W = "weather"
AFFINITY: dict[str, dict[str, float]] = {
    "harbour": {"water_level": 10, "bridge_vibration": 6, "wind_speed": 7, "seismic": 2, "noise": 5, "traffic_camera": 4, "smart_meter": 4,
                "gas_leak": 3, "air_quality": 4, "rain": 4, "pressure": 4, "light": 3, "street_light": 5, "waste_bin": 3, "humidity": 5},
    "historic": {"pedestrian_counter": 9, "noise": 8, "parking": 6, "waste_bin": 7, "street_light": 7, "air_quality": 5, "temperature": 5,
                 "humidity": 4, "bike_share": 5, "traffic_camera": 3, "uv_index": 3, "co2": 3},
    "centre": {"traffic_camera": 9, "parking": 9, "air_quality": 8, "traffic_light": 8, "pedestrian_counter": 8, "noise": 7, "radar": 4,
               "ev_charger": 6, "bike_share": 6, "street_light": 5, "smart_meter": 5, "co2": 4, "waste_bin": 4, "temperature": 3},
    "campus": {"bike_share": 8, "pedestrian_counter": 7, "co2": 6, "smart_meter": 6, "ev_charger": 5, "parking": 5, "noise": 4, "temperature": 5,
               "humidity": 4, "street_light": 4, "water_flow": 3, "waste_bin": 3, "light": 3},
    "riverside": {"water_level": 9, "water_flow": 8, "rain": 7, "humidity": 6, "soil_moisture": 5, "bridge_vibration": 5, "pressure": 4,
                  "wind_speed": 4, "pedestrian_counter": 3, "bike_share": 3, "street_light": 3, "temperature": 3},
    "airport": {"noise": 10, "air_quality": 8, "traffic_camera": 5, "radar": 5, "wind_speed": 8, "pressure": 6, "light": 5, "temperature": 5,
                "ev_charger": 4, "parking": 6, "smart_meter": 5, "co2": 4, "uv_index": 3, "rain": 4},
    "residential": {"noise": 7, "parking": 9, "waste_bin": 9, "street_light": 8, "smart_meter": 6, "water_pressure": 5, "water_flow": 3,
                    "ev_charger": 4, "traffic_light": 3, "temperature": 4, "humidity": 3, "bike_share": 3, "co2": 2},
    "industrial": {"air_quality": 10, "noise": 9, "smart_meter": 10, "gas_leak": 8, "water_flow": 6, "water_pressure": 6, "co2": 6,
                   "temperature": 5, "bridge_vibration": 3, "seismic": 3, "radar": 3, "traffic_camera": 3, "street_light": 4, "pressure": 3},
    "green": {"soil_moisture": 9, "humidity": 8, "temperature": 7, "rain": 7, "uv_index": 6, "light": 6, "wind_speed": 5, "air_quality": 5,
              "pedestrian_counter": 4, "bike_share": 4, "waste_bin": 4, "water_flow": 3, "pressure": 3},
    "suburb": {"parking": 6, "waste_bin": 7, "street_light": 7, "smart_meter": 6, "noise": 4, "water_pressure": 5, "ev_charger": 5,
               "traffic_light": 4, "radar": 4, "temperature": 3, "rain": 3, "humidity": 3},
    "market": {"pedestrian_counter": 8, "parking": 7, "waste_bin": 8, "noise": 7, "air_quality": 5, "traffic_camera": 5, "street_light": 5,
               "smart_meter": 5, "temperature": 4, "humidity": 3, "bike_share": 3, "ev_charger": 3},
    "rural": {"soil_moisture": 9, "rain": 8, "wind_speed": 7, "humidity": 6, "temperature": 6, "water_flow": 5, "water_pressure": 4,
              "light": 4, "uv_index": 4, "pressure": 4, "seismic": 3, "street_light": 3, "smart_meter": 3, "waste_bin": 2},
}

# How much a profile favours each kind of district (default 1) and some sensor types (default 1).
PROFILE_DISTRICTS = {
    "balanced": {},
    "industrial": {"industrial": 6, "airport": 3, "harbour": 3, "suburb": 1.5, "residential": 0.5, "historic": 0.3, "green": 0.4},
    "coastal": {"harbour": 6, "riverside": 4, "green": 1.5, "market": 1.5, "airport": 1, "industrial": 0.5, "campus": 0.5, "rural": 0.6},
    "tourist": {"historic": 6, "centre": 3, "market": 4, "harbour": 3, "green": 2, "riverside": 2, "industrial": 0.2, "suburb": 0.5, "rural": 0.4},
    "residential": {"residential": 6, "suburb": 5, "campus": 2, "green": 2, "rural": 1.5, "industrial": 0.3, "airport": 0.3, "harbour": 0.4},
}
PROFILE_TYPES = {
    "industrial": {"gas_leak": 2.5, "smart_meter": 2, "air_quality": 2, "noise": 1.5, "co2": 2, "seismic": 1.5},
    "coastal": {"water_level": 2.5, "wind_speed": 2, "rain": 1.5, "pressure": 1.5, "humidity": 1.3},
    "tourist": {"pedestrian_counter": 2.5, "bike_share": 2, "noise": 1.5, "uv_index": 1.5, "waste_bin": 1.5},
    "residential": {"waste_bin": 2, "parking": 2, "street_light": 1.5, "ev_charger": 1.5, "noise": 1.3},
}
PROTOCOL_WEIGHTS = {"direct": 3, "modbus_tcp": 3, "ethernet_ip": 2, "profinet": 2, "ethercat": 1.5, "profibus": 1}
FREQUENCY_FACTORS = ((0.5, 1), (1, 4), (2, 2), (4, 1))
BURST_CHOICES = ((0, 2), (1, 2), (2, 3), (3, 3), (5, 4), (8, 3), (10, 3), (15, 2), (20, 2), (30, 1), (45, 1))
POLL_CHOICES = ((1, 2), (2, 5), (3, 2), (5, 1))
NARROWABLE = {k for k, t in TYPES.items() if t.stuckable and t.sigma > 0}

_PREFIX = ("alder", "amber", "ash", "bay", "beacon", "birch", "bright", "brook", "cedar", "clear", "cliff", "cold", "copper", "coral",
           "crown", "dawn", "dune", "east", "elm", "fair", "fern", "flint", "fog", "frost", "gold", "grey", "green", "hallow", "harbor",
           "heron", "high", "holly", "iron", "ivy", "juniper", "kestrel", "lake", "lark", "lime", "linden", "maple", "marsh", "mill",
           "mist", "moss", "north", "oak", "olive", "orchard", "pearl", "pine", "quarry", "raven", "red", "reed", "river", "rock",
           "rose", "sage", "salt", "sand", "silver", "slate", "south", "star", "stone", "summer", "swan", "thorn", "tide", "vale",
           "west", "willow", "wind", "winter", "wren")
_SUFFIX = ("bridge", "brook", "burg", "bury", "chester", "cove", "crest", "dale", "field", "ford", "gate", "glen", "haven", "heights",
           "hill", "hollow", "holm", "island", "keep", "land", "mead", "mere", "mont", "mouth", "park", "point", "port", "ridge",
           "shore", "side", "spring", "stead", "ton", "vale", "view", "ville", "water", "way", "wick", "wood")
_LEAD = ("port", "new", "lake", "mount", "saint", "fort", "old", "east", "west", "north", "south", "san")
_TAIL = ("falls", "springs", "bay", "heights", "junction", "landing", "crossing", "harbor", "city", "park")


@dataclass
class Generated:
    city: dict                 # a spec `parse_city` accepts
    districts: list[dict]      # [{id, name, kind, sensors, groups, types}]
    profile: str
    size: str

    @property
    def name(self) -> str:
        return self.city["name"]

    @property
    def sensors(self) -> int:
        return sum(g["count"] for g in self.city["groups"])

    def report(self) -> dict:
        groups = self.city["groups"]
        return {"name": self.name, "seed": self.city["seed"], "size": self.size, "profile": self.profile, "sensors": self.sensors,
                "groups": len(groups), "types": len({g["type"] for g in groups}), "protocols": sorted({g["protocol"] for g in groups}),
                "districts": self.districts}


def _rng(*parts) -> random.Random:
    """A generator that depends on all its parts and is stable across processes and Python versions."""
    return random.Random(int.from_bytes(hashlib.sha256("|".join(map(str, parts)).encode()).digest()[:16], "big"))


def _pick(rng: random.Random, weighted: dict, n: int) -> list:
    """`n` distinct keys, drawn without replacement with the given weights."""
    pool = {k: w for k, w in weighted.items() if w > 0}
    out = []
    while pool and len(out) < n:
        key = rng.choices(list(pool), list(pool.values()))[0]
        out.append(key)
        del pool[key]
    return out


def _slug(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")


def random_name(rng: random.Random) -> str:
    """A plausible fictional city name as a slug, e.g. `port-alder`, `silverbrook`, `linden-falls`."""
    form = rng.random()
    a, b = rng.choice(_PREFIX), rng.choice(_SUFFIX)
    if form < 0.55:
        return _slug(a + (b if a[-1] != b[0] else b[1:]))
    if form < 0.8:
        return _slug(f"{rng.choice(_LEAD)} {a}{b}" if rng.random() < 0.5 else f"{rng.choice(_LEAD)} {a}")
    return _slug(f"{a}{b if rng.random() < 0.4 else ''} {rng.choice(_TAIL)}")


def unique_name(rng: random.Random, taken: set[str]) -> str:
    for _ in range(60):
        name = random_name(rng)
        if name not in taken and NAME.match(name):
            return name
    base = random_name(rng)[:36]
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def target_sensors(size, rng: random.Random) -> tuple[int, str]:
    """(how many sensors to aim for, the size label). `size` is small, medium, large or a number of sensors."""
    if isinstance(size, str) and size.strip().isdigit():
        size = int(size)
    if isinstance(size, bool) or not isinstance(size, (str, int)):
        raise SpecError("size must be small, medium, large or a number of sensors")
    if isinstance(size, int):
        if not MIN_SENSORS <= size <= MAX_CITY_SENSORS:
            raise SpecError(f"size must be small, medium, large or a number from {MIN_SENSORS} to {MAX_CITY_SENSORS}")
        return size, str(size)
    if size not in SIZES:
        raise SpecError("size must be small, medium, large or a number of sensors")
    jitter = math.exp(rng.gauss(0, 0.3))                       # two "medium" cities are not the same size
    return max(MIN_SENSORS, min(MAX_CITY_SENSORS, round(SIZES[size] * jitter))), size


def _skewed(rng: random.Random, n: int, shape: float) -> list[float]:
    """n shares that sum to 1 with a heavy head: a few big, many small."""
    raw = [rng.paretovariate(shape) for _ in range(n)]
    total = sum(raw)
    return [r / total for r in raw]


def _fit(counts: list[int], budget: int) -> list[int]:
    """Scale counts so they sum to at most `budget`, keeping each at least 1 (the budget is at least len(counts))."""
    while sum(counts) > budget:
        over = sum(counts) - budget
        big = sorted(range(len(counts)), key=lambda i: -counts[i])
        for i in big:
            cut = min(counts[i] - 1, max(1, round(over * counts[i] / sum(counts))))
            counts[i] -= cut
            over -= cut
            if over <= 0:
                break
        else:
            break
    return counts


def _range(rng: random.Random, kind: str) -> tuple[float | None, float | None]:
    """About one group in four runs over a narrower or shifted slice of its usual range."""
    if kind not in NARROWABLE or rng.random() > 0.25:
        return None, None
    t = TYPES[kind]
    lo = max(t.lo, t.mean - 2 * t.amp - 3 * t.sigma)
    hi = min(t.hi, t.mean + 2 * t.amp + 3 * t.sigma)
    span = hi - lo
    if span <= 0:
        return None, None
    width = span * rng.uniform(0.55, 0.95)
    start = lo + (span - width) * rng.random()
    new_lo, new_hi = max(t.lo, start), min(t.hi, start + width)
    digits = 2 if span < 100 else 0
    new_lo, new_hi = round(new_lo, digits), round(new_hi, digits)
    return (new_lo, new_hi) if new_lo < new_hi else (None, None)


def generate_city(seed: int, size="medium", profile: str = "mixed", name: str | None = None, taken: frozenset | set = frozenset(),
                  target_url: str | None = None, token_env: str = "CITYPULSE_INGEST_TOKEN", autostart: bool = False) -> Generated:
    """One demo city (`target_url` None: the fleet CityPulse reference when CITYPULSE_URL is set on this app, else empty). The same (seed, size, profile) always gives the same districts and groups; `name` and `taken` only decide the
    name (a given name is used as it is; otherwise one is made up and kept out of `taken`)."""
    if profile not in PROFILES:
        raise SpecError(f"profile must be one of {', '.join(PROFILES)}")
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**32:
        raise SpecError("seed must be a whole number from 0 to 4294967295")
    rng = _rng("cpsim-city", seed, size, profile)
    total, label = target_sensors(size, rng)
    if name is not None and (not isinstance(name, str) or not NAME.match(name)):
        raise SpecError("name must be lowercase letters, digits and dashes (1 to 40 characters)")
    city_name = name or unique_name(_rng("cpsim-name", seed, size, profile), set(taken))

    # which districts
    if label == "small" or total <= 400:
        low, high = 3, 7
    elif label == "large" or total >= 6000:
        low, high = 5, 12
    else:
        low, high = 3, 12
    n_districts = max(3, min(MAX_DISTRICTS, total // 2, round(rng.uniform(low - 0.5, high + 0.5))))
    if profile == "mixed":
        mix = {kind: math.exp(rng.gauss(0, 1.1)) for kind in KINDS}
    else:
        mix = {kind: PROFILE_DISTRICTS[profile].get(kind, 1) for kind in KINDS}
    kinds: list[str] = []                                     # a kind may repeat (two harbours), each repeat less likely
    for _ in range(n_districts):
        w = {k: mix[k] * rng.uniform(0.6, 1.4) / (3 ** kinds.count(k)) for k in KINDS}
        kinds.append(rng.choices(list(w), list(w.values()))[0])
    names: set[str] = set()
    named = []
    for kind in kinds:
        d = district_id(rng, kind, names)
        names.add(d)
        named.append((d, kind))
    named.sort()
    ids = [d for d, _ in named]
    kind_of = dict(named)
    type_boost = PROFILE_TYPES.get(profile, {})
    if profile == "mixed":                                    # a mixed city leans on two or three themes of its own
        for theme in rng.sample([t for t in PROFILE_TYPES.values()], 2):
            type_boost = {**type_boost, **{k: max(v, type_boost.get(k, 1)) for k, v in theme.items()}}

    # which sensor types, and how many of each
    shares = _skewed(rng, len(ids), 1.1)
    per_district = max(1, MAX_GROUPS // len(ids))
    plan: list[tuple[str, str]] = []                         # (district, type) in a stable order
    for d in ids:
        aff = AFFINITY[kind_of[d]]
        weights = {k: (aff.get(k, BASE) * type_boost.get(k, 1)) ** 1.3 * rng.uniform(0.5, 1.5) for k in TYPES}
        want = min(per_district, len(TYPES), max(2, round(rng.triangular(2, 9, 4))), max(1, total // (3 * len(ids))))
        plan += [(d, k) for k in _pick(rng, weights, want)]
    plan = plan[:MAX_GROUPS]
    dshare = dict(zip(ids, shares))
    inside = {d: _skewed(rng, sum(1 for p in plan if p[0] == d), 1.0) for d in ids}
    raw, seen = [], {d: 0 for d in ids}
    for d, _ in plan:
        raw.append(total * dshare[d] * inside[d][seen[d]])
        seen[d] += 1
    counts = _fit([max(1, min(MAX_GROUP_COUNT, round(x))) for x in raw], min(total, MAX_CITY_SENSORS))
    while len(counts) < len(plan):
        counts.append(1)

    # protocols (a city has its own lean), within each protocol's device limit
    lean = {p: w * rng.gammavariate(0.8, 1.0) + 0.05 for p, w in PROTOCOL_WEIGHTS.items()}
    room = {p: limit * MAX_CHANNELS for p, limit in DEVICE_LIMITS.items()}
    groups = []
    for (d, kind), count in sorted(zip(plan, counts), key=lambda x: -x[1]):    # big groups choose first
        ok = {p: w for p, w in lean.items() if p == "direct" or room[p] >= count}
        proto = rng.choices(list(ok), list(ok.values()))[0]
        if proto != "direct":
            room[proto] -= count
        t = TYPES[kind]
        frequency = max(2, min(600, round(t.interval * rng.choices(*zip(*FREQUENCY_FACTORS))[0])))
        lo, hi = _range(rng, kind)
        g = {"type": kind, "count": count, "district": d, "protocol": proto, "frequency": frequency,
             "burst_pct": rng.choices(*zip(*BURST_CHOICES))[0]}
        if lo is not None:
            g["lo"], g["hi"] = lo, hi
        groups.append(g)
    order = {d: i for i, d in enumerate(ids)}
    groups.sort(key=lambda g: (order[g["district"]], -g["count"], g["type"]))

    if target_url is None:
        target_url = fleet.default_target()
    city = {"name": city_name, "target_url": target_url, "token_env": token_env, "seed": seed,
            "poll_interval": rng.choices(*zip(*POLL_CHOICES))[0], "expose": False, "autostart": bool(autostart), "groups": groups}
    parse_city(city)                                          # never hand out something the rest of the app would refuse
    districts = [{"id": d, "name": pretty(d), "kind": kind_of[d],
                  "sensors": sum(g["count"] for g in groups if g["district"] == d),
                  "groups": sum(1 for g in groups if g["district"] == d),
                  "types": [g["type"] for g in groups if g["district"] == d]} for d in ids]
    return Generated(city, districts, profile, label)


def signature(g: Generated) -> tuple:
    return tuple((x["district"], x["type"], x["count"], x["protocol"]) for x in g.city["groups"])


def generate_cities(count: int = 1, size="medium", profile: str = "mixed", seed: int | None = None, name: str | None = None,
                    taken: set[str] | frozenset = frozenset(), **kw) -> list[Generated]:
    """`count` (1 to 50) different cities. With a `seed`, city i uses seed + i, so the batch is reproducible and no two are alike; with
    none, a random seed is drawn. Names never repeat and never collide with `taken`; a given `name` is used as it is for one city
    (an error if taken) and as the stem `name-1`, `name-2`... for several."""
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_COUNT:
        raise SpecError(f"count must be a whole number from 1 to {MAX_COUNT}")
    if seed is None:
        seed = random.SystemRandom().randrange(2**32)
    used, sigs, out = set(taken), set(), []
    if name is not None and (not isinstance(name, str) or not NAME.match(name)):
        raise SpecError("name must be lowercase letters, digits and dashes (1 to 40 characters)")
    if name and count == 1 and name in used:
        raise SpecError(f"a city called {name!r} already exists")
    for i in range(count):
        s = (seed + i) % 2**32
        for _ in range(20):
            if name and count > 1:
                n = 1
                while f"{name[:36]}-{n}" in used:
                    n += 1
                given = f"{name[:36]}-{n}"
            else:
                given = name
            g = generate_city(s, size, profile, given, used, **kw)
            if signature(g) not in sigs and g.name not in used:
                break
            s = (s + 1_000_003) % 2**32          # a clash is vanishingly rare; move on deterministically
        else:
            raise SpecError("could not make this city different from the others; try another seed")
        sigs.add(signature(g))
        used.add(g.name)
        out.append(g)
    return out


def render(g: Generated, fmt: str = "yaml") -> str:
    """The city as a file `cpsim run` and `parse_city` accept (a YAML header comment says what was generated)."""
    import json

    spec = {**g.city, "groups": [{k: v for k, v in grp.items() if v is not None} for grp in g.city["groups"]]}
    if fmt == "json":
        return json.dumps(spec, indent=2) + "\n"
    import yaml

    listing = [d["id"] + " [" + d["kind"] + "]" for d in g.districts]
    head = (f"# Generated demo city: seed {spec['seed']}, size {g.size}, profile {g.profile}, {g.sensors} sensors in "
            f"{len(g.districts)} districts ({', '.join(listing)}).\n"
            "# Secrets never go here: token_env names the ENVIRONMENT VARIABLE that holds the CityPulse ingest token.\n")
    groups = spec.pop("groups")
    body = yaml.safe_dump(spec, sort_keys=False)
    rows = "".join("  - " + yaml.safe_dump(grp, default_flow_style=True, sort_keys=False, width=200).strip() + "\n" for grp in groups)
    return head + body + "groups:\n" + rows
