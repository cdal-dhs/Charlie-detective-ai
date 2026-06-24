# Runbook — Detective.be Agent

> Incidents, procédures de secours et garde-fous opérationnels.
> Maintenu à jour à chaque incident ou changement critique de procédure.

---

## 1. Post-mortems

### INC-001 — 2026-05-16 : Crash serveur web après déploiement v1.8.0

**Symptôme** : Traefik retournait 502 sur toutes les routes. `/health` injoignable.

**Racine** : `app/web/app.py:35` montait `StaticFiles(directory="app/web/static")`, mais ce répertoire n'était jamais commité dans Git (il contenait des sous-répertoires vides `css/` et `js/`). Lors du build Docker, le `COPY app ./app` ne créait pas le dossier. Au boot, `uvicorn.Server.serve()` plantait silencieusement car `StaticFiles` levait une exception à l'init FastAPI. Le processus Python restait en vie (boucle IMAP) mais le serveur web ne démarrait jamais.

**Impact** : Cockpit indisponible ~15 min. Polling IMAP continuait de fonctionner (pas de perte de mail).

**Résolution** : mount conditionnel `if static_dir.exists(): app.mount(...)` + suppression du dossier `static/` du `.dockerignore` si nécessaire. Rebuild `--no-cache`.

**Garde-fous ajoutés** :
- `deploy-to-vps.sh` vérifie désormais que tous les répertoires/volumes montés dans `docker-compose.yml` existent dans le repo avant push.
- `deploy-to-vps.sh` lance un `docker build` local en dry-run pour valider que l'image démarre avant envoi sur le VPS.
- `deploy-to-vps.sh` vérifie que `app/web/static` existe, sinon le crée avec un `.gitkeep`.

---

## 2. Procédures opérationnelles

### Redémarrage urgent (le conteneur ne répond plus)

```bash
ssh root@69.62.110.165
cd /opt/DETECTIVE
docker compose down
docker compose up -d --build
docker compose logs -f --tail 20
```

### Rollback rapide

```bash
# Sur le VPS — revenir au commit précédent
cd /opt/DETECTIVE
git log --oneline -5          # identifier le bon commit
git reset --hard <COMMIT>
docker compose up -d --build
```

### Vérifier que le web écoute bien

```bash
# Depuis le VPS
docker exec detective-agent python3 -c "
import socket
s = socket.socket()
s.settimeout(2)
s.connect(('127.0.0.1', 8080))
s.close()
print('OK')
"
```

---

## 3. Checklist avant tout déploiement

- [ ] `git status` propre (pas de modifications non commitées)
- [ ] `git log origin/main..HEAD` vide (tout poussé)
- [ ] `scripts/deploy-to-vps.sh` passe les pre-flight checks
- [ ] `docker compose build` local réussit (dry-run)
- [ ] Tous les répertoires montés dans `docker-compose.yml` existent en local
- [ ] `CHANGELOG.md` à jour avec la version
- [ ] `app/_version.py` bumpée (source unique — **JAMAIS** `pyproject.toml` qui reste figé en `1.9.5`)
- [ ] Après deploy : `/health = 200` et `/auth/login = 200`

---

*Dernière mise à jour : 2026-06-24 (v1.25.26)*
