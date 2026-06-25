"""Huawei FusionSolar (SmartPVMS) — production onduleur, un site par toit.

Northbound API credentials are created in the FusionSolar portal (API user).
Station overview (Marin): NE=181648031 — Midi: NE=181647435
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests

from altena.enphase_client import aggregate_production_to_timeline
from altena.leneda_client import load_config
from altena.paths import SECRETS_PATH

ROOF_TO_SERIES = {
    "marin": "prod_marin_active",
    "midi": "prod_midi_active",
}

TZ_LUX = ZoneInfo("Europe/Luxembourg")
DEFAULT_API_BASE = "https://eu5.fusionsolar.huawei.com"

_session_lock = threading.Lock()
_session: requests.Session | None = None
_token_expires_at = 0.0
_probe_cache: dict[str, Any] | None = None
_probe_at = 0.0
_monthly_window_cache: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}

_FAIL_MESSAGES = {
    20056: (
        "Centrale(s) non autorisée(s) par le propriétaire (failCode 20056). "
        "Activer « accès API » dans l’app FusionSolar (Me → Power plant management → "
        "Basic info → Set permissions) pour Marin et Midi."
    ),
    407: "Limite de fréquence API Huawei — réessayer dans quelques minutes.",
}


def _read_secrets_file() -> dict[str, Any]:
    if not os.path.isfile(SECRETS_PATH):
        raise FileNotFoundError(f"Missing {SECRETS_PATH}")
    with open(SECRETS_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_fusion_solar_config(roof_id: str) -> dict[str, Any]:
    data = _read_secrets_file()
    block = data.get("fusion_solar")
    if not isinstance(block, dict):
        raise ValueError('secrets.json: add a "fusion_solar" block — see secrets.example.json')
    roof = block.get(roof_id)
    if not isinstance(roof, dict):
        raise ValueError(f"secrets.json: fusion_solar.{roof_id} missing")
    return roof


def _api_base(cfg: dict[str, Any]) -> str:
    return (cfg.get("api_base") or DEFAULT_API_BASE).rstrip("/")


def _credentials(cfg: dict[str, Any]) -> tuple[str, str, str]:
    user = (cfg.get("user_name") or cfg.get("username") or "").strip()
    secret = (cfg.get("system_code") or cfg.get("password") or "").strip()
    station = (cfg.get("station_code") or cfg.get("station_id") or "").strip()
    return user, secret, station


def fusion_solar_enabled() -> bool:
    try:
        config = load_config()
    except OSError:
        return False
    return bool((config.get("fusion_solar") or {}).get("enabled", False))


def fusion_solar_any_configured() -> bool:
    if not fusion_solar_enabled():
        return False
    return any(fusion_solar_configured(roof_id) for roof_id in ROOF_TO_SERIES)


def online_from() -> str:
    try:
        config = load_config()
    except OSError:
        return "2025-06-24"
    fs = config.get("fusion_solar") or {}
    if fs.get("online_from"):
        return str(fs["online_from"])[:10]
    mp = config.get("manual_production") or {}
    return str(mp.get("online_from") or "2025-06-24")[:10]


def fusion_solar_configured(roof_id: str) -> bool:
    try:
        cfg = load_fusion_solar_config(roof_id)
        user, secret, station = _credentials(cfg)
        return bool(user and secret and station)
    except (FileNotFoundError, ValueError):
        return False


def _parse_date(value: str) -> date:
    return date.fromisoformat(value[:10])


def _fail_message(body: dict[str, Any]) -> str:
    code = body.get("failCode")
    if code in _FAIL_MESSAGES:
        return _FAIL_MESSAGES[code]
    msg = (body.get("message") or "").strip()
    if msg:
        return f"FusionSolar API failCode {code}: {msg}"
    return f"FusionSolar API failCode {code}"


class FusionSolarApiError(RuntimeError):
    def __init__(self, body: dict[str, Any]):
        self.fail_code = body.get("failCode")
        self.body = body
        super().__init__(_fail_message(body))


def _login_session(cfg: dict[str, Any]) -> requests.Session:
    global _session, _token_expires_at

    user, secret, _ = _credentials(cfg)
    if not user or not secret:
        raise ValueError("FusionSolar: user_name and system_code required")

    with _session_lock:
        if _session is not None and time.time() < _token_expires_at - 60:
            return _session

        base = _api_base(cfg)
        session = requests.Session()
        session.headers.update({"Content-Type": "application/json"})
        body: dict[str, Any] = {}
        for attempt in range(2):
            if attempt:
                time.sleep(3)
            response = session.post(
                f"{base}/thirdData/login",
                json={"userName": user, "systemCode": secret},
                timeout=60,
            )
            response.raise_for_status()
            body = response.json()
            if body.get("success"):
                break
            if body.get("failCode") != 407 or attempt >= 1:
                raise FusionSolarApiError(body)
        else:
            raise FusionSolarApiError(body)

        token = response.headers.get("xsrf-token") or session.cookies.get("XSRF-TOKEN")
        if not token:
            raise RuntimeError("FusionSolar login OK but no xsrf-token in response")
        session.headers["xsrf-token"] = token
        _session = session
        _token_expires_at = time.time() + 1200
        return session


def _request(cfg: dict[str, Any], endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    base = _api_base(cfg)
    for attempt in range(3):
        session = _login_session(cfg)
        response = session.post(f"{base}/thirdData/{endpoint}", json=payload, timeout=60)
        response.raise_for_status()
        body = response.json()
        if body.get("success"):
            return body
        # Huawei sometimes returns failCode 407 with usable data payload.
        if body.get("failCode") == 407 and body.get("data"):
            return {
                **body,
                "success": True,
                "message": body.get("message") or "rate-limited-with-data",
            }
        if body.get("failCode") in (305, 306, 307):
            global _token_expires_at
            _token_expires_at = 0
            continue
        if body.get("failCode") == 407 and attempt < 2:
            time.sleep(5 * (attempt + 1))
            continue
        raise FusionSolarApiError(body)
    raise FusionSolarApiError({"failCode": 407, "message": "ACCESS_FREQUENCY_IS_TOO_HIGH"})


def probe_api_access(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Login + station list — used for dashboard status."""
    if cfg is None:
        cfg = load_fusion_solar_config("marin")
    user, secret, station = _credentials(cfg)
    if not user or not secret:
        return {"login_ok": False, "error": "Identifiants API incomplets"}

    try:
        _login_session(cfg)
    except FusionSolarApiError as exc:
        return {"login_ok": False, "error": str(exc), "fail_code": exc.fail_code}
    except requests.RequestException as exc:
        return {"login_ok": False, "error": str(exc)}

    try:
        body = _request(cfg, "getStationList", {"pageNo": 1, "pageSize": 100})
    except FusionSolarApiError as exc:
        if exc.fail_code == 20056:
            return {
                "login_ok": True,
                "stations_ok": False,
                "fail_code": 20056,
                "error": str(exc),
                "station_code": station,
            }
        return {"login_ok": True, "stations_ok": False, "error": str(exc), "fail_code": exc.fail_code}
    except requests.RequestException as exc:
        return {"login_ok": True, "stations_ok": False, "error": str(exc)}

    stations = body.get("data") or []
    codes = [
        (s.get("stationCode") or s.get("plantCode") or "").strip()
        for s in stations
        if isinstance(s, dict)
    ]
    return {
        "login_ok": True,
        "stations_ok": True,
        "station_count": len(stations),
        "station_codes": codes,
        "station_code": station,
    }


