#!/usr/bin/env python3
"""
One-time Enphase OAuth setup — saves access_token + refresh_token into secrets.json.

Prerequisites:
  - Watt plan app created on https://developer-v4.enphase.com/
  - secrets.json with enphase.api_key, client_id, client_secret filled in

Usage:
  python scripts/oauth/setup_enphase_oauth.py
"""

from __future__ import annotations

import base64
import json
import sys
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from marin_midi.paths import SECRETS_PATH
TOKEN_URL = "https://api.enphaseenergy.com/oauth/token"
DEFAULT_REDIRECT = "https://api.enphaseenergy.com/oauth/redirect_uri"


def read_secrets() -> dict:
    if not os.path.isfile(SECRETS_PATH):
        raise SystemExit(f"Create {SECRETS_PATH} from secrets.example.json first.")
    try:
        with open(SECRETS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"{SECRETS_PATH} is not valid JSON ({exc}).\n"
            "Check commas between top-level keys (e.g. after api_key, before \"enphase\")."
        ) from exc


def write_secrets(data: dict) -> None:
    with open(SECRETS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _ensure_redirect_on_auth_url(auth_url: str, redirect_uri: str) -> str:
    parsed = urlparse(auth_url.strip())
    qs = parse_qs(parsed.query)
    if "redirect_uri" not in qs:
        qs["redirect_uri"] = [redirect_uri]
    if "response_type" not in qs:
        qs["response_type"] = ["code"]
    flat = {k: v[0] for k, v in qs.items()}
    return urlunparse(parsed._replace(query=urlencode(flat)))


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def exchange_code(
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> dict:
    response = requests.post(
        TOKEN_URL,
        headers={
            "Authorization": _basic_auth_header(client_id, client_secret),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "authorization_code",
            "code": code.strip(),
            "redirect_uri": redirect_uri,
        },
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def main() -> None:
    data = read_secrets()
    enphase = data.get("enphase")
    if not isinstance(enphase, dict):
        raise SystemExit(
            'Add an "enphase" block to secrets.json — see secrets.example.json'
        )

    api_key = (enphase.get("api_key") or "").strip()
    client_id = (enphase.get("client_id") or "").strip()
    client_secret = (enphase.get("client_secret") or "").strip()
    if not all([api_key, client_id, client_secret]):
        raise SystemExit("Fill enphase.api_key, client_id, and client_secret in secrets.json")

    redirect_uri = (enphase.get("redirect_uri") or DEFAULT_REDIRECT).strip()
    auth_url = (enphase.get("authorization_url") or "").strip()
    if not auth_url:
        auth_url = (
            f"https://api.enphaseenergy.com/oauth/authorize"
            f"?response_type=code&client_id={client_id}&redirect_uri={redirect_uri}"
        )
    auth_url = _ensure_redirect_on_auth_url(auth_url, redirect_uri)

    print("1) Open this URL in your browser and log in as homeowner (approve access):\n")
    print(auth_url)
    print()
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    print(
        "2) After you approve, Enphase redirects to redirect_uri.\n"
        "   If you use the default redirect, the page shows the authorization code.\n"
        "   Copy the value of the 'code' query parameter (or paste the full URL).\n"
    )
    raw = input("Paste authorization code (or full redirect URL): ").strip()
    if "code=" in raw:
        parsed = urlparse(raw if "://" in raw else f"https://x/?{raw}")
        code = parse_qs(parsed.query).get("code", [""])[0]
    else:
        code = raw
    if not code:
        raise SystemExit("No authorization code provided.")

    tokens = exchange_code(client_id, client_secret, code, redirect_uri)
    enphase["redirect_uri"] = redirect_uri
    enphase["authorization_url"] = auth_url
    enphase["access_token"] = tokens.get("access_token", "")
    enphase["refresh_token"] = tokens.get("refresh_token", "")
    data["enphase"] = enphase
    write_secrets(data)

    print("\nSaved access_token and refresh_token to secrets.json")
    print(f"System id in config: {enphase.get('system_id', '(set in config.json)')}")
    print("Access token validity ~1 day; refresh_token ~1 month — refresh before building the client.")


if __name__ == "__main__":
    main()
