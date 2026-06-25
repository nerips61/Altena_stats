"""Production mensuelle brute saisie à la main (SQLite) — temporaire avant FusionSolar / Leneda."""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

from altena.cache_store import _connect, _lock, _now_iso, init_db
from altena.leneda_client import load_config

from altena.paths import MANUAL_PRODUCTION_SEED_PATH as SEED_PATH

SOURCE_MANUAL = "manual_production"
ROOF_TO_SERIES = {
    "marin": "prod_marin_active",
    "midi": "prod_midi_active",
}


def _ensure_table() -> None:
    init_db()
    with _lock:
        _connect().execute(
            """
            CREATE TABLE IF NOT EXISTS manual_production_monthly (
                roof_id TEXT NOT NULL,
                year_month TEXT NOT NULL,
                kwh INTEGER NOT NULL,
                note TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (roof_id, year_month)
            )
            """
        )
        _connect().commit()


def upsert_rows(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    _ensure_table()
    fetched_at = _now_iso()
    payload = [
        (
            str(r["roof_id"]).strip(),
            str(r["year_month"]).strip()[:7],
            int(r["kwh"]),
            (r.get("note") or "").strip() or None,
            fetched_at,
        )
        for r in rows
        if r.get("roof_id") and r.get("year_month") is not None
    ]
    with _lock:
        conn = _connect()
        conn.executemany(
            """
            INSERT INTO manual_production_monthly (roof_id, year_month, kwh, note, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(roof_id, year_month) DO UPDATE SET
                kwh = excluded.kwh,
                note = excluded.note,
                updated_at = excluded.updated_at
            """,
            payload,
        )
        conn.commit()
    return len(payload)


def load_seed_file(path: str | None = None) -> dict[str, Any]:
    seed_path = path or SEED_PATH
    with open(seed_path, encoding="utf-8") as f:
        return json.load(f)


def seed_from_file(path: str | None = None, force: bool = False) -> int:
    """Charge data/manual_production.json dans cache.db."""
    data = load_seed_file(path)
    rows = data.get("rows") or []
    if not force:
        _ensure_table()
        with _lock:
            n = _connect().execute("SELECT COUNT(*) FROM manual_production_monthly").fetchone()[0]
        if n > 0:
            return 0
    return upsert_rows(rows)


def get_monthly_map(
    roof_id: str,
    year_month_from: str,
    year_month_to: str,
) -> dict[str, int]:
    _ensure_table()
    with _lock:
        rows = _connect().execute(
            """
            SELECT year_month, kwh FROM manual_production_monthly
            WHERE roof_id = ? AND year_month >= ? AND year_month <= ?
            ORDER BY year_month
            """,
            (roof_id, year_month_from[:7], year_month_to[:7]),
        ).fetchall()
    return {r["year_month"]: int(r["kwh"]) for r in rows}


def get_kwh(roof_id: str, year_month: str) -> int | None:
    ym = year_month[:7]
    m = get_monthly_map(roof_id, ym, ym)
    return m.get(ym)


def list_all() -> list[dict[str, Any]]:
    _ensure_table()
    with _lock:
        rows = _connect().execute(
            """
            SELECT roof_id, year_month, kwh, note, updated_at
            FROM manual_production_monthly
            ORDER BY year_month, roof_id
            """
        ).fetchall()
    return [dict(r) for r in rows]


def manual_enabled() -> bool:
    try:
        cfg = load_config().get("manual_production") or {}
        return bool(cfg.get("enabled", True))
    except OSError:
        return True


def _roof_for_series_id(series_id: str) -> str | None:
    for roof, sid in ROOF_TO_SERIES.items():
        if sid == series_id:
            return roof
    return None


def _year_month_from_point(p: dict[str, Any]) -> str:
    raw = (p.get("bucket_start") or p.get("date") or "")[:7]
    return raw if len(raw) == 7 else ""


def _prorate_month_to_points(month_points: list[dict[str, Any]], manual_kwh: int) -> list[dict[str, Any]]:
    """Répartit le kWh mensuel manuel sur les buckets jour/semaine (forme Leneda conservée)."""
    leneda_sum = sum(int(p.get("value") or 0) for p in month_points)
    n = len(month_points)
    if n == 0:
        return []
    if leneda_sum <= 0:
        base, rem = divmod(manual_kwh, n)
        values = [base + (1 if i < rem else 0) for i in range(n)]
    else:
        values = [
            int(round(manual_kwh * int(p.get("value") or 0) / leneda_sum)) for p in month_points
        ]
        diff = manual_kwh - sum(values)
        if diff:
            idx = max(range(n), key=lambda i: values[i])
            values[idx] += diff
    out: list[dict[str, Any]] = []
    for p, value in zip(month_points, values):
        out.append(
            {
                **p,
                "value": value,
                "empty": value == 0,
                "manual_production": True,
            }
        )
    return out


def overlay_production_on_series(
    series: list[dict[str, Any]],
    chart_aggregation: str,
) -> tuple[list[dict[str, Any]], bool]:
    """
    Remplace la production Leneda par les kWh manuels (SQLite / Excel) quand une ligne existe.
    Mensuel : valeur directe. Journalier / hebdo : répartition proportionnelle dans le mois.
    Partages / injection restent Leneda.
    """
    if not manual_enabled() or chart_aggregation not in ("Month", "Day", "Week"):
        return series, False

    used = False
    out: list[dict[str, Any]] = []
    for entry in series:
        roof = _roof_for_series_id(entry.get("id", ""))
        if not roof:
            out.append(entry)
            continue

        points_in: list[dict[str, Any]] = list(entry.get("points") or [])
        if chart_aggregation == "Month":
            points: list[dict[str, Any]] = []
            for p in points_in:
                ym = _year_month_from_point(p)
                manual_kwh = get_kwh(roof, ym) if ym else None
                if manual_kwh is not None:
                    used = True
                    points.append(
                        {
                            **p,
                            "value": manual_kwh,
                            "empty": False,
                            "manual_production": True,
                        }
                    )
                else:
                    points.append(p)
        else:
            by_month: dict[str, list[dict[str, Any]]] = {}
            for p in points_in:
                ym = _year_month_from_point(p)
                if ym:
                    by_month.setdefault(ym, []).append(p)
            points = []
            for ym in sorted(by_month):
                month_points = by_month[ym]
                manual_kwh = get_kwh(roof, ym)
                if manual_kwh is not None:
                    used = True
                    points.extend(_prorate_month_to_points(month_points, manual_kwh))
                else:
                    points.extend(month_points)

        total = sum(int(p.get("value") or 0) for p in points)
        row_used = any(p.get("manual_production") for p in points)
        out.append({**entry, "points": points, "total": total, "manual_overlay": row_used})
    return out, used


def status_for_ui() -> dict[str, Any]:
    cfg = load_config().get("manual_production") or {}
    rows = list_all()
    return {
        "enabled": manual_enabled(),
        "count": len(rows),
        "online_from": cfg.get("online_from") or "2025-06-24",
        "note": cfg.get("note") or "",
        "rows": rows,
    }


def ensure_seeded() -> None:
    seed_from_file()
