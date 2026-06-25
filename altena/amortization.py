"""Amortissement PV — pré-Leneda (tableur) + bénéfice mesuré depuis Leneda/Enphase."""

from __future__ import annotations

from datetime import date
from typing import Any

from altena.cache_store import get_mwsolar_map
from altena.enphase_client import enphase_configured, fetch_production_series
from altena.leneda_client import fetch_all_series, load_config
from altena.metrics import compute_self_consumption
from altena.netztransparenz_client import supplier_injection_eur_per_kwh


def _parse_date(s: str) -> date:
    return date.fromisoformat(s[:10])


def _year_from_label(label: str, bucket_start: str) -> int:
    if len(label) >= 7 and label[4] == "-":
        return int(label[:4])
    if bucket_start:
        return int(bucket_start[:4])
    return date.today().year


def _year_month_from_row(label: str, bucket_start: str) -> str:
    if len(label) >= 7 and label[4] == "-":
        parts = label.split("-")
        if len(parts) >= 2:
            return f"{parts[0]}-{int(parts[1]):02d}"
    if bucket_start and len(bucket_start) >= 7:
        return bucket_start[:7]
    return f"{_year_from_label(label, bucket_start)}-01"


def _round_eur(value: float) -> float:
    return round(value, 2)


def _pre_leneda_benefit(pre: dict[str, Any]) -> dict[str, Any]:
    autocons_kwh = int(pre.get("autoconsumption_kwh") or 0)
    rate = float(pre.get("autoconsumption_eur_per_kwh") or 0.15)
    injection_eur = float(pre.get("injection_reimbursement_eur") or 0)
    autocons_eur = _round_eur(autocons_kwh * rate)
    total = _round_eur(autocons_eur + injection_eur)
    return {
        "autoconsumption_kwh": autocons_kwh,
        "autoconsumption_eur": autocons_eur,
        "injection_kwh": int(pre.get("injection_kwh") or 0),
        "injection_eur": injection_eur,
        "production_kwh": int(pre.get("production_kwh") or 0),
        "total_eur": total,
    }


def resolve_supplier(supplier_id: str | None, config: dict[str, Any]) -> dict[str, Any]:
    suppliers = config.get("suppliers") or {}
    if not suppliers:
        raise ValueError("config.json: add a 'suppliers' table (sudstroum, enovos, …)")
    am = config.get("amortization") or {}
    default_id = (am.get("injection_supplier") or "sudstroum").strip()
    sid = (supplier_id or default_id).strip().lower()
    if sid not in suppliers:
        known = ", ".join(sorted(suppliers.keys()))
        raise ValueError(f"Unknown injection supplier {sid!r} (known: {known})")
    profile = suppliers[sid]
    return {
        "id": sid,
        "label": profile.get("label", sid),
        "mwsolar_factor": float(profile.get("mwsolar_factor", 0.9)),
        "note": profile.get("note", ""),
    }


def suppliers_for_ui(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": sid,
            "label": (profile.get("label") or sid),
            "mwsolar_factor": float(profile.get("mwsolar_factor", 0)),
            "note": profile.get("note", ""),
        }
        for sid, profile in sorted((config.get("suppliers") or {}).items())
    ]


def _injection_rates_by_month(
    year_months: list[str],
    supplier: dict[str, Any],
    fallback_eur: float,
) -> tuple[dict[str, float], dict[str, float], list[str]]:
    """
    Returns (eur_per_kwh by month, mw_solar_ct by month, months using fallback).
    """
    if not year_months:
        return {}, {}, []
    ym_sorted = sorted(set(year_months))
    mwsolar_ct = get_mwsolar_map(ym_sorted[0], ym_sorted[-1])
    factor = supplier["mwsolar_factor"]
    rates: dict[str, float] = {}
    used_fallback: list[str] = []
    for ym in ym_sorted:
        ct = mwsolar_ct.get(ym)
        if ct is not None:
            rates[ym] = supplier_injection_eur_per_kwh(
                ct,
                supplier["id"],
                factors={supplier["id"]: factor},
            )
        else:
            rates[ym] = fallback_eur
            used_fallback.append(ym)
    return rates, mwsolar_ct, used_fallback


