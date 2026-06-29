#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")" || exit 1

# shellcheck source=../../../tools/scripts/ensure_venv.sh
source "../../../tools/scripts/ensure_venv.sh"
# shellcheck source=/dev/null
source "$VENV/bin/activate" || exit 1

PORT="$(python - <<'PY'
import json
with open("config/config.json", encoding="utf-8") as f:
    print(int((json.load(f).get("port") or 5070)))
PY
)"

if [[ "${DASHBOARD_PORTAL:-}" == "1" ]]; then
  if lsof -ti :"$PORT" >/dev/null 2>&1; then
    exit 0
  fi
  exec "$VENV/bin/python" app.py
  exit 0
fi

if lsof -ti :"$PORT" >/dev/null 2>&1; then
  echo "Port $PORT occupé: arrêt de l'ancienne instance…"
  lsof -ti :"$PORT" | xargs kill 2>/dev/null || true
  sleep 1
fi

find "$(dirname "$0")" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

echo "Solarenergie fir Altena — statistiques (navigateur dans ~1 s)"
echo "Mac: http://127.0.0.1:$PORT/ — iPhone (même Wi‑Fi): voir l'URL au démarrage."
echo "config/secrets.json : piren (membres) + marin/midi (production PV)."
echo "Garder cette fenêtre ouverte. Ctrl+C pour arrêter."
exec "$VENV/bin/python" app.py
