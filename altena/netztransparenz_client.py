"""Netztransparenz WebAPI — monthly MW Solar (MWSOLAR) for injection tariffs."""

from __future__ import annotations

import csv
import io
import json
import os
import re
from datetime import date, datetime
from typing import Any

import requests

from altena.paths import SECRETS_PATH

TOKEN_URL_DEFAULT = "https://identity.netztransparenz.de/users/connect/token"
API_BASE_DEFAULT = "https://ds.netztransparenz.de/api/v1"

MW_SOLAR_COLUMN = "MW Solar in ct/kWh"


def load_netztransparenz_config() -> dict[str, Any]:
    if not os.path.isfile(SECRETS_PATH):
        raise FileNotFoundError(f"Missing {SECRETS_PATH}")
    with open(SECRETS_PATH, encoding="utf-8") as f:
        data = json.load(f)
    ntp = data.get("netztransparenz")
    if not isinstance(ntp, dict):
        raise ValueError('secrets.json: add a "netztransparenz" block — see secrets.example.json')
    client_id = (ntp.get("client_id") or "").strip()
    client_secret = (ntp.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        raise ValueError("netztransparenz.client_id and client_secret must be non-empty")
    if len(client_id) < 10:
        raise ValueError(
            "netztransparenz.client_id looks too short (e.g. 'FK21' is a label). "
            "Use the full Client ID from the Netztransparenz Extranet OAuth Manager "
            "(often starts with cm_app_ntp_id)."
        )
    return ntp


def fetch_access_token(ntp: dict[str, Any] | None = None) -> str:
    cfg = ntp or load_netztransparenz_config()
    token_url = (cfg.get("token_url") or TOKEN_URL_DEFAULT).strip()
    response = requests.post(
        token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": cfg["client_id"].strip(),
            "client_secret": cfg["client_secret"].strip(),
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=60,
    )
    if not response.ok:
        detail = response.text[:300]
        if response.status_code == 400 and "invalid_client" in detail:
            raise ValueError(
                "Netztransparenz authentication failed (invalid_client). "
                "Check client_id and client_secret in secrets.json — use the full "
                "Client ID from the Extranet OAuth Manager, not a short display name."
            ) from None
        response.raise_for_status()
    token = (response.json().get("access_token") or "").strip()
    if not token:
        raise ValueError("Netztransparenz token response had no access_token")
    return token


def _api_base(ntp: dict[str, Any]) -> str:
    return (ntp.get("api_base") or API_BASE_DEFAULT).rstrip("/")


def fetch_marktpraemie_csv(
    month_from: int,
    year_from: int,
    month_to: int,
    year_to: int,
    *,
    ntp: dict[str, Any] | None = None,
    token: str | None = None,
) -> str:
    """GET /data/marktpraemie/{m}/{y}/{m}/{y} — monthly market values (Format 12)."""
    cfg = ntp or load_netztransparenz_config()
    access = token or fetch_access_token(cfg)
    url = (
        f"{_api_base(cfg)}/data/marktpraemie/"
        f"{month_from}/{year_from}/{month_to}/{year_to}"
    )
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {access}"},
        timeout=60,
    )
    response.raise_for_status()
    return response.text


def _parse_month_label(label: str) -> tuple[int, int] | None:
    """'1/2024' or '01/2024' -> (2024, 1)."""
    m = re.match(r"^(\d{1,2})/(\d{4})$", (label or "").strip())
    if not m:
        return None
    month, year = int(m.group(1)), int(m.group(2))
    if 1 <= month <= 12:
        return year, month
    return None


def _parse_ct_value(raw: str) -> float | None:
    s = (raw or "").strip().replace(",", ".")
    if not s or s.upper() in ("N.A.", "NA", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_mw_solar_rows(csv_text: str) -> list[dict[str, Any]]:
    """Extract year_month + MW Solar (ct/kWh) from marktpraemie CSV."""
    reader = csv.DictReader(io.StringIO(csv_text), delimiter=";")
    if not reader.fieldnames:
        return []
    solar_col = next((c for c in reader.fieldnames if "MW Solar" in c), None)
    if not solar_col:
        raise ValueError(f"Column {MW_SOLAR_COLUMN!r} not found in API CSV")
    month_col = reader.fieldnames[0]

    rows: list[dict[str, Any]] = []
    for line in reader:
        label = (line.get(month_col) or "").strip()
        parsed = _parse_month_label(label)
        if not parsed:
            continue
        year, month = parsed
        ct = _parse_ct_value(line.get(solar_col) or "")
        if ct is None:
            continue
        rows.append(
            {
                "year_month": f"{year}-{month:02d}",
                "mw_solar_ct_per_kwh": ct,
                "label": label,
            }
        )
    return rows


def fetch_mw_solar_monthly(
    start: date,
    end: date,
    *,
    ntp: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """MW Solar monthly values between start and end (inclusive, by month)."""
    if (start.year, start.month) > (end.year, end.month):
        return []
    cfg = ntp or load_netztransparenz_config()
    token = fetch_access_token(cfg)
    csv_text = fetch_marktpraemie_csv(
        start.month,
        start.year,
        end.month,
        end.year,
        ntp=cfg,
        token=token,
    )
    return parse_mw_solar_rows(csv_text)


def supplier_injection_eur_per_kwh(
    mw_solar_ct_per_kwh: float,
    supplier: str,
    factors: dict[str, float] | None = None,
) -> float:
    """Sudstroum 90 %, Enovos 80 % of MW Solar (ct/kWh → €/kWh)."""
    fac = factors or {}
    key = supplier.strip().lower()
    if key in ("sudstroum", "sud", "stroum"):
        factor = float(fac.get("sudstroum", 0.90))
    elif key in ("enovos", "eno"):
        factor = float(fac.get("enovos", 0.80))
    else:
        raise ValueError(f"Unknown supplier {supplier!r} (use sudstroum or enovos)")
    return round((mw_solar_ct_per_kwh / 100.0) * factor, 5)


def check_api_health(ntp: dict[str, Any] | None = None) -> str:
    cfg = ntp or load_netztransparenz_config()
    token = fetch_access_token(cfg)
    url = f"{_api_base(cfg)}/health"
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    response.raise_for_status()
    return response.text.strip()
