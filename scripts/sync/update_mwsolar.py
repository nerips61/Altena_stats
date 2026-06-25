#!/usr/bin/env python3
"""CLI: sync MW Solar monthly values from Netztransparenz into cache.db."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from altena.cache_store import get_mwsolar_monthly
from altena.leneda_client import load_config
from altena.mwsolar_sync import sync_mwsolar
from altena.netztransparenz_client import supplier_injection_eur_per_kwh


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--health", action="store_true")
    args = parser.parse_args()

    if args.health:
        from altena.netztransparenz_client import check_api_health, load_netztransparenz_config

        print("health:", check_api_health(load_netztransparenz_config()))
        return 0

    result = sync_mwsolar(load_config(), force=args.force)
    if result.get("message"):
        print(result["message"])
    if not result.get("ok") and not result.get("skipped"):
        return 1
    if result.get("skipped") and result.get("reason") == "up_to_date":
        print(f"Cache déjà complet jusqu'à {result.get('through')}.")

    still = result.get("still_missing") or []
    if still:
        print("Toujours absents:", ", ".join(still))

    ym = result.get("through")
    if ym:
        row = get_mwsolar_monthly(ym)
        if row:
            sud = supplier_injection_eur_per_kwh(row["mw_solar_ct_per_kwh"], "sudstroum")
            print(f"Dernier mois clos {ym}: {row['mw_solar_ct_per_kwh']} ct → Sudstroum {sud:.5f} €/kWh")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
