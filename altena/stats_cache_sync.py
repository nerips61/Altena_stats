"""Refresh current-month Leneda / Enphase buckets in cache.db."""

from __future__ import annotations

from datetime import date
from typing import Any


def current_month_range() -> tuple[str, str]:
    today = date.today()
    return today.replace(day=1).isoformat(), today.isoformat()


def sync_current_month(config: dict[str, Any] | None = None) -> dict[str, Any]:
    from altena.enphase_client import enphase_configured, fetch_daily_production as fetch_enphase_daily
    from altena.fusion_solar_client import (
        fusion_solar_any_configured,
        fetch_daily_production as fetch_fs_daily,
        online_from,
        sync_fusion_solar_monthly_backfill,
    )
    from altena.leneda_client import fetch_all_series, load_config

    config = config or load_config()
    if not config.get("cache_enabled", True):
        return {"skipped": True, "reason": "cache_disabled"}

    start, end = current_month_range()
    fetch_all_series(start, end, chart_aggregation="Month")
    enphase = False
    if enphase_configured():
        fetch_enphase_daily(start, end)
        enphase = True
    fusion_solar: list[str] = []
    fusion_solar_errors: list[str] = []
    fusion_solar_monthly: dict[str, Any] = {}
    if fusion_solar_any_configured():
        fs_start = max(start, online_from())
        if fs_start <= end:
            for roof_id in ("marin", "midi"):
                try:
                    fetch_fs_daily(roof_id, fs_start, end)
                    fusion_solar.append(roof_id)
                except Exception as exc:
                    fusion_solar_errors.append(f"{roof_id}: {exc}")
        try:
            fusion_solar_monthly = sync_fusion_solar_monthly_backfill(
                start_date=online_from(),
                end_date=end,
            )
        except Exception as exc:
            fusion_solar_errors.append(f"monthly backfill: {exc}")
    return {
        "ok": True,
        "start": start,
        "end": end,
        "enphase": enphase,
        "fusion_solar": fusion_solar,
        "fusion_solar_monthly": fusion_solar_monthly,
        "fusion_solar_errors": fusion_solar_errors,
    }
