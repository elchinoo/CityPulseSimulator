"""Fictional district names, so two generated cities almost never share one.

A district id is `<qualifier>-<noun>`: the qualifier is any of ~160 short words (`alder`, `north`, `st-clair`), the noun depends on the
district's KIND (a harbour has a `quay`, `wharf`, `docks`; an industrial district a `mill`, `works`, `yard`). Every kind has 20 nouns, so
a kind has about 3,100 names and the twelve kinds about 37,000 in all; a city has 3 to 12 districts, so two cities rarely meet. The id is the
name (CityPulse's rule: `^[a-z0-9][a-z0-9_-]{0,23}$`); UIs show it prettified (`alder-quay` as `Alder Quay`). Words are short enough
that no name passes 24 characters, and sensor ids that embed it stay valid (engine.py shortens them if they would pass 64).
"""
import random

QUALIFIERS: tuple[str, ...] = (
    "alder", "amber", "ash", "aspen", "azure", "bay", "beacon", "birch", "bishop", "bright", "briar", "brook", "cedar", "chalk", "cherry",
    "clay", "clear", "cliff", "clover", "cobble", "cold", "copper", "coral", "cotton", "crane", "crown", "cypress", "daisy", "dawn", "dune",
    "eagle", "east", "elder", "elm", "ember", "fair", "falcon", "fern", "fig", "flint", "fog", "forge", "fox", "frost", "garnet",
    "gold", "grand", "granite", "grey", "grove", "gull", "hallow", "hawk", "hazel", "heather", "heron", "high", "holly", "honey",
    "indigo", "iron", "ivory", "ivy", "jasper", "juniper", "kestrel", "king", "lake", "lantern", "lark", "laurel", "lemon", "lilac",
    "lime", "linden", "lotus", "lower", "magnolia", "maple", "marble", "marsh", "meadow", "mill", "mist", "moon", "moss", "mulberry",
    "new", "north", "oak", "old", "olive", "onyx", "orchard", "osprey", "otter", "oyster", "pearl", "pebble", "pine", "plum", "poplar",
    "poppy", "prior", "queen", "quill", "rain", "raven", "red", "reed", "ridge", "river", "robin", "rook", "rose", "rowan", "royal",
    "russet", "saffron", "sage", "salt", "sand", "sapphire", "scarlet", "silver", "slate", "sparrow", "spruce", "star", "stone",
    "summer", "sun", "swan", "sycamore", "tansy", "teal", "thistle", "thorn", "tide", "timber", "upper", "velvet", "violet", "walnut",
    "west", "wheat", "willow", "wind", "winter", "wren", "yarrow", "yew", "south", "st-clair", "st-anne", "st-jude", "st-mark",
)

NOUNS: dict[str, tuple[str, ...]] = {
    "harbour": ("quay", "wharf", "docks", "pier", "marina", "jetty", "slipway", "basin", "anchorage", "boatyard", "seawall", "breakwater",
                "landing", "mole", "shipyard", "waterfront", "dockside", "lighthouse", "customs", "fishmarket"),
    "historic": ("old-town", "cloister", "abbey", "castle", "minster", "citadel", "gatehouse", "rampart", "chapel", "priory", "keep",
                 "lanes", "cobbles", "guildhall", "almshouses", "cathedral", "bastion", "heritage", "square", "vennel"),
    "centre": ("central", "plaza", "exchange", "crossing", "forum", "arcade", "boulevard", "circus", "quarter", "downtown", "concourse",
               "junction", "commons", "terrace", "parade", "metro", "avenue", "gateway", "financial", "civic"),
    "campus": ("campus", "college", "academy", "institute", "labs", "quad", "library", "observatory", "science-park", "university",
               "hall", "lecture", "scholars", "polytechnic", "seminary", "research", "faculty", "residence", "cloisters", "study"),
    "riverside": ("riverside", "bend", "ford", "weir", "embankment", "towpath", "wetlands", "floodplain", "bank", "mudflats", "shallows",
                  "reach", "confluence", "millrace", "locks", "oxbow", "riverwalk", "fen", "delta", "backwater"),
    "airport": ("airfield", "runway", "terminal", "hangars", "aerodrome", "airpark", "apron", "skyport", "control", "flightline",
                "cargo", "tarmac", "departures", "aviation", "landing-strip", "airside", "heliport", "beacon-field", "windsock", "airhub"),
    "residential": ("heights", "gardens", "court", "mews", "row", "close", "terraces", "villas", "homes", "green", "lane", "walk",
                    "crescent", "grange", "orchards", "cottages", "meadows", "rise", "dene", "estate"),
    "industrial": ("mill", "works", "yard", "foundry", "forge", "depot", "smelter", "warehouses", "refinery", "factory", "kilns",
                   "tannery", "gasworks", "industrial", "wharfs", "rail-yard", "workshops", "steelworks", "plant", "logistics"),
    "green": ("park", "woods", "common", "glade", "arboretum", "reserve", "downs", "heath", "dell", "nature-park", "gardens", "copse",
              "orchard", "greenway", "hollow", "vale", "pasture", "botanic", "lawns", "wildwood"),
    "suburb": ("hills", "fields", "village", "outskirts", "commons", "bypass", "greens", "gate", "parkside", "edge", "end", "vale",
               "hamlet", "cross", "lea", "mount", "ridgeway", "northside", "gardens-end", "newtown"),
    "market": ("market", "bazaar", "souk", "stalls", "emporium", "fairground", "market-hall", "bullring", "shambles", "mercantile",
               "marketplace", "trading-post", "arcades", "high-street", "corn-exchange", "haymarket", "fishgate", "wares", "shops", "grocers"),
    "rural": ("farm", "barns", "pastures", "fields", "croft", "meadow", "granary", "orchard", "paddock", "ranch", "steading", "hedgerow",
              "vineyard", "furrow", "homestead", "dairy", "farmland", "moor", "byre", "haywain"),
}
KINDS = tuple(NOUNS)
assert all(len(v) >= 8 and len(set(v)) == len(v) for v in NOUNS.values()) and len(set(QUALIFIERS)) == len(QUALIFIERS) >= 40
assert all(len(q) + 1 + len(n) <= 24 for q in QUALIFIERS for v in NOUNS.values() for n in v)


def district_id(rng: random.Random, kind: str, taken: set[str]) -> str:
    """A name for a district of this kind that is not in `taken` (the districts of the same city)."""
    nouns = NOUNS[kind]
    for _ in range(100):
        name = f"{rng.choice(QUALIFIERS)}-{rng.choice(nouns)}"
        if name not in taken:
            return name
    n = 2                                                    # cannot happen with 3,100 names per kind and at most 12 districts
    while f"{name[:21]}-{n}" in taken:
        n += 1
    return f"{name[:21]}-{n}"


def pretty(district: str) -> str:
    """`alder-quay` as `Alder Quay`, `d01` as `D01`."""
    return " ".join(w.capitalize() for w in district.replace("_", "-").split("-") if w)
