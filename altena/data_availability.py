"""Dernière date Leneda disponible — borne la fin de période affichée."""

from __future__ import annotations

import threading
import time
from datetime import date

from altena.cache_store import last_available_leneda_date_from_cache
from altena.leneda_client import probe_leneda_last_available_date

_PROBE_CACHE: tuple[float, str | None] | None = None
_PROBE_LOCK = threading.Lock()
_PROBE_TTL_SEC = 900


def _memory_cached_date() -> str | None:
    with _PROBE_LOCK:
        if _PROBE_CACHE is not None:
            return _PROBE_CACHE[1]
    return None


def _combine_last_dates(*dates: str | None) -> str | None:
    valid = [d for d in dates if d]
    return max(valid) if valid else None


def _probed_last_date(*, force: bool = False) -> str | None:
    global _PROBE_CACHE
    now = time.monotonic()
    with _PROBE_LOCK:
        if not force and _PROBE_CACHE is not None:
            ts, val = _PROBE_CACHE
            if now - ts < _PROBE_TTL_SEC:
                return val
    result = probe_leneda_last_available_date()
    with _PROBE_LOCK:
        _PROBE_CACHE = (now, result)
    return result


def resolve_max_end_date(
    *,
    probe_live: bool = True,
    force_refresh: bool = False,
) -> dict[str, str | bool | None]:
    """Retourne la dernière date sélectionnable (min entre aujourd'hui et Leneda)."""
    today = date.today().isoformat()
    probed = _probed_last_date(force=force_refresh) if probe_live else None
    cached = last_available_leneda_date_from_cache()
    mem = _memory_cached_date()
    last = _combine_last_dates(probed, mem, cached)
    if last and last > today:
        last = today
    max_end = last or today
    source = "probe" if probed else ("cache" if cached else ("memory" if mem else "today"))
    if probed and cached and cached > probed:
        source = "cache"
    elif not probed and mem and cached and cached > mem:
        source = "cache"
    elif not probed and mem:
        source = "memory"
    return {
        "today": today,
        "last_available_date": last,
        "max_end_date": max_end,
        "data_lag": bool(last and last < today),
        "source": source,
    }


def clamp_period(
    start: str,
    end: str,
    *,
    probe_live: bool = False,
    force_refresh: bool = False,
) -> tuple[str, str, dict]:
    """Borne end à max_end_date ; retourne (start, end_effectif, meta)."""
    info = resolve_max_end_date(probe_live=probe_live, force_refresh=force_refresh)
    max_end = str(info["max_end_date"])
    capped = end > max_end
    effective_end = end if end <= max_end else max_end
    if effective_end < start:
        effective_end = start
    meta = {**info, "period_capped": capped, "requested_end": end}
    return start, effective_end, meta


def default_period(*, probe_live: bool = True, force_refresh: bool = True) -> tuple[str, str, dict]:
    """Période par défaut : operational_from ou 1er du mois → dernière date Leneda."""
    today = date.today()
    from altena.leneda_client import load_config

    config = load_config()
    op_from = (config.get("operational_from") or "").strip()
    start = today.replace(day=1).isoformat()
    if op_from:
        try:
            op_date = date.fromisoformat(op_from[:10])
            if today >= op_date:
                start = op_from[:10]
        except ValueError:
            pass
    _, end, meta = clamp_period(
        start,
        today.isoformat(),
        probe_live=probe_live,
        force_refresh=force_refresh,
    )
    return start, end, meta
