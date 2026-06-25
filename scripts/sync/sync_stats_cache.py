#!/usr/bin/env python3
"""CLI: sync current month Leneda + Enphase into cache.db."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from marin_midi.stats_cache_sync import sync_current_month


def main() -> int:
    result = sync_current_month()
    if result.get("skipped"):
        print(f"Skipped: {result.get('reason')}")
        return 0
    extra: list[str] = []
    if result.get("enphase"):
        extra.append("Enphase")
    if result.get("fusion_solar"):
        extra.append(f"FusionSolar ({', '.join(result['fusion_solar'])})")
    monthly = result.get("fusion_solar_monthly") or {}
    for roof_id, info in (monthly.get("roofs") or {}).items():
        if info.get("error"):
            print(f"  FusionSolar monthly {roof_id}: {info['error']}")
            continue
        missing = info.get("missing_months") or []
        miss = f", missing {len(missing)}" if missing else ""
        print(
            f"  FusionSolar monthly {roof_id}: {info.get('months', 0)} months "
            f"({info.get('total', 0)} kWh{miss})"
        )
    suffix = f" + {' + '.join(extra)}" if extra else ""
    print(f"Synced {result['start']} → {result['end']} (Month){suffix}")
    for err in result.get("fusion_solar_errors") or []:
        print(f"  FusionSolar warning: {err}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
