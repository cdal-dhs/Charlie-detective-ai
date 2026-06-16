# HANDOVER — Detective.be Agent IA (Charlie)

> Document de transfert pour tout agent (Claude Sonnet/Opus 4.X, GPT, etc.).
> **Dernière mise à jour** : 2026-06-16 · **Version courante** : v1.22.7 · **Déployé sur** : `detective.digitalhs.biz`

---

## TABLE DES MATIÈRES (TL;DR)

1. [Qui, quoi, pourquoi](#1-qui-quoi-pourquoi)
2. [Architecture actuelle v1.22.5](#2-architecture-actuelle-v1225)
3. [Le pipeline Charlie AI](#3-le-pipeline-charlie-ai)
4. [Stack technique détaillée](#4-stack-technique-détaillée)
5. [Cerveau2-Det — second cerveau](#5-cerveau2-det--second-cerveau)
6. [Déploiement production](#6-déploiement-production)
7. [Données et bases SQLite](#7-données-et-bases-sqlite)
8. [Règles critiques](#8-règles-critiques-à-respecter-impérativement)
9. [Bugs résolus et points de vigilance](#9-bugs-résolus-et-points-de-vigilance)
10. [Procédures d'urgence](#10-procédures-durgence)
11. [Contacts et ressources](#11-contacts-et-ressources)
12. [Checklist reprise agent](#12-checklist-reprise-agent)
13. [Sécurité anti-crash silencieux](#13-sécurité-anti-crash-silencieux)

---

## 1. Qui, quoi, pourquoi

| | |
|---|---|
| **Client** | Daniel Hurchon — détective privé belge, cabinet **Detective.be** (3 marques : Detective Belgique FR, Detective Belgium EN/multi, DPDH Investigations) |
| **Intégrateur & ops** | CDAL (`cdal@digitalhs.biz`) — c'est l'utilisateur que tu assistes |
| **Produit** | Agent IA Python qui poll 3 boîtes Infomaniak toutes les 5 min, classifie les mails en 8 catégories, et **uniquement** pour les `demande_client` génère un brouillon "à la Daniel" via RAG sur ~2000 paires Q/R historiques (3 DB SQLite + sqlite-vec) |
| **Livraison V2a** (depuis v1.17) | Brouillons déposés **directement dans Drafts IMAP de la boîte source** (flag `\Draft`, sujet `DEMANDE D'Approbation - Reponse Demande Client : ...`). Resend conservé **uniquement** comme fallback. Daniel approuve/rejette depuis sa boîte mail. |
| **Cerveau2-Det** | Vault Markdown + API FastAPI sur `cerveau2-det.digitalhs.biz` (sqlite-vec + E5-large, ingestion 100% emails + PJ) |
| **Cockpit web** | `detective.digitalhs.biz` — FastAPI + HTMX + Tailwind CDN. Inbox filtrable, conversation détaillée, chat AI Charlie, dashboard admin |
| **Canal Boss ↔ Charlie** | Slack Bot interactif (`slack_bolt`) sur `#detective` — @mention ou DM. **Telegram module conservé inactif** (Slack suffit) |
| **Urgence** | Fiabilité critique — bugs "pas trouvé" malgré données existantes = tolérance zéro |

---

## 2. Architecture actuelle (v1.22.7)

```
[3 boîtes Infomaniak IMAP] ──polling 5min──► [Worker asyncio Python]
                                                    │
                    ┌───────────────────────────────┼───────────────────────────────┐
                    ▼                               ▼                               ▼
        [Pipeline IMAP]                    [Cockpit web FastAPI]              [Cerveau2 API]
          prefilter ──► classifier          /inbox, /conversation, /admin      /query, /ingest
          priority ──► generator            /api/charlie/ask                 vault Markdown
          translator (NL/EN/DE/ES...)      /api/drafts/{id}/retry            sqlite-vec
          renderer (4 blocs multilingues)  /api/charlie/feedback
          few-shot Daniel (v1.22.4)
          delivery (IMAP Drafts / Resend fallback)
                                                    │
                                            [agent_state.db]
                                            mail_processed + charlie_memory
                                            email_attachment
                                            app_settings (overrides runtime)
```

### Fichiers clés et rôles

| Fichier | Rôle critique | À savoir |
|---|---|---|
| `app/_version.py` | **Source unique de vérité** version | `VERSION = "1.22.4"`. Tolérance zéro. Ne JAMAIS utiliser `importlib.metadata`. |
| `app/charlie.py` | **Cœur intelligent Charlie AI** | `ask_charlie()` : extraction entités → SQL programmatique (bypass LLM) + vault Cerveau2 (fallback direct GET) + archives + corrections + mémoire → nuage de liaison familial → **résumé de dossier narratif LLM** (v1.19.1) → garde anti-vide + garde anti-"pas trouvé" |
| `app/charlie_memory.py` | **Mémoire persistante** | Table `charlie_memory` (feedback good/bad, corrections, auto-save) |
| `app/cerveau_client.py` | **Client HTTP Cerveau2** | `query_vault()`, `get_vault_note()` (fallback direct), `feed_correspondance()`, `feed_document()`. Bearer Token statique. **Dégradation silencieuse** (retourne `[]` si Cerveau2 down) |
| `app/config.py` | **Configuration pydantic-settings** | `llm_model_default = "openai/kimi-k2.6:cloud"` (Ollama Pro, cloud). Provider `openai/` + `api_base=https://ollama.com/v1` |
| `app/llm/router.py` | **Wrapper LiteLLM** | `complete()` avec fallback automatique + extraction `reasoning_content` (kimi-k2.6 reasoning) + post-traitement `_clean_reasoning()` (30+ patterns pour traces raisonnement) |
| `app/pipeline/translator.py` | **Aide lecture multilingue (v1.21.0)** | `translate_to_fr()` + `translate_from_fr()` avec try/except, troncature 12K. Utilisé si langue mail ≠ FR |
| `app/pipeline/draft_renderer.py` | **Rendu brouillon enrichi (v1.21.0)** | Compose 4 blocs : email d'origine + traduction FR + proposition FR + traduction langue source |
| `app/pipeline/generator.py` | **Génération brouillon** | Appelle `translate_to_fr` + `translate_from_fr` en parallèle. **`_load_daniel_fewshot()` (v1.22.4)** : récupère 200 candidats SQL, parse date RFC 2822 en Python, garde top 4 dans fenêtre 30j — **CRITIQUE** : c'est ce qui permet au LLM d'imiter le vrai style Daniel |
| `app/pipeline/classifier.py` | **Classification LLM** (v1.22.1 hardened) | 8 catégories avec few-shots. Prompt durci pour ne plus rater aucun `demande_client` |
| `app/pipeline/language.py` | **Détection langue** | `Language = str` (toutes BCP-47), `language_label()` pour affichage humain |
| `app/web/api.py` | **Endpoints HTMX + Charlie** | `charlie_ask()`, `charlie_feedback()`, `draft_generate()`, **`POST /api/drafts/{id}/retry`** (régénération manuelle) |
| `app/workers/imap_poller.py` | **Polling IMAP** | 1 task asyncio par boîte, flag `AgentProcessed` (sans `$`) + flag `AgentAttempted` (libère la queue même en cas de crash, v1.21.3). Appelle `generate_draft()` pour `demande_client` → brouillon enrichi |
| `app/delivery/imap_draft.py` | **Dépôt brouillon IMAP** (V2a) | `append_draft()` : flag `\Draft`, SELECT probe pour auto-découverte dossier Drafts (v1.21.9 fix Infomaniak) |
| `scripts/deploy-to-vps.sh` | **Déploiement one-shot** | Pre-flight checks, sync data (exclut `agent_state.db`), build, healthcheck |
| `scripts/backfill_demande_client.py` | **(v1.22.1)** Re-classifie + génère brouillons pour les mails historiques manqués | |
| `scripts/deliver_pending_drafts.py` | **(v1.22.2)** Livre les brouillons existants en IMAP Drafts (idempotent via `delivered_at`) | |
| `scripts/cleanup_old_drafts.py` | **(v1.22.3)** Supprime vieux brouillons IMAP Drafts (SELECT probe + SEARCH SUBJECT) | |

---

## 3. Le pipeline Charlie AI (état v1.22.5)

Le fichier `app/charlie.py` contient `ask_charlie()`. Flow exact :

### Phase 1 — Questions générales (bypass)
- `_general_response()` répond en dur à "salut", "version", "merci", "au revoir", "qui es-tu".
- **Aucun appel LLM** — latence nulle, coût nul.

### Phase 2 — Extraction entités
- `_extract_dossier_id()` : regex pour détecter un dossier (ex: ADF, #DPDH).
- `_extract_year()` : regex `20\d{2}`.
- `_enrichir_question()` : ajoute des synonymes métier si type d'enquête détecté.
- `_extract_date_filter()` : parse "depuis le 20 mai", "en mai 2026", etc. en SQL `processed_at`.

### Phase 3 — Génération SQL (bypass programmatique + LLM fallback)
**Bypass programmatique** (pas d'appel LLM, 100% déterministe) :
- `_build_count_sql()` : comptages d'emails (combien, nombre, total).
- `_build_status_sql()` : listes de statut (pending, urgent, demandes clients en attente) — fuzzy matching sur mots-clés (tolère "deamdnes" → "demand").

**LLM fallback** : si le bypass ne match pas, le LLM génère `SQL: <SELECT>` via `CHARLIE_SYSTEM_PROMPT`.
- `parse_charlie_response()` extrait SQL + réponse.
- `is_safe_sql()` vérifie SELECT uniquement (ignore `replace(` pour normalisation numéros, v1.20.6).

### Phase 4 — Recherches parallèles (asyncio.gather)
| Tâche | Fonction | Quand |
|---|---|---|
| SQL local | `run_sql(db_agent_state, sql)` | Si SQL safe |
| Vault Cerveau2 | `query_vault(question, dossier_id)` | Toujours (sauf identité → `dossier_id=None`) |
| Fallback direct entités | `get_vault_note(path)` | Si question identitaire et fiches `04_entities/personnes/*.md` non trouvées par sqlite-vec |
| Mémoire | `query_memory(db, question, dossier_id)` | Toujours |
| Corrections locales | `query_corrections(db, limit=3)` | Toujours |
| Corrections Cerveau2 | `query_corrections_vault(question)` | Toujours |
| Archives historiques | `_search_historical_by_keyword()` / `_search_historical_all()` | Si `dossier_id` ou `year` |
| Dossiers Cerveau2 | `query_dossiers()` | Si comptage/liste de dossiers |

**Bases historiques** : `data/boite1.sqlite`, `boite2.sqlite`, `boite3.sqlite` (emails avant cutoff 2026-06-01).
**Base courante** : `data/agent_state.db` → table `mail_processed` (emails post-cutoff).

### Phase 5 — Nuage de liaison
Après réception des notes Cerveau2 :
- `_resolve_links()` scanne les `[[wikilinks]]` dans le contenu ET le frontmatter YAML.
- Clés relationnelles suivies : `employeur`, `adresse_principale`, `related`, `dossier`, `lieu`, `personne`, `entities`, **et familiales** (`epouse`, `mari`, `conjoint`, `compagne`, `fille`, `fils`, `enfant`, `pere`, `mere`, `soeur`, `frere`, `cousin`, `cousine`, `oncle`, `tante`).
- Les notes liées sont injectées dans le contexte LLM.

### Phase 6 — Construction du contexte
Ordre de priorité dans le prompt final :
1. **Corrections utilisateur** (Cerveau2 + locales) — PRIORITÉ ABSOLUE
2. **Résultats SQL** (anonymisés via `_sanitize_rows_for_prompt()` — expose `subject`, `received_at`, `category`, `status`, `priority`)
3. **Archives historiques** (répartition par catégorie + 50 premiers sujets)
4. **Notes du second cerveau** (vault Cerveau2 + notes liées)
5. **Souvenirs de Charlie** (mémoire courte)

### Phase 7 — Réponse
#### Bypass direct (Python, 0 ms, pas de LLM)
- **Comptages** : `_build_count_sql` + addition SQL + archives.
- **Listes de dossiers** : `query_dossiers()` ou fallback archives.
- **Identités** : `_extract_identity_answer()` parse frontmatter YAML pour suivre les wikilinks relationnels.
- **Dossier par ville** : `_extract_dossier_par_ville()`.
- **Entreprise (siège/adresse)** : `_extract_entreprise_info()`.

#### Résumé de dossier (v1.19.1)
- Détecté par `is_dossier_summary` (keywords : "résume", "synthèse", "infos", "détails" + `dossier_id` extrait).
- Bypass dédié : assemble les **contenus complets** des emails (body, pas preview) et appelle le LLM avec prompt ultra-ciblé : "UN SEUL PARAGRAPHE FLUIDE ET NARRATIF".
- Le LLM doit raconter l'histoire du dossier (client, demande, dates, montants financiers).
- `hide_rows=True` dans `CharlieResult` → le template web **ne montre pas** le tableau SQL brut sous le résumé.
- 2 tentatives avec le modèle chat, fallback sur `llm_model_fallback`.

#### LLM final (questions spécifiques)
- `complete(model=settings.llm_model_chat, ...)` — `kimi-k2.6:cloud` via Ollama Pro Cloud.

#### Garde-fous de secours (CRITIQUE)
Si le LLM dit "pas trouvé" / "aucune information" malgré des résultats SQL en base :
```python
_BAD = ("je n'ai pas trouvé", "aucun résultat", "aucune information", ...)
if any(p in response.lower() for p in _BAD) and rows:
    response = ""  # force secours
if not response and rows:
    # Reconstruit la réponse directement à partir des rows SQL
    lines = [f"J'ai trouvé **{len(rows)}** résultat(s) :", ""]
    for r in rows[:20]:
        # Affiche subject, date, catégorie, statut, priorité
```

---

## 4. Stack technique détaillée (v1.22.5)

| Couche | Outil | Version / Détail |
|---|---|---|
| Python | 3.11+ | VPS = 3.11, Mac CDAL = 3.14 |
| Concurrence | `asyncio` | Tout est `async def` |
| IMAP | `aioimaplib` | 2.0.1 |
| LLM router | **LiteLLM** | + post-traitement `_clean_reasoning()` (filtre traces raisonnement) |
| LLM chat + pipeline (classifier + generator + Charlie) | **`kimi-k2.6:cloud`** via Ollama Pro Cloud | `openai/kimi-k2.6:cloud` (reasoning model, extraction `reasoning_content`) |
| LLM fallback | **`glm-5.1:cloud`** via Ollama Pro Cloud | `openai/glm-5.1:cloud` |
| Embeddings | `text-embedding-3-small` via OpenRouter | API stateless, image Docker ~800MB au lieu de ~4GB |
| Vector store | `sqlite-vec` | Vit dans les DB existantes |
| Détection langue | `langdetect` | v1.21.0+ : `Language = str` (toutes BCP-47) |
| Aide lecture multilingue | `app/pipeline/translator.py` + `draft_renderer.py` | v1.21.0 : 4 blocs pour mails NL/EN/DE/ES/etc. |
| Email outbound principal | **IMAP Drafts** (V2a) | Brouillon dans `Drafts` de la boîte source, flag `\Draft` |
| Email outbound fallback | **Resend API** | `agent@digitalhs.biz`, alertes système |
| Web framework | **FastAPI** | 0.136.1 |
| Templating | **Jinja2** + HTMX | Pas de React |
| CSS | **Tailwind CSS** | CDN |
| Logs | `structlog` | JSON structuré, rotation 7j |
| Config | `pydantic-settings` | `.env` |
| Serveur | **uvicorn** | 0.47.0 |
| Reverse proxy | **Traefik** | Docker network `root_default` |

---

## 5. Cerveau2-Det — Le second cerveau

### Qu'est-ce que c'est
Cerveau2-Det est un **vault Markdown** structuré + une **API FastAPI** qui expose recherche sémantique, ingestion et anonymisation. Il vit sur le même VPS (`cerveau2-det.digitalhs.biz`).

### Structure du vault
```
vault/
├── 00_system/       ← Logs, index, config AGENTS.md
├── 01_inbox/        ← Raw (jamais édité manuellement)
├── 02_dossiers/     ← Dossiers d'enquête actifs
├── 03_doctrine/     ← Méthodologie, jurisprudence
├── 04_entities/     ← CRM transversal (personnes, sociétés, lieux)
│   ├── personnes/   ← Fiches individuelles (YAML frontmatter + wikilinks)
│   └── societes/    ← Fiches entreprises
├── 05_clients/      ← Coordonnées clients + facturation
├── 99_archives/     ← Dossiers clos
└── 99_attachments/  ← Binaires originaux
```

### Endpoints utilisés par Charlie
| Endpoint | Usage | Client |
|---|---|---|
| `POST /query` | Recherche sémantique + keyword | `app/cerveau_client.py::query_vault()` |
| `GET /notes/{path}` | Récupération directe d'une fiche (bypass sqlite-vec) | `app/cerveau_client.py::get_vault_note()` |
| `POST /ingest-email` | Alimentation continue emails | `app/cerveau_client.py::feed_correspondance()` |
| `POST /ingest-note` | Alimentation documents | `app/cerveau_client.py::feed_document()` |
| `GET /dossiers` | Liste des dossiers clients | `app/cerveau_client.py::query_dossiers()` |
| `GET /corrections` | Corrections utilisateur enregistrées | `app/cerveau_client.py::query_corrections_vault()` |
| `POST /corrections` | Pousser une correction | `app/cerveau_client.py::push_correction()` |

### Authentification
- **Bearer Token statique** (pas d'OAuth, pas de JWT).
- Secret défini dans `.env` : `CERVEAU2_API_SECRET`.
- Sur le VPS, le secret est dans `/opt/CERVEAU2/.env`.
- **Jamais commité** — toujours via `get_settings()`.

### Connexion depuis Charlie
Le client est dans `app/cerveau_client.py`. Il est **dégradation silencieuse** : si Cerveau2 est down, retourne `[]` et Charlie continue avec SQL + mémoire seuls.

### Limitation connue — sqlite-vec et entités manuelles
Les fiches `04_entities/personnes/*.md` créées manuellement **ne sont PAS automatiquement indexées** dans `chunk_embeddings` (sqlite-vec). La recherche sémantique Cerveau2 ne les trouve donc pas.
**Contournement** : `_vault_task()` dans `app/charlie.py` fait un `GET /notes/{path}` direct pour les slugs d'entités connus (christophe-dalla-valle, sarah-dalla-valle, daniel-hurchon, digitalhs-llc).

---

## 6. Déploiement production

### VPS
- **Host** : `root@69.62.110.165`
- **Répertoire** : `/opt/DETECTIVE`
- **Container** : `detective-agent` (service Docker Compose `detective`)
- **DNS** : `detective.digitalhs.biz` → A record `69.62.110.165`
- **Reverse proxy** : Traefik (network Docker `root_default` externe)
- **SSL** : Let's Encrypt via Traefik (`mytlschallenge`)

### Déployer depuis le Mac de CDAL (méthode standard)
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

### Déploiement rapide (hotfix sans rebuild complet)
Pour un hotfix Python pur (1-2 fichiers, pas de `requirements.txt` modifié) :
```bash
# 1. Commit + push depuis le Mac
git add app/charlie.py app/_version.py
git commit -m "fix(...): ..."
git push origin main

# 2. Pull + restart sur le VPS
ssh root@69.62.110.165 "cd /opt/DETECTIVE && git fetch --all && git reset --hard origin/main && docker compose restart detective"
```
> ⚠️ Le container DOIT avoir `./app:/app/app:ro` (dev mount) dans docker-compose.yml, sinon les modifs Python ne sont pas prises. Vérifier avec `docker exec detective-agent grep VERSION /app/app/_version.py`.

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
      - ./app:/app/app:ro          # dev mount (CRITIQUE pour hotfix)
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

### Cron externes (niveaux anti-crash 3-4)
- **Niveau 3** : `/etc/cron.d/detective-healthcheck` (toutes les 1 min) + `/usr/local/bin/detective-healthcheck.sh` + state dir `/var/lib/detective-healthcheck/`. Alerte Resend si 3 échecs consécutifs (anti-spam 1h/boîte).
- **Niveau 4** : Healthchecks.io `HEALTHCHECKS_PING_URL` dans `/opt/DETECTIVE/.env.production`. Ping envoyé par le cron watchdog (pas par Charlie).
- **Cleanup Docker** : `/etc/cron.d/detective-docker-clean` (`0 4 * * 0` = dimanche 4h). `docker system prune -af` avec safety list (ABORT si container prod stopped).

---

## 7. Données et bases SQLite

### `data/agent_state.db` (base courante — NE PAS ÉCRASER EN DEPLOY)
| Table | Rôle |
|---|---|
| `mail_processed` | Emails traités par le pipeline (post-cutoff 2026-06-01). Contient : id, imap_uid, mailbox_name, subject, sender, received_at (RFC 2822), category, draft_generated, draft_sent_at, processed_at, status, priority, ai_draft, **human_draft** (corrections Daniel, base du few-shot), reviewed_by, reviewed_at, sent_at, sent_by, body_preview, body, **delivered_at** (v1.22.2) |
| `charlie_memory` | Mémoire Charlie (feedback good/bad, corrections, faits auto-sauvés) |
| `email_attachment` | Pièces jointes détectées |
| `users` | Utilisateurs cockpit (auth magic link) |
| `audit_log` | Traçabilité actions cockpit |
| `app_settings` | Overrides runtime (LLM model, etc.) — ⚠️ purge obligatoire si modifs des défauts dans `app/config.py` |

### `data/boite1.sqlite`, `boite2.sqlite`, `boite3.sqlite` (archives historiques)
- Contiennent les emails **avant** le cutoff 2026-06-01.
- **Ne pas modifier** sans confirmation de CDAL.
- Charlie les interroge via `_search_historical_by_keyword()` et `_search_historical_all()`.

### Cutoff date
`process_since_date = "2026-06-01"` dans `.env`.
Le poller IMAP ne traite que les mails reçus depuis cette date. Les archives historiques contiennent tout l'historique.

---

## 8. Règles critiques (à respecter impérativement)

### Règle 1 — Tolérance zéro version
- Source unique : `app/_version.py`.
- **Jamais** `importlib.metadata`.
- À chaque release (nouveauté, bugfix, correction) → bump `app/_version.py` + mettre à jour `CHANGELOG.md`.
- `pyproject.toml` reste volontairement figé en `1.9.5` — c'est voulu (la version affichée par `pip show` n'est pas la version de prod).
- La version affichée dans le cockpit est lue dynamiquement depuis `app/_version.py`.

### Règle 2 — Ne jamais écrire dans les vraies boîtes Infomaniak en dev
- Mode `--dry-run` disponible sur les scripts d'action.
- En dev, utiliser un compte mail de test si besoin.
- La première vraie connexion en prod est surveillée par CDAL.

### Règle 3 — Ne jamais envoyer de mail réel via Resend en test
- Si `RESEND_API_KEY` est vide, le module skip avec un warning.
- En test automatisé, mocker ou laisser la clé vide.

### Règle 4 — Flags IMAP : `AgentProcessed` (sans `$`) + `AgentAttempted` (libère la queue même en cas de crash)
- Infomaniak rejette les flags avec préfixe `$`.
- Le code utilise `AgentProcessed` (succès) + `AgentAttempted` (tente, libère la queue même si crash) — lignes confirmées dans `imap_poller.py`.

### Règle 5 — Multilingue obligatoire : TOUJOURS en français (langue de travail Daniel)
- La réponse générée est **TOUJOURS en français**, même si le mail entrant est NL/EN/DE/ES.
- Pour les mails non-FR, le brouillon est enrichi (v1.21.0) avec 4 blocs : email d'origine + traduction FR + proposition FR + traduction langue source. Daniel peut lire le mail source dans sa langue ET voir la proposition FR.

### Règle 6 — Pas de framework JS lourd, pas de Docker au MVP en dev
- Cockpit : Jinja2 + HTMX, Alpine.js autorisé pour micro-interactivité.
- Dev : Python natif + SQLite. Docker uniquement en prod.
- Ne pas introduire Kubernetes, Swarm, Celery, Redis, Postgres, ORM lourd, React/Vue/Angular sans discussion explicite.

### Règle 7 — Ne jamais logger le contenu intégral d'un mail entrant
- Métadonnées uniquement (message-id, expéditeur, sujet, classification).
- Pour debug, ajouter un flag explicite `LOG_MAIL_BODY=true`.
- Idem pour les conversations Slack (`LOG_SLACK_CONVERSATION=true`).

### Règle 8 — Provider LLM pour Ollama Cloud = `openai/<model>` + `api_base=https://ollama.com/v1`
- **JAMAIS** `ollama_chat/<model>` (force litellm vers `localhost:11434`).
- **Vrai nom des modèles** : `kimi-k2.6:cloud` (avec `.6` et `:cloud`), `glm-5.1:cloud`.
- **JAMAIS** `kimi-k2` (404), `gemma4:31b` (obsolète), `claude-sonnet-4` (404 OpenRouter).
- Si un nouveau modèle ne répond pas, vérifier immédiatement provider + api_base + nom.

### Règle 9 — JAMAIS builder Docker sur le VPS de prod
- VPS `69.62.110.165` = VPS de PROD. Build Docker consomme tout CPU/RAM, site inaccessible 10-30 min.
- Builder en local Mac M4 Max, puis `docker save | ssh ... docker load`.
- Le script `deploy-to-vps.sh` intègre ce workflow. **Ne jamais utiliser `docker compose up -d --build` directement sur le VPS.**

### Règle 10 — Pas d'ajouts de nouveaux services d'orchestration
- Périmètre Docker actuel (1 service Compose + Traefik externe `root_default`) est figé.
- Pas de Kubernetes, Swarm, Nomad, Celery, Redis, etc. sans discussion explicite.

---

## 9. Bugs résolus et points de vigilance (état au 2026-06-16, v1.22.7)

### ✅ Bugs résolus récents (v1.22.0 → v1.22.5)

| # | Problème | Statut | Fichier | Notes |
|---|---|---|---|---|
| 1 | **Le LLM n'a JAMAIS vu le vrai Daniel depuis v1.22.0** — bug latent : `_load_daniel_fewshot()` utilisait `date(received_at) >= ?` en SQL, mais `received_at` est stocké en RFC 2822 (`Sat, 13 Jun 2026 05:41:38 +0000`), fonction SQLite `date()` ne parse pas → 0 candidat retourné | ✅ **Corrigé v1.22.4** | `app/pipeline/generator.py` | Le filtre temporel est FAIT EN PYTHON (regex RFC 2822) après récupération d'un panel de 200 candidats SQL. Pattern mutualisé de `scripts/cleanup_old_drafts.py`. Test live : 2 corrections Daniel injectées (mail #561 Soldermann 1990 chars + mail #83 Wastiau 997 chars) → 6122 chars dans le system prompt au lieu de 0. Le LLM imite enfin le format Daniel (intro "Monsieur X,", estimations HTVA × scénarios, mention "On vous téléphonera...", "Bien cordialement, Daniel Hurchon"). |
| 13 | **Tests rouges + robustesse mémoire Charlie** — `query_vault` retourne un tuple `(notes, answer)` mais les tests mockaient une liste ; `charlie_memory` plantait avec `no such table` si la DB n'était pas initialisée ; `_is_vault_relevant` référençait `_VAULT_KEYWORDS` indéfini ; `_extract_dossier_id` ne capturait pas "affaire XYZ123" | ✅ **Corrigé v1.22.5** | `app/charlie_memory.py` + `app/charlie.py` + `tests/test_cerveau_client.py` + `tests/test_charlie_vault.py` + `tests/test_cerveau_feed.py` | `init_memory_table()` appelée dans toutes les fonctions publiques de `charlie_memory.py` avec dégradation silencieuse sur `OperationalError`. `_VAULT_KEYWORDS` défini. Pattern `affaire` ajouté. **75/75 tests verts**. |
| 14 | **Bouton Copier cassé + traçabilité actions Daniel** — le bouton Copier sur `/conversation` ne copiait pas (Alpine.js inline peu fiable) ; CDAL suspectait des demandes clients `detective_belgium` approuvées automatiquement | ✅ **Corrigé v1.22.6** | `app/web/templates/app/conversation.html` + `app/web/admin.py` + `app/web/templates/admin/audit.html` | Listener vanilla JS délégué pour le copier. Section "Dernières actions de Daniel" dans `/admin/audit` montre les `draft_approve`/`draft_reject`/`status_update` de `user_id=2`. Investigation VPS a confirmé : les mails arrivent `pending`/`high`, Daniel les approuve via le cockpit. |
| 15 | **Qualification prospect insuffisante dans les brouillons** — les réponses de Charlie ne posaient pas assez de questions métier pour permettre à Daniel de faire un appel de clôture/devis solide | ✅ **Corrigé v1.22.7** | `app/prompts/prospect_qualification.md` + `app/pipeline/case_classifier.py` + `app/pipeline/generator.py` + `app/workers/imap_poller.py` + `app/config.py` | Directive de qualification intégrée au system prompt. Détection automatique du cas de figure (5 cas). Tarifs configurables. Brouillons générés aussi pour `prise_contact`. Modèle qualifier configurable, défaut `openai/gemma4:31b`. |
| 2 | **76 mails `demande_client` manqués** par classifier v1.21.5 (trop conservateur sur les cas ambigus) | ✅ **Corrigé v1.22.1** | `app/pipeline/classifier.py` + `scripts/backfill_demande_client.py` | Prompt classifier durci. Backfill script one-shot re-classifie + génère brouillons pour les 76 mails historiques ratés. |
| 3 | **153 brouillons en DB mais 0 dans Drafts IMAP** — poller ne re-livre pas les brouillons existants (`is_new` condition) | ✅ **Corrigé v1.22.2** | `scripts/deliver_pending_drafts.py` + colonne `delivered_at` | Script one-shot livre les brouillons existants en IMAP Drafts. Bilan : 153/154 livrés. 14 échecs dus à CRLF dans sujets (Google Calendar invitations) → corrigé via `_sanitize_subject()`. |
| 4 | **127 vieux brouillons accumulés dans Drafts IMAP** (avant 2026-06-02) | ✅ **Corrigé v1.22.3** | `scripts/cleanup_old_drafts.py` | Script one-shot avec dry-run par défaut, SELECT probe (v1.21.9 fix), SEARCH SUBJECT, store +FLAGS \Deleted + EXPUNGE. Deux passes : 80 supprimés (cutoff 2026-01-02) + 47 supprimés (cutoff 2026-06-02). |
| 5 | **Mail #105 = spam** (juste la signature Daniel, pas de contenu) | ✅ Marqué spam | DB update | `status='spam'` (catégorie manuelle). |
| 6 | **18 mails pré-cutoff en `pending/high`** | ✅ Marqués approved/normal | DB update | `status='approved', priority='normal'` pour received_at < 2026-06-02. |
| 7 | **CRLF dans sujets** (Google Calendar, convocations A.G.) — interdit par RFC 5322 | ✅ **Corrigé v1.22.2** | `scripts/deliver_pending_drafts.py::_sanitize_subject()` | Remplace CRLF/CR/LF par espace. |
| 8 | **gemma4:31b obsolète + claude-sonnet-4 404** | ✅ **Corrigé v1.21.1** | `app/config.py` + `.env.production` | Vrai nom = `kimi-k2.6:cloud`. Idem pour `glm-5.1` → `glm-5.1:cloud`. `ollama_pro_base_url` corrigé de `/api` vers `/v1`. Table `app_settings` prod purgée des 3 entrées obsolètes. |
| 9 | **kimi-k2.6:cloud est un reasoning model** — réponse dans `reasoning_content` pas dans `content` | ✅ **Corrigé v1.21.1** | `app/llm/router.py` | Extraction : si `content` vide, fallback sur `reasoning_content`. |
| 10 | **Traces de raisonnement kimi-k2.6 polluent les brouillons** — "L'utilisateur demande...", "The user wants...", "Refonte :", "Version plus X :", "C'est mieux.", etc. | ✅ **Corrigé v1.21.2** | `app/llm/router.py::_clean_reasoning()` | 30+ patterns regex filtrent les artefacts. EN + FR + listes + guillemets + auto-critique post-mail. |
| 11 | **Poller IMAP crash en boucle** sur boîte `detective_belgique` (3 bugs cumulés) | ✅ **Corrigé v1.21.3** | `app/workers/imap_poller.py` + `app/alerts.py` | Bug 1 : `_decode_header` crash sur charset `unknown-8bit`. Bug 2 : `_persist` crash sur `Header` objects. Bug 3 : retry éternel. Fix : try/except englobant + nouveau flag `AgentAttempted`. 19 tests de résilience. Alerte Resend si ≥5 crashes/boîte (anti-spam 1h/boîte). |
| 12 | **Filtre date hardcodé incohérent** — code dit `2026-06-01`, .env disait `2026-05-01` (régression silencieuse) | ✅ **Corrigé v1.21.4** | `app/workers/imap_poller.py` + `.env.example` + `.env.production` | Tous alignés à `2026-06-01`. |

### 🔴 Points de vigilance ouverts (état au 2026-06-16)

#### Point de vigilance #1 — RAG cassé depuis 2026-05-28 (CRITIQUE)
**Pire que prévu** : le RAG est cassé sur les **3 boîtes** (pas seulement `boite2`).

**État** :
- `boite1.sqlite` : table `pairs` existe mais **0 rows** (était censé en avoir 2042)
- `boite2.sqlite` : table `pairs` **n'existe pas**
- `boite3.sqlite` : table `pairs` **n'existe pas**

**Cause** : le bootstrap a crashé le 2026-05-28 avec `litellm.BadRequestError: LLM Provider NOT provided. ... You passed model=intfloat/multilingual-e5-large` — le script utilisait encore l'ancien embedder local `e5-large` alors que la v1.18.0 avait basculé vers `openai/text-embedding-3-small` via OpenRouter. **Le bootstrap n'a jamais été ré-exécuté après la bascule.** La purge de 7781 vieux mails (v1.18.1, le même jour) a contribué à vider `boite1`.

**Conséquence** : tous les brouillons générés depuis le 2026-05-28 ont RAG=0. La v1.22.0+ (refonte qualité LLM avec personnalité + few-shot + Cerveau2) compense en partie, mais le RAG historique est HS.

**Fix (à planifier)** :
```bash
ssh root@69.62.110.165
cd /opt/DETECTIVE
docker compose exec detective python -m scripts.bootstrap_embeddings
```

**Hors-scope aujourd'hui** (décision CDAL) — à traiter avant V2c (feedback loop qualité) pour que les corrections Daniel soient comparées à des brouillons avec RAG actif. Vérifier aussi les catégories de `boite2` (10 catégories dont `PRISE_CONTACT:182` majoritaire) et `boite3` (12 catégories dont `INVESTIGATION_ENTREPRISE:399`) avant de relancer.

**Note d'espoir** : depuis v1.22.4, le few-shot few-shot compense une partie de la perte RAG (2 corrections Daniel injectées en attendant la réindexation).

#### Point de vigilance #2 — Provider litellm pour Ollama Cloud (CRITIQUE v1.21.1)
`ollama_chat/<model>` force litellm vers `localhost:11434` (Ollama **local**). Le provider correct pour Ollama **Cloud** est `openai/<model>` avec `api_base=https://ollama.com/v1`.
**Vrai nom des modèles** : `openai/kimi-k2.6:cloud` (principal + classifier + chat), `openai/glm-5.1:cloud` (fallback).
**Si un nouveau modèle ne répond pas** → vérifier immédiatement provider (openai/ vs ollama_chat/), l'URL api_base (`/v1` pas `/api`), et que le nom de modèle existe sur ollama.com/library.

#### Point de vigilance #3 — kimi-k2.6:cloud est un reasoning model
Sa réponse finale est dans `message.reasoning_content`, pas dans `message.content` (vide). Le wrapper `complete()` extrait automatiquement, MAIS :
- Soit utiliser un autre modèle non-reasoning si on veut du contenu direct
- Soit accepter le coût (raisonnement = plus de tokens) + le post-traitement `_clean_reasoning()`

#### Point de vigilance #4 — Traces de raisonnement kimi-k2.6 (CRITIQUE v1.21.2)
Le modèle produit des métadiscours parasites : "L'utilisateur demande...", "Let me analyze...", "Points importants :", "Refonte :", "Version plus X :", "C'est mieux.", etc. Le post-traitement `_clean_reasoning()` filtre ~30 patterns, **MAIS** si un nouveau type d'artefact apparaît, il faut **enrichir `_REASONING_LINE_PATTERNS`** dans `app/llm/router.py`. Le cleaning n'est jamais "complet" — c'est une bataille continue.

#### Point de vigilance #5 — Tables `app_settings` peuvent être stale
Si tu modifies `app/config.py` (défauts `llm_model_*`), il faut aussi **purger la table `app_settings` en prod** (clés `llm_model_default`, `llm_model_classifier`, `llm_model_fallback`) — sinon les valeurs runtime en DB écrasent tes défauts.
Procédure :
```python
import sqlite3
conn = sqlite3.connect("/app/data/agent_state.db")
conn.execute("DELETE FROM app_settings WHERE key LIKE 'llm_model%'")
conn.commit()
```

#### Point de vigilance #6 — Dense search Cerveau2 = implicit AND (CRITIQUE)
Quand on envoie une phrase complète à Cerveau2 (`"retrouve le dossier avec téléphone 0488/411192"`), le dense retrieval calcule un vecteur moyen de TOUS les concepts. Les documents qui ne contiennent pas tous les mots ont un score faible.
**Solution** : pour les recherches factuelles, n'envoyer que les identifiants précis (`"0488411192"`) — voir `docs/CERVEAU2_RECHERCHE_FACTUELLE.md`.

#### Point de vigilance #7 — Faux négatifs du LLM de synthèse Cerveau2
Le LLM de synthèse Cerveau2 peut écrire "je ne trouve pas" alors que le document est dans le `context`. C'est un biais du modèle, pas un bug du retrieval.
**Solution** : vérifier si le numéro/nom recherché est présent dans `vault_answer` (la chaîne brute retournée par Cerveau2) malgré les patterns négatifs — voir `_bad_vault` et la logique de faux négatif dans `app/charlie.py`.

#### Point de vigilance #8 — Entités Cerveau2 non indexées dans sqlite-vec
Les fiches `04_entities/personnes/*.md` créées manuellement ne sont pas dans l'index sémantique. Le fallback direct `GET /notes/{path}` contourne ce problème, mais la **vraie solution** serait de réindexer le vault Cerveau2. Toutes les tentatives sur le VPS ont échoué (problèmes volume mount, extension sqlite3 vec0 manquante).

#### Point de vigilance #9 — Périmètre Cerveau2 (à garder en tête)
**Important** : les hotfix v1.21.x (poller IMAP), v1.22.0 (LLM), v1.22.1 (classifier), v1.22.2 (deliver), v1.22.3 (cleanup), v1.22.4 (few-shot) sont **100% côté Charlie** (instance Detective.be). Aucun changement n'a été fait dans :
- `app/cerveau_client.py` (wrapper HTTP Charlie → Cerveau2)
- `app/cerveau_feed.py` (wrapper d'ingestion)
- Le serveur `CERVEAU2-DEtective` (`/Users/cdal/DEV_APP_CLAUDE/CERVEAU2-DEtective/`)
- Le produit `SECONDCERVEAU-PRO` (`/Users/cdal/DEV_APP_CLAUDE/SECONDCERVEAU-PRO/`)
- L'instance `CDAL2` (`/Users/cdal/DEV_APP_CLAUDE/CDAL2/`)

Le serveur Cerveau2 n'est pas affecté par ces fixes. Si tu réutilises Charlie comme base pour un nouveau client (via `SECONDCERVEAU-PRO`), les patches sont **réutilisables** — voir `docs/PATTERNS_FROM_CHARLIE_V1.21.3.md` pour le détail d'implémentation.

---

## 10. Procédures d'urgence

### Redémarrage container
```bash
ssh root@69.62.110.165
cd /opt/DETECTIVE
docker compose down
docker compose up -d
docker compose logs -f --tail 20
```

### Hotfix rapide (sans rebuild complet)
```bash
# Sur le Mac
git add <fichiers>
git commit -m "fix(...): ..."
git push origin main

# Sur le VPS
ssh root@69.62.110.165 "cd /opt/DETECTIVE && git fetch --all && git reset --hard origin/main && docker compose restart detective"
```
> ⚠️ Le container doit avoir `./app:/app/app:ro` (dev mount) pour que les modifs Python soient prises sans rebuild.

### Rollback rapide
```bash
# Sur le VPS
ssh root@69.62.110.165 "cd /opt/DETECTIVE && git log --oneline -5"
# Identifier le commit précédent
ssh root@69.62.110.165 "cd /opt/DETECTIVE && git reset --hard <COMMIT_HASH> && docker compose restart detective"
```

### Vérifier l'état
```bash
# Health (couvre Charlie + Traefik + DNS + cert TLS)
curl -s -o /dev/null -w "%{http_code}" https://detective.digitalhs.biz/health   # 200 = OK
curl -s -o /dev/null -w "%{http_code}" https://detective.digitalhs.biz/auth/login   # 200 = OK

# Logs container
ssh root@69.62.110.165 "cd /opt/DETECTIVE && docker compose logs --tail 50"

# Version
ssh root@69.62.110.165 "docker exec detective-agent python -c 'from app._version import VERSION; print(VERSION)'"

# DB state
ssh root@69.62.110.165 "docker exec detective-agent sqlite3 /app/data/agent_state.db 'SELECT count(*), category FROM mail_processed GROUP BY category'"
```

### Rejouer le pipeline pour un mail (debug brouillon)
```bash
ssh root@69.62.110.165 "docker exec detective-agent sqlite3 /app/data/agent_state.db \"UPDATE mail_processed SET draft_generated=0, ai_draft=NULL, delivered_at=NULL WHERE id=<MAIL_ID>\""

# Puis attendre le prochain cycle de poller (5 min) ou forcer via retry-draft :
ssh root@69.62.110.165 "curl -X POST https://detective.digitalhs.biz/api/drafts/<MAIL_ID>/retry -H 'Cookie: session=...'"
```

### Livrer un brouillon existant en IMAP Drafts (si backfill manqué)
```bash
ssh root@69.62.110.165 "docker exec detective-agent python -m scripts.deliver_pending_drafts --apply"
```

### Nettoyer les vieux brouillons IMAP Drafts
```bash
# Dry-run d'abord
ssh root@69.62.110.165 "docker exec detective-agent python -m scripts.cleanup_old_drafts --mailbox detective_belgique"

# Apply
ssh root@69.62.110.165 "docker exec detective-agent python -m scripts.cleanup_old_drafts --apply --mailbox detective_belgique"
```

---

## 11. Contacts et ressources

| Ressource | Où trouver |
|---|---|
| Spec technique | `docs/SPEC.md` (figée 2026-05-13, désalignée sur certains points avec la prod actuelle) |
| État réel du projet | `HANDOVER.md` (ce fichier, source de vérité opérationnelle) |
| Roadmap | `docs/ROADMAP.md` |
| Contexte business | `docs/CONTEXT.md` |
| Runbook incidents | `docs/RUNBOOK.md` |
| Guide Cerveau2 | `docs/CERVEAU2_INTEGRATION.md` |
| API Cerveau2 | `docs/CERVEAU2_API.md` |
| Recherche factuelle Cerveau2 | `docs/CERVEAU2_RECHERCHE_FACTUELLE.md` |
| Patterns réutilisables (v1.21.3) | `docs/PATTERNS_FROM_CHARLIE_V1.21.3.md` |
| Changelog | `CHANGELOG.md` |
| Instructions Claude Code | `CLAUDE.md` |
| Intégrateur | CDAL — `cdal@digitalhs.biz` |
| Client | Daniel Hurchon — Detective.be |
| VPS | `root@69.62.110.165` (`/opt/DETECTIVE`) |
| Cerveau2 | `cerveau2-det.digitalhs.biz` |
| Cockpit | `detective.digitalhs.biz` |
| Healthchecks.io | `https://healthchecks.io` (check "Charlie-detective") |
| Resend | `https://app.resend.com/emails` (filtre "Watchdog VPS" / "Charlie") |
| Slack | Canal `#detective` (workspace CDAL) |

---

## 12. Checklist reprise agent

Avant de modifier quoi que ce soit :

- [ ] **Lire `CLAUDE.md`** (conventions, garde-fous, stack)
- [ ] **Lire ce `HANDOVER.md`** (contexte actuel, bugs résolus, points de vigilance, procédures urgence)
- [ ] **Vérifier `app/_version.py`** — est-ce bien `1.22.4` ?
- [ ] **Vérifier `CHANGELOG.md`** — la dernière version est-elle documentée avec bilan déploiement ?
- [ ] **Vérifier `docs/ROADMAP.md`** — quelle phase est en cours (V2b/V2c) ?
- [ ] **Tester le healthcheck** : `curl -s https://detective.digitalhs.biz/health` → `{"ok":true}`
- [ ] **Vérifier le poller IMAP** : `ssh root@69.62.110.165 "docker compose logs --tail 20 | grep poller"`
- [ ] **Vérifier le few-shot** (depuis v1.22.4) : `docker exec detective-agent python -c "from app.pipeline.generator import _load_daniel_fewshot; print(len(_load_daniel_fewshot()))"` → doit retourner `> 0`
- [ ] **Lire les 100 dernières lignes de `app/charlie.py`** pour comprendre le pipeline actuel (bypass SQL, garde-fous, nuage de liaison)
- [ ] **Si une décision n'est pas dans la spec** → demander à CDAL

### Si tu dois faire un hotfix (procédure rapide)
1. Identifier le bug dans le code
2. Modifier le fichier Python concerné
3. Bump `app/_version.py` (ex: `1.22.4` → `1.22.5`)
4. Ajouter entrée dans `CHANGELOG.md` (section Fixé)
5. `git add ... && git commit -m "fix(...): ..." && git push origin main`
6. `ssh root@69.62.110.165 "cd /opt/DETECTIVE && git fetch --all && git reset --hard origin/main && docker compose restart detective"`
7. Vérifier : `curl -s https://detective.digitalhs.biz/health` + logs container

---

## 13. Sécurité anti-crash silencieux — INSTALLÉ EN PROD (état au 2026-06-15)

**Contexte** : le 2026-06-04, CDAL a explicitement demandé « plus jamais de crash sans être prévenu : c'est impossible et interdit ». La v1.21.5 a ajouté Slack + heartbeat startup/shutdown (niveaux 1-2). Les niveaux 3-4 ont été installés manuellement le 2026-06-05 puis vérifiés opérationnels au 2026-06-15.

**Statut des 4 niveaux (vérifié en SSH)** :
1. ✅ **Niveau 1 — Slack in-app** (`notify_startup` / `notify_shutdown` v1.21.5) : actif
2. ✅ **Niveau 2 — Resend in-app** (`alert_poller_persistent_failure` v1.21.3, anti-spam 1h/boîte) : actif
3. ✅ **Niveau 3 — Cron watchdog externe** : `/etc/cron.d/detective-healthcheck` (toutes les 1 min) + script dans `/usr/local/bin/detective-healthcheck.sh` + state dir `/var/lib/detective-healthcheck/`. Alerte Resend si 3 échecs consécutifs.
4. ✅ **Niveau 4 — Uptime checker Healthchecks.io** : `HEALTHCHECKS_PING_URL=https://hc-ping.com/1d6f6a30-...` dans `.env.production`. Ping envoyé par le cron watchdog (pas par Charlie, design voulu — le cron continue même si Charlie est HS).

**Bonus** : `docker-clean` cron hebdo dim 4h (`/etc/cron.d/detective-docker-clean`) installé, gain attendu 64GB+ de build cache.

### Détail Niveau 3 — Watchdog externe (cron VPS)

**Pourquoi** : si Charlie crash COMPLÈTEMENT (OOM, kill -9, deadlock asyncio), aucune alerte in-app ne s'exécute. Il faut un script externe au processus qui ping `/health` et alerte Resend si down.

**État** : ✅ installé et opérationnel. Script présent dans `/opt/DETECTIVE/scripts/detective-healthcheck.sh` ET dans `/usr/local/bin/`. Cron `/etc/cron.d/detective-healthcheck` actif. State dir `/var/lib/detective-healthcheck/` créé (survit aux redéplois Docker, hors conteneur).

**Endpoint pingé** : `https://detective.digitalhs.biz/health` (HTTPS public via Traefik, port 8080 du conteneur mappé). Teste **tout le stack** : Charlie + Traefik + DNS + cert TLS.

**Décisions CDAL** :
- **Anti-spam** : 1 alerte max par heure
- **Pas d'auto-restart** : on alerte seulement, le restart reste manuel. Évite crashloop

**Procédure d'install VPS** :
```bash
ssh root@69.62.110.165
install -m 755 /opt/DETECTIVE/scripts/detective-healthcheck.sh /usr/local/bin/
mkdir -p /var/lib/detective-healthcheck
chmod 755 /var/lib/detective-healthcheck
chown root:root /var/lib/detective-healthcheck

cat > /etc/cron.d/detective-healthcheck <<'EOF'
# Watchdog Charlie AI : ping /health toutes les minutes, alerte si 3 échecs consécutifs
* * * * * root /usr/local/bin/detective-healthcheck.sh
EOF
chmod 644 /etc/cron.d/detective-healthcheck

cat > /etc/logrotate.d/detective-healthcheck <<'EOF'
/var/log/detective-healthcheck.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
    create 0644 root root
}
EOF
```

### Détail Niveau 4 — Healthchecks.io externe

**Pourquoi** : le cron watchdog niveau 3 tourne SUR le VPS. Si le VPS lui-même crash, le cron ne s'exécute plus → aucune alerte. Il faut un service externe qui reçoit un ping et alerte si silence.

**État** : ✅ installé et opérationnel. `HEALTHCHECKS_PING_URL=https://hc-ping.com/1d6f6a30-126f-4f71-a622-b3a5fecdc50c` dans `/opt/DETECTIVE/.env.production`.

**Code livré** (`scripts/detective-healthcheck.sh`) :
```bash
# Ping Healthchecks.io (niveau 4 anti-crash silencieux)
if [ -n "${HEALTHCHECKS_PING_URL:-}" ]; then
    curl --max-time 5 -fsS -o /dev/null "$HEALTHCHECKS_PING_URL" 2>/dev/null || true
fi
```

**Setup Healthchecks.io (manuel CDAL, ~5 min)** :
1. Aller sur https://healthchecks.io → Sign up
2. **New check** : Name = `Charlie-detective`, Period = `5 minutes`, Grace = `3 minutes`
3. Copier l'**URL ping** affichée
4. Ajouter dans `/opt/DETECTIVE/.env.production` : `HEALTHCHECKS_PING_URL=https://hc-ping.com/<UUID>`

**Critère d'acceptation** :
- ✅ Healthchecks.io affiche "Up" (vert) dans les 30s suivant le 1er ping
- ✅ Si Charlie est down mais le cron tourne → ping continue → "Up" (on distingue Charlie-down de VPS-down)
- ✅ Si le cron ne tourne pas (VPS down) → pas de ping → "Down" après 8 min (5 + 3 grace) → email CDAL

### Limitation connue
- **Niveau 3 (cron)** : si Charlie est down mais le cron tourne, on ne sait pas distinguer du tout-up. Mais c'est détecté par Healthchecks.io quand même.
- **Niveau 4 (Healthchecks.io gratuit)** : retard de ~1-3 min pour l'envoi d'email en cas de downtime. Acceptable.
- **3 niveaux de redondance** : si les 3 disent rien, c'est qu'Internet est coupé 😄

### Cleanup Docker hebdo (niveau bonus)

**Pourquoi** : VPS à 60% disque. Accumulation images Docker / build cache / vieux volumes risque de remplir disque dans 2-3 mois → crash Charlie.

**État** : ✅ installé. Script `scripts/detective-docker-clean.sh` dans `/opt/DETECTIVE/scripts/` ET `/usr/local/bin/`. Cron `/etc/cron.d/detective-docker-clean` (`0 4 * * 0` = dimanche 4h).

**Safety list** : avant tout prune, le script vérifie qu'**aucun container de prod** n'est en status `stopped`/`exited`/`dead`/`created`. Si oui → **ABORT** du prune + alerte Slack. Préfixes surveillés : `detective-`, `cerveau2-`, `cdal2-`, `magicreator-`, `mondayupartner-`, `icoonebali-`, `photobooth`, `scrappingtool`, `n8n`, `traefik`.

---

## Note pour le prochain agent

État au **2026-06-16** : v1.22.5 en cours de validation. Hotfix de robustesse mémoire + tests Cerveau2 corrigés (75/75 tests verts). Les chantiers v1.22.x (backfill 76 mails, deliver 153 brouillons, cleanup 127 vieux drafts, fix few-shot learning) sont terminés. Le seul chantier ouvert significatif reste le **bug RAG (point de vigilance #1)** — à traiter avant V2c (feedback loop qualité Daniel). Pour le reste, voir HANDOVER §12 (checklist reprise) et §13 (4 niveaux anti-crash silencieux opérationnels).

**Philosophie CDAL** : MVP simple d'abord, V2 quand qualité prouvée. Pas d'over-engineering. ROI client : "solde 24/7" sans surdimensionner. Communique court en français, écrit parfois avec des fautes de frappe rapides — décoder l'intention.

---

*Document mis à jour le 2026-06-16 pour la v1.22.5 de Detective.be Agent IA.*

