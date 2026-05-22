# HANDOVER — Detective.be Agent IA (Charlie)

> Document de transfert pour Claude Opus 4.7 ou tout agent ultérieur.  
> Dernière mise à jour : **2026-05-22** · Version courante : **V1.14.2** · Déployé sur : `detective.digitalhs.biz`

---

## 1. Qui, quoi, pourquoi

| | |
|---|---|
| **Client** | Daniel Hurchon — détective privé belge, cabinet **Detective.be** |
| **Intégrateur & ops** | CDAL (`cdal@digitalhs.biz`) — c'est l'utilisateur que tu assistes |
| **Produit** | Agent IA Python qui poll 3 boîtes mail Infomaniak, classifie, et génère des brouillons de réponse "à la Daniel" |
| **Canal Boss** | Bot Telegram direct Daniel ↔ Charlie (notifications, résumés, validations) |
| **Second cerveau** | **Cerveau2-Det** — vault Markdown + API FastAPI sémantique (sqlite-vec + E5-large) |
| **Cockpit web** | `detective.digitalhs.biz` — inbox, conversation, chat AI Charlie, dashboard admin |
| **Urgence** | Démo client imminente — la fiabilité des réponses Charlie est critique |

---

## 2. Architecture actuelle (V1.14.2)

```
[3 boîtes Infomaniak IMAP] ──polling 5min──► [Worker asyncio Python]
                                                    │
                    ┌───────────────────────────────┼───────────────────────────────┐
                    ▼                               ▼                               ▼
        [Pipeline IMAP]                    [Cockpit web FastAPI]              [Cerveau2 API]
          prefilter ──► classifier          /inbox, /conversation, /admin      /query, /ingest
          priority ──► generator            /api/charlie/ask                 vault Markdown
          delivery (Resend/Slack)           /api/charlie/feedback            sqlite-vec
                                                    │
                                            [agent_state.db]
                                            mail_processed
                                            charlie_memory
                                            email_attachment
```

### Fichiers clés et rôles

| Fichier | Rôle critique | À savoir |
|---|---|---|
| `app/_version.py` | **Source unique de vérité** version | `VERSION = "1.14.2"`. Tolérance zéro sur la désynchronisation. |
| `app/charlie.py` | **Cœur intelligent Charlie AI** | Pipeline `ask_charlie()` : question → extraction entités → SQL + vault + archives + corrections + mémoire en parallèle → LLM final → garde anti-vide |
| `app/charlie_memory.py` | **Mémoire persistante** | Table `charlie_memory` (feedback good/bad, corrections, auto-save). |
| `app/cerveau_client.py` | **Client HTTP Cerveau2** | `query_vault()`, `feed_correspondance()`, `feed_document()`. Bearer Token statique. |
| `app/config.py` | **Configuration pydantic-settings** | `llm_model_chat = "openai/deepseek-v4-pro"` (nouveau en V1.14.1). |
| `app/llm/router.py` | **Wrapper LiteLLM** | `complete()` avec fallback automatique vers `llm_model_fallback`. Expose les clés API dans les env vars. |
| `app/web/api.py` | **Endpoints HTMX + Charlie** | `charlie_ask()` (ligne 571) et `charlie_feedback()` (ligne 707). |
| `app/web/app.py` | **App FastAPI** | Monte StaticFiles conditionnel, inclut les routers. |
| `app/workers/imap_poller.py` | **Polling IMAP** | 1 task asyncio par boîte, flag `AgentProcessed` (sans `$` — Infomaniak rejette `$`). |
| `scripts/deploy-to-vps.sh` | **Déploiement one-shot** | Pre-flight checks, sync data (exclut `agent_state.db`), build, healthcheck. |

---

## 3. Le pipeline Charlie AI (état V1.14.2)

Le fichier `app/charlie.py:620-846` contient `ask_charlie()`. Voici le flow exact :

### Phase 1 — Questions générales (bypass)
- `_general_response()` répond en dur à "salut", "version", "merci", "au revoir", "qui es-tu".
- **Aucun appel LLM** — latence nulle, coût nul.

