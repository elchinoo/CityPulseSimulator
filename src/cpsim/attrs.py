"""Device metadata of a simulated sensor (`attrs`, stored by CityPulse as JSONB): vendor, firmware, install date, calibration, tags.

A pure function of the sensor's id, type, district and city (sha256, no seed, no clock), so the same sensor has the same metadata after a
restart, a recreate or a changed seed, in every process and on every Python version. The shape is what CityPulse's gateway accepts
(`check_attrs`: keys `[a-z][a-z0-9_]*`, two levels, lists of at most 8 scalars, strings up to 120 characters) and is kept under
MAX_BYTES, far below the gateway's 1024.

SYNTHETIC SAMPLE BEHAVIOUR: the oldest firmware (OLD_FIRMWARE, about 8 % of the sensors) is also the one that faults more often: the
generator multiplies the chance of starting a burst episode by OLD_FIRMWARE_FAULT_FACTOR for those sensors (`SensorSim`). That makes
CityPulse's "Explain this district" finding "this firmware stands out among the problem sensors" true in the sample data. Real devices
have no such rule; it exists to give the metadata something to find. It does not depend on whether attrs are sent (CPSIM_ATTRS / `attrs`).
"""
import datetime as dt
import hashlib
import json

MAX_BYTES = 400                     # budget for one attrs object once serialized compactly (the gateway allows 1024)
ATTRS_EVERY = 30                    # a sensor repeats its attrs on every 30th reading (the first one carries them), like lat/lon
OLD_FIRMWARE = "1.4.2"              # shared by every vendor, so "firmware 1.4.2" is one value CityPulse can count across vendors
OLD_FIRMWARE_SHARE = 0.08
OLD_FIRMWARE_FAULT_FACTOR = 3.0     # chance multiplier for starting a fault episode on the old firmware

# Three or four plausible vendors per sensor type (invented names).
VENDORS: dict[str, list[str]] = {
    "rain": ["Aerolab", "Meteosense", "Nimbus Instruments"], "light": ["Aerolab", "Luxon", "Nimbus Instruments"],
    "humidity": ["Meteosense", "Aerolab", "Nimbus Instruments", "Ecoscan"], "temperature": ["Aerolab", "Meteosense", "Nimbus Instruments"],
    "wind_speed": ["Meteosense", "Nimbus Instruments", "Aerolab"], "pressure": ["Aerolab", "Meteosense", "Nimbus Instruments"],
    "uv_index": ["Luxon", "Aerolab", "Meteosense"],
    "traffic_camera": ["Urbanis", "Trafficon", "Carrefour Systems", "Visionway"], "traffic_light": ["Trafficon", "Carrefour Systems", "Urbanis"],
    "radar": ["Trafficon", "Visionway", "Carrefour Systems"], "parking": ["Urbanis", "Parkwise", "Trafficon"],
    "pedestrian_counter": ["Visionway", "Urbanis", "Trafficon"], "bike_share": ["Urbanis", "Parkwise", "Gridwise"],
    "ev_charger": ["Gridwise", "Parkwise", "Urbanis", "Voltaro"],
    "air_quality": ["Ecoscan", "Aerolab", "Nimbus Instruments"], "co2": ["Ecoscan", "Aerolab", "Nimbus Instruments"],
    "noise": ["Ecoscan", "Urbanis", "Aerolab"], "soil_moisture": ["Ecoscan", "Hydrotek", "Nimbus Instruments"],
    "water_level": ["Hydrotek", "Gridwise", "Urbanis"], "water_flow": ["Hydrotek", "Gridwise", "Urbanis"],
    "water_pressure": ["Hydrotek", "Gridwise", "Urbanis"], "smart_meter": ["Gridwise", "Voltaro", "Urbanis", "Hydrotek"],
    "street_light": ["Voltaro", "Urbanis", "Gridwise"], "gas_leak": ["Gridwise", "Hydrotek", "Ecoscan"],
    "waste_bin": ["Urbanis", "Gridwise", "Ecoscan"],
    "bridge_vibration": ["Structa", "Seismix", "Gridwise"], "seismic": ["Seismix", "Structa", "Gridwise"],
}
_NEWER = ("2.0.1", "2.1.4", "2.2.0")          # firmware per vendor: the old one plus three newer ones, none shared across the next list
_FW_SUFFIX = {"Aerolab": "", "Meteosense": "-m", "Nimbus Instruments": "-n", "Ecoscan": "-e", "Luxon": "-l", "Urbanis": "-u",
              "Trafficon": "-t", "Carrefour Systems": "-c", "Visionway": "-v", "Parkwise": "-p", "Gridwise": "-g", "Voltaro": "-o",
              "Hydrotek": "-h", "Structa": "-s", "Seismix": "-x"}
