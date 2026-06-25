"""Auto-sync MW Solar monthly values into cache.db (Netztransparenz)."""

from __future__ import annotations

import calendar
from datetime import date
from typing import Any

from altena.cache_store import get_mwsolar_map, init_db, put_mwsolar_monthly
from altena.leneda_client import load_config


def _add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


def last_closed_year_month(today: date | None = None) -> str:
    """Last calendar month considered published (excludes current month)."""
    t = today or date.today()
    y, m = _add_months(t.year, t.month, -1)
    return f"{y}-{m:02d}"


def year_month_range(year_month_from: str, year_month_to: str) -> list[str]:
    y0, m0 = (int(year_month_from[:4]), int(year_month_from[5:7]))
    y1, m1 = (int(year_month_to[:4]), int(year_month_to[5:7]))
    out: list[str] = []
    y, m = y0, m0
    while (y, m) <= (y1, m1):
        out.append(f"{y}-{m:02d}")
        y, m = _add_months(y, m, 1)
    return out


def _sync_from_date(config: dict[str, Any]) -> str:
    am = config.get("amortization") or {}
    explicit = (config.get("mwsolar_sync_from") or am.get("mwsolar_sync_from") or "").strip()
    if explicit:
        return explicit[:10] if len(explicit) >= 7 else explicit
    commissioning = (am.get("commissioning_date") or "").strip()
    leneda_from = (am.get("leneda_benefit_from") or "2025-01-01").strip()
    if commissioning and commissioning < leneda_from:
        return commissioning[:10]
    return leneda_from[:10]


def missing_closed_months(config: dict[str, Any]) -> tuple[list[str], str, str]:
    """
    Expected year_months through last closed month that are absent from SQLite.
    Returns (missing_list, range_from_ym, range_to_ym).
    """
    start_date = _sync_from_date(config)
    start_ym = start_date[:7]
    end_ym = last_closed_year_month()
    if start_ym > end_ym:
        return [], start_ym, end_ym
    expected = year_month_range(start_ym, end_ym)
    cached = get_mwsolar_map(start_ym, end_ym)
    missing = [ym for ym in expected if ym not in cached]
    return missing, start_ym, end_ym


def netztransparenz_configured() -> bool:
    try:
        from altena.netztransparenz_client import load_netztransparenz_config

        load_netztransparenz_config()
        return True
    except (FileNotFoundError, ValueError, OSError):
        return False


def sync_mwsolar(
    config: dict[str, Any] | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """
    Fetch missing MW Solar months from Netztransparenz into cache.db.
    Only closed months (before current calendar month) are required.
    """
    cfg = config or load_config()
    if not cfg.get("mwsolar_auto_update", True):
        return {"ok": True, "skipped": True, "reason": "disabled"}

    missing, start_ym, end_ym = missing_closed_months(cfg)
    if not missing and not force:
        return {
            "ok": True,
            "skipped": True,
            "reason": "up_to_date",
            "through": end_ym,
            "cached_months": len(year_month_range(start_ym, end_ym)),
        }

    if not netztransparenz_configured():
        return {
            "ok": False,
            "skipped": True,
            "reason": "no_credentials",
            "missing": missing,
            "message": "MW Solar : identifiants Netztransparenz absents ou invalides dans secrets.json.",
        }

    from altena.netztransparenz_client import fetch_mw_solar_monthly

    fetch_from_ym = missing[0] if missing else start_ym
    fetch_to_ym = missing[-1] if missing else end_ym
    y0, m0 = int(fetch_from_ym[:4]), int(fetch_from_ym[5:7])
    y1, m1 = int(fetch_to_ym[:4]), int(fetch_to_ym[5:7])
    last_day = calendar.monthrange(y1, m1)[1]
    start = date(y0, m0, 1)
    end = date(y1, m1, last_day)

    init_db()
    rows = fetch_mw_solar_monthly(start, end)
    wanted = set(missing) if missing else set(year_month_range(start_ym, end_ym))
    rows = [r for r in rows if r.get("year_month") in wanted]
    stored = put_mwsolar_monthly(rows)

    cached_after = get_mwsolar_map(fetch_from_ym, fetch_to_ym)
    still_missing = [ym for ym in (missing or year_month_range(start_ym, end_ym)) if ym not in cached_after]

    result: dict[str, Any] = {
        "ok": True,
        "skipped": False,
        "stored": stored,
        "fetched": len(rows),
        "missing_before": len(missing),
        "still_missing": still_missing,
        "through": end_ym,
    }
    if missing:
        filled = len(missing) - len([ym for ym in missing if ym in still_missing])
        result["message"] = (
            f"MW Solar : {stored} mois mis à jour en cache "
            f"({filled}/{len(missing)} manquants complétés, jusqu'à {end_ym})."
        )
    else:
        result["message"] = f"MW Solar : {stored} mois rafraîchis en cache."
    if still_missing:
        result["message"] += (
            f" Non publiés ou indisponibles : {', '.join(still_missing[:4])}"
            f"{'…' if len(still_missing) > 4 else ''}."
        )
    return result


def sync_mwsolar_on_startup(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Called when the Flask app starts; never raises."""
    try:
        return sync_mwsolar(config)
    except Exception as exc:
        return {
            "ok": False,
            "skipped": True,
            "reason": "error",
            "message": f"MW Solar sync failed: {exc}",
        }