def _monthly_benefit_eur(
    autocons_kwh: int,
    shared_l2_kwh: int,
    shared_cel_kwh: int,
    export_kwh: int,
    year: int,
    year_month: str,
    rates: dict[str, Any],
    injection_eur_per_kwh: float,
    mw_solar_ct: float | None,
) -> dict[str, float]:
    supplier_rate = float((rates.get("supplier_by_year") or {}).get(str(year), 0.186))
    shared_kwh = shared_l2_kwh + shared_cel_kwh
    autocons_eur = autocons_kwh * supplier_rate
    # Partages : valeur = énergie non achetée au fournisseur (pas le tarif injection MW Solar).
    community_eur = shared_kwh * supplier_rate
    export_eur = export_kwh * injection_eur_per_kwh
    total = autocons_eur + community_eur + export_eur
    out: dict[str, Any] = {
        "autoconsumption_eur": _round_eur(autocons_eur),
        "community_eur": _round_eur(community_eur),
        "export_eur": _round_eur(export_eur),
        "total_eur": _round_eur(total),
        "injection_eur_per_kwh": round(injection_eur_per_kwh, 5),
    }
    if mw_solar_ct is not None:
        out["mw_solar_ct_per_kwh"] = round(mw_solar_ct, 3)
    return out


def _estimate_payback_year(
    chart_points: list[dict[str, Any]],
    capex_net: float,
    measured_months: int,
    measured_total_eur: float,
) -> int | None:
    if capex_net <= 0:
        return None
    for pt in chart_points:
        if float(pt.get("cumulative_eur") or 0) >= capex_net:
            label = str(pt.get("label", ""))
            if len(label) >= 4 and label[4] == "-":
                return int(label[:4])
            return date.today().year
    if measured_months < 1 or measured_total_eur <= 0:
        return None
    last = chart_points[-1]
    cum = float(last.get("cumulative_eur") or 0)
    remaining = capex_net - cum
    per_month = measured_total_eur / measured_months
    if per_month <= 0:
        return None
    months_left = remaining / per_month
    return date.today().year + int(months_left / 12) + (1 if months_left % 12 > 0 else 0)


def _timeline_starts(series: list[dict[str, Any]]) -> dict[str, str]:
    starts: dict[str, str] = {}
    for s in series:
        for p in s.get("points") or []:
            label = p.get("date")
            if label:
                starts[label] = p.get("bucket_start") or label
    return starts


def _fetch_measured_monthly(
    from_date: str,
    to_date: str,
) -> list[dict[str, Any]]:
    data = fetch_all_series(from_date, to_date, chart_aggregation="Month")
    timeline = data.get("timeline") or []
    if not timeline:
        return []

    series = data.get("series") or []
    timeline_starts = _timeline_starts(series)
    enphase_series = None
    if enphase_configured():
        try:
            enphase_series = fetch_production_series(
                from_date,
                to_date,
                "Month",
                timeline,
                timeline_starts,
            )
        except Exception:
            enphase_series = None

    sc = compute_self_consumption(timeline, series, enphase_series)
    by_id = {s["id"]: s for s in series}
    config = load_config()
    am = config.get("amortization") or {}
    flows = am.get("measured_flows") or {}

    def kwh_by_id(sid: str) -> dict[str, int]:
        s = by_id.get(sid)
        if not s:
            return {}
        return {p["date"]: int(p.get("value") or 0) for p in s.get("points") or []}

    def kwh_sum(ids: list[str]) -> dict[str, int]:
        merged: dict[str, int] = {}
        for sid in ids:
            for label, value in kwh_by_id(sid).items():
                merged[label] = merged.get(label, 0) + value
        return merged

    autocons = {p["date"]: int(p.get("value") or 0) for p in sc.get("points") or []}
    l2_ids = flows.get("shared_l2") or ["prod_shared_l2"]
    cel_ids = flows.get("shared_cel") or ["prod_shared_cel"]
    export_ids = flows.get("export") or ["prod_market"]
    l2 = kwh_sum(l2_ids) if len(l2_ids) > 1 else kwh_by_id(l2_ids[0])
    cel = kwh_sum(cel_ids) if len(cel_ids) > 1 else kwh_by_id(cel_ids[0])
    export = kwh_sum(export_ids) if len(export_ids) > 1 else kwh_by_id(export_ids[0])

    return [
        {
            "label": label,
            "bucket_start": timeline_starts.get(label, label if len(label) > 7 else f"{label}-01"),
            "year_month": _year_month_from_row(
                label, timeline_starts.get(label, label if len(label) > 7 else f"{label}-01")
            ),
            "autoconsumption_kwh": autocons.get(label, 0),
            "shared_l2_kwh": l2.get(label, 0),
            "shared_cel_kwh": cel.get(label, 0),
            "export_kwh": export.get(label, 0),
        }
        for label in timeline
    ]