### Phase 2 — Extraction entités
- `_extract_dossier_id()` : regex pour détecter un dossier (ex: ADF, #DPDH).
- `_extract_year()` : regex `20\d{2}`.
- `_enrichir_question()` : ajoute des synonymes métier si le type d'enquête est détecté.

### Phase 3 — Génération SQL (1 appel LLM)
- Prompt system `CHARLIE_SYSTEM_PROMPT` (lignes 29-124) très détaillé avec règles SQL, Mode A (catégorie exacte) vs Mode B (LIKE mot-clé), comptage, liens cliquables.
- Le LLM génère `SQL: <SELECT>` + `---` + `RÉPONSE: <texte>`.
- `parse_charlie_response()` extrait le SQL et la réponse.
- `is_safe_sql()` vérifie que c'est un SELECT (whitelist `starts with select`, blacklist mots dangereux).

### Phase 4 — Recherches parallèles (asyncio.gather)
| Tâche | Fonction | Quand elle s'exécute |
|---|---|---|
| SQL local | `run_sql(db_agent_state, sql)` | Si SQL safe |
| Vault Cerveau2 | `query_vault(question, dossier_id)` | Toujours |
| Mémoire | `query_memory(db, question, dossier_id)` | Toujours |
| Corrections | `query_corrections(db, dossier_id)` | Si `dossier_id` détecté |
| Archives historiques | `_search_historical_by_keyword()` ou `_search_historical_all()` | Si `dossier_id` ou `year` |

**Bases historiques** : `data/boite1.sqlite`, `boite2.sqlite`, `boite3.sqlite` (emails avant cutoff 2026-05-15).  
**Base courante** : `data/agent_state.db` → table `mail_processed` (emails post-cutoff, ~19 lignes).

### Phase 5 — Construction du contexte
Le contexte injecté dans le prompt final contient, dans cet ordre de priorité :
1. **Corrections utilisateur** (priorité absolue)
2. **Résultats SQL** (anonymisés via `_sanitize_rows_for_prompt()`)
3. **Archives historiques** (répartition par catégorie + 50 premiers sujets)
4. **Notes du second cerveau** (vault Cerveau2)
5. **Souvenirs de Charlie** (mémoire courte)

### Phase 6 — Réponse
#### Bypass comptage direct (Python, pas de LLM)
Si `is_count_request` ET pas de `dossier_id` ET `archive_rows` existe :
- Total = `len(archive_rows)` + `len(rows)` si SQL > 0
- Réponse construite directement en Python : "J'ai trouvé **N** emails en 2026. (tous dans les archives)"

#### LLM final (questions spécifiques)
- Prompt final `final_prompt` (ligne 792-807) avec le contexte + règles format.
- `complete(model=settings.llm_model_chat, ...)` — utilise **deepseek-v4-pro via Ollama Pro** (`openai/deepseek-v4-pro`).

#### Garde anti-réponse vide (V1.14.2 — critique)
Si le LLM retourne une chaîne vide (ce qui arrive avec deepseek-v4-pro sur certains prompts longs) :
```python
if is_count_request and (rows or archive_rows):
    # Construit une réponse de secours avec les données brutes
    response = f"J'ai trouvé **{total}** emails pour ..."
elif is_list_request and archive_rows:
    # Liste les 25 premiers sujets d'archives + "… et X autres"
    response = "\n".join(lines)
else:
    response = "Je n'ai pas trouvé d'informations."
```

---

## 4. Stack technique détaillée

| Couche | Outil | Version / Détail |
|---|---|---|
| Python | 3.11+ | VPS = 3.11, Mac CDAL = 3.14 |
| Concurrence | `asyncio` | Tout est `async def` |
| IMAP | `aioimaplib` | 2.0.1 |
| LLM router | **LiteLLM** | 1.85.0 |
| LLM chat (Charlie AI) | **deepseek-v4-pro** via Ollama Pro | `openai/deepseek-v4-pro` |
| LLM fallback | **OpenRouter** | `openrouter/anthropic/claude-3.5-sonnet` |
| LLM pipeline (classifier) | Kimi K2 via Ollama Pro | `ollama_chat/kimi-k2` |
| Embeddings | `intfloat/multilingual-e5-large` | sentence-transformers, local CPU |
| Vector store | `sqlite-vec` | 0.1.9, vit dans les DB existantes |
| Détection langue | `langdetect` | Remplace fasttext (ne build pas sur Mac ARM) |
| Email outbound | **Resend API** | `agent@digitalhs.biz` |
| Web framework | **FastAPI** | 0.136.1 |
| Templating | **Jinja2** + HTMX | Pas de React |
| CSS | **Tailwind CSS** | CDN |
| Logs | `structlog` | JSON structuré, rotation 3j |
| Config | `pydantic-settings` | `.env` |
| Serveur | **uvicorn** | 0.47.0 |
| Reverse proxy | **Traefik** | Docker network `root_default` |

---

## 5. Cerveau2-Det — Le second cerveau

### Qu'est-ce que c'est
Cerveau2-Det est un **vault Markdown** structuré + une **API FastAPI** qui expose recherche sémantique, ingestion et anonymisation. Il vit sur le même VPS (`cerveau2-det.digitalhs.biz`) ou un sous-domaine séparé.

### Structure du vault
```
vault/
├── 00_system/       ← Logs, index, config AGENTS.md
├── 01_inbox/        ← Raw (jamais édité manuellement)
├── 02_dossiers/     ← Dossiers d'enquête actifs
├── 03_doctrine/     ← Méthodologie, jurisprudence
├── 04_entities/     ← CRM transversal (personnes, sociétés, lieux)
├── 05_clients/      ← Coordonnées clients + facturation
├── 99_archives/     ← Dossiers clos
└── 99_attachments/  ← Binaires originaux
```

### Endpoints utilisés par Charlie
| Endpoint | Usage | Client |
|---|---|---|
| `POST /query` | Recherche sémantique + keyword | `app/cerveau_client.py::query_vault()` |
| `POST /ingest-email` | Alimentation continue emails | `app/cerveau_client.py::feed_correspondance()` |
| `POST /ingest-note` | Alimentation documents | `app/cerveau_client.py::feed_document()` |
| `POST /anonymize` | Anonymisation avant LLM cloud | Appelé côté Cerveau2, pas directement par Charlie |

### Authentification
- **Bearer Token statique** (pas d'OAuth, pas de JWT).
- Secret défini dans `.env` : `CERVEAU2_API_SECRET`.
- Sur le VPS, le secret est dans `/opt/CERVEAU2/.env`.
- **Jamais commité** — toujours via `get_settings()`.

### Connexion depuis Charlie
Le client est dans `app/cerveau_client.py`. Il est **dégradation silencieuse** : si Cerveau2 est down, retourne `[]` et Charlie continue avec SQL + mémoire seuls.

---

## 6. Déploiement production

### VPS
- **Host** : `root@69.62.110.165`
- **Répertoire** : `/opt/DETECTIVE`
- **Container** : `detective-agent`
- **DNS** : `detective.digitalhs.biz` → A record `69.62.110.165`
- **Reverse proxy** : Traefik (network Docker `root_default` externe)
- **SSL** : Let's Encrypt via Traefik (`mytlschallenge`)

### Déployer depuis le Mac de CDAL
```bash
bash scripts/deploy-to-vps.sh
```
Ce script exécute :
1. Pre-flight checks (branche main, pas de modifs non commitées, push auto)
2. Vérification répertoires montés docker-compose.yml
3. Smoke test Docker local
4. `git pull` sur le VPS
5. Backup `agent_state.db` sur le VPS
6. `rsync data/` (exclut `agent_state.db` pour ne pas écraser les catégories/priorités modifiées via le cockpit)
7. `rsync .env` → `.env.production`
8. `docker compose up -d --build`
9. Healthcheck `/health` + `/auth/login` (12 tentatives × 5s)

### Manuellement sur le VPS (si le script échoue)
```bash
ssh root@69.62.110.165
cd /opt/DETECTIVE
git fetch origin && git reset --hard origin/main   # si divergences
docker compose up -d --build
docker compose logs -f --tail 20
```

### Docker Compose (résumé)
```yaml
services:
  detective:
    build: .
    container_name: detective-agent
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
      - ./.env.production:/app/.env:ro
      - hf_cache:/root/.cache/huggingface
      - ./app:/app/app:ro          # dev mount
    environment:
      WEB_BIND_HOST: "0.0.0.0"
      HEALTHCHECK_HOST: "0.0.0.0"
      DATA_DIR: "/app/data"
      DB_AGENT_STATE: "/app/data/agent_state.db"
    labels:
      - traefik.http.routers.detective.rule=Host(`detective.digitalhs.biz`)
    networks:
      - root_default
```

---

## 7. Données et bases SQLite

### `data/agent_state.db` (base courante — NE PAS ÉCRASER EN DEPLOY)
| Table | Rôle |
|---|---|
| `mail_processed` | Emails traités par le pipeline (post-cutoff 2026-05-15) |
| `charlie_memory` | Mémoire Charlie (feedback, corrections, faits auto-sauvés) |
| `email_attachment` | Pièces jointes détectées |
| `users` | Utilisateurs cockpit (auth magic link) |
| `audit_log` | Traçabilité actions cockpit |

### `data/boite1.sqlite`, `boite2.sqlite`, `boite3.sqlite` (archives historiques)
- Contiennent les emails **avant** le cutoff (699 emails 2026 dans boite1).
- **Ne pas modifier** sans confirmation de CDAL.
- Charlie les interroge via `_search_historical_by_keyword()` et `_search_historical_all()`.

### Cutoff date
`process_since_date = "2026-05-15"` dans `.env`.  
Le poller IMAP ne traite que les mails reçus depuis cette date. Les archives historiques contiennent tout l'historique.

---

## 8. Règles critiques (à respecter impérativement)

### Règle 1 — Tolérance zéro version
- Source unique : `app/_version.py`.
- **Jamais** `importlib.metadata`.
- À chaque release (nouveauté, bugfix, correction) → bump `app/_version.py` + mettre à jour `CHANGELOG.md`.
- La version affichée dans le cockpit est lue dynamiquement depuis `app/_version.py`.

### Règle 2 — Ne jamais écrire dans les vraies boîtes Infomaniak en dev
- Mode `--dry-run` disponible.
- En dev, utiliser un compte mail de test si besoin.
- La première vraie connexion en prod est surveillée par CDAL.

### Règle 3 — Ne jamais envoyer de mail réel via Resend en test
- Si `RESEND_API_KEY` est vide, le module skip avec un warning.
- En test automatisé, mocker ou laisser la clé vide.

### Règle 4 — Flag IMAP = `AgentProcessed` (sans `$`)
- Infomaniak rejette les flags avec préfixe `$`.  
- Le code utilise `AgentProcessed` (ligne confirmée dans `imap_poller.py`).

### Règle 5 — Multilingue obligatoire
- La réponse générée DOIT être dans la langue détectée du mail entrant (FR/NL/EN).
- Tester systématiquement les 3 langues.

### Règle 6 — Pas de Docker au MVP
- L'architecture est volontairement légère : Python natif + SQLite + Docker uniquement en prod.
- Ne pas introduire Docker Compose en dev sans discussion.

### Règle 7 — Ne jamais logger le contenu intégral d'un mail
- Métadonnées uniquement (message-id, expéditeur, sujet, classification).
- Pour debug, ajouter un flag explicite `LOG_MAIL_BODY=true`.

---

## 9. Bugs connus et points de vigilance (2026-05-22)

| # | Problème | Statut | Fichier concerné |
|---|---|---|---|
| 1 | **LLM retourne vide** sur comptages ADF | ✅ Corrigé V1.14.2 | `app/charlie.py:822-835` — garde anti-vide |
| 2 | **Réponses list montrent des stats** au lieu de noms de dossiers | ✅ Corrigé V1.14.2 | `app/charlie.py:756-767` — bypass Python pour list supprimé, contexte 50 emails |
| 3 | **Count ADF = 0** car SQL cherchait `subject LIKE '%ADF%'` mais emails ADF viennent de `@groupeadf.com` | ✅ Corrigé V1.14.1 | `CHARLIE_SYSTEM_PROMPT` — Mode B recherche aussi dans `sender` |
| 4 | **Corrections écrasaient les questions analytiques** | ✅ Corrigé V1.14.0 | `_summarize_results()` — bypass correction ne s'applique que si `_is_identity_query()` |
| 5 | **Bouton "Envoyer correction"** potentiellement non fonctionnel | ⚠️ À investiguer | `app/web/api.py:707` — `charlie_feedback()` reçoit bien `corrected_response`, mais l'UX JS/HTMX pourrait avoir une régression |
| 6 | **deepseek-v4-pro via `openai/` prefix** | ✅ Résolu | `llm_model_chat = "openai/deepseek-v4-pro"` dans `config.py` |
| 7 | **Modèle fallback invalide en DB** | ✅ Résolu | `app/settings_store.py` — fallback mis à jour sur `openrouter/anthropic/claude-3.5-sonnet` |

### Point de vigilance #1 — deepseek-v4-pro et réponses vides
Ce modèle (via Ollama Pro) retourne parfois `length=0` sur des prompts longs (contexte SQL + vault + archives + mémoire). Le fallback LiteLLM ne se déclenche **pas** sur une réponse vide — seulement sur une exception.  
**Garde** : le bloc `if not response:` (ligne 822) est la dernière ligne de défense.

### Point de vigilance #2 — mail_processed ne contient que ~19 emails
La base courante `agent_state.db/mail_processed` ne contient que les emails post-cutoff (2026-05-15). Les vrais données (699 emails 2026) sont dans `boite1.sqlite`.  
**Conséquence** : pour les questions sur 2026, les archives historiques sont la source principale. Le SQL local retourne souvent 0.

### Point de vigilance #3 — Cerveau2 peut être down
Le client `query_vault()` est dégradation silencieuse. Si Cerveau2 est indisponible, Charlie répond avec SQL + mémoire seuls. Vérifier les logs `cerveau.query_failed`.

---

## 10. Procédures d'urgence

### Redémarrage container
```bash
ssh root@69.62.110.165
cd /opt/DETECTIVE
docker compose down
docker compose up -d --build
docker compose logs -f --tail 20
```

### Rollback rapide
```bash
cd /opt/DETECTIVE
git log --oneline -5
git reset --hard <COMMIT_PRÉCÉDENT>
docker compose up -d --build
```

### Vérifier l'état
```bash
# Health
 curl -s -o /dev/null -w "%{http_code}" https://detective.digitalhs.biz/health
 curl -s -o /dev/null -w "%{http_code}" https://detective.digitalhs.biz/auth/login

# Logs container
ssh root@69.62.110.165 "cd /opt/DETECTIVE && docker compose logs --tail 50"
```

---

## 11. Contacts et ressources

| Ressource | Où trouver |
|---|---|
| Spec technique | `docs/SPEC.md` |
| Roadmap | `docs/ROADMAP.md` |
| Contexte business | `docs/CONTEXT.md` |
| Guide Cerveau2 | `docs/CERVEAU2_INTEGRATION.md` |
| API Cerveau2 | `docs/CERVEAU2_API.md` |
| Runbook incidents | `docs/RUNBOOK.md` |
| Checklist démo | `docs/DEMO_CHECKLIST.md` |
| Changelog | `CHANGELOG.md` |
| Instructions Claude Code | `CLAUDE.md` |
| Intégrateur | CDAL — `cdal@digitalhs.biz` |
| Client | Daniel Hurchon — Detective.be |

---

## 12. Pour le prochain agent (checklist reprise)

Avant de modifier quoi que ce soit :
- [ ] Lire `CLAUDE.md` (conventions, garde-fous, stack)
- [ ] Lire ce `HANDOVER.md` (contexte actuel, état des bugs)
- [ ] Vérifier `app/_version.py` — est-ce la bonne version ?
- [ ] Vérifier `CHANGELOG.md` — la dernière version est-elle documentée ?
- [ ] Lire les 50 dernières lignes de `app/charlie.py` pour comprendre le pipeline actuel
- [ ] Lire `docs/ROADMAP.md` pour savoir quelle phase est en cours
- [ ] Si une décision n'est pas dans la spec → demander à CDAL

---

*Document généré le 2026-05-22 pour la V1.14.2 de Detective.be Agent IA.*