def fusion_solar_accounts_status(*, live_probe: bool = False) -> dict[str, dict[str, Any]]:
    """Roof-level FusionSolar config; live API probe is optional (to avoid 407 spam)."""
    roofs = ("marin", "midi")
    out: dict[str, dict[str, Any]] = {}
    probe: dict[str, Any] | None = None

    try:
        data = _read_secrets_file()
    except FileNotFoundError as exc:
        return {r: {"configured": False, "error": str(exc)} for r in roofs}

    block = data.get("fusion_solar")
    if not isinstance(block, dict):
        return {
            r: {"configured": False, "error": "Bloc fusion_solar absent dans secrets.json"}
            for r in roofs
        }

    for roof_id in roofs:
        roof = block.get(roof_id)
        if not isinstance(roof, dict):
            out[roof_id] = {"configured": False, "error": f"fusion_solar.{roof_id} absent"}
            continue

        user, secret, station = _credentials(roof)
        if not (user and secret and station):
            out[roof_id] = {
                "configured": False,
                "error": f"Compléter fusion_solar.{roof_id} (API user + station)",
            }
            continue

        entry: dict[str, Any] = {
            "configured": True,
            "station_code": station,
            "login_ok": None,
            "status": "configured",
        }
        if live_probe:
            if probe is None:
                global _probe_cache, _probe_at
                if _probe_cache is not None and time.time() - _probe_at < 300:
                    probe = _probe_cache
                else:
                    probe = probe_api_access(roof)
                    _probe_cache = probe
                    _probe_at = time.time()
            entry["login_ok"] = probe.get("login_ok")
            if probe.get("login_ok") and probe.get("stations_ok"):
                entry["status"] = "ok"
                entry["message"] = f"API OK ({probe.get('station_count', 0)} centrale(s))"
            elif probe.get("login_ok"):
                entry["status"] = "auth_pending"
                entry["error"] = probe.get("error")
                entry["fail_code"] = probe.get("fail_code")
            elif probe.get("fail_code") == 407:
                entry["status"] = "rate_limited"
                entry["error"] = probe.get("error")
            else:
                entry["status"] = "login_failed"
                entry["error"] = probe.get("error")
                entry["fail_code"] = probe.get("fail_code")
        out[roof_id] = entry

    return out


