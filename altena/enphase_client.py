"""Enphase Enlighten API v4 client (Watt plan: energy_lifetime daily production)."""

from __future__ import annotations

import json
import os
import threading
from datetime import date, datetime, timedelta
from typing import Any

import requests
from zoneinfo import ZoneInfo

TZ_LUX = ZoneInfo("Europe/Luxembourg")

from altena.paths import SECRETS_PATH, enphase_token_path
API_ROOT = "https://api.enphaseenergy.com/api/v4"
TOKEN_URL = "https://api.enphaseenergy.com/oauth/token"
DEFAULT_REDIRECT = "https://api.enphaseenergy.com/oauth/redirect_uri"

_secrets_lock = threading.Lock()


def _read_secrets_file() -> dict[str, Any]:
    if not os.path.isfile(SECRETS_PATH):
        raise FileNotFoundError(
            f"Missing {SECRETS_PATH}. Copy secrets.example.json and complete OAuth."
        )
    with open(SECRETS_PATH, encoding="utf-8") as f:
        return json.load(f)


def _write_secrets_file(data: dict[str, Any]) -> None:
    with open(SECRETS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _load_enphase_block() -> dict[str, Any]:
    data = _read_secrets_file()
    enphase = data.get("enphase")
    if not isinstance(enphase, dict):
        raise ValueError('secrets.json: add an "enphase" block — see secrets.example.json')
    merged = dict(enphase)
    path = enphase_token_path()
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                overlay = json.load(f)
            if isinstance(overlay, dict):
                for key in ("access_token", "refresh_token"):
                    if overlay.get(key):
                        merged[key] = overlay[key]
        except (OSError, json.JSONDecodeError):
            pass
    return merged


def _persist_enphase_tokens(enphase: dict[str, Any]) -> None:
    token_updates: dict[str, str] = {
        "access_token": (enphase.get("access_token") or "").strip(),
    }
    if (enphase.get("refresh_token") or "").strip():
        token_updates["refresh_token"] = enphase["refresh_token"].strip()

    with _secrets_lock:
        if os.path.isfile(SECRETS_PATH) and os.access(SECRETS_PATH, os.W_OK):
            data = _read_secrets_file()
            if isinstance(data.get("enphase"), dict):
                data["enphase"]["access_token"] = token_updates["access_token"]
                if token_updates.get("refresh_token"):
                    data["enphase"]["refresh_token"] = token_updates["refresh_token"]
                _write_secrets_file(data)
            return

        path = enphase_token_path()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(token_updates, f, indent=2, ensure_ascii=False)
            f.write("\n")


def load_enphase_config() -> dict[str, Any]:
    return _load_enphase_block()


def enphase_configured() -> bool:
    try:
        ep = load_enphase_config()
    except (FileNotFoundError, ValueError):
        return False
    return bool(
        (ep.get("api_key") or "").strip()
        and (ep.get("client_id") or "").strip()
        and (ep.get("client_secret") or "").strip()
        and (ep.get("refresh_token") or "").strip()
    )


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    import base64

    raw = f"{client_id}:{client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def refresh_access_token(enphase: dict[str, Any]) -> str:
    client_id = (enphase.get("client_id") or "").strip()
    client_secret = (enphase.get("client_secret") or "").strip()
    refresh_token = (enphase.get("refresh_token") or "").strip()
    if not all([client_id, client_secret, refresh_token]):
        raise ValueError("enphase: client_id, client_secret, and refresh_token required")

    response = requests.post(
        TOKEN_URL,
        headers={
            "Authorization": _basic_auth_header(client_id, client_secret),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        timeout=60,
    )
    response.raise_for_status()
    tokens = response.json()
    access = (tokens.get("access_token") or "").strip()
    if not access:
        raise ValueError("Enphase token refresh returned no access_token")
    enphase["access_token"] = access
    if tokens.get("refresh_token"):
        enphase["refresh_token"] = tokens["refresh_token"]
    return access


def _api_headers(enphase: dict[str, Any]) -> dict[str, str]:
    api_key = (enphase.get("api_key") or "").strip()
    access = (enphase.get("access_token") or "").strip()
    if not api_key:
        raise ValueError("enphase.api_key missing in secrets.json")
    if not access:
        access = refresh_access_token(enphase)
        _persist_enphase_tokens(enphase)
    return {"Authorization": f"Bearer {access}", "key": api_key}


def _request(
    enphase: dict[str, Any],
    path: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    system_id = (enphase.get("system_id") or "").strip()
    if not system_id:
        raise ValueError("enphase.system_id missing in secrets.json")

    url = f"{API_ROOT}/systems/{system_id}/{path}"
    headers = _api_headers(enphase)

    response = requests.get(url, headers=headers, params=params or {}, timeout=60)
    if response.status_code == 401:
        access = refresh_access_token(enphase)
        _persist_enphase_tokens(enphase)
        headers["Authorization"] = f"Bearer {access}"
        response = requests.get(url, headers=headers, params=params or {}, timeout=60)
    response.raise_for_status()
    return response.json()


def _kwh_from_wh(wh: int | float) -> int:
    return int(round(float(wh) / 1000.0))


def _parse_date(s: str) -> date:
    return date.fromisoformat(s[:10])


def _today_local() -> date:
    return datetime.now(TZ_LUX).date()


def _energy_lifetime_end_for_api(user_end: date) -> date:
    """
    Enphase returns an empty production[] when end_date is today (incomplete day).
    Use yesterday as the last closed day in energy_lifetime; add today via summary.
    """
    today = _today_local()
    if user_end >= today:
        return today - timedelta(days=1)
    return user_end


def _fetch_today_wh(enphase: dict[str, Any]) -> int:
    summary = _request(enphase, "summary")
    return int(summary.get("energy_today") or 0)


def _parse_energy_lifetime_points(
    payload: dict[str, Any],
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    production = payload.get("production") or []
    if not production:
        return []
    api_start = _parse_date(payload.get("start_date") or start.isoformat())
    points: list[dict[str, Any]] = []
    for offset, wh in enumerate(production):
        day = api_start + timedelta(days=offset)
        if day < start or day > end:
            continue
        iso = day.isoformat()
        points.append(
            {
                "date": iso,
                "bucket_start": iso,
                "value": _kwh_from_wh(wh),
            }
        )
    return points


def _fetch_daily_production_api(start_date: str, end_date: str) -> dict[str, Any]:
    """
    Daily production from GET .../energy_lifetime (Wh per day → kWh).
    Watt plan does not allow /stats (405); consumption meter may be absent.

    API quirk: end_date equal to today yields production=[]. Closed days use
    end_date <= yesterday; today is taken from GET .../summary (energy_today).
    """
    with _secrets_lock:
        enphase = _load_enphase_block()

    start = _parse_date(start_date)
    end = _parse_date(end_date)
    if start > end:
        return {"unit": "kWh", "points": [], "total": 0, "source": "energy_lifetime"}

    today = _today_local()
    includes_today = end >= today
    api_end = _energy_lifetime_end_for_api(end)

    params: dict[str, str] = {"start_date": start.isoformat()}
    if api_end and api_end >= start:
        params["end_date"] = api_end.isoformat()

    payload = _request(enphase, "energy_lifetime", params)
    _persist_enphase_tokens(enphase)

    points = _parse_energy_lifetime_points(payload, start, end)

    # Empty response with end_date=today — retry without end_date, then filter
    if not points and "end_date" in params:
        payload = _request(enphase, "energy_lifetime", {"start_date": start.isoformat()})
        points = _parse_energy_lifetime_points(payload, start, end)

    today_note: str | None = None
    if includes_today:
        wh_today = _fetch_today_wh(enphase)
        _persist_enphase_tokens(enphase)
        if wh_today > 0:
            iso = today.isoformat()
            points = [p for p in points if p["bucket_start"] != iso]
            points.append(
                {
                    "date": iso,
                    "bucket_start": iso,
                    "value": _kwh_from_wh(wh_today),
                    "partial_day": True,
                }
            )
            points.sort(key=lambda p: p["bucket_start"])
            today_note = (
                f"Aujourd’hui ({iso}) : production partielle depuis l’API summary "
                f"({_kwh_from_wh(wh_today)} kWh à {datetime.now(TZ_LUX).strftime('%H:%M')})."
            )

    total = sum(p["value"] for p in points)
    meta = payload.get("meta") or {}
    result: dict[str, Any] = {
        "unit": "kWh",
        "points": points,
        "total": total,
        "source": "energy_lifetime",
        "has_consumption_meter": False,
        "meta": {
            "status": meta.get("status"),
            "last_report_at": meta.get("last_report_at"),
        },
    }
    if today_note:
        result["note"] = today_note
    return result


def _enphase_cache_enabled() -> bool:
    try:
        from altena.leneda_client import load_config

        return bool(load_config().get("cache_enabled", True))
    except OSError:
        return True


def fetch_daily_production(start_date: str, end_date: str) -> dict[str, Any]:
    if not _enphase_cache_enabled():
        return _fetch_daily_production_api(start_date, end_date)
    from altena.cache_store import fetch_daily_enphase_with_cache

    return fetch_daily_enphase_with_cache(
        start_date,
        end_date,
        _fetch_daily_production_api,
    )


def aggregate_production_to_timeline(
    daily_points: list[dict[str, Any]],
    timeline: list[str],
    timeline_starts: dict[str, str],
    chart_aggregation: str,
) -> list[dict[str, Any]]:
    """Roll daily Enphase Wh into Leneda chart buckets (Day / Week / Month labels)."""
    by_day = {p["bucket_start"]: p["value"] for p in daily_points}

    if chart_aggregation == "Day":
        aligned = []
        for label in timeline:
            start = timeline_starts.get(label) or label
            aligned.append(
                {
                    "date": label,
                    "bucket_start": start,
                    "value": by_day.get(start, 0),
                    "empty": start not in by_day,
                }
            )
        return aligned

    if chart_aggregation == "Month":
        aligned = []
        for label in timeline:
            month_key = label if len(label) == 7 else label[:7]
            total = 0
            found = False
            for day_iso, val in by_day.items():
                if day_iso[:7] == month_key:
                    total += val
                    found = True
            aligned.append(
                {
                    "date": label,
                    "bucket_start": timeline_starts.get(label) or f"{month_key}-01",
                    "value": total,
                    "empty": not found,
                }
            )
        return aligned

    # Week: sum days whose bucket_start falls in [week_start, week_end] from timeline_starts
    aligned = []
    sorted_days = sorted(by_day.keys())
    for i, label in enumerate(timeline):
        week_start = timeline_starts.get(label) or label
        if i + 1 < len(timeline):
            next_start = timeline_starts.get(timeline[i + 1]) or timeline[i + 1]
            week_end = (_parse_date(next_start) - timedelta(days=1)).isoformat()
        else:
            week_end = sorted_days[-1] if sorted_days else week_start
        total = 0
        found = False
        for day_iso, val in by_day.items():
            if week_start <= day_iso <= week_end:
                total += val
                found = True
        aligned.append(
            {
                "date": label,
                "bucket_start": week_start,
                "value": total,
                "empty": not found,
            }
        )
    return aligned


def fetch_production_series(
    start_date: str,
    end_date: str,
    chart_aggregation: str,
    timeline: list[str],
    timeline_starts: dict[str, str],
) -> dict[str, Any]:
    daily = fetch_daily_production(start_date, end_date)
    aligned = aggregate_production_to_timeline(
        daily["points"],
        timeline,
        timeline_starts,
        chart_aggregation,
    )
    period_total = daily["total"]
    entry: dict[str, Any] = {
        "id": "enphase_production",
        "label": "Production PV (Enphase)",
        "group": "enphase",
        "color": "#ea580c",
        "unit": "kWh",
        "points": aligned,
        "total": period_total,
        "source": daily["source"],
        "has_consumption_meter": daily.get("has_consumption_meter", False),
    }
    if daily.get("note"):
        entry["note"] = daily["note"]
    return entry
