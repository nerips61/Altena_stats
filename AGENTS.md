# AGENTS.md

## Cursor Cloud specific instructions

Local Flask energy dashboard (`app.py`). See `readme.txt` for the product overview.

### Environment
- Python dependencies (`requirements.txt`) are installed by the Cloud environment update script into a per-repo virtualenv at `~/venvs/altena_stats`.
- `run.command` is a macOS/iCloud double-click launcher that sources `../../../tools/scripts/ensure_venv.sh`, a path that does not exist in Cloud. Do not use it here — run `app.py` directly.

### Run (dev)
- `source ~/venvs/altena_stats/bin/activate && python app.py`
- Serves on the `port` from `config/config.json` (default **5070**), bound to `0.0.0.0`.
- On startup the app spawns daemon threads that try to open a browser and sync live data (Leneda / FusionSolar); these fail gracefully in Cloud and do not block the server.

### Live data / secrets
- Live series need `config/secrets.json` (Leneda / Enphase / FusionSolar keys — see `config/secrets.example.json`). Without it the dashboard still renders but shows zeros and an "Avertissements" block. Secrets are not required to boot the app.

### Lint / test
- No linter or test framework is configured. Validate by running the app and loading `http://127.0.0.1:5070/`, or hitting `GET /ping`, `GET /api/data`, `GET /api/data-availability`.
