#!/usr/bin/env python3
"""Charge data/manual_production.json dans cache.db (production brute Marin / Midi)."""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from marin_midi.manual_production import list_all, load_seed_file, seed_from_file, upsert_rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed manual monthly PV production into SQLite")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Réécrire même si la table contient déjà des lignes",
    )
    parser.add_argument(
        "--json",
        default="",
        help="Fichier JSON alternatif (défaut: data/manual_production.json)",
    )
    args = parser.parse_args()
    if args.json:
        data = load_seed_file(args.json)
        n = upsert_rows(data.get("rows") or [])
    else:
        n = seed_from_file(force=args.force)
    print(f"→ {n} ligne(s) enregistrée(s) dans cache.db (manual_production_monthly)")
    for row in list_all():
        print(f"  {row['year_month']} {row['roof_id']}: {row['kwh']} kWh")


if __name__ == "__main__":
    main()
