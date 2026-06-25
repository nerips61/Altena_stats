#!/usr/bin/env python3
"""CLI: fetch all configured series and print JSON (for testing)."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from marin_midi.leneda_client import fetch_all_series

if __name__ == "__main__":
    start = sys.argv[1] if len(sys.argv) > 1 else "2026-05-01"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-05-22"
    agg = sys.argv[3] if len(sys.argv) > 3 else "Day"
    data = fetch_all_series(start, end, chart_aggregation=agg)
    print(json.dumps(data, indent=2, ensure_ascii=False))