def _day_kwh_from_kpi_point(point: dict[str, Any]) -> float:
    data_map = point.get("dataItemMap") or {}
    for key in ("day_power", "day_cap", "inverter_power", "product_power"):
        raw = data_map.get(key)
        if raw is not None:
            try:
                return float(raw)
            except (TypeError, ValueError):
                continue
    return 0.0


def _month_kwh_from_kpi_point(point: dict[str, Any]) -> float:
    data_map = point.get("dataItemMap") or {}
    for key in ("month_power", "month_cap", "product_power", "inverter_power"):
        raw = data_map.get(key)
        if raw is not None:
            try:
                return float(raw)
            except (TypeError, ValueError):
                continue
    return 0.0


def _fetch_daily_production_api(
    roof_id: str,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    cfg = load_fusion_solar_config(roof_id)
    _, _, station_code = _credentials(cfg)
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    if start > end:
        return {"unit": "kWh", "points": [], "total": 0, "source": "fusion_solar"}

    points: list[dict[str, Any]] = []
    day = start.replace(day=1)
    month_idx = 0
    while day <= end:
        if month_idx:
            time.sleep(2.5)
        month_idx += 1
        collect_ms = int(
            datetime(day.year, day.month, day.day, tzinfo=TZ_LUX).timestamp() * 1000
        )
        body = _request(
            cfg,
            "getKpiStationDay",
            {"stationCodes": station_code, "collectTime": collect_ms},
        )
        for item in body.get("data") or []:
            if not isinstance(item, dict):
                continue
            collect_time = item.get("collectTime")
            if not collect_time:
                continue
            day_iso = datetime.fromtimestamp(collect_time / 1000, tz=TZ_LUX).date().isoformat()
            if day_iso < start.isoformat() or day_iso > end.isoformat():
                continue
            kwh = _day_kwh_from_kpi_point(item)
            points.append(
                {
                    "date": day_iso,
                    "bucket_start": day_iso,
                    "value": kwh,
                }
            )
        if day.month == 12:
            day = day.replace(year=day.year + 1, month=1)
        else:
            day = day.replace(month=day.month + 1)

    points.sort(key=lambda p: p["bucket_start"])
    by_day = {p["bucket_start"]: p for p in points}
    points = list(by_day.values())
    total = sum(p["value"] for p in points)
    return {
        "unit": "kWh",
        "points": points,
        "total": total,
        "source": "fusion_solar",
        "station_code": station_code,
    }


def _fetch_monthly_production_api(
    roof_id: str,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    cfg = load_fusion_solar_config(roof_id)
    _, _, station_code = _credentials(cfg)
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    if start > end:
        return {"unit": "kWh", "points": [], "total": 0, "source": "fusion_solar"}

    points: list[dict[str, Any]] = []
    for idx, year in enumerate(range(start.year, end.year + 1)):
        if idx:
            time.sleep(4)
        collect_ms = int(datetime(year, 1, 1, tzinfo=TZ_LUX).timestamp() * 1000)
        body = _request(
            cfg,
            "getKpiStationMonth",
            {"stationCodes": station_code, "collectTime": collect_ms},
        )
        for item in body.get("data") or []:
            if not isinstance(item, dict):
                continue
            collect_time = item.get("collectTime")
            if not collect_time:
                continue
            dt = datetime.fromtimestamp(collect_time / 1000, tz=TZ_LUX)
            month_start = date(dt.year, dt.month, 1).isoformat()
            if month_start[:7] < start.isoformat()[:7] or month_start[:7] > end.isoformat()[:7]:
                continue
            kwh = _month_kwh_from_kpi_point(item)
            points.append({"date": month_start[:7], "bucket_start": month_start, "value": kwh})

    points.sort(key=lambda p: p["bucket_start"])
    by_month = {p["bucket_start"]: p for p in points}
    points = list(by_month.values())
    return {
        "unit": "kWh",
        "points": points,
        "total": sum(float(p.get("value") or 0) for p in points),
        "source": "fusion_solar",
        "station_code": station_code,
    }


def _monthly_result(
    points: list[dict[str, Any]],
    source: str,
    *,
    station_code: str | None = None,
) -> dict[str, Any]:
    return {
        "unit": "kWh",
        "points": points,
        "total": sum(float(p.get("value") or 0) for p in points),
        "source": source,
        **({"station_code": station_code} if station_code else {}),
    }


def fetch_monthly_production(roof_id: str, start_date: str, end_date: str) -> dict[str, Any]:
    cache_key = (roof_id, start_date[:10], end_date[:10])
    cached_win = _monthly_window_cache.get(cache_key)
    if cached_win and (time.time() - cached_win[0]) < 600:
        return cached_win[1]

    if not _fusion_solar_cache_enabled():
        out = _fetch_monthly_production_api(roof_id, start_date, end_date)
        _monthly_window_cache[cache_key] = (time.time(), out)
        return out
    from altena.cache_store import (
        SOURCE_FUSION_SOLAR,
        expected_bucket_starts,
        get_buckets,
        missing_bucket_starts,
        points_from_cache,
        put_buckets,
    )

    expected = expected_bucket_starts(start_date, end_date, "Month")
    cached = get_buckets(SOURCE_FUSION_SOLAR, roof_id, "Month", start_date, end_date)
    need_api = missing_bucket_starts(cached, expected, "Month")
    station_code: str | None = None
    api_points: list[dict[str, Any]] = []

    if need_api:
        try:
            fresh = _fetch_monthly_production_api(roof_id, start_date, end_date)
            station_code = fresh.get("station_code")
            api_points = fresh.get("points") or []
            if api_points:
                put_buckets(SOURCE_FUSION_SOLAR, roof_id, "Month", api_points)
        except Exception:
            pass

    cached = get_buckets(SOURCE_FUSION_SOLAR, roof_id, "Month", start_date, end_date)
    points = points_from_cache(cached, expected, "Month")
    if not points:
        if api_points:
            out = _monthly_result(api_points, "fusion_solar", station_code=station_code)
        else:
            out = _monthly_result([], "fusion_solar", station_code=station_code)
        _monthly_window_cache[cache_key] = (time.time(), out)
        return out

    still_missing = missing_bucket_starts(cached, expected, "Month")
    if api_points and not still_missing:
        source = "fusion_solar"
    elif api_points:
        source = "fusion_solar_partial"
    else:
        source = "fusion_solar_cache"
    out = _monthly_result(points, source, station_code=station_code)
    out["missing_months"] = still_missing
    _monthly_window_cache[cache_key] = (time.time(), out)
    return out


def sync_fusion_solar_monthly_backfill(
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """Fill missing FusionSolar monthly buckets in cache.db (rate-limit friendly)."""
    if not fusion_solar_any_configured() or not _fusion_solar_cache_enabled():
        return {"skipped": True, "reason": "fusion_solar_disabled"}

    start = (start_date or online_from())[:10]
    end = (end_date or date.today().isoformat())[:10]
    results: dict[str, Any] = {"start": start, "end": end, "roofs": {}}

    for idx, roof_id in enumerate(ROOF_TO_SERIES):
        if idx:
            time.sleep(4)
        try:
            out = fetch_monthly_production(roof_id, start, end)
            results["roofs"][roof_id] = {
                "months": len(out.get("points") or []),
                "total": int(out.get("total") or 0),
                "source": out.get("source"),
                "missing_months": out.get("missing_months") or [],
            }
        except Exception as exc:
            results["roofs"][roof_id] = {"error": str(exc)}

    return results


def _fusion_solar_cache_enabled() -> bool:
    try:
        return bool(load_config().get("cache_enabled", True))
    except OSError:
        return True


def fetch_daily_production(roof_id: str, start_date: str, end_date: str) -> dict[str, Any]:
    if not _fusion_solar_cache_enabled():
        return _fetch_daily_production_api(roof_id, start_date, end_date)
    from altena.cache_store import fetch_daily_fusion_solar_with_cache

    return fetch_daily_fusion_solar_with_cache(
        roof_id,
        start_date,
        end_date,
        lambda s, e: _fetch_daily_production_api(roof_id, s, e),
    )


def _roof_for_series_id(series_id: str) -> str | None:
    for roof_id, sid in ROOF_TO_SERIES.items():
        if sid == series_id:
            return roof_id
    return None


def _distribute_month_total(
    month_points: list[dict[str, Any]],
    month_total_kwh: float,
) -> list[dict[str, Any]]:
    """Spread one monthly total over day/week buckets using Leneda profile."""
    n = len(month_points)
    if n == 0:
        return []
    base_vals = [float(p.get("value") or 0) for p in month_points]
    base_sum = sum(base_vals)
    if base_sum <= 0:
        share = month_total_kwh / n
        values = [share for _ in month_points]
    else:
        values = [(month_total_kwh * v / base_sum) for v in base_vals]
    out: list[dict[str, Any]] = []
    for p, value in zip(month_points, values):
        out.append(
            {
                **p,
                "value": value,
                "empty": value == 0,
                "fusion_solar": True,
            }
        )
    return out


def overlay_fusion_solar_on_series(
    series: list[dict[str, Any]],
    chart_aggregation: str,
    start_date: str,
    end_date: str,
) -> tuple[list[dict[str, Any]], bool]:
    """Replace Leneda production totals with FusionSolar inverter data when available."""
    if not fusion_solar_any_configured() or chart_aggregation not in ("Month", "Day", "Week"):
        return series, False

    online = online_from()
    effective_start = start_date if start_date >= online else online
    if effective_start > end_date:
        return series, False

    timeline: list[str] = []
    timeline_starts: dict[str, str] = {}
    for entry in series:
        for p in entry.get("points") or []:
            label = p.get("date")
            if not label or label in timeline_starts:
                continue
            timeline.append(label)
            timeline_starts[label] = p.get("bucket_start") or label
    if not timeline:
        return series, False

    used = False
    out: list[dict[str, Any]] = []
    for entry in series:
        roof_id = _roof_for_series_id(entry.get("id", ""))
        if not roof_id or not fusion_solar_configured(roof_id):
            out.append(entry)
            continue

        try:
            monthly_guard = fetch_monthly_production(roof_id, effective_start, end_date)
            if float(monthly_guard.get("total") or 0) <= 0:
                out.append(entry)
                continue
            if chart_aggregation == "Month":
                fs = fetch_production_series(
                    roof_id,
                    effective_start,
                    end_date,
                    chart_aggregation,
                    timeline,
                    timeline_starts,
                )
                by_label = {p["date"]: p for p in fs.get("points") or []}
                new_points: list[dict[str, Any]] = []
                for p in entry.get("points") or []:
                    bucket = (p.get("bucket_start") or p.get("date") or "")[:10]
                    if bucket < online:
                        new_points.append(p)
                        continue
                    fs_p = by_label.get(p["date"])
                    if fs_p is None or fs_p.get("empty"):
                        new_points.append(p)
                        continue
                    value = float(fs_p.get("value") or 0)
                    new_points.append(
                        {
                            **p,
                            "value": value,
                            "empty": value == 0,
                            "fusion_solar": True,
                        }
                    )
            else:
                monthly = monthly_guard
                monthly_by_ym = {
                    (p.get("bucket_start") or "")[:7]: float(p.get("value") or 0)
                    for p in (monthly.get("points") or [])
                }
                groups: dict[str, list[dict[str, Any]]] = {}
                passthrough: list[dict[str, Any]] = []
                for p in entry.get("points") or []:
                    bucket = (p.get("bucket_start") or p.get("date") or "")[:10]
                    if bucket < online:
                        passthrough.append(p)
                        continue
                    ym = bucket[:7]
                    groups.setdefault(ym, []).append(p)
                new_points = list(passthrough)
                for ym in sorted(groups):
                    month_points = groups[ym]
                    month_total = monthly_by_ym.get(ym)
                    if month_total is None:
                        new_points.extend(month_points)
                    else:
                        new_points.extend(_distribute_month_total(month_points, month_total))
                new_points.sort(key=lambda p: p.get("bucket_start") or p.get("date") or "")
        except Exception:
            out.append(entry)
            continue

        row_used = any(p.get("fusion_solar") for p in new_points)
        total = int(round(sum(float(p.get("value") or 0) for p in new_points)))
        label = entry.get("label", "")
        if row_used and "FusionSolar" not in label:
            label = label.replace("production totale", "production (FusionSolar)")
            label = label.replace("Production totale", "Production (FusionSolar)")

        used = used or row_used
        out.append(
            {
                **entry,
                "points": new_points,
                "total": total,
                "fusion_solar_overlay": row_used,
                "label": label,
                "source": "fusion_solar" if row_used else entry.get("source"),
            }
        )
    return out, used


def fetch_production_series(
    roof_id: str,
    start_date: str,
    end_date: str,
    chart_aggregation: str,
    timeline: list[str],
    timeline_starts: dict[str, str],
) -> dict[str, Any]:
    if chart_aggregation == "Month":
        monthly = fetch_monthly_production(roof_id, start_date, end_date)
        by_month = {p["date"]: p for p in monthly["points"]}
        aligned = []
        for label in timeline:
            month_key = label[:7]
            p = by_month.get(month_key)
            aligned.append(
                {
                    "date": label,
                    "bucket_start": timeline_starts.get(label) or f"{month_key}-01",
                    "value": float(p.get("value") or 0) if p else 0.0,
                    "empty": p is None,
                }
            )
        total = sum(float(p.get("value") or 0) for p in aligned)
        source = monthly.get("source")
        station_code = monthly.get("station_code")
    else:
        daily = fetch_daily_production(roof_id, start_date, end_date)
        aligned = aggregate_production_to_timeline(
            daily["points"],
            timeline,
            timeline_starts,
            chart_aggregation,
        )
        total = daily["total"]
        source = daily["source"]
        station_code = daily.get("station_code")
    roof_label = roof_id.capitalize()
    return {
        "id": f"fusion_solar_{roof_id}",
        "label": f"Production PV — {roof_label} (FusionSolar)",
        "group": "fusion_solar",
        "color": "#ea580c" if roof_id == "marin" else "#f97316",
        "unit": "kWh",
        "points": aligned,
        "total": total,
        "source": source,
        "station_code": station_code,
        "roof_id": roof_id,
    }