_NEWER_WEIGHTS = (0.25, 0.42, 0.25)             # shares among the newer versions, scaled to the remaining 92 %
TAGS = ("roof", "pole", "kerb", "underground", "solar", "mains", "battery", "north", "south", "east", "west")
INSTALL_END = dt.date(2026, 10, 1)              # install dates fall in the five years before this fixed day (no clock, so no drift)
INSTALL_DAYS = 5 * 365


def _unit(*parts: object) -> float:
    """A stable number in [0, 1) from the parts."""
    h = hashlib.sha256("\x1f".join(("cpsim-attrs", *map(str, parts))).encode()).digest()
    return int.from_bytes(h[:8], "big") / 2**64


def vendor_of(sensor_id: str, kind: str, city: str = "") -> str:
    vendors = VENDORS[kind]
    return vendors[int(_unit(city, sensor_id, "vendor") * len(vendors))]


def firmware_of(sensor_id: str, kind: str, city: str = "") -> str:
    """The firmware of a sensor: the old, rare OLD_FIRMWARE for about 8 % of them, else one of three newer versions of its vendor."""
    vendor = vendor_of(sensor_id, kind, city)
    r = _unit(city, sensor_id, "firmware")
    if r < OLD_FIRMWARE_SHARE:
        return OLD_FIRMWARE
    r = (r - OLD_FIRMWARE_SHARE) / (1 - OLD_FIRMWARE_SHARE)
    acc = 0.0
    for fw, w in zip(_NEWER, _NEWER_WEIGHTS):
        acc += w
        if r < acc:
            return fw + _FW_SUFFIX[vendor]
    return _NEWER[-1] + _FW_SUFFIX[vendor]


def fault_factor(sensor_id: str, kind: str, city: str = "") -> float:
    """How much more readily a sensor starts fault episodes: OLD_FIRMWARE_FAULT_FACTOR on the old firmware, else 1."""
    return OLD_FIRMWARE_FAULT_FACTOR if firmware_of(sensor_id, kind, city) == OLD_FIRMWARE else 1.0


def sensor_attrs(sensor_id: str, kind: str, district: str, city: str = "") -> dict:
    """vendor, firmware, install_date, calibration {offset, scale, date}, tags (1 to 3, sorted) of one sensor."""
    install = int(_unit(city, sensor_id, "install") * INSTALL_DAYS)
    cal = min(install + int(_unit(city, sensor_id, "cal") * 400), INSTALL_DAYS - 1)

    def day(n: int) -> str:
        return (INSTALL_END - dt.timedelta(days=INSTALL_DAYS - n)).isoformat()

    tags = {TAGS[int(_unit(city, sensor_id, "tag", i) * len(TAGS))] for i in range(1 + int(_unit(city, sensor_id, "ntags") * 3))}
    return {"vendor": vendor_of(sensor_id, kind, city), "firmware": firmware_of(sensor_id, kind, city), "install_date": day(install),
            "calibration": {"offset": round((_unit(city, sensor_id, "off") - 0.5) * 0.4, 3),
                            "scale": round(1 + (_unit(city, sensor_id, "scl") - 0.5) * 0.02, 4), "date": day(cal)},
            "tags": sorted(tags)}


def size(attrs: dict) -> int:
    return len(json.dumps(attrs, separators=(",", ":"), ensure_ascii=False).encode())
