"""Project root and standard file locations."""

from __future__ import annotations

import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(PROJECT_ROOT, "config")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
SECRETS_PATH = os.path.join(CONFIG_DIR, "secrets.json")
SECRETS_EXAMPLE_PATH = os.path.join(CONFIG_DIR, "secrets.example.json")
CACHE_PATH = os.environ.get("ALTENA_CACHE_PATH") or os.path.join(PROJECT_ROOT, "cache.db")


def enphase_token_path() -> str:
    """Writable OAuth token store (Docker: /data/ when secrets.json is :ro)."""
    env = os.environ.get("ALTENA_ENPHASE_TOKEN_PATH", "").strip()
    if env:
        return env
    return os.path.join(os.path.dirname(CACHE_PATH), "enphase_tokens.json")
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
MANUAL_PRODUCTION_SEED_PATH = os.path.join(DATA_DIR, "manual_production.json")
