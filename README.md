# Detective.be — Agent IA email (Charlie)

Agent IA Python qui assiste **Daniel Hurchon** (Detective.be, cabinet d'enquêtes privées) dans le traitement de ses emails clients.

L'agent surveille 3 boîtes Infomaniak (3 marques : Detective Belgique FR, Detective Belgium EN/multi, DPDH Investigations), classifie les mails entrants en 8 catégories, assigne une priorité intelligente, et génère des brouillons de réponse "à la Daniel" pour les demandes clients. **Multilingue** : TOUJOURS en français (langue de travail), avec aide lecture 4 blocs pour mails NL/EN/DE/ES/etc.

> **Pour Claude Code** : lis `CLAUDE.md` en premier pour le contexte, les conventions et les garde-fous.
> **Pour un nouvel agent** : lis `HANDOVER.md` (état complet, bugs, procédures urgence).
> **Pour la version exacte en prod** : `app/_version.py` (source unique) + `CHANGELOG.md` (historique).

---

## Architecture en une image

```
[3 boîtes Infomaniak IMAP]
         ↓ polling 5 min
[Worker asyncio Python]
         ↓
[Pipeline]
  Pré-filtre règles    → newsletter / facture / phishing / rappel / demande_client évidents → tag & skip
  Classification LLM   → 8 catégories avec few-shots
  Priorité intelligente → demande client chaude = HIGH
  Extraction pièces jointes → stockage local + ingestion Cerveau2 (100%, zéro tolérance)
  Si demande_client / prise_contact :
    Détection langue (toutes BCP-47)
    Classification fine du cas métier (incapacité, filature, recherche personne, récupération dette, etc.)
    Brouillon qualifiant déterministe (v1.22.7+) : questions structurées + tarifs + règles métier codées
    RAG sur paires Q/R historiques (sqlite-vec + text-embedding-3-small) pour les autres catégories
    Génération LLM few-shot style Daniel + personnalité
  Chat AI Charlie (cockpit + Slack) :
    Recherche SQL + archives historiques (boite1/2/3) + Cerveau2 vault + mémoire
    Modèle chat : openai/kimi-k2.6:cloud (Ollama Pro Cloud, reasoning model)
         ↓
[Flag IMAP AgentProcessed]        → idempotence
[Flag IMAP AgentAttempted]        → libère la queue même en cas de crash (v1.21.3)
[DB SQLite mail_processed]        → stockage + cockpit web + table email_attachment
[Brouillon IMAP \Draft]           → dans Drafts de la boîte source (V2a, livré v1.17+)
         ↓
[Cockpit web FastAPI]             → detective.digitalhs.biz
  - Auth magic link
  - Inbox filtrable (tabs, checkboxes boîtes, recherche texte, tri) + badge PJ
  - Édition inline catégorie/statut/priorité (HTMX)
  - Conversation détaillée avec viewer pièces jointes (preview texte, download)
  - Chat AI Charlie (SQL + Cerveau2 vault + mémoire courte)
  - Dashboard admin (stats, settings LLM, audit logs, télémétrie)
  - Endpoint POST /api/drafts/{id}/retry (régénération manuelle brouillon, v1.21.0)
  - Simulateur de brouillon super-admin `/admin/draft-simulator` (v1.22.9+) : tester les brouillons sans envoyer de vrai email
[Cerveau2 vault FastAPI]          → cerveau2-det.digitalhs.biz
  - Ingestion continue emails + pièces jointes (zéro tolérance sur le skip)
  - Recherche globale insensible aux accents, sans troncation
  - Blindé path-traversal + audit log
[Slack Bot Charlie AI]            → @mention ou DM sur #detective
  - Résumés de dossiers narratifs
  - Recherche factuelle
  - Feedback loop
[4 niveaux anti-crash silencieux]  → Slack + Resend + cron watchdog + Healthchecks.io
```

Spec complète : [`docs/SPEC.md`](docs/SPEC.md). Roadmap : [`docs/ROADMAP.md`](docs/ROADMAP.md). Runbook incidents : [`docs/RUNBOOK.md`](docs/RUNBOOK.md).

---

## Setup local (Mac de CDAL)

```bash
# 1. Environnement Python
python3 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"

# 2. Config
cp .env.example .env
# → éditer .env avec : 3× app passwords Infomaniak (boîte1/2/3),
#   clé Ollama Pro (OLLAMA_PRO_API_KEY), clé Resend, CERVEAU2_API_SECRET,
#   tokens Slack Bot, PUBLIC_BASE_URL

# 3. Données : déposer les 3 DB SQLite anonymisées dans data/
#   data/boite1.sqlite
#   data/boite2.sqlite
#   data/boite3.sqlite

# 4. Bootstrap one-shot (S1) — après que les DB soient là
python -m scripts.bootstrap_embeddings   # indexe les paires dans pairs_vec (sqlite-vec)
python -m scripts.extract_personality    # génère app/prompts/personality_daniel.txt
python -m scripts.bootstrap_cerveau2     # initialise Cerveau2 (une fois)

# 5. Lancer l'agent
python -m app.main
```

> ⚠️ **Bug latent RAG (point de vigilance #9 du HANDOVER)** : si tu n'as pas re-bootstrappé les `pairs_vec` après la bascule embedder local → OpenRouter (v1.18.0 du 2026-05-28), le RAG retournera 0 résultat sur les 3 boîtes. Voir HANDOVER §9.

---

## Déploiement production (VPS Hostinger + Docker + Traefik)

L'agent est containerisé et exposé via Traefik sur `detective.digitalhs.biz`.

```bash
# Depuis le Mac de CDAL — one-shot avec pre-flight checks
bash scripts/deploy-to-vps.sh
```

Le script vérifie automatiquement :
- Branche `main` active
- Aucune modification non commitée
- Push automatique des commits locaux sur GitHub
- Backup `agent_state.db` côté VPS
- rsync `data/` (exclut `agent_state.db`)
- Healthcheck post-deploy (12 tentatives × 5s)

> ⚠️ **JAMAIS `docker compose up -d --build` directement sur le VPS de prod** (consomme tout le CPU/RAM, site inaccessible 10-30 min). Builder en local Mac M4 Max, puis `docker save | ssh ... docker load`. Le script `deploy-to-vps.sh` intègre ce workflow.

**Manuellement sur le VPS** (si besoin) :
```bash
ssh root@69.62.110.165
cd /opt/DETECTIVE
git fetch --all && git reset --hard origin/main
docker compose restart detective   # si juste un hotfix Python (pas de build)
# ou bien :
docker compose up -d --build      # si requirements.txt ou Dockerfile modifiés
```

**Prérequis sur le VPS** :
- Docker + Docker Compose
- Réseau Traefik externe `root_default`
- DNS A record `detective.digitalhs.biz` → `69.62.110.165`
- Watchdog cron `/etc/cron.d/detective-healthcheck` (déjà installé, ping /health toutes les minutes)
- Cleanup Docker hebdo `/etc/cron.d/detective-docker-clean` (déjà installé, dim 4h)
- Healthchecks.io `HEALTHCHECKS_PING_URL` dans `/opt/DETECTIVE/.env.production`

---

## Stack technique (état v1.22.16)

| Couche | Choix |
|---|---|
| Runtime | Python 3.11+ (VPS = 3.11, Mac CDAL = 3.14) |
| Concurrence | `asyncio` |
| IMAP | `aioimaplib` |
| LLM router | **LiteLLM** + post-traitement `_clean_reasoning()` (30+ patterns regex, v1.21.2) |
| LLM principal (classifier + generator + chat) | **`openai/kimi-k2.6:cloud`** via Ollama Pro Cloud (20€/mois), `api_base=https://ollama.com/v1` — **reasoning model** (extraction `reasoning_content`) |
| LLM fallback | **`openai/glm-5.1:cloud`** via Ollama Pro Cloud |
| Embeddings | **`openai/text-embedding-3-small`** via OpenRouter (API stateless, image Docker ~800MB au lieu de ~4GB) |
| Vector store | **`sqlite-vec`** (extension SQLite, vit dans les DB existantes) |
| Détection langue | **`langdetect`** — `Language = str` (toutes BCP-47, v1.21.0+) |
| Aide lecture multilingue | `app/pipeline/translator.py` + `draft_renderer.py` (v1.21.0) — 4 blocs si mail ≠ FR |
| Email outbound principal | **IMAP Drafts** (V2a) — flag `\Draft`, dossier auto-découvert via SELECT probe |
| Email outbound fallback | **Resend API** (`agent@digitalhs.biz`) — uniquement si APPEND IMAP échoue + alertes système |
| Canal Boss ↔ Charlie | **Slack Bot** (`slack_bolt`) sur `#detective` — Telegram module conservé inactif |
| Cerveau2-Det | Vault Markdown + API FastAPI sur `cerveau2-det.digitalhs.biz` (sqlite-vec + E5-large, ingestion 100% emails + PJ) |
| Cockpit web | **FastAPI + HTMX + Tailwind CDN** sur `:8080` exposé via Traefik |
| Healthcheck | FastAPI sur `127.0.0.1:8080/health` (interne, mappé via Traefik HTTPS) |
| Service prod | **Docker + Docker Compose + Traefik** (VPS Hostinger KVM8) |
| Logs | `structlog` (JSON structuré, rotation 7j) |
| Config | `pydantic-settings` depuis `.env` |
| Version | Source unique `app/_version.py` (`VERSION = "1.22.14"`) — `pyproject.toml` figé en 1.9.5 (volontaire) |

**Ne PAS introduire** sans discussion : Kubernetes, Swarm, Celery, Redis, Postgres, ORM lourd, framework JS front (React/Vue/Angular). Le périmètre Docker actuel (1 service Compose + Traefik externe) est figé.

---

## Layout

```
DETECTIVE_BE/
├── CLAUDE.md                    # Instructions Claude Code (à lire en 1er)
├── README.md                    # Ce fichier
├── HANDOVER.md                  # État complet + bugs + procédures pour nouvel agent
├── CHANGELOG.md                 # Historique des versions
├── pyproject.toml               # Dépendances, ruff, pytest — version volontairement figée
├── .env.example                 # Template config
├── Dockerfile                   # Image Docker Python 3.11
├── docker-compose.yml           # Traefik + labels
├── docs/
│   ├── SPEC.md                  # Spec technique (figée 2026-05-13 — désalignée sur certains points, voir HANDOVER)
│   ├── ROADMAP.md               # Découpage S1→V2 + état courant
│   ├── CONTEXT.md               # Contexte business client
│   ├── RUNBOOK.md               # Post-mortems + procédures d'urgence
│   ├── CERVEAU2.md              # Vue d'ensemble second cerveau
│   ├── CERVEAU2_API.md          # Référence API
│   ├── CERVEAU2_INTEGRATION.md  # Guide intégration agents externes
│   ├── CERVEAU2_EXTRACTION.md   # Comment traiter/extraire les informations
│   ├── CERVEAU2_RECHERCHE_FACTUELLE.md  # Recherche factuelle (dense search, faux négatifs LLM)
│   └── PATTERNS_FROM_CHARLIE_V1.21.3.md # Patterns réutilisables pour Second Cerveau Pro
├── app/
│   ├── _version.py              # VERSION source unique (TOLÉRANCE ZÉRO)
│   ├── main.py                  # Entrypoint asyncio (poller + web)
│   ├── config.py                # pydantic-settings depuis .env
│   ├── charlie.py               # Cœur intelligent Charlie AI (ask_charlie)
│   ├── charlie_memory.py        # Mémoire persistante (charlie_memory table)
│   ├── cerveau_client.py        # Client HTTP Cerveau2 (dégradation silencieuse)
│   ├── alerts.py                # Notifications Slack + Resend
│   ├── healthcheck.py           # FastAPI /health + startup/shutdown hooks
│   ├── workers/
│   │   ├── imap_poller.py       # 1 task asyncio par boîte (5 min)
│   │   └── newsletter_digest.py # Digest quotidien Slack
│   ├── pipeline/
│   │   ├── prefilter.py         # Règles headers/expéditeurs
│   │   ├── classifier.py        # LLM → 8 catégories avec few-shots
│   │   ├── priority.py          # Priorité intelligente (high/normal/low)
│   │   ├── language.py          # Détection langue (toutes BCP-47)
│   │   ├── rag.py               # Embed + retrieve sqlite-vec
│   │   ├── generator.py         # Assemblage prompt + appel LLM + _load_daniel_fewshot()
│   │   ├── translator.py        # Aide lecture multilingue (v1.21.0)
│   │   └── draft_renderer.py    # Rendu brouillon enrichi 4 blocs (v1.21.0)
│   ├── delivery/
│   │   ├── imap_draft.py        # Dépôt brouillon IMAP Drafts (V2a) + SELECT probe
│   │   ├── resend_notifier.py   # Email brouillon (fallback uniquement)
│   │   ├── slack_notifier.py    # Notifications webhook Slack
│   │   └── slack_bot.py         # Slack Bot Charlie AI interactif
│   ├── llm/router.py            # Wrapper LiteLLM + extraction reasoning_content + _clean_reasoning()
│   ├── web/                     # Cockpit web FastAPI
│   │   ├── app.py               # Application FastAPI
│   │   ├── auth.py              # Magic link login
│   │   ├── app_routes.py        # Inbox + conversation
│   │   ├── api.py               # Endpoints HTMX + Charlie AI + /api/drafts/{id}/retry
│   │   ├── admin.py             # Dashboard + settings
│   │   ├── deps.py              # Dependencies auth + DB
│   │   ├── utils.py             # Audit log
│   │   ├── models.py            # Schéma SQLite
│   │   ├── static/              # CSS/JS
│   │   └── templates/           # Jinja2
│   └── prompts/
│       ├── classifier_prompt.txt
│       └── personality_daniel.txt
├── scripts/
│   ├── bootstrap_embeddings.py            # Indexe paires Q/R dans pairs_vec
│   ├── extract_personality.py             # → app/prompts/personality_daniel.txt
│   ├── bootstrap_cerveau2.py              # Init Cerveau2 (one-shot)
│   ├── deliver_pending_drafts.py         # (v1.22.2) Livre les brouillons existants en IMAP Drafts
│   ├── cleanup_old_drafts.py              # (v1.22.3) Supprime vieux brouillons IMAP Drafts
│   ├── manual_draft_deposit.py            # Dépôt manuel brouillon IMAP (V2a)
│   ├── backfill_demande_client.py         # (v1.22.1) Re-classifie + génère brouillons ratés
│   ├── test_pipeline.py                   # Smoke test pipeline complet (mock IMAP)
│   ├── smoke_test_llm.py                  # Vérifie connectivité LLM
│   ├── smoke_test_sqlite_vec.py           # Vérifie sqlite-vec
│   ├── detective-healthcheck.sh           # Watchdog cron niveau 3 (anti-crash silencieux)
│   ├── detective-docker-clean.sh          # Cleanup Docker hebdo dim 4h
│   └── deploy-to-vps.sh                   # Deploy one-shot Mac → VPS
├── deploy/
│   └── detective-agent.service  # systemd unit (legacy)
├── data/                        # DB SQLite (gitignored) — NE PAS COMMIT
│   ├── agent_state.db           # Base courante (mail_processed post-cutoff 2026-06-01)
│   ├── boite1.sqlite            # Archives historiques (avant 2026-06-01)
│   ├── boite2.sqlite            # Archives historiques
│   └── boite3.sqlite            # Archives historiques
├── logs/
└── tests/
```

---

## Statut

✅ **Production active** — `detective.digitalhs.biz` — **v1.22.16**

- **Pipeline IMAP** : polling 3 boîtes toutes les 5 min, classification 8 catégories, priorité intelligente, flag `AgentProcessed` (succès) + `AgentAttempted` (libère la queue même en cas de crash, v1.21.3).
- **Génération brouillon** : kimi-k2.6:cloud (reasoning model), style Daniel imité via few-shot learning (v1.22.0) + personnalité Cerveau2 + RAG sqlite-vec.
- **Aide lecture multilingue v1.21.0** : pour mails NL/EN/DE/ES/etc., brouillon enrichi avec 4 blocs (email d'origine + traduction FR + proposition FR + traduction langue source). Réponse TOUJOURS en FR.
- **Livraison V2a — Drafts IMAP (v1.17+)** : dépôt direct dans la boîte source, flag `\Draft`, sujet `DEMANDE D'Approbation - Reponse Demande Client : ...`. Resend conservé en fallback uniquement.
- **Endpoint retry-draft v1.21.0** : `POST /api/drafts/{id}/retry` pour régénérer un brouillon manquant (cas deadlock poller).
- **Backfill v1.22.1** : re-classification robuste (76 mails historiques `demande_client` récupérés).
- **Delivery one-shot v1.22.2** : `deliver_pending_drafts.py` livre 153/154 brouillons en IMAP Drafts.
- **Cleanup drafts v1.22.3** : `cleanup_old_drafts.py` supprime 127 vieux brouillons (< 2026-06-02).
- **Few-shot learning fixé v1.22.4** : le LLM voit enfin le VRAI Daniel (corrections #561 + #83 injectées).
- **Cockpit web** : inbox filtrable, conversation avec viewer PJ, bloc Charlie remonté à droite.
- **Chat AI Charlie** : SQL programmatique bypass LLM, Cerveau2 vault, nuage de liaison familial YAML, archives historiques, résumé de dossier narratif, garde anti-hallucination, garde anti-"pas trouvé" malgré données présentes, scoring mots-clés avec bonus noms concrets.
- **Slack Bot Charlie AI** : @mention + DM sur #detective.
- **Cerveau2 vault** : ingestion 100% emails + PJ, recherche sans troncation, insensible aux accents, blindé injection, mapping priorité high→urgent/low→faible (v1.18.6), timeout 120s.
- **LLM kimi-k2.6:cloud stable v1.21.1+** : extraction `reasoning_content` + post-traitement `_clean_reasoning()` v1.21.2 (filtre 30+ patterns de traces de raisonnement).
- **Dashboard admin** : stats, settings LLM, audit logs, télémétrie poller, backup Cerveau2.
- **4 niveaux anti-crash silencieux** : Slack + Resend in-app + cron watchdog externe + Healthchecks.io.

Voir `docs/ROADMAP.md` pour la roadmap V2b/V2c (polishing cockpit, feedback loop qualité Daniel).

---

## Versions

Version source de vérité : **`app/_version.py`** (`VERSION = "1.22.14"`).

Le badge affiché dans le cockpit est lu dynamiquement depuis `app/_version.py`. **Tolérance zéro** sur la désynchronisation.

Voir [`CHANGELOG.md`](CHANGELOG.md) pour l'historique détaillé (1.18.x → 1.22.4).

---

## Documentation Cerveau2

- [`docs/CERVEAU2_RECHERCHE_FACTUELLE.md`](docs/CERVEAU2_RECHERCHE_FACTUELLE.md) — **Recherche factuelle via Cerveau2** (dense search = implicit AND, faux négatifs LLM, normalisation numéros, déduplication)
- [`docs/CERVEAU2_EXTRACTION.md`](docs/CERVEAU2_EXTRACTION.md) — **Comment traiter et extraire les informations** (fiches entreprise, contact, wikilinks, ingestion PJ)
- [`docs/CERVEAU2_API.md`](docs/CERVEAU2_API.md) — Référence API interne (endpoints, formats, mappings)
- [`docs/CERVEAU2_INTEGRATION.md`](docs/CERVEAU2_INTEGRATION.md) — Guide d'intégration pour agents externes (Hermes, etc.)
