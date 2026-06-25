"""Leneda metering API client — one login per roof when configured."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

from altena.paths import CONFIG_PATH, SECRETS_PATH

TZ_LUX = ZoneInfo("Europe/Luxembourg")
API_BASE = "https://api.leneda.eu/api/metering-points"


def load_config() -> dict[str, Any]:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _read_secrets_file() -> dict[str, Any]:
    if not os.path.isfile(SECRETS_PATH):
        raise FileNotFoundError(
            f"Missing {SECRETS_PATH}. Copy secrets.example.json and add credentials."
        )
    with open(SECRETS_PATH, encoding="utf-8") as f:
        return json.load(f)


def _credential_block(data: dict[str, Any], account: str) -> dict[str, Any]:
    """Resolve secrets for account id (marin, midi, …)."""
    leneda = data.get("leneda")
    if isinstance(leneda, dict):
        block = leneda.get(account)
        if isinstance(block, dict):
            return block
    if account in ("default", ""):
        return data
    return {}


def load_secrets(account: str = "default") -> tuple[str, str]:
    data = _read_secrets_file()
    block = _credential_block(data, account)
    energy_id = (block.get("energy_id") or "").strip()
    api_key = (block.get("api_key") or "").strip()
    if not energy_id or not api_key:
        if account != "default":
            raise ValueError(
                f"secrets.json: leneda.{account}.energy_id and api_key must be non-empty"
            )
        raise ValueError("secrets.json: energy_id and api_key must be non-empty")
    return energy_id, api_key


def required_leneda_accounts() -> list[str]:
    mapping = load_config().get("pod_accounts") or {}
    return sorted({str(v).strip() for v in mapping.values() if str(v).strip()})


def leneda_account_for_pod_key(pod_key: str) -> str:
    mapping = load_config().get("pod_accounts") or {}
    account = (mapping.get(pod_key) or pod_key or "default").strip()
    return account or "default"


def leneda_accounts_status() -> dict[str, dict[str, Any]]:
    """Which roof logins are present in secrets.json (no API call)."""
    try:
        data = _read_secrets_file()
    except FileNotFoundError as exc:
        return {"_error": {"configured": False, "error": str(exc)}}

    out: dict[str, dict[str, Any]] = {}
    for account in required_leneda_accounts() or ["marin", "midi"]:
        block = _credential_block(data, account)
        energy_id = (block.get("energy_id") or "").strip()
        api_key = (block.get("api_key") or "").strip()
        if energy_id and api_key:
            out[account] = {"configured": True, "energy_id": energy_id}
        else:
            out[account] = {
                "configured": False,
                "error": f"Compléter secrets.json → leneda.{account}",
            }
    return out


def _headers(account: str = "default") -> dict[str, str]:
    energy_id, api_key = load_secrets(account)
    return {"X-Energy-Id": energy_id, "X-API-Key": api_key}


def _headers_for_pod_key(pod_key: str) -> dict[str, str]:
    return _headers(leneda_account_for_pod_key(pod_key))


AGGREGATION_LEVELS = frozenset({"Day", "Week", "Month", "Infinite"})


def _kwh(value: float) -> int:
    return int(round(value))


def _reconcile_parts_to_total(total: int, parts: list[int]) -> list[int]:
    """After rounding each series, nudge the largest part so components sum to total."""
    if not parts:
        return parts
    adjusted = list(parts)
    diff = total - sum(adjusted)
    if diff == 0:
        return adjusted
    idx = max(range(len(adjusted)), key=lambda i: adjusted[i])
    adjusted[idx] += diff
    return adjusted


def _parse_api_instant(iso_ts: str) -> datetime | None:
    if not iso_ts:
        return None
    return datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).astimezone(TZ_LUX)


def _effective_start(user_start: str, spec: dict[str, Any]) -> str:
    """Earliest date to request for totals (logical go-live, but not after period end)."""
    eff = (spec.get("effective_from") or "").strip()
    if eff and eff > user_start:
        return eff
    return user_start


def _api_range(
    user_start: str, end_date: str, spec: dict[str, Any]
) -> tuple[str, str] | None:
    """API window for a series, or None if the period ends before go-live."""
    start = _effective_start(user_start, spec)
    if start > end_date:
        return None
    return start, end_date


def _empty_series_data() -> dict[str, Any]:
    return {"unit": "kWh", "points": [], "total": 0}


def _bucket_start_date(item: dict[str, Any]) -> str:
    start_dt = _parse_api_instant(item.get("startedAt") or "")
    return start_dt.date().isoformat() if start_dt else ""


def _bucket_label(item: dict[str, Any], aggregation_level: str) -> str:
    """Label buckets in Luxembourg local time (Leneda timestamps are UTC)."""
    start_dt = _parse_api_instant(item.get("startedAt") or "")
    if not start_dt:
        return ""
    if aggregation_level == "Month":
        return f"{start_dt.year}-{start_dt.month:02d}"
    end_dt = _parse_api_instant(item.get("endedAt") or "")
    if aggregation_level == "Week" and end_dt:
        # Include year — DD/MM alone collides across years and sorts incorrectly
        return f"{start_dt.strftime('%d/%m/%y')}–{end_dt.strftime('%d/%m/%y')}"
    return start_dt.date().isoformat()


def _parse_aggregated_payload(payload: dict[str, Any], aggregation_level: str) -> dict[str, Any]:
    unit = payload.get("unit") or "kWh"
    points = []
    for item in payload.get("aggregatedTimeSeries") or []:
        label = _bucket_label(item, aggregation_level)
        bucket_start = _bucket_start_date(item)
        if not label or not bucket_start:
            continue
        points.append(
            {
                "date": label,
                "bucket_start": bucket_start,
                "value": _kwh(float(item.get("value") or 0)),
            }
        )
    points.sort(key=lambda p: p["bucket_start"])
    if aggregation_level == "Infinite":
        total = _kwh(float((payload.get("aggregatedTimeSeries") or [{}])[0].get("value") or 0))
    else:
        total = _kwh(sum(p["value"] for p in points))
    return {"unit": unit, "points": points, "total": total}


def fetch_obis_series(
    metering_point: str,
    obis_code: str,
    start_date: str,
    end_date: str,
    aggregation_level: str = "Day",
    *,
    pod_key: str | None = None,
    leneda_account: str | None = None,
) -> dict[str, Any]:
    url = f"{API_BASE}/{metering_point}/time-series/aggregated"
    params = {
        "obisCode": obis_code,
        "startDate": start_date,
        "endDate": end_date,
        "aggregationLevel": aggregation_level,
        "transformationMode": "Accumulation",
    }
    account = leneda_account or (leneda_account_for_pod_key(pod_key) if pod_key else "default")
    response = requests.get(
        url, headers=_headers(account), params=params, timeout=60
    )
    response.raise_for_status()
    return _parse_aggregated_payload(response.json(), aggregation_level)


def fetch_context_series(
    metering_point: str,
    time_series_context: str,
    start_date: str,
    end_date: str,
    aggregation_level: str = "Day",
    *,
    pod_key: str | None = None,
    leneda_account: str | None = None,
) -> dict[str, Any]:
    url = f"{API_BASE}/{metering_point}/time-series/by-context/aggregated"
    params = {
        "timeSeriesContext": time_series_context,
        "startDate": start_date,
        "endDate": end_date,
        "aggregationLevel": aggregation_level,
        "transformationMode": "Accumulation",
    }
    account = leneda_account or (leneda_account_for_pod_key(pod_key) if pod_key else "default")
    response = requests.get(
        url, headers=_headers(account), params=params, timeout=60
    )
    response.raise_for_status()
    return _parse_aggregated_payload(response.json(), aggregation_level)


def _fetch_series_for_spec_api(
    pod_code: str,
    spec: dict[str, Any],
    start_date: str,
    end_date: str,
    aggregation_level: str,
) -> dict[str, Any]:
    pod_key = spec.get("pod") or ""
    if spec.get("source") == "by_context":
        return fetch_context_series(
            pod_code,
            spec["time_series_context"],
            start_date,
            end_date,
            aggregation_level,
            pod_key=pod_key,
        )
    return fetch_obis_series(
        pod_code,
        spec["obis"],
        start_date,
        end_date,
        aggregation_level,
        pod_key=pod_key,
    )


def _cache_enabled() -> bool:
    try:
        return bool(load_config().get("cache_enabled", True))
    except OSError:
        return True


def fetch_series_for_spec(
    pod_code: str,
    spec: dict[str, Any],
    start_date: str,
    end_date: str,
    aggregation_level: str,
) -> dict[str, Any]:
    if not _cache_enabled():
        return _fetch_series_for_spec_api(
            pod_code, spec, start_date, end_date, aggregation_level
        )
    from altena.cache_store import SOURCE_LENEDA, fetch_series_with_cache

    series_id = spec["id"]

    def fetch_fn(s: str, e: str, agg: str) -> dict[str, Any]:
        return _fetch_series_for_spec_api(pod_code, spec, s, e, agg)

    return fetch_series_with_cache(
        SOURCE_LENEDA,
        series_id,
        aggregation_level,
        start_date,
        end_date,
        fetch_fn,
    )


def _is_before_go_live(
    label: str,
    bucket_start: str,
    effective_from: str,
    aggregation_level: str,
) -> bool:
    if aggregation_level == "Month":
        return label < effective_from[:7]
    return bool(bucket_start) and bucket_start < effective_from


def align_points_to_timeline(
    points: list[dict[str, Any]],
    timeline: list[str],
    timeline_starts: dict[str, str],
    effective_from: str | None,
    aggregation_level: str,
) -> list[dict[str, Any]]:
    """Same x-axis buckets on every chart; 0 kWh where group did not exist yet."""
    by_date = {p["date"]: p for p in points}
    eff = (effective_from or "").strip()
    aligned: list[dict[str, Any]] = []

    for label in timeline:
        bucket_start = timeline_starts.get(label) or (f"{label}-01" if aggregation_level == "Month" else label)
        before_live = eff and _is_before_go_live(label, bucket_start, eff, aggregation_level)
        if label in by_date:
            p = by_date[label]
            point_start = p.get("bucket_start") or bucket_start
            before_point = eff and _is_before_go_live(label, point_start, eff, aggregation_level)
            if not before_live and not before_point:
                aligned.append({**p, "empty": False, "value": _kwh(p["value"])})
                continue
        else:
            aligned.append(
                {
                    "date": label,
                    "bucket_start": bucket_start,
                    "value": 0,
                    "empty": True,
                }
            )
    return aligned


def _fill_derived_supplier_remainder(
    results: list[dict[str, Any]],
    api_dates_by_series: dict[str, set[str]],
) -> bool:
    """
    Leneda often omits 1-65:1.29.9 for months before CEL even though grid supply existed.
    Estimate: reste fournisseur ≈ consommation active − couvert CEL (per bucket).
    """
    by_id = {r["id"]: r for r in results}
    active = by_id.get("cons_active")
    cel = by_id.get("cons_cel")
    grid = by_id.get("cons_grid")
    if not (active and cel and grid):
        return False

    api_months = api_dates_by_series.get("cons_grid", set())
    active_by = {p["date"]: p["value"] for p in active["points"]}
    cel_by = {p["date"]: p["value"] for p in cel["points"]}
    derived_any = False

    for point in grid["points"]:
        label = point["date"]
        if label in api_months:
            continue
        consumption = active_by.get(label, 0)
        covered = cel_by.get(label, 0)
        if consumption > 0 or covered > 0:
            point["value"] = _kwh(max(0, consumption - covered))
            point["derived"] = True
            point["empty"] = False
            derived_any = True

    if derived_any:
        grid["total"] = _kwh(sum(p["value"] for p in grid["points"]))
        grid["derived_fill"] = True
        grid["derived_note"] = (
            "Certains mois/semaines sans donnée 1.29.9 chez Leneda : barres estimées "
            "(consommation totale − CEL)."
        )
    return derived_any


def _roof_consumption_flow_ids() -> dict[str, dict[str, str]]:
    """Per-roof series ids for onemeter consumption derivation."""
    return {
        "marin": {
            "active": "cons_marin_active",
            "cel": "cons_marin_cel",
            "grid": "cons_marin_grid",
            "prod": "prod_marin_active",
            "shared_l2": "prod_marin_shared_l2",
            "shared_cel": "prod_marin_shared_cel",
            "export": "prod_marin_market",
        },
        "midi": {
            "active": "cons_midi_active",
            "cel": "cons_midi_cel",
            "grid": "cons_midi_grid",
            "prod": "prod_midi_active",
            "shared_l2": "prod_midi_shared_l2",
            "shared_cel": "prod_midi_shared_cel",
            "export": "prod_midi_market",
        },
    }


def _fill_derived_onemeter_consumption(
    results: list[dict[str, Any]],
    chart_aggregation: str,
) -> bool:
    """
    When Leneda returns no 1-1:1.29.0 on the common PV POD, estimate monthly consumption:
    consommation ≈ production (brute) − injection marché (= autoconsommation + partages vers appartements).
    """
    try:
        config = load_config()
    except OSError:
        return False
    if not config.get("onemeter_derive_consumption", True):
        return False
    if chart_aggregation != "Month":
        return False

    by_id = {r["id"]: r for r in results}
    derived_any = False

    for _roof, ids in _roof_consumption_flow_ids().items():
        active = by_id.get(ids["active"])
        prod = by_id.get(ids["prod"])
        export_s = by_id.get(ids["export"])
        if not (active and prod and export_s):
            continue

        prod_by = {p["date"]: int(p.get("value") or 0) for p in prod.get("points") or []}
        export_by = {p["date"]: int(p.get("value") or 0) for p in export_s.get("points") or []}
        cel_by = {
            p["date"]: int(p.get("value") or 0)
            for p in (by_id.get(ids["cel"]) or {}).get("points") or []
        }
        grid_by = {
            p["date"]: int(p.get("value") or 0)
            for p in (by_id.get(ids["grid"]) or {}).get("points") or []
        }

        for point in active.get("points") or []:
            label = point["date"]
            if int(point.get("value") or 0) > 0:
                continue
            production = prod_by.get(label, 0)
            if production <= 0:
                continue
            injection = export_by.get(label, 0)
            derived_total = _kwh(max(0, production - injection))
            cel = cel_by.get(label, 0)
            grid = grid_by.get(label, 0)
            if cel > 0 or grid > 0:
                derived_total = _kwh(max(derived_total, cel + grid))
            point["value"] = derived_total
            point["derived"] = True
            point["empty"] = False
            derived_any = True

        if derived_any:
            active["total"] = _kwh(sum(int(p.get("value") or 0) for p in active["points"]))
            active["derived_fill"] = True
            active["derived_note"] = (
                "Consommation estimée (compteur onemeter) : production − injection marché "
                "tant que Leneda ne renvoie pas 1-1:1.29.0 sur le POD commun."
            )

    return derived_any


def _period_total(
    pod_code: str,
    user_start: str,
    end_date: str,
    spec: dict[str, Any],
) -> int:
    window = _api_range(user_start, end_date, spec)
    if not window:
        return 0
    start, end = window
    series_id = spec["id"]
    if _cache_enabled():
        from altena.cache_store import SOURCE_LENEDA, get_period_total, put_period_total

        cached = get_period_total(SOURCE_LENEDA, series_id, start, end)
        if cached is not None:
            return cached
    data = _fetch_series_for_spec_api(pod_code, spec, start, end, "Infinite")
    total = data["total"]
    if _cache_enabled():
        from altena.cache_store import SOURCE_LENEDA, put_period_total

        put_period_total(SOURCE_LENEDA, series_id, start, end, total)
    return total


_TIMELINE_FALLBACK_IDS = ("prod_marin_active", "prod_midi_active", "cons_jacoby_cel")


def _timeline_from_spec(
    spec: dict[str, Any],
    pods: dict[str, str],
    start_date: str,
    end_date: str,
    chart_aggregation: str,
) -> tuple[list[str], dict[str, str]]:
    pod_code = pods.get(spec.get("pod") or "")
    if not pod_code:
        return [], {}
    chart_window = _api_range(start_date, end_date, spec)
    if not chart_window:
        return [], {}
    chart_data = fetch_series_for_spec(
        pod_code, spec, chart_window[0], chart_window[1], chart_aggregation
    )
    pts = sorted(chart_data["points"], key=lambda p: p.get("bucket_start") or p["date"])
    if not pts:
        return [], {}
    return (
        [p["date"] for p in pts],
        {p["date"]: p.get("bucket_start") or p["date"] for p in pts},
    )


def _resolve_timeline(
    config: dict[str, Any],
    pods: dict[str, str],
    start_date: str,
    end_date: str,
    chart_aggregation: str,
) -> tuple[list[str], dict[str, str]]:
    candidates: list[str] = []
    master_id = (config.get("timeline_master_id") or "").strip()
    if master_id:
        candidates.append(master_id)
    candidates.extend(_TIMELINE_FALLBACK_IDS)
    series = config.get("series") or []
    seen: set[str] = set()
    for sid in candidates:
        if sid in seen:
            continue
        seen.add(sid)
        spec = next((s for s in series if s["id"] == sid), None)
        if not spec:
            continue
        timeline, timeline_starts = _timeline_from_spec(
            spec, pods, start_date, end_date, chart_aggregation
        )
        if timeline:
            return timeline, timeline_starts
    return [], {}


def _fetch_series_entry(
    spec: dict[str, Any],
    pods: dict[str, str],
    start_date: str,
    end_date: str,
    chart_aggregation: str,
    timeline: list[str],
    timeline_starts: dict[str, str],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, set[str]]:
    pod_key = spec["pod"]
    pod_code = pods.get(pod_key)
    if not pod_code:
        return None, {"id": spec["id"], "error": f"Unknown pod key: {pod_key}"}, set()
    try:
        chart_window = _api_range(start_date, end_date, spec)
        if chart_window:
            chart_data = fetch_series_for_spec(
                pod_code, spec, chart_window[0], chart_window[1], chart_aggregation
            )
        else:
            chart_data = _empty_series_data()
        api_dates = {p["date"] for p in chart_data["points"]}
        aligned = align_points_to_timeline(
            chart_data["points"],
            timeline,
            timeline_starts,
            spec.get("effective_from"),
            chart_aggregation,
        )
        period_total = _period_total(pod_code, start_date, end_date, spec)
        entry: dict[str, Any] = {
            "id": spec["id"],
            "label": spec["label"],
            "group": spec["group"],
            "obis": spec.get("obis") or spec.get("time_series_context", ""),
            "color": spec.get("color", "#64748b"),
            "unit": chart_data["unit"],
            "points": aligned,
            "total": period_total,
        }
        if spec.get("effective_from"):
            entry["effective_from"] = spec["effective_from"]
        if spec.get("note"):
            entry["note"] = spec["note"]
        if spec.get("source"):
            entry["source"] = spec["source"]
        if spec.get("roof_id"):
            entry["roof_id"] = spec["roof_id"]
        if spec.get("pending_leneda"):
            entry["pending_leneda"] = True
        return entry, None, api_dates
    except requests.HTTPError as exc:
        detail = ""
        if exc.response is not None:
            detail = exc.response.text[:200]
        account = leneda_account_for_pod_key(pod_key)
        return (
            None,
            {
                "id": spec["id"],
                "label": spec.get("label", spec["id"]),
                "leneda_account": account,
                "error": (
                    f"[{account}] HTTP "
                    f"{exc.response.status_code if exc.response else '?'}: {detail}"
                ),
            },
            set(),
        )
    except Exception as exc:
        return None, {"id": spec["id"], "label": spec.get("label"), "error": str(exc)}, set()


def fetch_all_series(
    start_date: str,
    end_date: str,
    chart_aggregation: str = "Day",
) -> dict[str, Any]:
    if chart_aggregation not in AGGREGATION_LEVELS - {"Infinite"}:
        raise ValueError(f"chart_aggregation must be Day, Week, or Month (got {chart_aggregation!r})")

    config = load_config()
    pods = config["pods"]
    series_specs = config.get("series") or []
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    api_dates_by_series: dict[str, set[str]] = {}
    timeline, timeline_starts = _resolve_timeline(
        config, pods, start_date, end_date, chart_aggregation
    )

    entries_by_id: dict[str, dict[str, Any]] = {}
    max_workers = min(12, max(4, len(series_specs)))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(
                _fetch_series_entry,
                spec,
                pods,
                start_date,
                end_date,
                chart_aggregation,
                timeline,
                timeline_starts,
            ): spec
            for spec in series_specs
        }
        for fut in as_completed(futures):
            spec = futures[fut]
            entry, err, api_dates = fut.result()
            if entry:
                entries_by_id[entry["id"]] = entry
                api_dates_by_series[entry["id"]] = api_dates
            if err:
                errors.append(err)

    if not timeline:
        for spec in series_specs:
            entry = entries_by_id.get(spec["id"])
            if entry and entry.get("points"):
                timeline = [p["date"] for p in entry["points"]]
                timeline_starts = {
                    p["date"]: p.get("bucket_start") or p["date"] for p in entry["points"]
                }
                break

    for spec in series_specs:
        entry = entries_by_id.get(spec["id"])
        if entry:
            results.append(entry)

    # Fallback only when grid still uses OBIS without by-context data
    derived_grid = False
    grid_spec = next((s for s in config.get("series") or [] if s["id"] == "cons_grid"), None)
    if grid_spec and grid_spec.get("source") != "by_context":
        derived_grid = _fill_derived_supplier_remainder(results, api_dates_by_series)

    prod_l2 = next((r for r in results if r["id"] == "prod_shared_l2"), None)
    prod_cel = next((r for r in results if r["id"] == "prod_shared_cel"), None)
    shared_sum = None
    if prod_l2 and prod_cel:
        shared_sum = _kwh(prod_l2["total"] + prod_cel["total"])

    fusion_solar_overlay = False
    manual_overlay = False
    fs_configured = False
    fs_enabled = bool((config.get("fusion_solar") or {}).get("enabled", False))
    if fs_enabled:
        try:
            from altena.fusion_solar_client import (
                fusion_solar_any_configured,
                overlay_fusion_solar_on_series,
            )

            fs_configured = fusion_solar_any_configured()
            if fs_configured:
                results, fusion_solar_overlay = overlay_fusion_solar_on_series(
                    results, chart_aggregation, start_date, end_date
                )
        except Exception:
            fusion_solar_overlay = False
            fs_configured = False

    if fs_enabled and not fusion_solar_overlay and fs_configured:
        errors.append(
            {
                "id": "fusion_solar_overlay",
                "label": "FusionSolar",
                "error": (
                    "Production FusionSolar indisponible pour cette période — "
                    "affichage Leneda en secours. "
                    "Relancez l’app ou exécutez scripts/sync/sync_stats_cache.py "
                    "pour compléter le cache."
                ),
            }
        )

    if not fusion_solar_overlay:
        try:
            from altena.manual_production import ensure_seeded, overlay_production_on_series

            ensure_seeded()
            results, manual_overlay = overlay_production_on_series(results, chart_aggregation)
        except Exception:
            manual_overlay = False

    derived_consumption = _fill_derived_onemeter_consumption(results, chart_aggregation)

    by_id = {r["id"]: r for r in results}
    summary_rows = config.get("summary_rows") or []
    totals_by_id = {
        sid: {
            "id": sid,
            "label": by_id[sid]["label"],
            "total": by_id[sid]["total"],
            "unit": by_id[sid]["unit"],
        }
        for sid in by_id
    }

    period_notes = [
        {"series_id": s["id"], "label": s["label"], "note": s.get("note", ""), "effective_from": s["effective_from"]}
        for s in config.get("series") or []
        if s.get("effective_from")
    ]
    if fusion_solar_overlay:
        fs = config.get("fusion_solar") or {}
        period_notes.append(
            {
                "series_id": "fusion_solar",
                "label": "Production onduleur (FusionSolar)",
                "note": fs.get(
                    "note",
                    "Production brute Huawei — remplace le total Leneda 1-1:2.29.0 sur la période.",
                ),
                "effective_from": fs.get("online_from", "2025-06-24"),
            }
        )
    elif manual_overlay:
        mp = config.get("manual_production") or {}
        period_notes.append(
            {
                "series_id": "manual_production",
                "label": "Production brute (saisie manuelle)",
                "note": mp.get(
                    "note",
                    "Juin 2025 ≈ 1 semaine en ligne — voir data/manual_production.json",
                ),
                "effective_from": mp.get("online_from", "2025-06-24"),
            }
        )

    # Harmonise parts only when the selected period starts after all sharing groups
    earliest_sharing = min(
        (s["effective_from"] for s in config.get("series") or [] if s.get("effective_from")),
        default=None,
    )
    reconcile_equations = not earliest_sharing or start_date >= earliest_sharing

    if reconcile_equations:
        for row in summary_rows:
            ids = row.get("ids") or []
            if len(ids) < 2:
                continue
            total_id, part_ids = ids[0], ids[1:]
            if total_id not in totals_by_id:
                continue
            part_totals = [totals_by_id[pid]["total"] for pid in part_ids if pid in totals_by_id]
            if len(part_totals) != len(part_ids):
                continue
            total = int(totals_by_id[total_id]["total"] or 0)
            # Admin CEL : 1-1:1.29.0 souvent absent — ne pas écraser le partage CEL à 0.
            if total == 0 and sum(part_totals) > 0:
                continue
            for pid, value in zip(
                part_ids, _reconcile_parts_to_total(total, part_totals)
            ):
                totals_by_id[pid]["total"] = value

    return {
        "site_label": config.get("site_label", ""),
        "start_date": start_date,
        "end_date": end_date,
        "chart_aggregation": chart_aggregation,
        "timeline": timeline,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "leneda_accounts": leneda_accounts_status(),
        "series": results,
        "errors": errors,
        "summary": {
            "production_shared_l2_plus_cel_kwh": shared_sum,
            "rows": summary_rows,
            "totals_by_id": totals_by_id,
            "reconcile_equations": reconcile_equations,
            "period_notes": period_notes,
            "derived_grid_fill": derived_grid,
            "derived_consumption_fill": derived_consumption,
            "manual_production_overlay": manual_overlay,
            "fusion_solar_overlay": fusion_solar_overlay,
        },
    }
