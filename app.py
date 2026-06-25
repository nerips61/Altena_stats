#!/usr/bin/env python3
"""Local home energy dashboard — Leneda, Enphase, derived self-consumption."""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import threading
import time
import urllib.request
from datetime import date
from typing import Any

from flask import Flask, jsonify, render_template, request

from altena.amortization import build_amortization
from altena.enphase_client import enphase_configured, fetch_production_series
from altena.fusion_solar_client import fusion_solar_enabled, sync_fusion_solar_monthly_backfill
from altena.leneda_client import fetch_all_series, leneda_accounts_status, load_config
from altena.metrics import (
    build_chart_groups,
    build_overview,
    compute_self_consumption,
    consumption_charts_series,
    enrich_consumption_for_dashboard,
    production_charts_series,
)
from altena.mwsolar_sync import sync_mwsolar_on_startup

logging.getLogger("werkzeug").setLevel(logging.ERROR)

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
app.config["TEMPLATES_AUTO_RELOAD"] = True


@app.after_request
def _no_cache_local(response):
    if request.path in ("/", "/amort") or request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


def _default_period() -> tuple[str, str]:
    today = date.today()
    config = load_config()
    op_from = (config.get("operational_from") or "").strip()
    if op_from:
        try:
            op_date = date.fromisoformat(op_from[:10])
            if today >= op_date:
                return op_from[:10], today.isoformat()
        except ValueError:
            pass
    start = today.replace(day=1)
    return start.isoformat(), today.isoformat()


def _render_dashboard(initial_view: str = "stats"):
    start, end = _default_period()
    config = load_config()
    am = config.get("amortization") or {}
    amort_enabled = bool(am)
    return render_template(
        "index.html",
        app_title=config.get("app_title", "Solarenergie fir Altena — énergie"),
        site_label=config.get("site_label", "Communauté énergétique locale"),
        start_date=start,
        end_date=end,
        operational_from=config.get("operational_from", "2026-06-12"),
        operational_note=config.get("operational_note", ""),
        injection_suppliers=config.get("suppliers") or {},
        default_injection_supplier=(am.get("injection_supplier") or "sudstroum"),
        amortization_enabled=amort_enabled,
        initial_view=initial_view if amort_enabled else "stats",
    )


@app.route("/ping")
def ping():
    return "ok\n", 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/")
def index():
    return _render_dashboard("stats")


@app.route("/amort")
def amort_page():
    return _render_dashboard("amort")


def _timeline_starts_from_series(series: list[dict[str, Any]]) -> dict[str, str]:
    starts: dict[str, str] = {}
    for s in series:
        for p in s.get("points") or []:
            label = p.get("date")
            if label:
                starts[label] = p.get("bucket_start") or label
    return starts


def fetch_dashboard(
    start_date: str,
    end_date: str,
    chart_aggregation: str,
) -> dict[str, Any]:
    payload = fetch_all_series(start_date, end_date, chart_aggregation=chart_aggregation)
    timeline = payload.get("timeline") or []
    timeline_starts = _timeline_starts_from_series(payload.get("series") or [])

    enphase_series = None
    enphase_error: str | None = None
    if enphase_configured():
        try:
            enphase_series = fetch_production_series(
                start_date,
                end_date,
                chart_aggregation,
                timeline,
                timeline_starts,
            )
        except Exception as exc:
            enphase_error = str(exc)
    else:
        enphase_error = (
            "Enphase non configuré (optionnel) — ce site utilise les deux POD PV Leneda."
        )

    self_consumption = compute_self_consumption(
        timeline,
        payload.get("series") or [],
        enphase_series,
    )
    payload["self_consumption"] = self_consumption
    payload["series"] = enrich_consumption_for_dashboard(
        payload.get("series") or [],
        self_consumption,
    )
    payload["overview"] = build_overview(
        payload.get("summary") or {},
        payload.get("series") or [],
        enphase_series,
        self_consumption,
        start_date=start_date,
        end_date=end_date,
    )
    payload["chart_groups"] = build_chart_groups(
        payload.get("series") or [],
        enphase_series,
    )
    payload["consumption_chart_series"] = consumption_charts_series(
        payload.get("series") or [],
    )
    payload["production_chart_series"] = production_charts_series(
        payload.get("series") or [],
        enphase_series,
    )
    payload["enphase"] = {
        "configured": enphase_configured(),
        "series": enphase_series,
        "error": enphase_error,
    }
    payload["leneda_accounts"] = leneda_accounts_status()
    payload["fusion_solar"] = {"enabled": fusion_solar_enabled()}
    try:
        from altena.manual_production import status_for_ui

        payload["manual_production"] = status_for_ui()
    except Exception:
        payload["manual_production"] = {"enabled": False, "count": 0}
    return payload


