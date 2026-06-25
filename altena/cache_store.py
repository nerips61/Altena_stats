"""SQLite cache for Leneda / Enphase time-series buckets."""

from __future__ import annotations

import os
import sqlite3
import threading
from calendar import monthrange
from datetime import date, datetime, timedelta
from typing import Any, Callable

from altena.paths import CACHE_PATH

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SOURCE_LENEDA = "leneda"
SOURCE_ENPHASE = "enphase"
SOURCE_FUSION_SOLAR = "fusion_solar"
ENPHASE_SERIES_ID = "production_daily"


def cache_path() -> str:
    return CACHE_PATH


def _today() -> date:
    return date.today()


def _first_day_current_month() -> date:
    t = _today()
    return t.replace(day=1)


def _last_day_previous_month() -> date:
    first = _first_day_current_month()
    return first - timedelta(days=1)


def bucket_needs_refresh(bucket_start: str, aggregation: str) -> bool:
    """Past closed months stay cached; current month and today always refresh."""
    if not bucket_start:
        return True
    bs = date.fromisoformat(bucket_start[:10])
    first = _first_day_current_month()
    if aggregation == "Month":
        return bs >= first
    if aggregation == "Week":
        return bs + timedelta(days=6) >= first
    if aggregation == "Infinite":
        return True
    if bs >= _today():
        return True
    return bs >= first


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(CACHE_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn


def init_db() -> None:
    with _lock:
        conn = _connect()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS buckets (
                source TEXT NOT NULL,
                series_id TEXT NOT NULL,
                bucket_start TEXT NOT NULL,
                aggregation TEXT NOT NULL,
                kwh INTEGER NOT NULL,
                fetched_at TEXT NOT NULL,
                PRIMARY KEY (source, series_id, bucket_start, aggregation)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_buckets_lookup
            ON buckets (source, series_id, aggregation, bucket_start)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS period_totals (
                source TEXT NOT NULL,
                series_id TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                kwh INTEGER NOT NULL,
                fetched_at TEXT NOT NULL,
                PRIMARY KEY (source, series_id, start_date, end_date)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mwsolar_monthly (
                year_month TEXT NOT NULL PRIMARY KEY,
                mw_solar_ct_per_kwh REAL NOT NULL,
                fetched_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def put_mwsolar_monthly(rows: list[dict[str, Any]]) -> int:
    """Upsert MW Solar (ct/kWh) by year_month (YYYY-MM). Returns rows written."""
    if not rows:
        return 0
    init_db()
    fetched_at = _now_iso()
    payload = [
        (r["year_month"], float(r["mw_solar_ct_per_kwh"]), fetched_at)
        for r in rows
        if r.get("year_month") is not None
    ]
    with _lock:
        conn = _connect()
        conn.executemany(
            """
            INSERT INTO mwsolar_monthly (year_month, mw_solar_ct_per_kwh, fetched_at)
            VALUES (?, ?, ?)
            ON CONFLICT(year_month) DO UPDATE SET
                mw_solar_ct_per_kwh = excluded.mw_solar_ct_per_kwh,
                fetched_at = excluded.fetched_at
            """,
            payload,
        )
        conn.commit()
    return len(payload)


def get_mwsolar_map(year_month_from: str, year_month_to: str) -> dict[str, float]:
    """year_month (YYYY-MM) -> MW Solar in ct/kWh."""
    init_db()
    with _lock:
        rows = _connect().execute(
            """
            SELECT year_month, mw_solar_ct_per_kwh
            FROM mwsolar_monthly
            WHERE year_month >= ? AND year_month <= ?
            ORDER BY year_month
            """,
            (year_month_from, year_month_to),
        ).fetchall()
    return {r["year_month"]: float(r["mw_solar_ct_per_kwh"]) for r in rows}


def get_mwsolar_monthly(year_month: str) -> dict[str, Any] | None:
    init_db()
    with _lock:
        row = _connect().execute(
            "SELECT year_month, mw_solar_ct_per_kwh, fetched_at FROM mwsolar_monthly WHERE year_month = ?",
            (year_month,),
        ).fetchone()
    if not row:
        return None
    return {
        "year_month": row["year_month"],
        "mw_solar_ct_per_kwh": float(row["mw_solar_ct_per_kwh"]),
        "fetched_at": row["fetched_at"],
    }


def normalize_bucket_start(bucket_start: str, aggregation: str) -> str:
    bs = bucket_start[:10]
    if aggregation == "Month" and len(bs) >= 7:
        return f"{bs[:7]}-01"
    return bs


def get_buckets(
    source: str,
    series_id: str,
    aggregation: str,
    start_date: str,
    end_date: str,
) -> dict[str, dict[str, Any]]:
    init_db()
    with _lock:
        rows = _connect().execute(
            """
            SELECT bucket_start, kwh, fetched_at
            FROM buckets
            WHERE source = ? AND series_id = ? AND aggregation = ?
              AND bucket_start >= ? AND bucket_start <= ?
            """,
            (source, series_id, aggregation, start_date, end_date),
        ).fetchall()
    return {r["bucket_start"]: {"kwh": int(r["kwh"]), "fetched_at": r["fetched_at"]} for r in rows}


def put_buckets(
    source: str,
    series_id: str,
    aggregation: str,
    points: list[dict[str, Any]],
) -> None:
    if not points:
        return
    init_db()
    fetched_at = _now_iso()
    rows: list[tuple[str, str, str, str, int, str]] = []
    today_iso = _today().isoformat()
    for p in points:
        raw = (p.get("bucket_start") or "")[:10]
        if not raw:
            continue
        bucket_start = normalize_bucket_start(raw, aggregation)
        if source in (SOURCE_ENPHASE, SOURCE_FUSION_SOLAR) and bucket_start == today_iso:
            continue
        rows.append(
            (
                source,
                series_id,
                bucket_start,
                aggregation,
                int(p.get("value") or 0),
                fetched_at,
            )
        )
    if not rows:
        return
    with _lock:
        conn = _connect()
        conn.executemany(
            """
            INSERT INTO buckets (source, series_id, bucket_start, aggregation, kwh, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, series_id, bucket_start, aggregation) DO UPDATE SET
                kwh = excluded.kwh,
                fetched_at = excluded.fetched_at
            """,
            rows,
        )
        conn.commit()


def get_period_total(
    source: str,
    series_id: str,
    start_date: str,
    end_date: str,
) -> int | None:
    if period_total_needs_refresh(end_date):
        return None
    init_db()
    with _lock:
        row = _connect().execute(
            """
            SELECT kwh FROM period_totals
            WHERE source = ? AND series_id = ? AND start_date = ? AND end_date = ?
            """,
            (source, series_id, start_date, end_date),
        ).fetchone()
    return int(row["kwh"]) if row else None


def put_period_total(
    source: str,
    series_id: str,
    start_date: str,
    end_date: str,
    kwh: int,
) -> None:
    if period_total_needs_refresh(end_date):
        return
    init_db()
    with _lock:
        conn = _connect()
        conn.execute(
            """
            INSERT INTO period_totals (source, series_id, start_date, end_date, kwh, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, series_id, start_date, end_date) DO UPDATE SET
                kwh = excluded.kwh,
                fetched_at = excluded.fetched_at
            """,
            (source, series_id, start_date, end_date, kwh, _now_iso()),
        )
        conn.commit()


def period_total_needs_refresh(end_date: str) -> bool:
    end = date.fromisoformat(end_date[:10])
    return end >= _first_day_current_month()


def _add_months(d: date, months: int) -> date:
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, monthrange(year, month)[1])
    return date(year, month, day)


def expected_month_bucket_starts(start: date, end: date) -> list[str]:
    out: list[str] = []
    cur = start.replace(day=1)
    last = end.replace(day=1)
    while cur <= last:
        out.append(cur.isoformat())
        cur = _add_months(cur, 1)
    return out


def expected_day_bucket_starts(start: date, end: date) -> list[str]:
    out: list[str] = []
    cur = start
    while cur <= end:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def expected_bucket_starts(start_date: str, end_date: str, aggregation: str) -> list[str] | None:
    start = date.fromisoformat(start_date[:10])
    end = date.fromisoformat(end_date[:10])
    if aggregation == "Month":
        return expected_month_bucket_starts(start, end)
    if aggregation == "Day":
        return expected_day_bucket_starts(start, end)
    return None


def split_range_for_cache(start_date: str, end_date: str) -> tuple[str | None, str | None, str | None]:
    """
    Returns (hist_start, hist_end, live_start) where live portion needs API refresh.
    If entire range is historical, live_start is None.
    """
    start = date.fromisoformat(start_date[:10])
    end = date.fromisoformat(end_date[:10])
    first_live = _first_day_current_month()
    if end < first_live:
        return start_date, end_date, None
    if start >= first_live:
        return None, None, start_date
    hist_end = _last_day_previous_month()
    if hist_end < start:
        return None, None, start_date
    return start_date, hist_end.isoformat(), first_live.isoformat()


def missing_bucket_starts(
    cached: dict[str, dict[str, Any]],
    expected: list[str],
    aggregation: str = "Day",
) -> list[str]:
    missing: list[str] = []
    for bs in expected:
        if bs not in cached:
            missing.append(bs)
            continue
        if bucket_needs_refresh(bs, aggregation):
            missing.append(bs)
    return missing


def _chart_label(bucket_start: str, aggregation: str) -> str:
    if aggregation == "Month":
        return bucket_start[:7]
    return bucket_start


def points_from_cache(
    cached: dict[str, dict[str, Any]],
    bucket_starts: list[str],
    aggregation: str,
) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for bs in bucket_starts:
        row = cached.get(bs)
        if not row:
            continue
        points.append(
            {
                "date": _chart_label(bs, aggregation),
                "bucket_start": bs,
                "value": row["kwh"],
            }
        )
    return points


def fetch_series_with_cache(
    source: str,
    series_id: str,
    aggregation: str,
    start_date: str,
    end_date: str,
    fetch_fn: Callable[[str, str, str], dict[str, Any]],
) -> dict[str, Any]:
    """
    Load chart buckets from SQLite when possible; API only for gaps and live period.
    fetch_fn(start, end, aggregation) returns {unit, points, total} as leneda/enphase parsers.
    """
    if aggregation in ("Infinite", "Week"):
        return fetch_fn(start_date, end_date, aggregation)

    hist_start, hist_end, live_start = split_range_for_cache(start_date, end_date)
    all_points: list[dict[str, Any]] = []
    expected = expected_bucket_starts(start_date, end_date, aggregation)

    if hist_start and hist_end:
        cached = get_buckets(source, series_id, aggregation, hist_start, hist_end)
        need_api_hist = False
        if expected:
            hist_expected = [b for b in expected if b <= hist_end]
            if missing_bucket_starts(cached, hist_expected, aggregation):
                need_api_hist = True
        elif not cached:
            need_api_hist = True

        if need_api_hist:
            fresh = fetch_fn(hist_start, hist_end, aggregation)
            pts = fresh.get("points") or []
            put_buckets(source, series_id, aggregation, pts)
            if aggregation == "Month":
                for p in pts:
                    bs = normalize_bucket_start(p.get("bucket_start") or "", aggregation)
                    all_points.append(
                        {**p, "bucket_start": bs, "date": _chart_label(bs, aggregation)}
                    )
            else:
                all_points.extend(pts)
        else:
            hist_expected = expected or sorted(cached.keys())
            hist_expected = [b for b in hist_expected if hist_start <= b <= hist_end]
            all_points.extend(points_from_cache(cached, hist_expected, aggregation))

    if live_start:
        fresh = fetch_fn(live_start, end_date, aggregation)
        put_buckets(source, series_id, aggregation, fresh.get("points") or [])
        all_points.extend(fresh.get("points") or [])

    if not hist_start and not live_start:
        fresh = fetch_fn(start_date, end_date, aggregation)
        put_buckets(source, series_id, aggregation, fresh.get("points") or [])
        all_points.extend(fresh.get("points") or [])

    by_start: dict[str, dict[str, Any]] = {}
    for p in all_points:
        bs = p.get("bucket_start") or ""
        if bs:
            by_start[bs] = p
    points = sorted(by_start.values(), key=lambda p: p["bucket_start"])
    total = int(round(sum(p["value"] for p in points)))
    return {"unit": "kWh", "points": points, "total": total}


def fetch_daily_enphase_with_cache(
    start_date: str,
    end_date: str,
    fetch_fn: Callable[[str, str], dict[str, Any]],
) -> dict[str, Any]:
    """Daily Enphase production; today always from API, closed days from cache."""
    start = date.fromisoformat(start_date[:10])
    end = date.fromisoformat(end_date[:10])
    today = _today()
    all_points: list[dict[str, Any]] = []
    meta: dict[str, Any] = {}
    note: str | None = None
    has_meter = False

    hist_end = min(end, today - timedelta(days=1))
    if start <= hist_end:
        hist_end_s = hist_end.isoformat()
        cached = get_buckets(SOURCE_ENPHASE, ENPHASE_SERIES_ID, "Day", start_date, hist_end_s)
        expected = expected_day_bucket_starts(start, hist_end)
        if missing_bucket_starts(cached, expected):
            fresh = fetch_fn(start_date, hist_end_s)
            put_buckets(
                SOURCE_ENPHASE,
                ENPHASE_SERIES_ID,
                "Day",
                fresh.get("points") or [],
            )
            hist_points = [
                p for p in fresh.get("points") or []
                if not p.get("partial_day") and p.get("bucket_start") != today.isoformat()
            ]
        else:
            hist_points = [
                {
                    "date": bs,
                    "bucket_start": bs,
                    "value": cached[bs]["kwh"],
                }
                for bs in expected
            ]
        all_points.extend(hist_points)

    if end >= today:
        fresh = fetch_fn(today.isoformat(), end_date)
        note = fresh.get("note")
        meta = fresh.get("meta") or {}
        has_meter = bool(fresh.get("has_consumption_meter"))
        today_points = [
            p for p in fresh.get("points") or [] if p.get("bucket_start") == today.isoformat()
        ]
        all_points.extend(today_points)
    elif not all_points:
        fresh = fetch_fn(start_date, end_date)
        return fresh

    by_start = {p["bucket_start"]: p for p in all_points if p.get("bucket_start")}
    points = sorted(by_start.values(), key=lambda p: p["bucket_start"])
    result: dict[str, Any] = {
        "unit": "kWh",
        "points": points,
        "total": sum(int(p.get("value") or 0) for p in points),
        "source": "energy_lifetime",
        "has_consumption_meter": has_meter,
        "meta": meta,
    }
    if note:
        result["note"] = note
    return result


def fetch_daily_fusion_solar_with_cache(
    roof_id: str,
    start_date: str,
    end_date: str,
    fetch_fn: Callable[[str, str], dict[str, Any]],
) -> dict[str, Any]:
    """Daily FusionSolar production per roof; today always from API."""
    start = date.fromisoformat(start_date[:10])
    end = date.fromisoformat(end_date[:10])
    today = _today()
    all_points: list[dict[str, Any]] = []

    hist_end = min(end, today - timedelta(days=1))
    if start <= hist_end:
        hist_end_s = hist_end.isoformat()
        cached = get_buckets(SOURCE_FUSION_SOLAR, roof_id, "Day", start_date, hist_end_s)
        expected = expected_day_bucket_starts(start, hist_end)
        if missing_bucket_starts(cached, expected):
            fresh = fetch_fn(start_date, hist_end_s)
            put_buckets(SOURCE_FUSION_SOLAR, roof_id, "Day", fresh.get("points") or [])
            hist_points = [
                p
                for p in fresh.get("points") or []
                if p.get("bucket_start") != today.isoformat()
            ]
        else:
            hist_points = [
                {
                    "date": bs,
                    "bucket_start": bs,
                    "value": cached[bs]["kwh"],
                }
                for bs in expected
            ]
        all_points.extend(hist_points)

    if end >= today:
        fresh = fetch_fn(today.isoformat(), end_date)
        today_points = [
            p for p in fresh.get("points") or [] if p.get("bucket_start") == today.isoformat()
        ]
        all_points.extend(today_points)
    elif not all_points:
        return fetch_fn(start_date, end_date)

    by_start = {p["bucket_start"]: p for p in all_points if p.get("bucket_start")}
    points = sorted(by_start.values(), key=lambda p: p["bucket_start"])
    return {
        "unit": "kWh",
        "points": points,
        "total": sum(float(p.get("value") or 0) for p in points),
        "source": "fusion_solar",
    }
