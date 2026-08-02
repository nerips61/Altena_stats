#!/usr/bin/env python3
"""Export statique pour Solarenergie fir Altena (energy-communities.net).

Sous-domaine encore à trancher (s4a / altena / …) — l'export ne dépend pas du DNS.

- Statistiques : 5 périodes figées (mêmes raccourcis que l'UI :
  Depuis mise en service, Année courante, Année passée, Semestre courant,
  Semestre passé) × granularités autorisées (Day <=62j, Week <153j, Month
  toujours). Réutilise fetch_dashboard() de app.py telle quelle.
- Pas de profils horaires (absents de l'app).
- Amortissement non exporté tant que config.json n'a pas de bloc amortization
  (UI locale déjà désactivée).

Ne touche jamais à app.py ni cache.db. Décomptes reste local
(module type=billing, portal/entities.json).

Retire des payloads le statut interne des comptes Leneda (sans intérêt public).

Usage :
  python3 scripts/export_static.py

Puis, une fois vérifié en local (preview_static.command) :
  git add web/ && git commit -m "Export Altena stats" && git push
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.chdir(ROOT)

from app import fetch_dashboard  # noqa: E402
from altena.data_availability import resolve_max_end_date  # noqa: E402
from altena.leneda_client import load_config  # noqa: E402

WEB_DIR = ROOT / "web"
DATA_DIR = WEB_DIR / "data"

PRESET_LABELS = {
    "since-operational": "Depuis mise en service",
    "year-current": "Année courante",
    "year-past": "Année passée",
    "semester-current": "Semestre courant",
    "semester-past": "Semestre passé",
}

AGGREGATIONS = ("Day", "Week", "Month")


def _capped(d: date, max_end: date) -> date:
    return min(d, max_end)


def compute_stats_presets(
    today: date,
    max_end: date,
    operational_from: date,
) -> dict[str, tuple[str, str]]:
    """Même logique que applyPeriodPreset() en JS."""
    y, m = today.year, today.month

    presets: dict[str, tuple[str, str]] = {
        "since-operational": (
            operational_from.isoformat(),
            _capped(today, max_end).isoformat(),
        ),
        "year-current": (date(y, 1, 1).isoformat(), _capped(today, max_end).isoformat()),
        "year-past": (
            date(y - 1, 1, 1).isoformat(),
            _capped(date(y - 1, 12, 31), max_end).isoformat(),
        ),
    }
    if m <= 6:
        presets["semester-current"] = (date(y, 1, 1).isoformat(), _capped(today, max_end).isoformat())
        presets["semester-past"] = (
            date(y - 1, 7, 1).isoformat(),
            _capped(date(y - 1, 12, 31), max_end).isoformat(),
        )
    else:
        presets["semester-current"] = (date(y, 7, 1).isoformat(), _capped(today, max_end).isoformat())
        presets["semester-past"] = (
            date(y, 1, 1).isoformat(),
            _capped(date(y, 6, 30), max_end).isoformat(),
        )
    return presets


def period_span_days(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days + 1


def granularity_allowed(level: str, span: int) -> bool:
    if level == "Day":
        return span <= 62
    if level == "Week":
        return span < 153
    return True


def default_granularity(span: int) -> str:
    if span >= 122:
        return "Month"
    if span > 62:
        return "Week"
    return "Day"


def export_stats(
    today: date,
    max_end: date,
    operational_from: date,
    availability: dict,
) -> list[dict]:
    presets = compute_stats_presets(today, max_end, operational_from)
    generated = []
    print("Statistiques — périodes figées :")
    for preset_id, (start, end) in presets.items():
        if start > end:
            print(f"  {preset_id:20s} ignoré (start {start} > end {end})")
            continue
        span = period_span_days(start, end)
        allowed = [lvl for lvl in AGGREGATIONS if granularity_allowed(lvl, span)]
        default_agg = default_granularity(span)
        print(f"  {preset_id:20s} {start} → {end} ({span}j) — {', '.join(allowed)}")
        for agg in allowed:
            payload = fetch_dashboard(start, end, chart_aggregation=agg)
            payload["data_availability"] = {
                **availability,
                "period_capped": False,
                "requested_end": end,
            }
            payload.pop("leneda_accounts", None)
            payload.pop("fusion_solar", None)
            out_path = DATA_DIR / f"stats-{preset_id}-{agg}.json"
            out_path.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
        generated.append(
            {
                "id": preset_id,
                "label": PRESET_LABELS[preset_id],
                "start": start,
                "end": end,
                "aggregations": allowed,
                "default_aggregation": default_agg,
            }
        )
    return generated


def main() -> int:
    config = load_config()
    operational_raw = str(config.get("operational_from") or "2026-06-12")
    operational_from = date.fromisoformat(operational_raw)

    print("Sonde disponibilité Leneda…")
    availability = resolve_max_end_date(probe_live=True, force_refresh=True)
    max_end = date.fromisoformat(str(availability["max_end_date"]))
    today = date.today()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stats_presets = export_stats(today, max_end, operational_from, availability)

    default_preset = "since-operational"
    if today < operational_from:
        default_preset = "year-current"

    meta = {
        "app_title": config.get("app_title", "Solarenergie fir Altena"),
        "site_label": config.get("site_label", ""),
        "operational_from": operational_raw,
        "operational_note": config.get("operational_note") or "",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "data_availability": availability,
        "stats_presets": stats_presets,
        "default_stats_preset": default_preset,
        "amortization_enabled": False,
    }
    (DATA_DIR / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    static_src = ROOT / "static"
    static_dst = WEB_DIR / "static"
    static_dst.mkdir(parents=True, exist_ok=True)
    for name in ("style.css", "embedded.js", "chart.umd.min.js"):
        src = static_src / name
        if src.is_file():
            shutil.copyfile(src, static_dst / name)

    print(f"\nOK — export dans {WEB_DIR}")
    print("Vérifier en local (preview_static.command), puis :")
    print('  git add web/ && git commit -m "Export Altena stats" && git push')
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
