"""Cities and the sensor groups they contain, with strict validation (a city is user input)."""
import ipaddress
import math
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse

from .catalog import DISTRICT_ID, TYPES
from .protocols import PROTOCOLS
from .protocols.image import MAX_CHANNELS

PROTOCOL_CHOICES = ("direct", *PROTOCOLS)
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
MAX_GROUPS, MAX_GROUP_COUNT, MAX_CITY_SENSORS = 100, 5000, 10_000
# How many devices an address space allows on one server (one protocol endpoint per city).
DEVICE_LIMITS = {"modbus_tcp": 247, "ethernet_ip": 1000, "ethercat": 4096, "profinet": 4096, "profibus": 125}


class SpecError(ValueError):
    pass


@dataclass
class Group:
    type: str
    count: int
    district: str = "d01"
    protocol: str = "direct"
    lo: float | None = None           # operating range; defaults to the type's usual range
    hi: float | None = None
    frequency: float = 30.0           # seconds between readings
    burst_pct: float = 10.0           # 0..100: how likely a sensor is to enter a burst episode

    def span(self) -> tuple[float, float]:
        t = TYPES[self.type]
        lo = self.lo if self.lo is not None else max(t.lo, t.mean - 2 * t.amp - 3 * t.sigma)
        hi = self.hi if self.hi is not None else min(t.hi, t.mean + 2 * t.amp + 3 * t.sigma)
        if hi <= lo:                   # a type with no swing (traffic lights, parking): use its limits
            lo, hi = t.lo, t.hi
        return lo, hi


@dataclass
class City:
    name: str
    target_url: str = ""              # CityPulse base URL; empty means "generate only, do not send"
    token_env: str = "CITYPULSE_INGEST_TOKEN"   # NAME of the environment variable holding the ingest token
    seed: int | None = None           # makes the random data and the bursts reproducible
    poll_interval: float = 2.0        # how often the gateway polls field devices
    expose: bool = False              # bind the protocol servers to all interfaces instead of loopback
    autostart: bool = False           # start this city when the app starts
    groups: list[Group] = field(default_factory=list)

    def sensor_count(self) -> int:
        return sum(g.count for g in self.groups)

    def to_dict(self) -> dict:
        return asdict(self)


def _num(value, what: str, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lo <= value <= hi:
        raise SpecError(f"{what} must be a number between {lo:g} and {hi:g}")
    return float(value)


def check_target(url: str) -> str:
    """CityPulse's address: https anywhere, plain http only for this machine; no credentials, no link-local."""
    if not url:
        return ""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise SpecError("target_url must be an http(s) URL without credentials, query or fragment")
    host = u.hostname
    local = host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost")
    if u.scheme == "http" and not local:
        raise SpecError("target_url must use https unless it points at this machine")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None                      # a hostname, not an address literal
    if ip is not None and ip.is_link_local:   # cloud metadata services live here
        raise SpecError("target_url must not point at a link-local address")
    return url.rstrip("/")


def parse_group(d: dict) -> Group:
    if not isinstance(d, dict):
        raise SpecError("each group must be an object")
    unknown = set(d) - {"type", "count", "district", "protocol", "lo", "hi", "frequency", "burst_pct"}
    if unknown:
        raise SpecError(f"unknown group field(s): {', '.join(sorted(unknown))}")
    kind = d.get("type")
    if kind not in TYPES:
        raise SpecError(f"unknown sensor type {kind!r}; choose one of the {len(TYPES)} catalogue types")
    count = d.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_GROUP_COUNT:
        raise SpecError(f"count must be a whole number from 1 to {MAX_GROUP_COUNT}")
    district = d.get("district", "d01")
    if not isinstance(district, str) or not DISTRICT_ID.match(district):
        raise SpecError("district must be a short id such as d01")
    protocol = d.get("protocol", "direct")
    if protocol not in PROTOCOL_CHOICES:
        raise SpecError(f"protocol must be one of {', '.join(PROTOCOL_CHOICES)}")
    lo = None if d.get("lo") is None else _num(d["lo"], "lo", -1e9, 1e9)
    hi = None if d.get("hi") is None else _num(d["hi"], "hi", -1e9, 1e9)
    if lo is not None and hi is not None and not lo < hi:
        raise SpecError("lo must be smaller than hi")
    return Group(kind, count, district, protocol, lo, hi, _num(d.get("frequency", 30.0), "frequency", 1, 3600),
                 _num(d.get("burst_pct", 10.0), "burst_pct", 0, 100))


def parse_city(d: dict) -> City:
    if not isinstance(d, dict):
        raise SpecError("a city must be an object")
    unknown = set(d) - {"name", "target_url", "token_env", "seed", "poll_interval", "expose", "autostart", "groups"}
    if unknown:
        raise SpecError(f"unknown city field(s): {', '.join(sorted(unknown))}")
    name = d.get("name")
    if not isinstance(name, str) or not NAME.match(name):
        raise SpecError("name must be lowercase letters, digits and dashes (1 to 40 characters)")
    token_env = d.get("token_env", "CITYPULSE_INGEST_TOKEN")
    if not isinstance(token_env, str) or not ENV_NAME.match(token_env):
        raise SpecError("token_env is the NAME of an environment variable (e.g. CITYPULSE_INGEST_TOKEN), never the token itself")
    seed = d.get("seed")
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**32):
        raise SpecError("seed must be a whole number from 0 to 4294967295")
    groups_raw = d.get("groups", [])
    if not isinstance(groups_raw, list) or len(groups_raw) > MAX_GROUPS:
        raise SpecError(f"groups must be a list of at most {MAX_GROUPS}")
    city = City(name, check_target(d.get("target_url") or ""), token_env, seed,
                _num(d.get("poll_interval", 2.0), "poll_interval", 0.5, 60), bool(d.get("expose", False)),
                bool(d.get("autostart", False)), [parse_group(g) for g in groups_raw])
    if city.sensor_count() > MAX_CITY_SENSORS:
        raise SpecError(f"a city can have at most {MAX_CITY_SENSORS} sensors")
    for proto, limit in DEVICE_LIMITS.items():
        sensors = sum(g.count for g in city.groups if g.protocol == proto)
        if -(-sensors // MAX_CHANNELS) > limit:
            raise SpecError(f"{proto} supports at most {limit} devices of {MAX_CHANNELS} sensors; this city needs more")
    return city
