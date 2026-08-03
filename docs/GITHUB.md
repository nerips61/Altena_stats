# Git — Solarenergie fir Altena

## Dépôt

| | |
|--|--|
| **GitHub** | `nerips61/Altena_stats` (privé) |
| **Dossier** | `_Tableaux-de-bord/apps/communities/altena/` |
| **Port local** | 5070 |
| **Site public** | sous-domaine `*.energy-communities.net` à trancher (ex. `s4a` vs nom plus long) |
| **Racine Pages** | dossier `web/` (pas de build npm) |

C’est un dépôt Git **indépendant** du dépôt parent `_Tableaux-de-bord`.

## Workflow courant (code local)

```bash
cd "_Tableaux-de-bord/apps/communities/altena"
git pull
# … modifications …
git add -A
git commit -m "…"
git push
```

Ou via `_Tableaux-de-bord/tools/` : `pull_gits.command` / `publish_gits.command`.

## Rafraîchir le site public (export statique)

Cloudflare Pages ne sert que des fichiers statiques : pas d’API ni de `cache.db`.
L’export fige les 5 périodes de l’UI (Depuis mise en service, Année courante/passée,
Semestres) × granularités autorisées. Pas d’horaires ; amortissement non exporté tant
que le bloc `amortization` n’est pas configuré.

1. Mettre à jour le cache local si besoin.
2. Activer le même venv que `run.command`, puis :
   ```bash
   python3 scripts/export_static.py
   ```
3. Commit + push :
   ```bash
   git add web/
   git commit -m "Export Altena stats JJ/MM"
   git push
   ```

Pages redéploie depuis `main` ; vérifier ensuite sur le site public.
Access protège le domaine (OTP + e-mails autorisés).

Les **décomptes** restent locaux (module portail `type=billing`) — non exportés.

## Hors Git

| Fichier | iCloud entre Macs | GitHub |
|---------|-------------------|--------|
| `config/secrets.json` | **Oui** | **Non** |
| `enphase_tokens.json` | Local / OAuth | Non |
| `cache.db` | Peut apparaître dans iCloud ; régénérable | Non |

## Liens

- Portail : `portal/entities.json` → port 5070
- Export : `scripts/export_static.py` → `web/`