def build_amortization(supplier_id: str | None = None) -> dict[str, Any]:
    config = load_config()
    am = config.get("amortization")
    if not am:
        return {"enabled": False}

    supplier = resolve_supplier(supplier_id, config)
    capex_net = float(am.get("capex_net_eur") or 0)
    if capex_net <= 0:
        gross = float(am.get("capex_gross_eur") or 0)
        subsidy = float(am.get("subsidy_eur") or 0)
        capex_net = gross - subsidy

    pre_cfg = am.get("pre_leneda") or {}
    pre = _pre_leneda_benefit(pre_cfg)
    pre_label = pre_cfg.get("period_label") or "03/2023 – 12/2024"

    from_date = (am.get("leneda_benefit_from") or "2025-01-01").strip()
    to_date = date.today().isoformat()
    rates = am.get("eur_per_kwh") or {}
    fallback_injection = float(
        rates.get("injection_fallback_eur_per_kwh")
        or rates.get("grid_export_avg")
        or 0.05
    )

    monthly_rows: list[dict[str, Any]] = []
    measured_total_eur = 0.0
    mwsolar_missing: list[str] = []
    try:
        measured = _fetch_measured_monthly(from_date, to_date)
        year_months = [r["year_month"] for r in measured]
        injection_by_month, mwsolar_ct_by_month, mwsolar_missing = _injection_rates_by_month(
            year_months,
            supplier,
            fallback_injection,
        )

        for row in measured:
            ym = row["year_month"]
            year = _year_from_label(row["label"], row["bucket_start"])
            inj_rate = injection_by_month.get(ym, fallback_injection)
            benefit = _monthly_benefit_eur(
                row["autoconsumption_kwh"],
                row["shared_l2_kwh"],
                row["shared_cel_kwh"],
                row["export_kwh"],
                year,
                ym,
                rates,
                inj_rate,
                mwsolar_ct_by_month.get(ym),
            )
            measured_total_eur += benefit["total_eur"]
            monthly_rows.append({**row, **benefit, "phase": "measured"})
    except Exception as exc:
        return {
            "enabled": True,
            "error": str(exc),
            "capex_net_eur": capex_net,
            "injection": {"supplier_id": supplier["id"], "supplier_label": supplier["label"]},
        }

    cumulative = pre["total_eur"]
    chart_points: list[dict[str, Any]] = [
        {
            "label": pre_label,
            "bucket_start": pre_cfg.get("period_end") or "2024-12-31",
            "phase": "pre_leneda",
            "benefit_eur": pre["total_eur"],
            "cumulative_eur": cumulative,
        }
    ]

    for row in monthly_rows:
        cumulative = _round_eur(cumulative + row["total_eur"])
        chart_points.append(
            {
                "label": row["label"],
                "bucket_start": row["bucket_start"],
                "phase": "measured",
                "benefit_eur": row["total_eur"],
                "cumulative_eur": cumulative,
                "autoconsumption_kwh": row["autoconsumption_kwh"],
                "shared_l2_kwh": row["shared_l2_kwh"],
                "shared_cel_kwh": row["shared_cel_kwh"],
                "export_kwh": row["export_kwh"],
            }
        )

    total_benefit = cumulative
    recovered_pct = _round_eur(100.0 * total_benefit / capex_net) if capex_net else 0.0
    remaining = _round_eur(max(0.0, capex_net - total_benefit))
    payback_year = _estimate_payback_year(
        chart_points, capex_net, len(monthly_rows), measured_total_eur
    )

    ref_year = int(am.get("reference_full_year") or 2026)
    ref_row = next((r for r in monthly_rows if r["label"].startswith(str(ref_year))), None)
    ui = am.get("ui_labels") or {}
    pct = int(round(100 * supplier["mwsolar_factor"]))

    formula = (
        f"Bénéfice mesuré (depuis {from_date}) = autoconsommation × tarif fournisseur (année) "
        f"+ partages totaux (ACR + CEL) × tarif fournisseur (année) "
        f"+ injection × taux {supplier['label']} ({pct} % MW Solar mensuel Netztransparenz)."
    )
    if mwsolar_missing:
        formula += (
            f" Mois sans MW Solar en cache ({', '.join(mwsolar_missing[:6])}"
            f"{'…' if len(mwsolar_missing) > 6 else ''}) : repli "
            f"{fallback_injection:.3f} €/kWh (sera mis à jour au prochain lancement si publié)."
        )

    return {
        "enabled": True,
        "ui_labels": ui,
        "capex_gross_eur": float(am.get("capex_gross_eur") or 0),
        "subsidy_eur": float(am.get("subsidy_eur") or 0),
        "capex_net_eur": capex_net,
        "subsidy_simulation": am.get("subsidy_simulation"),
        "commissioning_date": am.get("commissioning_date"),
        "leneda_benefit_from": from_date,
        "reference_full_year": ref_year,
        "injection": {
            "supplier_id": supplier["id"],
            "supplier_label": supplier["label"],
            "mwsolar_factor": supplier["mwsolar_factor"],
            "mwsolar_factor_pct": pct,
            "suppliers": suppliers_for_ui(config),
            "default_supplier_id": (am.get("injection_supplier") or "sudstroum"),
            "mwsolar_months_missing": mwsolar_missing,
            "injection_fallback_eur_per_kwh": fallback_injection,
        },
        "pre_leneda": {
            "period_label": pre_label,
            "note": pre_cfg.get("note", "Données tableur Excel — AC1 ignoré"),
            **pre,
        },
        "measured_benefit_eur": _round_eur(measured_total_eur),
        "total_benefit_eur": total_benefit,
        "recovered_pct": recovered_pct,
        "remaining_eur": remaining,
        "payback_reached": total_benefit >= capex_net,
        "payback_year_estimate": payback_year,
        "reference_year_benefit_eur": ref_row["total_eur"] if ref_row else None,
        "energy_priority_note": am.get("energy_priority_note", ""),
        "formula": formula,
        "pre_leneda_formula": (
            f"Pré-Leneda : {pre['autoconsumption_kwh']} kWh × "
            f"{pre_cfg.get('autoconsumption_eur_per_kwh', 0.15)} € "
            f"+ remboursement injection {pre['injection_eur']:.0f} € (tableur)"
        ),
        "chart": {
            "labels": [p["label"] for p in chart_points],
            "cumulative_eur": [p["cumulative_eur"] for p in chart_points],
            "benefit_eur": [p["benefit_eur"] for p in chart_points],
            "capex_net_eur": capex_net,
        },
        "monthly": monthly_rows,
    }