@app.route("/api/amortization")
def api_amortization():
    supplier = request.args.get("supplier", "").strip() or None
    try:
        return jsonify(build_amortization(supplier_id=supplier))
    except FileNotFoundError as exc:
        return jsonify({"enabled": True, "error": str(exc)}), 500
    except ValueError as exc:
        return jsonify({"enabled": True, "error": str(exc)}), 500
    except Exception as exc:
        return jsonify({"enabled": True, "error": str(exc)}), 500


@app.route("/api/data")
def api_data():
    start = request.args.get("start", "").strip()
    end = request.args.get("end", "").strip()
    if not start or not end:
        return jsonify({"error": "start and end query params required (YYYY-MM-DD)"}), 400
    if start > end:
        return jsonify({"error": "start must be on or before end"}), 400
    aggregation = request.args.get("aggregation", "Day").strip()
    if aggregation not in ("Day", "Week", "Month"):
        return jsonify({"error": "aggregation must be Day, Week, or Month"}), 400
    try:
        payload = fetch_dashboard(start, end, chart_aggregation=aggregation)
        return jsonify(payload)
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 500
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 500
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


def _local_lan_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return None


def _bind_host(config: dict[str, Any]) -> str:
    if config.get("lan_access", False):
        return "0.0.0.0"
    return str(config.get("bind_host") or "127.0.0.1")


def _open_browser(port: int) -> None:
    if os.environ.get("DASHBOARD_PORTAL") == "1":
        print("Portail unifié : navigateur non ouvert (iframe :8700).")
        return
    url = f"http://127.0.0.1:{port}/"
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}ping", timeout=1.5) as resp:
                if resp.status == 200:
                    break
        except OSError:
            time.sleep(0.25)
    else:
        print(f"Serveur lent — ouvrez manuellement : {url}")
    subprocess.run(["open", url], check=False)
    print(f"Opened browser → {url}")
    print("(Si la page reste vide : copier-coller l’URL dans Safari, pas Cursor.)")
    print("Leave this Terminal window open. Press Ctrl+C to stop the server.")


def _print_startup_urls(port: int, host: str, config: dict[str, Any]) -> None:
    title = config.get("app_title", "Marin–Midi")
    local = f"http://127.0.0.1:{port}/"
    print(f"{title} → {local}")
    if host != "127.0.0.1":
        lan_ip = _local_lan_ip()
        if lan_ip:
            print(f"iPhone / même Wi‑Fi → http://{lan_ip}:{port}/")
            print("  (n’utilisez pas 127.0.0.1 sur le téléphone)")
        else:
            print("iPhone / même Wi‑Fi : IP locale du Mac introuvable — Réglages → Réseau.")
        print("  Réseau local uniquement ; pas de mot de passe sur cette page.")


def _startup_mwsolar_sync() -> None:
    if os.environ.get("DASHBOARD_PORTAL") == "1":
        return
    try:
        config = load_config()
        if not config.get("mwsolar_auto_update", True):
            return
        result = sync_mwsolar_on_startup(config)
        msg = result.get("message")
        if msg:
            print(msg)
        elif result.get("skipped") and result.get("reason") == "up_to_date":
            print(f"MW Solar : cache à jour (jusqu'à {result.get('through', '?')}).")
    except Exception as exc:
        print(f"MW Solar sync: {exc}")


def _startup_fusion_solar_backfill() -> None:
    if os.environ.get("DASHBOARD_PORTAL") == "1":
        return
    try:
        config = load_config()
        fs_cfg = config.get("fusion_solar") or {}
        if fs_cfg.get("enabled") is False:
            return
        if not config.get("cache_enabled", True):
            return
        result = sync_fusion_solar_monthly_backfill()
        if result.get("skipped"):
            return
        for roof_id, info in (result.get("roofs") or {}).items():
            if info.get("error"):
                print(f"FusionSolar {roof_id}: {info['error']}")
                continue
            missing = info.get("missing_months") or []
            suffix = f", {len(missing)} mois manquants" if missing else ""
            print(
                f"FusionSolar {roof_id}: {info.get('months', 0)} mois en cache "
                f"({info.get('total', 0)} kWh{suffix})."
            )
    except Exception as exc:
        print(f"FusionSolar backfill: {exc}")


if __name__ == "__main__":
    config = load_config()
    port = int(config.get("port", 5070))
    host = _bind_host(config)
    _print_startup_urls(port, host, config)
    threading.Thread(target=_startup_mwsolar_sync, daemon=True).start()
    threading.Thread(target=_startup_fusion_solar_backfill, daemon=True).start()
    threading.Thread(target=_open_browser, args=(port,), daemon=True).start()
    app.run(host=host, port=port, debug=False, use_reloader=False)
