"""Derived overview KPIs and self-consumption from Leneda + Enphase."""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from altena.cache_store import get_mwsolar_map
from altena.leneda_client import load_config
from altena.netztransparenz_client import supplier_injection_eur_per_kwh


def _series_by_id(series: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {s["id"]: s for s in series}


def _points_by_label(series: dict[str, Any] | None) -> dict[str, int]:
    if not series:
        return {}
    return {p["date"]: int(p.get("value") or 0) for p in series.get("points") or []}


def _sum_points_by_label(maps: list[dict[str, int]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for m in maps:
        for label, value in m.items():
            out[label] = out.get(label, 0) + value
    return out


def compute_self_consumption_for_roof(
    timeline: list[str],
    leneda_series: list[dict[str, Any]],
    roof: dict[str, Any],
    enphase_series: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Autoconsommation for one PV POD (production − export − partages)."""
    by_id = _series_by_id(leneda_series)
    total_id = roof["total_id"]
    market_id = roof["market_id"]
    l2_id = roof["shared_l2_id"]
    cel_id = roof["shared_cel_id"]
    sc_id = roof.get("self_consumption_id") or f"self_consumption_{roof.get('roof_id', 'roof')}"

    prod_market = _points_by_label(by_id.get(market_id))
    prod_l2 = _points_by_label(by_id.get(l2_id))
    prod_cel = _points_by_label(by_id.get(cel_id))
    prod_leneda = _points_by_label(by_id.get(total_id))

    prod_src = prod_leneda
    production_label = f"Leneda ({roof.get('title', total_id)})"
    master_series = by_id.get(total_id)
    if master_series and master_series.get("fusion_solar_overlay"):
        production_label = "FusionSolar (onduleur)"
    elif master_series and master_series.get("manual_overlay"):
        production_label = "Production brute (saisie manuelle / SQLite)"
    if enphase_series and enphase_series.get("points"):
        prod_src = _points_by_label(enphase_series)
        production_label = "Enphase"

    bucket_starts: dict[str, str] = {}
    master = by_id.get(total_id)
    if master:
        for p in master.get("points") or []:
            bucket_starts[p["date"]] = p.get("bucket_start") or p["date"]

    points: list[dict[str, Any]] = []
    for label in timeline:
        production = prod_src.get(label, 0)
        export = prod_market.get(label, 0)
        shared = prod_l2.get(label, 0) + prod_cel.get(label, 0)
        value = max(0, production - export - shared)
        points.append(
            {
                "date": label,
                "bucket_start": bucket_starts.get(label, label),
                "value": value,
                "empty": production == 0 and export == 0 and shared == 0,
            }
        )

    total = sum(p["value"] for p in points)
    formula = (
        f"max(0, production ({production_label}) − export marché − partagée L2 − partagée CEL)"
    )
    return {
        "id": sc_id,
        "label": f"Autoconsommation — {roof.get('roof_id', 'toit').capitalize()}",
        "group": "derived",
        "color": "#0d9488",
        "unit": "kWh",
        "points": points,
        "total": total,
        "formula": formula,
        "production_source": production_label,
        "roof_id": roof.get("roof_id"),
    }


def compute_self_consumption(
    timeline: list[str],
    leneda_series: list[dict[str, Any]],
    enphase_series: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    Autoconsommation ≈ production − export − partagée L2 − partagée CEL.
    Single-POD (Mondercange) or summed over production_overviews (Marin–Midi).
    """
    config = load_config()
    roofs = config.get("production_overviews")
    if roofs:
        per_roof = [
            compute_self_consumption_for_roof(timeline, leneda_series, roof, None)
            for roof in roofs
        ]
        combined_points: list[dict[str, Any]] = []
        for label in timeline:
            bucket_start = label
            value = 0
            empty = True
            for sc in per_roof:
                by_label = _points_by_label(sc)
                if label in by_label:
                    value += by_label[label]
                    empty = False
                for p in sc.get("points") or []:
                    if p["date"] == label:
                        bucket_start = p.get("bucket_start") or label
            combined_points.append(
                {
                    "date": label,
                    "bucket_start": bucket_start,
                    "value": value,
                    "empty": empty and value == 0,
                }
            )
        by_id = _series_by_id(leneda_series)
        prod_sources: list[str] = []
        for roof in roofs:
            master = by_id.get(roof["total_id"])
            if master and master.get("fusion_solar_overlay"):
                prod_sources.append("FusionSolar")
            elif master and master.get("manual_overlay"):
                prod_sources.append("saisie manuelle")
            else:
                prod_sources.append("Leneda")
        if all(s == "FusionSolar" for s in prod_sources):
            production_source = "FusionSolar (onduleur)"
        elif any(s == "FusionSolar" for s in prod_sources):
            production_source = "FusionSolar + Leneda (mixte par toit)"
        elif all(s == "saisie manuelle" for s in prod_sources):
            production_source = "Production saisie manuellement"
        else:
            production_source = "Leneda (les deux POD PV)"
        return {
            "id": "self_consumption",
            "label": "Autoconsommation (Marin + Midi)",
            "group": "derived",
            "color": "#0d9488",
            "unit": "kWh",
            "points": combined_points,
            "total": sum(p["value"] for p in combined_points),
            "formula": "Somme autoconsommation Marin + Midi (max(0, prod − export − partages) par toit)",
            "production_source": production_source,
            "by_roof": per_roof,
        }

    by_id = _series_by_id(leneda_series)
    prod_market = _points_by_label(by_id.get("prod_market"))
    prod_l2 = _points_by_label(by_id.get("prod_shared_l2"))
    prod_cel = _points_by_label(by_id.get("prod_shared_cel"))
    prod_leneda = _points_by_label(by_id.get("prod_active"))

    if enphase_series and enphase_series.get("points"):
        prod_src = _points_by_label(enphase_series)
        production_label = "Enphase"
    else:
        prod_src = prod_leneda
        production_label = "Leneda (production totale)"

    bucket_starts: dict[str, str] = {}
    if enphase_series:
        for p in enphase_series.get("points") or []:
            bucket_starts[p["date"]] = p.get("bucket_start") or p["date"]
    master = by_id.get("prod_active")
    if master:
        for p in master.get("points") or []:
            bucket_starts.setdefault(p["date"], p.get("bucket_start") or p["date"])

    points: list[dict[str, Any]] = []
    for label in timeline:
        production = prod_src.get(label, 0)
        export = prod_market.get(label, 0)
        shared = prod_l2.get(label, 0) + prod_cel.get(label, 0)
        value = max(0, production - export - shared)
        points.append(
            {
                "date": label,
                "bucket_start": bucket_starts.get(label, label),
                "value": value,
                "empty": production == 0 and export == 0 and shared == 0,
            }
        )

    total = sum(p["value"] for p in points)
    formula = (
        f"max(0, production ({production_label}) − export marché − partagée L2 − partagée CEL)"
    )
    return {
        "id": "self_consumption",
        "label": "Autoconsommation (calcul local)",
        "group": "derived",
        "color": "#0d9488",
        "unit": "kWh",
        "points": points,
        "total": total,
        "formula": formula,
        "production_source": production_label,
    }


def _total_from_summary(totals: dict[str, Any], series_id: str) -> int:
    entry = totals.get(series_id) or {}
    return int(entry.get("total") or 0)


def _total_from_series(series: list[dict[str, Any]], series_id: str) -> int:
    """Total API brut (avant harmonisation summary_rows / FusionSolar sur les parts)."""
    for entry in series:
        if entry.get("id") == series_id:
            return int(entry.get("total") or 0)
    return 0


def _label_from_summary(totals: dict[str, Any], series_id: str, fallback: str) -> str:
    entry = totals.get(series_id) or {}
    return str(entry.get("label") or fallback)


def _overview_supplier_rate(year: int | None = None) -> float:
    config = load_config()
    y = str(year or date.today().year)
    member = config.get("member_economics") or {}
    by_year = member.get("supplier_by_year") or {}
    if y in by_year:
        return float(by_year[y])
    rates = (config.get("amortization") or {}).get("eur_per_kwh") or {}
    return float((rates.get("supplier_by_year") or {}).get(y, 0.186))


def _overview_cel_share_rate() -> float:
    config = load_config()
    member = config.get("member_economics") or {}
    if member.get("cel_share_eur_per_kwh") is not None:
        return float(member["cel_share_eur_per_kwh"])
    rates = (config.get("amortization") or {}).get("eur_per_kwh") or {}
    return float(rates.get("cel_share_eur_per_kwh") or 0.10)


def _member_cel_economics(kwh: int, year: int | None = None) -> dict[str, float | None]:
    """Montants membre sur part CEL : équivalent fournisseur, dû CEL, économie nette."""
    if kwh <= 0:
        return {"supplier_equiv_eur": None, "cel_due_eur": None, "savings_eur": None}
    supplier = _overview_supplier_rate(year)
    cel = _overview_cel_share_rate()
    supplier_equiv = _round_eur(kwh * supplier)
    due = _round_eur(kwh * cel)
    return {
        "supplier_equiv_eur": supplier_equiv,
        "cel_due_eur": due,
        "savings_eur": _round_eur(supplier_equiv - due),
    }


def _consumption_card_economics(cel_kwh: int, year: int | None = None) -> dict[str, Any]:
    eco = _member_cel_economics(cel_kwh, year)
    return {**eco, "eur_mode": "member_cel"}


def _round_eur(value: float) -> float:
    return round(value, 2)


def _eur_from_production_kwh(kwh: int) -> int | None:
    """Valeur production au tarif CEL (ct/kWh), arrondi à l'euro."""
    if kwh <= 0:
        return None
    return int(round(kwh * _overview_cel_share_rate()))


def _overview_injection_fallback() -> float:
    config = load_config()
    prod = config.get("producer_economics") or {}
    if prod.get("injection_fallback_eur_per_kwh") is not None:
        return float(prod["injection_fallback_eur_per_kwh"])
    am = config.get("amortization") or {}
    rates = am.get("eur_per_kwh") or {}
    return float(
        rates.get("injection_fallback_eur_per_kwh")
        or rates.get("grid_export_avg")
        or 0.05
    )


def _overview_injection_supplier() -> dict[str, Any]:
    config = load_config()
    prod = config.get("producer_economics") or {}
    am = config.get("amortization") or {}
    supplier_id = (
        prod.get("injection_supplier")
        or am.get("injection_supplier")
        or "sudstroum"
    )
    profile = (config.get("suppliers") or {}).get(supplier_id) or {}
    return {
        "id": supplier_id,
        "mwsolar_factor": float(profile.get("mwsolar_factor", 0.9)),
    }


def _year_month_range(year: int) -> tuple[str, str]:
    return f"{year}-01", f"{year}-12"


def _chart_month_key(point: dict[str, Any]) -> str | None:
    label = point.get("date") or ""
    if len(label) >= 7 and label[4] == "-":
        parts = label.split("-")
        if len(parts) >= 2:
            try:
                return f"{parts[0]}-{int(parts[1]):02d}"
            except ValueError:
                pass
    bucket = point.get("bucket_start") or ""
    if len(bucket) >= 7:
        return bucket[:7]
    return None


def _injection_weighted_from() -> str:
    prod = load_config().get("producer_economics") or {}
    return (prod.get("injection_weighted_from") or "2025-06-01").strip()[:10]


def _marin_midi_injection_by_month(from_date: str, to_date: str) -> dict[str, int]:
    """Injection marché Marin + Midi par mois (kWh), pour moyenne pondérée."""
    from altena.leneda_client import fetch_all_series

    data = fetch_all_series(from_date, to_date, chart_aggregation="Month")
    by_id = _series_by_id(data.get("series") or [])
    out: dict[str, int] = {}
    for sid in ("prod_marin_market", "prod_midi_market"):
        for point in (by_id.get(sid) or {}).get("points") or []:
            ym = _chart_month_key(point)
            if ym:
                out[ym] = out.get(ym, 0) + int(point.get("value") or 0)
    return out


def _simple_annual_injection_rate(
    ref_date: date,
    supplier_id: str,
    factor: float,
) -> dict[str, Any] | None:
    """Repli : moyenne simple MW Solar sur l'année (ou N-1, ou dernier mois)."""
    ref_year = ref_date.year

    def rates_for_year(year: int) -> list[float]:
        ym_from, ym_to = _year_month_range(year)
        mwsolar_ct = get_mwsolar_map(ym_from, ym_to)
        rates: list[float] = []
        for ym in sorted(mwsolar_ct):
            if not ym.startswith(f"{year}-"):
                continue
            rates.append(
                supplier_injection_eur_per_kwh(
                    mwsolar_ct[ym],
                    supplier_id,
                    factors={supplier_id: factor},
                )
            )
        return rates

    rates = rates_for_year(ref_year)
    source = f"moyenne simple MW Solar {ref_year}"
    if not rates:
        rates = rates_for_year(ref_year - 1)
        source = f"moyenne simple MW Solar {ref_year - 1}"
    if rates:
        return {
            "eur_per_kwh": round(sum(rates) / len(rates), 5),
            "source": source,
            "months": len(rates),
            "volume_weighted": False,
        }

    scan_from = f"{ref_year - 2}-01"
    scan_to = ref_date.strftime("%Y-%m")
    mwsolar_ct = get_mwsolar_map(scan_from, scan_to)
    if mwsolar_ct:
        last_ym = sorted(mwsolar_ct.keys())[-1]
        rate = supplier_injection_eur_per_kwh(
            mwsolar_ct[last_ym],
            supplier_id,
            factors={supplier_id: factor},
        )
        return {
            "eur_per_kwh": rate,
            "source": f"MW Solar {last_ym}",
            "months": 1,
            "volume_weighted": False,
        }
    return None


def _overview_injection_rate(end_date: str | None = None) -> dict[str, Any]:
    """
    Tarif injection de référence pour le gain producteur.
    Priorité : moyenne pondérée par les injections Marin–Midi (MW Solar × fournisseur).
    """
    ref_date = date.fromisoformat(end_date[:10]) if end_date else date.today()
    supplier = _overview_injection_supplier()
    fallback = _overview_injection_fallback()
    supplier_id = supplier["id"]
    factor = supplier["mwsolar_factor"]
    from_date = _injection_weighted_from()
    to_date = ref_date.isoformat()

    try:
        inj_by_month = _marin_midi_injection_by_month(from_date, to_date)
    except Exception:
        inj_by_month = {}

    if inj_by_month:
        ym_from, ym_to = min(inj_by_month), max(inj_by_month)
        mwsolar_ct = get_mwsolar_map(ym_from, ym_to)
        weighted_sum = 0.0
        weighted_kwh = 0
        simple_rates: list[float] = []
        matched_months = 0
        for ym in sorted(inj_by_month):
            kwh = inj_by_month[ym]
            ct = mwsolar_ct.get(ym)
            if ct is None:
                continue
            rate = supplier_injection_eur_per_kwh(
                ct, supplier_id, factors={supplier_id: factor}
            )
            simple_rates.append(rate)
            if kwh > 0:
                weighted_sum += kwh * rate
                weighted_kwh += kwh
                matched_months += 1
        if weighted_kwh > 0:
            vol_avg = weighted_sum / weighted_kwh
            simple_avg = sum(simple_rates) / len(simple_rates) if simple_rates else None
            return {
                "eur_per_kwh": round(vol_avg, 5),
                "source": (
                    f"moyenne pondérée injection Marin–Midi "
                    f"depuis {from_date[:7]}"
                ),
                "months": matched_months,
                "injection_kwh": int(weighted_kwh),
                "simple_avg_eur_per_kwh": (
                    round(simple_avg, 5) if simple_avg is not None else None
                ),
                "mwsolar_factor": factor,
                "fallback": False,
                "volume_weighted": True,
            }

    simple = _simple_annual_injection_rate(ref_date, supplier_id, factor)
    if simple:
        return {
            **simple,
            "mwsolar_factor": factor,
            "fallback": False,
            "simple_avg_eur_per_kwh": simple["eur_per_kwh"],
        }

    return {
        "eur_per_kwh": fallback,
        "source": "repli config",
        "months": 0,
        "mwsolar_factor": factor,
        "fallback": True,
        "volume_weighted": False,
    }


def _producer_share_economics(
    cel_kwh: int, injection_rate: float
) -> dict[str, float | None]:
    """Gain producteur sur partage CEL : dû CEL − équivalent injection."""
    if cel_kwh <= 0:
        return {"cel_due_eur": None, "injection_equiv_eur": None, "gain_eur": None}
    cel_due = _round_eur(cel_kwh * _overview_cel_share_rate())
    injection_equiv = _round_eur(cel_kwh * injection_rate)
    return {
        "cel_due_eur": cel_due,
        "injection_equiv_eur": injection_equiv,
        "gain_eur": _round_eur(cel_due - injection_equiv),
    }


def _production_card_economics(
    cel_kwh: int, injection_ctx: dict[str, Any]
) -> dict[str, Any]:
    rate = float(injection_ctx["eur_per_kwh"])
    return {
        **_producer_share_economics(cel_kwh, rate),
        "eur_mode": "producer_share",
    }


def _build_applied_rates(injection_ctx: dict[str, Any]) -> dict[str, Any]:
    cel_ct = round(_overview_cel_share_rate() * 100, 2)
    rates: dict[str, Any] = {
        "cel_ct_per_kwh": cel_ct,
        "cel_label": "Partage CEL",
    }
    inj_eur = injection_ctx.get("eur_per_kwh")
    if inj_eur is not None:
        rates["injection_ct_per_kwh"] = round(float(inj_eur) * 100, 2)
        rates["injection_label"] = (
            "Injection (pondérée)"
            if injection_ctx.get("volume_weighted")
            else "Injection (estimée)"
        )
    return rates


def _short_member_label(roof: dict[str, Any]) -> str:
    if roof.get("short_label"):
        return str(roof["short_label"])
    title = roof.get("title", "")
    m = re.search(r"entrée\s+(\S)", title, re.IGNORECASE)
    if m:
        letter = m.group(1).upper()
        return f"Entrée {letter}"
    if title.lower().startswith("eschville"):
        rest = re.sub(r"^eschville\s*", "", title, flags=re.IGNORECASE).strip()
        rest = re.sub(r"\s*sàrl\s*$", "", rest, flags=re.IGNORECASE).strip()
        return rest or title
    return title.split("—")[0].strip() if "—" in title else title


def _attach_pct(cards: list[dict[str, Any]], total_id: str) -> None:
    by_id = {c["id"]: c for c in cards}
    base = int(by_id.get(total_id, {}).get("total") or 0)
    part_cards: list[dict[str, Any]] = []

    for c in cards:
        if c["id"] == total_id:
            c["pct"] = 100
        elif base > 0:
            c["pct"] = int(round(100 * int(c.get("total") or 0) / base))
            part_cards.append(c)
        else:
            c["pct"] = None

    if not part_cards or base <= 0:
        return

    diff = 100 - sum(int(c["pct"] or 0) for c in part_cards)
    if diff != 0:
        idx = max(range(len(part_cards)), key=lambda i: int(part_cards[i].get("total") or 0))
        part_cards[idx]["pct"] = int(part_cards[idx]["pct"] or 0) + diff


def _is_aggregate_production_id(series_id: str) -> bool:
    return series_id in ("prod_active",) or series_id.endswith("_active")


def enrich_consumption_for_dashboard(
    series: list[dict[str, Any]],
    self_consumption: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """
    Ajoute consommation totale calculée (nette Leneda + autoconsommation PV)
    et retire la série « marché » dupliquée sur le POD ELEC.
    """
    config = load_config()
    roofs = config.get("consumption_overviews") or []
    if not roofs or not self_consumption:
        return series

    by_id = _series_by_id(series)
    sc_by_roof = {r.get("roof_id"): r for r in self_consumption.get("by_roof") or []}
    hide_ids: set[str] = set()
    additions: list[dict[str, Any]] = []

    for roof in roofs:
        roof_id = roof.get("roof_id")
        if not roof_id:
            continue
        net_id = roof.get("net_id") or roof.get("total_id")
        grid_id = roof.get("grid_id")
        net_s = by_id.get(net_id or "")
        grid_s = by_id.get(grid_id or "")
        sc = sc_by_roof.get(roof_id)
        if not net_s or not sc:
            continue

        net_pts = _points_by_label(net_s)
        ac_pts = _points_by_label(sc)
        if grid_s:
            net_t = int(net_s.get("total") or 0)
            grid_t = int(grid_s.get("total") or 0)
            if net_t > 0 and abs(net_t - grid_t) <= max(1, int(0.001 * net_t)):
                hide_ids.add(grid_id)

        labels = sorted(set(net_pts) | set(ac_pts))
        bucket_starts: dict[str, str] = {}
        for src in (net_s, sc):
            for p in src.get("points") or []:
                bucket_starts[p["date"]] = p.get("bucket_start") or p["date"]

        points: list[dict[str, Any]] = []
        for label in labels:
            net_v = net_pts.get(label, 0)
            ac_v = ac_pts.get(label, 0)
            value = net_v + ac_v
            points.append(
                {
                    "date": label,
                    "bucket_start": bucket_starts.get(label, label),
                    "value": value,
                    "empty": value == 0 and net_v == 0 and ac_v == 0,
                }
            )

        title = roof.get("title", roof_id)
        short = title.split("—")[0].strip() if "—" in title else roof_id.capitalize()
        additions.append(
            {
                "id": f"cons_{roof_id}_total_calc",
                "label": f"{short} — consommation totale (calculée)",
                "group": "consumption",
                "unit": "kWh",
                "color": "#0f766e",
                "points": points,
                "total": sum(int(p.get("value") or 0) for p in points),
                "derived": True,
            }
        )

    out = [s for s in series if s.get("id") not in hide_ids]
    return out + additions


def consumption_charts_series(leneda_series: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Séries consommation affichées en graphiques (exclut celles avec chart: false dans config)."""
    hidden: set[str] = set()
    for spec in load_config().get("series") or []:
        if spec.get("group") == "consumption" and spec.get("chart") is False:
            hidden.add(spec["id"])
    return [
        s
        for s in leneda_series
        if s.get("group") == "consumption" and s.get("id") not in hidden
    ]


def _chart_meter_short_label(chart_label: str) -> str:
    m = re.search(r"entrée\s+(\S)", chart_label, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    return chart_label.strip()


def _multi_meter_chart_meta(series_list: list[dict[str, Any]]) -> str:
    """Détail kWh par compteur pour le sous-titre du graphique."""
    parts: list[str] = []
    for series in series_list:
        kwh = int(series.get("total") or 0)
        label = _chart_meter_short_label(series.get("chart_label") or series.get("label") or "?")
        parts.append(f"{label} {kwh}")
    return " · ".join(parts)


def _chart_series_entry(
    series_by_id: dict[str, dict[str, Any]],
    series_id: str,
    chart_label: str,
) -> dict[str, Any] | None:
    entry = series_by_id.get(series_id)
    if not entry:
        return None
    return {**entry, "chart_label": chart_label}


def build_chart_groups(
    leneda_series: list[dict[str, Any]],
    enphase_series: dict[str, Any] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """
    Graphiques consolidés : 4 conso (Jacoby, L&M, Eschville×4, Levant×4) + production Marin–Midi.
    Métrique conso et production : part CEL uniquement (1-65:2.29.2) — pas le partage ACR.
    """
    _ = enphase_series
    config = load_config()
    cons_roofs = config.get("consumption_overviews") or []
    prod_roofs = config.get("production_overviews") or []
    by_id = _series_by_id(leneda_series)

    consumption: list[dict[str, Any]] = []
    production: list[dict[str, Any]] = []

    def member_title(roof: dict[str, Any]) -> str:
        title = roof.get("title", "")
        return title.split("—")[0].strip() if "—" in title else title

    for roof_id in ("lm_sci", "jacoby"):
        roof = next((r for r in cons_roofs if r.get("roof_id") == roof_id), None)
        if not roof:
            continue
        entry = _chart_series_entry(by_id, roof["cel_id"], "Énergie partagée (CEL)")
        if entry:
            consumption.append(
                {
                    "id": f"chart_{roof_id}",
                    "title": member_title(roof),
                    "multi": False,
                    "layout": "half",
                    "series": [entry],
                }
            )

    levant_roofs = [r for r in cons_roofs if str(r.get("roof_id", "")).startswith("levant_")]
    levant_series = [
        s
        for r in levant_roofs
        if (s := _chart_series_entry(by_id, r["cel_id"], _short_member_label(r)))
    ]
    if levant_series:
        consumption.append(
            {
                "id": "chart_levant",
                "title": "Levant — compteurs communs",
                "meta_breakdown": _multi_meter_chart_meta(levant_series),
                "multi": True,
                "layout": "wide",
                "series": levant_series,
            }
        )

    eschville_roofs = [
        r for r in cons_roofs if str(r.get("roof_id", "")).startswith("eschville_")
    ]
    eschville_series = [
        s
        for r in eschville_roofs
        if (s := _chart_series_entry(by_id, r["cel_id"], _short_member_label(r)))
    ]
    if eschville_series:
        consumption.append(
            {
                "id": "chart_eschville",
                "title": "Eschville",
                "meta_breakdown": _multi_meter_chart_meta(eschville_series),
                "multi": True,
                "layout": "wide",
                "series": eschville_series,
            }
        )

    prod_labels = {"marin": "Partage Marin", "midi": "Partage Midi"}
    for roof in prod_roofs:
        roof_id = roof.get("roof_id", "")
        entry = _cel_production_series(
            roof,
            by_id,
            prod_labels.get(roof_id, f"Partage {roof_id}"),
        )
        if entry:
            production.append(
                {
                    "id": f"chart_prod_{roof_id}",
                    "title": prod_labels.get(roof_id, f"Partage {roof_id}"),
                    "multi": False,
                    "layout": "half",
                    "series": [entry],
                }
            )

    return {"consumption": consumption, "production": production}


def _cel_production_kwh(
    roof: dict[str, Any],
    leneda_series: list[dict[str, Any]],
) -> int:
    """Partage CEL exporté par le POD production (1-65:2.29.2) — pas l'ACR (2.29.3).

    Contrôle interne (non affiché) : somme des cons_*_cel (membres) ≈ somme prod_*_shared_cel.
    """
    return _total_from_series(leneda_series, roof["shared_cel_id"])


def _cel_production_series(
    roof: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    chart_label: str,
) -> dict[str, Any] | None:
    return _chart_series_entry(by_id, roof["shared_cel_id"], chart_label)


def production_charts_series(
    leneda_series: list[dict[str, Any]],
    enphase_series: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Charts: Enphase total + Leneda breakdown, or all production series."""
    prod = [
        s
        for s in leneda_series
        if s.get("group") == "production" and not _is_aggregate_production_id(s["id"])
    ]
    if enphase_series and enphase_series.get("points"):
        total_chart = {
            **enphase_series,
            "id": "prod_total_enphase",
            "label": "Production totale (Enphase)",
            "group": "production",
            "obis": "Enphase",
            "color": enphase_series.get("color", "#ca8a04"),
        }
        return [total_chart, *prod]
    return [s for s in leneda_series if s.get("group") == "production"]


def _consumption_net_kwh(
    roof: dict[str, Any],
    totals: dict[str, Any],
    leneda_series: list[dict[str, Any]] | None = None,
) -> tuple[int, int, int]:
    """kWh par flux : net (1.29.0, souvent invisible admin CEL), part CEL, réseau."""
    net_id = roof.get("net_id") or roof.get("total_id")
    cel_id = roof["cel_id"]
    grid_id = roof["grid_id"]
    cons_net = _total_from_summary(totals, net_id)
    if leneda_series:
        cel = _total_from_series(leneda_series, cel_id)
        grid = _total_from_series(leneda_series, grid_id)
    else:
        cel = _total_from_summary(totals, cel_id)
        grid = _total_from_summary(totals, grid_id)
    if cons_net == 0 and grid > 0:
        cons_net = grid
    if cons_net == 0 and cel > 0:
        cons_net = max(0, cel)
    return cons_net, cel, grid


def _consumption_simple_card(
    roof: dict[str, Any],
    totals: dict[str, Any],
    leneda_series: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Carte unique : énergie partagée CEL (kWh) + valeur estimée en €."""
    _cons_net, cel, _grid = _consumption_net_kwh(roof, totals, leneda_series)
    year = date.today().year
    return {
        "id": f"shared_{roof.get('roof_id', 'roof')}",
        "label": "Énergie partagée (CEL)",
        "total": cel,
        "small": True,
        **_consumption_card_economics(cel, year),
    }


def _compact_consumption_group(
    group_id: str,
    title: str,
    roofs: list[dict[str, Any]],
    totals: dict[str, Any],
    leneda_series: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    cards: list[dict[str, Any]] = []
    for roof in roofs:
        _cons_net, cel, _grid = _consumption_net_kwh(roof, totals, leneda_series)
        cards.append(
            {
                "id": f"shared_{roof.get('roof_id', 'roof')}",
                "label": _short_member_label(roof),
                "total": cel,
                "small": True,
                **_consumption_card_economics(cel),
            }
        )
    return {
        "id": group_id,
        "title": title,
        "layout": "compact_grid",
        "cards": cards,
    }


def _consumption_simple_group(
    roof: dict[str, Any],
    totals: dict[str, Any],
    leneda_series: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    title = roof.get("title", "Consommation")
    if "—" in title:
        title = title.split("—")[0].strip()
    return {
        "id": f"consumption_{roof.get('roof_id', 'roof')}",
        "title": title,
        "layout": "simple",
        "cards": [_consumption_simple_card(roof, totals, leneda_series)],
    }


def _production_simple_card(
    roof: dict[str, Any],
    totals: dict[str, Any],
    self_consumption: dict[str, Any],
    reconcile: bool,
    leneda_series: list[dict[str, Any]],
    series_by_id: dict[str, dict[str, Any]] | None = None,
    label: str | None = None,
    injection_ctx: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Partage CEL production (kWh) + dû CEL, injection estimée et gain."""
    cel_kwh = _cel_production_kwh(roof, leneda_series)
    card_label = label or roof.get("title", "Partage").split("—")[0].strip()
    card: dict[str, Any] = {
        "id": f"prod_{roof.get('roof_id', 'roof')}",
        "label": card_label,
        "total": cel_kwh,
        "small": True,
    }
    if injection_ctx:
        card.update(_production_card_economics(cel_kwh, injection_ctx))
    else:
        card.update(
            {
                "eur": _eur_from_production_kwh(cel_kwh),
                "eur_mode": "producer",
                "eur_label": "CEL est.",
            }
        )
    return card


_CEL_BALANCE_CONTEXT_NOTES = [
    "Eschville (4 compteurs) : ~25–30 logements — base résidentielle assez stable sur ~7 jours.",
    "Jacoby et L&M SCI : bureaux — consommation quasi nulle le week-end.",
    "Parties communes Levant (C–F) : profil distinct des logements et des bureaux.",
    "L'injection résiduelle est une marge théorique : un nouveau membre doit consommer en "
    "phase avec le soleil (coïncidence horaire).",
    "Pour dimensionner de nouveaux membres : privilégier printemps ou automne, sur ≥ 1 mois "
    "(idéalement 3).",
]


def _total_cel_consumption_kwh(
    cons_roofs: list[dict[str, Any]],
    leneda_series: list[dict[str, Any]],
) -> int:
    total = 0
    for roof in cons_roofs:
        cel_id = roof.get("cel_id")
        if cel_id:
            total += _total_from_series(leneda_series, cel_id)
    return total


def _cel_balance_reliability(start_date: str | None, end_date: str | None) -> dict[str, Any]:
    """Indicateur de fiabilité saisonnière pour estimer place CEL / injection."""
    if not start_date or not end_date:
        return {
            "level": "unknown",
            "title": "Fiabilité saisonnière",
            "summary": "Période non définie.",
            "details": [],
        }

    try:
        start = date.fromisoformat(start_date[:10])
        end = date.fromisoformat(end_date[:10])
    except ValueError:
        return {
            "level": "unknown",
            "title": "Fiabilité saisonnière",
            "summary": "Période invalide.",
            "details": [],
        }

    if end < start:
        start, end = end, start

    days = (end - start).days + 1
    months: set[int] = set()
    cursor = start
    while cursor <= end:
        months.add(cursor.month)
        cursor += timedelta(days=1)

    spring_autumn = {3, 4, 5, 9, 10, 11}
    summer_holiday = {7, 8}
    peak_summer = {6, 7, 8}

    details: list[str] = []
    level = "good"

    if days < 28:
        level = "caution"
        details.append(
            f"Période courte ({days} j.) — tendance indicative ; viser ≥ 1 mois après "
            "stabilisation des 10 PODs."
        )

    if months & spring_autumn:
        details.append(
            "Printemps / automne : profils proches d'une année type (hors congés estivaux) — "
            "période la plus fiable pour estimer partage vs injection."
        )

    if months & summer_holiday:
        if level == "good":
            level = "caution"
        details.append(
            "Été (juil.–août) : congés → baisse de conso résidentielle ; l'injection peut "
            "être surévaluée comme « marge disponible »."
        )

    if months and months <= peak_summer:
        if level == "good":
            level = "caution"
        details.append(
            "Période estivale seule : surplus solaire élevé mais demande atypique — ne pas "
            "extrapoler à l'année."
        )

    if months & {12, 1, 2} and not (months & spring_autumn):
        if level == "good":
            level = "caution"
        details.append(
            "Hiver : production PV basse — le taux CEL peut sembler faible alors que la "
            "contrainte est plutôt côté production."
        )

    if not details:
        details.append(
            "Comparer de préférence des mois de mars–mai ou sept.–nov. sur au moins 4 semaines."
        )

    summary_by_level = {
        "good": "Période globalement représentative pour estimer partage CEL et injection.",
        "caution": "Interpréter avec prudence — biais saisonnier ou période trop courte.",
        "unknown": "Fiabilité non évaluée.",
    }

    return {
        "level": level,
        "title": "Fiabilité saisonnière",
        "summary": summary_by_level.get(level, summary_by_level["unknown"]),
        "details": details,
    }


def _build_cel_balance(
    leneda_series: list[dict[str, Any]],
    cons_roofs: list[dict[str, Any]],
    prod_roofs: list[dict[str, Any]],
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any] | None:
    """Synthèse partage CEL vs injection pour évaluer la marge communautaire."""
    if not prod_roofs:
        return None

    cel_prod = sum(_cel_production_kwh(r, leneda_series) for r in prod_roofs)
    injection = sum(
        _total_from_series(leneda_series, r["market_id"]) for r in prod_roofs
    )
    cel_cons = _total_cel_consumption_kwh(cons_roofs, leneda_series)
    surplus = cel_prod + injection
    utilization_pct = round(100 * cel_prod / surplus) if surplus > 0 else None
    headroom_pct = round(100 * injection / surplus) if surplus > 0 else None
    reconcile_delta = cel_prod - cel_cons

    metrics: list[dict[str, Any]] = [
        {
            "id": "cel_prod",
            "label": "Partage CEL (Marin–Midi)",
            "kwh": cel_prod,
            "share_pct": utilization_pct,
            "hint": "Énergie partagée sortie des POD PV (1-65:2.29.2). "
            "% = part du surplus PV alloué (partage CEL + injection).",
        },
        {
            "id": "injection",
            "label": "Injection résiduelle",
            "kwh": injection,
            "share_pct": headroom_pct,
            "hint": "Surplus injecté au réseau (1-65:2.29.9). "
            "% = part du surplus PV alloué (partage CEL + injection).",
        },
        {
            "id": "cel_cons",
            "label": "Réception membres (CEL)",
            "kwh": cel_cons,
            "hint": "Somme des parts CEL consommées (1-65:1.29.2) — contrôle Leneda.",
        },
    ]

    notes = list(_CEL_BALANCE_CONTEXT_NOTES)
    if surplus > 0 and headroom_pct is not None:
        notes.insert(
            0,
            f"Sur la période : {utilization_pct} % du surplus PV partagé en CEL, "
            f"{headroom_pct} % injecté.",
        )
    if abs(reconcile_delta) > max(5, int(0.02 * max(cel_prod, cel_cons, 1))):
        notes.append(
            f"Écart prod./réception : {reconcile_delta:+,} kWh "
            "(décalage temporel Leneda ou POD pas encore actifs sur toute la période)."
            .replace(",", "\u202f")
        )

    return {
        "title": "Bilan CEL — capacité & injection",
        "metrics": metrics,
        "reliability": _cel_balance_reliability(start_date, end_date),
        "notes": notes,
    }


def _marin_midi_production_group(
    prod_roofs: list[dict[str, Any]],
    totals: dict[str, Any],
    self_consumption: dict[str, Any],
    reconcile: bool,
    leneda_series: list[dict[str, Any]],
    series_by_id: dict[str, dict[str, Any]] | None = None,
    injection_ctx: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cards: list[dict[str, Any]] = []
    labels = {"marin": "Partage Marin", "midi": "Partage Midi"}
    for roof in prod_roofs:
        card = _production_simple_card(
            roof,
            totals,
            self_consumption,
            reconcile,
            leneda_series,
            series_by_id,
            label=labels.get(roof.get("roof_id", ""), None),
            injection_ctx=injection_ctx,
        )
        cards.append(card)
    return {
        "id": "production_marin_midi",
        "title": "Marin–Midi",
        "layout": "compact_grid",
        "cards": cards,
    }


def _consumption_pair_group(
    roofs: list[dict[str, Any]],
    totals: dict[str, Any],
    leneda_series: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = []
    for roof in roofs:
        title = roof.get("title", "Consommation")
        if "—" in title:
            title = title.split("—")[0].strip()
        blocks.append(
            {
                "title": title,
                "cards": [_consumption_simple_card(roof, totals, leneda_series)],
            }
        )
    return {
        "id": "consumption_lm_jacoby",
        "title": "",
        "layout": "pair_row",
        "blocks": blocks,
    }


def build_overview(
    leneda_summary: dict[str, Any],
    leneda_series: list[dict[str, Any]],
    enphase_series: dict[str, Any] | None,
    self_consumption: dict[str, Any],
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    summary = leneda_summary or {}
    totals = summary.get("totals_by_id") or {}
    config = load_config()
    cons_roofs = config.get("consumption_overviews") or []
    prod_roofs = config.get("production_overviews") or []
    series_by_id = _series_by_id(leneda_series)

    groups: list[dict[str, Any]] = []
    reconcile = summary.get("reconcile_equations", True)

    levant_roofs = [r for r in cons_roofs if str(r.get("roof_id", "")).startswith("levant_")]
    eschville_roofs = [r for r in cons_roofs if str(r.get("roof_id", "")).startswith("eschville_")]
    pair_roofs = [
        r
        for roof_id in ("lm_sci", "jacoby")
        for r in cons_roofs
        if r.get("roof_id") == roof_id
    ]

    if levant_roofs:
        groups.append(
            _compact_consumption_group(
                "levant_commons",
                "Levant — compteurs communs",
                levant_roofs,
                totals,
                leneda_series,
            )
        )

    if pair_roofs:
        groups.append(_consumption_pair_group(pair_roofs, totals, leneda_series))

    if eschville_roofs:
        groups.append(
            _compact_consumption_group(
                "eschville",
                "Eschville",
                eschville_roofs,
                totals,
                leneda_series,
            )
        )

    if prod_roofs:
        injection_rate_ctx = _overview_injection_rate(end_date)
        groups.append(
            _marin_midi_production_group(
                prod_roofs,
                totals,
                self_consumption,
                reconcile,
                leneda_series,
                series_by_id,
                injection_rate_ctx,
            )
        )
    else:
        injection_rate_ctx = _overview_injection_rate(end_date)

    if not prod_roofs:
        autocons = int(self_consumption.get("total") or 0)
        l2 = _total_from_series(leneda_series, "prod_shared_l2")
        cel = _total_from_series(leneda_series, "prod_shared_cel")
        export = _total_from_series(leneda_series, "prod_market")

        if enphase_series and enphase_series.get("points") is not None:
            prod_total = int(enphase_series.get("total") or 0)
        else:
            prod_total = _total_from_series(leneda_series, "prod_active")
            parts_sum = autocons + l2 + cel + export
            if prod_total == 0 and parts_sum > 0:
                prod_total = parts_sum

        if reconcile and prod_total > 0:
            autocons = max(0, prod_total - l2 - cel - export)

        prod_cards = [
            {"id": "prod_total", "label": "Production totale", "total": prod_total},
            {"id": "self_consumption", "label": "Autoconsommation", "total": autocons},
            {"id": "prod_shared_l2", "label": "Partage AC1", "total": l2},
            {"id": "prod_shared_cel", "label": "Partage CEL", "total": cel},
            {"id": "prod_market", "label": "Injection (marché)", "total": export},
        ]
        if prod_total > 0:
            _attach_pct(prod_cards, "prod_total")

        prod_row = {
            "ids": [
                "prod_total",
                "self_consumption",
                "prod_shared_l2",
                "prod_shared_cel",
                "prod_market",
            ],
            "operators": ["=", "+", "+", "+"],
        }
        groups.append(
            {
                "id": "production",
                "title": "Production (Leneda / Enphase)",
                "equation": prod_row,
                "cards": prod_cards,
            }
        )

    hint_parts: list[str] = []
    if summary.get("reconcile_equations") is False:
        hint_parts.append(
            "Période antérieure à une ou plusieurs mises en service — les totaux partiels "
            "ne sont pas forcés à égaler les totaux."
        )
    else:
        hint_parts.append(
            "Marché (conso et injection) : totaux API Leneda bruts, comme le tableau « Flux d'énergie »."
        )
        if prod_roofs:
            hint_parts.append(
                "Marin–Midi : partage CEL uniquement (1-65:2.29.2) — pas l'ACR ni la production "
                "totale onduleur."
            )
        else:
            hint_parts.append(
                "Production : partages Leneda (CEL) — pas la production totale onduleur."
            )
    if summary.get("derived_grid_fill"):
        hint_parts.append(
            "Reste fournisseur : périodes sans 1.29.9 chez Leneda complétées (consommation − CEL)."
        )
    if summary.get("fusion_solar_overlay"):
        hint_parts.append(
            "Production : données onduleur FusionSolar (Huawei) à la place du total Leneda "
            "1-1:2.29.0 — partages et injection restent Leneda."
        )
    elif summary.get("manual_production_overlay"):
        hint_parts.append(
            "Production : valeurs saisies (SQLite / Excel) à la place du total Leneda sur les mois "
            "enregistrés — partages et injection restent Leneda."
        )
    member_cfg = load_config().get("member_economics") or {}
    prod_cfg = config.get("producer_economics") or {}
    cel_rate = _overview_cel_share_rate()
    supplier_rate = _overview_supplier_rate()
    if prod_roofs and injection_rate_ctx:
        inj_rate = float(injection_rate_ctx["eur_per_kwh"])
        pct = int(round(100 * injection_rate_ctx.get("mwsolar_factor", 0.9)))
        months = injection_rate_ctx.get("months") or 0
        inj_kwh = injection_rate_ctx.get("injection_kwh")
        months_suffix = f", {months} mois" if months else ""
        kwh_suffix = f", {inj_kwh:,} kWh inj.".replace(",", "\u202f") if inj_kwh else ""
        prod_eur_hint = (
            f"Partage Marin–Midi : gain = kWh × tarif CEL ({cel_rate:.2f} €) "
            f"− kWh × tarif injection ({inj_rate:.3f} €, "
            f"{injection_rate_ctx.get('source', '')}{months_suffix}{kwh_suffix}, {pct} % MW Solar)."
        )
        simple_avg = injection_rate_ctx.get("simple_avg_eur_per_kwh")
        if injection_rate_ctx.get("volume_weighted") and simple_avg is not None:
            prod_eur_hint += (
                f" Moyenne simple MW Solar sur la même période : {simple_avg:.3f} €/kWh "
                "(surestime le gain car les mois à fort tarif ont peu d'injection)."
            )
    elif prod_roofs:
        prod_eur_hint = (
            f"Partage CEL Marin–Midi (1-65:2.29.2) : kWh × tarif CEL ({cel_rate:.2f} €/kWh)."
        )
    else:
        prod_eur_hint = f"Production : kWh × tarif CEL ({cel_rate:.2f} €/kWh), arrondi à l'euro."
    hint_parts.append(
        f"Montants conso : équivalent fournisseur − dû CEL = économie nette "
        f"(kWh CEL × {supplier_rate:.3f} € − kWh × {cel_rate:.3f} €). "
        f"{prod_eur_hint}"
    )
    ref_id = member_cfg.get("reference_supplier", "sudstroum")
    ref_label = (config.get("suppliers") or {}).get(ref_id, {}).get("label", ref_id)
    hint_parts.append(
        f"Tarif fournisseur d'estimation unique ({ref_label}) pour tous les membres — "
        "Sudénergie, Enovos, etc. diffèrent légèrement ; non détaillé ici."
    )
    if member_cfg.get("note"):
        hint_parts.append(str(member_cfg["note"]))
    if prod_cfg.get("note"):
        hint_parts.append(str(prod_cfg["note"]))
    for roof in cons_roofs:
        cons_net, cel, _grid = _consumption_net_kwh(roof, totals, leneda_series)
        label = roof.get("title", "Compteur").split("—")[0].strip()
        if cel > 0:
            hint_parts.append(
                f"{label} : part CEL {cel:,} kWh (1-65:1.29.2)."
                .replace(",", " ")
            )
        elif cons_net > 0:
            hint_parts.append(
                f"{label} : soutirage Leneda (1.29.0) {cons_net:,} kWh — CEL + réseau."
                .replace(",", " ")
            )
    if summary.get("derived_consumption_fill"):
        hint_parts.append(
            "Consommation : estimée (production − injection) sur le POD onemeter — "
            "pas encore de 1-1:1.29.0 via l’API Leneda."
        )

    cel_balance = _build_cel_balance(
        leneda_series,
        cons_roofs,
        prod_roofs,
        start_date=start_date,
        end_date=end_date,
    )

    return {
        "groups": groups,
        "cel_balance": cel_balance,
        "hint": " ".join(hint_parts),
        "period_notes": summary.get("period_notes") or [],
        "formula": self_consumption.get("formula", ""),
        "applied_rates": _build_applied_rates(injection_rate_ctx),
    }
