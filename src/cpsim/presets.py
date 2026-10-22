"""CityPulse as a workload: a city IS the CityPulse workload (it streams on its own schedule), so this only builds one from a few numbers.

A city's schedule is `poll_interval` (how often the gateway polls its field devices) and each group's `frequency` (seconds between
readings of one sensor). The "live feed" preset puts every sensor on `direct` (no protocol sockets, so it runs in a container with
nothing published) and sets both to the interval you ask for: a reading from each of `sensors` sensors every `interval` seconds.
"""
from .model import City, SpecError, check_target, parse_city

# type, share of the sensors, district
LIVE_FEED_MIX = [("temperature", 20, "d01"), ("humidity", 10, "d01"), ("air_quality", 15, "d06"), ("noise", 10, "d08"),
                 ("water_pressure", 10, "d04"), ("smart_meter", 15, "d05"), ("traffic_light", 10, "d03"), ("rain", 10, "d07")]
FIELDS = {"name", "kind", "target_url", "token_env", "params", "schedule"}


def live_feed(name: str, target_url: str = "", token_env: str = "CITYPULSE_INGEST_TOKEN", interval: float = 5.0, sensors: int = 60,
              seed: int | None = None) -> dict:
    """The spec of a city that sends `sensors` readings every `interval` seconds (1 to 3600)."""
    if isinstance(sensors, bool) or not isinstance(sensors, int) or not 8 <= sensors <= 10_000:
        raise SpecError("params.sensors must be a whole number from 8 to 10000")
    groups, left = [], sensors
    for i, (type_, share, district) in enumerate(LIVE_FEED_MIX):
        n = left if i == len(LIVE_FEED_MIX) - 1 else max(1, round(sensors * share / 100))
        n = min(n, left)
        if n:
            groups.append({"type": type_, "count": n, "district": district, "protocol": "direct", "frequency": interval, "burst_pct": 5})
            left -= n
    return {"name": name, "target_url": target_url, "token_env": token_env, "seed": seed, "poll_interval": max(0.5, min(interval, 60)),
            "autostart": False, "groups": groups}


def city_from_workload(d: dict) -> City:
    """A headless-file entry with `kind: citypulse` becomes a city: schedule.interval is the reading interval, params.sensors the size."""
    unknown = set(d) - FIELDS
    if unknown:
        raise SpecError(f"unknown workload field(s): {', '.join(sorted(unknown))}")
    params, sched = d.get("params") or {}, d.get("schedule") or {}
    extra = set(params) - {"sensors", "seed"}
    if extra:
        raise SpecError(f"unknown param(s): {', '.join(sorted(extra))}")
    extra = set(sched) - {"interval"}
    if extra:
        raise SpecError(f"a citypulse workload takes only schedule.interval (a city streams continuously); not: {', '.join(sorted(extra))}")
    interval = sched.get("interval", 5)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not 1 <= interval <= 3600:
        raise SpecError("schedule.interval must be a number between 1 and 3600")
    return parse_city(live_feed(d.get("name"), check_target(d.get("target_url") or ""), d.get("token_env") or "CITYPULSE_INGEST_TOKEN",
                                float(interval), params.get("sensors", 60), params.get("seed")))
