"""Version semver + empreinte Git pour le pied de page."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from altena.version import VERSION, VERSION_NOTE

APP_ROOT = Path(__file__).resolve().parent.parent


def app_build_info(app_id: str = "app") -> dict[str, Any]:
    build = ""
    dirty = False
    if (APP_ROOT / ".git").is_dir():
        try:
            rev = subprocess.run(
                ["git", "-C", str(APP_ROOT), "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
                timeout=2,
            )
            if rev.returncode == 0:
                build = rev.stdout.strip()
            status = subprocess.run(
                ["git", "-C", str(APP_ROOT), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=False,
                timeout=2,
            )
            if status.returncode == 0:
                dirty = bool(status.stdout.strip())
        except (OSError, subprocess.TimeoutExpired):
            pass
    return {
        "app_id": app_id,
        "version": VERSION,
        "version_note": VERSION_NOTE,
        "build": build,
        "dirty": dirty,
    }


def app_build_stamp(app_id: str = "app") -> dict[str, str]:
    info = app_build_info(app_id)
    version = str(info.get("version") or "").strip() or "dev"
    note = str(info.get("version_note") or "").strip()
    build = str(info.get("build") or "").strip()
    dirty = bool(info.get("dirty"))
    label_core = f"v{version}"
    if build:
        label_core = f"{label_core} · {build}{'*' if dirty else ''}"
    title = f"v{version}"
    if note:
        title += f" — {note}"
    if build:
        title += f" · commit {build}"
        if dirty:
            title += " (modifications locales non commitées)"
    return {"label": label_core, "title": title}
