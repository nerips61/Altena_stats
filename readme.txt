Solarenergie fir Altena — statistiques énergie (port 5070 — pas 5060 : port SIP bloqué par Chrome/Safari)
==========================================================

Même logique que Marin–Midi / Piren : Leneda (séries OBIS + partages),
granularité journalier / hebdomadaire / mensuel, cache SQLite.

Mise en service : 12/06/2026 — SU00000049.
Intégrés Leneda (15/06/2026) : Jacoby, L&M, Eschville ×4 + prod Marin/Midi.
Levant parties communes (entrées C–F) : intégrés 22/06/2026.
Référence export : Dropbox/…/LENEDA/sharing-group-configuration_SU00000049_2026-06-15_….xlsx

Lancement
---------
  ./run.command
  (venv partagé via tools/scripts/ensure_venv.sh)

Portail Docker (8700) : service « altena » — DASHBOARD_PORTAL=1.

Site public (Cloudflare Pages)
------------------------------
  Export statique : python3 scripts/export_static.py → web/
  Aperçu local : ./preview_static.command
  Procédure Git / Pages : docs/GITHUB.md
  Sous-domaine energy-communities.net : encore à trancher.

Configuration
-------------
  config/config.json   — PODs, séries, operational_from
  config/secrets.json  — copier depuis secrets.example.json (non versionné)

Comptes Leneda (3 contextes API) :
  - piren         : Sylvain Piren — consommation des 10 PODs membres Altena (+ même login que Piren_Stats)
  - marin, midi   : production PV Marin/Midi (compteurs communs, injection, partage)

FusionSolar (optionnel) : réutilise fusion_solar.marin / .midi pour autoconsommation.

Sync cache manuelle : python scripts/sync/sync_stats_cache.py

Git : nerips61/Altena_stats — secrets et cache.db hors Git.
  Voir aussi docs/GITHUB.md (export, push, Access).
