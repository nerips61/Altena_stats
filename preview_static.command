#!/bin/bash
# Prévisualise web/ (export statique) en local — nécessaire car ouvrir
# index.html directement (file://) bloque les fetch() JSON dans le navigateur.
set -euo pipefail
cd "$(dirname "$0")/web" || exit 1

PORT=8901
URL="http://127.0.0.1:${PORT}/"

if lsof -ti ":${PORT}" >/dev/null 2>&1; then
  echo "Port ${PORT} déjà utilisé — arrêt de l'ancien serveur…"
  lsof -ti ":${PORT}" | xargs kill 2>/dev/null || true
  sleep 1
fi

echo "Aperçu Solarenergie fir Altena (statique) → ${URL}"
echo "Laisser cette fenêtre Terminal ouverte. Ctrl+C pour arrêter."
( sleep 1; open "$URL" ) &
python3 -m http.server "$PORT" --bind 127.0.0.1
