#!/usr/bin/env python3
"""Copy the real-city GeoJSON files from the sibling CityPulse checkout into src/cpsim/geodata/.

    python3 scripts/sync_geodata.py [--from ../CityPulse/src/citypulse/geodata]

CityPulse owns the data (its scripts/build_boundaries.py downloads, simplifies and writes it, and stores the same district polygons in
PostGIS); the simulator needs the polygons and roads to place sensors INSIDE them. The copy is byte for byte, so a position computed
here lies inside the polygon CityPulse draws. A test checks that the district ids in the files match realcities.KINDS.
"""
import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = ("*.districts.geojson", "*.context.geojson", "ATTRIBUTION.md")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--from", dest="src", default=str(ROOT.parent / "CityPulse" / "src" / "citypulse" / "geodata"))
    a = ap.parse_args()
    src, dst = Path(a.src), ROOT / "src" / "cpsim" / "geodata"
    files = sorted({f for pat in FILES for f in src.glob(pat)})
    if not files:
        sys.exit(f"no geodata in {src}")
    dst.mkdir(parents=True, exist_ok=True)
    for f in files:
        shutil.copyfile(f, dst / f.name)
        print(f"{f.name}: {f.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
