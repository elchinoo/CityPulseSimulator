"""The workload data: a deterministic simulated city turned into text rows and search queries.

A city comes from the simulator's own generator (`citygen.generate_city`, same seed gives the same city), its sensors are the simulator's
`SensorSim`, and each row of the benchmark corpus is one observation of one sensor written as a sentence (type, district, vendor,
firmware, reading with unit, fault episode if any). That is the text an operator would want to search ("which air quality sensors in the
harbor reported faults").
"""
import math
import random
import time
from dataclasses import dataclass

from .. import attrs as attrs_mod
from .. import catalog, citygen, model
from ..generator import SensorSim

MAX_SENSORS = 10_000
EPISODE_WORDS = {"spike": "a spike beyond its physical range", "stuck": "a stuck value", "noise": "heavy noise", "dropout": "no signal (dropout)",
                 "corrupt": "a corrupt value", "flood": "a burst of rapid reports"}


@dataclass(frozen=True)
class Row:
    sensor_id: str
    district: str
    kind: str
    body: str


@dataclass
class City:
    name: str
    seed: int
    sensors: list[SensorSim]
    districts: list[str]
    kinds: list[str]


def build_city(sensors: int, seed: int = 1, profile: str = "mixed") -> City:
    if not citygen.MIN_SENSORS <= sensors <= MAX_SENSORS:
        raise model.SpecError(f"sensors must be from {citygen.MIN_SENSORS} to {MAX_SENSORS}")
    g = citygen.generate_city(seed, sensors, profile, name="bench-city", real=False)
    city = model.parse_city(g.city)
    now = time.time()
    sims: list[SensorSim] = []
    for grp in city.groups:
        lo, hi = grp.span()
        for _ in range(grp.count):
            sid = f"{grp.type.replace('_', '-')}-{grp.district}-{len(sims):05d}"
            sims.append(SensorSim(sid, grp.type, grp.district, grp.protocol, lo, hi, grp.frequency, grp.burst_pct, seed, now, city.name))
    return City(city.name, seed, sims, sorted({s.district for s in sims}), sorted({s.type for s in sims}))


def district_name(did: str) -> str:
    return catalog.DISTRICTS.get(did, did)


def describe(sim: SensorSim, value: float | None, episode: str | None, when: float) -> str:
    t = catalog.TYPES[sim.type]
    a = attrs_mod.sensor_attrs(sim.id, sim.type, sim.district, sim.city)
    head = f"{t.label} sensor {sim.id} in {district_name(sim.district)} ({a['vendor']}, firmware {a['firmware']}, {sim.protocol.replace('_', ' ')})"
    if value is None:
        reading = "reported nothing this cycle"
    elif isinstance(value, float) and math.isnan(value):
        reading = "reported an unreadable value"
    else:
        reading = f"measured {value:.1f} {t.unit}"
    tail = f"; it is showing {EPISODE_WORDS[episode]}" if episode in EPISODE_WORDS else "; it looks healthy"
    return f"{head} {reading} at {time.strftime('%H:%M:%S', time.gmtime(when))} UTC{tail}."


class Corpus:
    """An endless, deterministic stream of observation rows: sensors report in turn, advancing their own simulation."""

    def __init__(self, city: City, seed: int = 1):
        self.city, self._i, self._t = city, 0, time.time()
        self._rng = random.Random(seed)

    def rows(self, n: int) -> list[Row]:
        out = []
        sensors = self.city.sensors
        for _ in range(n):
            s = sensors[self._i % len(sensors)]
            self._i += 1
            self._t += 0.01
            tick = s.step(self._t)
            out.append(Row(s.id, s.district, s.type, describe(s, tick.value, tick.kind, self._t)))
        return out

    def queries(self, n: int) -> list[str]:
        """Natural-language questions an operator or an agent would ask of this city."""
        rng = random.Random(self._rng.random())
        kinds, districts = self.city.kinds, self.city.districts
        templates = [
            lambda: f"which {catalog.TYPES[rng.choice(kinds)].label.lower()} sensors are showing faults",
            lambda: f"sensors in {district_name(rng.choice(districts))} that reported a spike",
            lambda: f"{catalog.TYPES[rng.choice(kinds)].label.lower()} readings in {district_name(rng.choice(districts))}",
            lambda: f"devices with a stuck value or heavy noise near {district_name(rng.choice(districts))}",
            lambda: f"sensors that lost signal in {district_name(rng.choice(districts))}",
            lambda: f"which {catalog.TYPES[rng.choice(kinds)].label.lower()} sensor looks healthy",
        ]
        return [rng.choice(templates)() for _ in range(n)]


def marker_text(token: str) -> str:
    """A unique, distinctive observation used to time how long a new row takes to become retrievable."""
    return f"Calibration marker {token}: technician note about sensor probe {token} verified against a reference instrument."
