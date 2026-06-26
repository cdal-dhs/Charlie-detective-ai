# Detective.be — Agent IA email (Charlie)

Agent IA Python qui assiste **Daniel Hurchon** (Detective.be, cabinet d'enquêtes privées) dans le traitement de ses emails clients.

L'agent surveille 4 boîtes email (3 boîtes Infomaniak : Detective Belgique FR, Detective Belgium EN/multi, DPDH Investigations + 1 boîte OVH : Detectives Belgique), classifie les mails entrants en 8 catégories, assigne une priorité intelligente, et génère des brouillons de réponse "à la Daniel" pour les demandes clients. **Multilingue** : TOUJOURS en français (langue de travail), avec aide lecture 4 blocs pour mails NL/EN/DE/ES/etc.

> **Pour Claude Code** : lis `CLAUDE.md` en premier pour le contexte, les conventions et les garde-fous.
> **Pour un nouvel agent** : lis `HANDOVER.md` (état complet, bugs, procédures urgence).
> **Pour la version exacte en prod** : `app/_version.py` (source unique) + `CHANGELOG.md` (historique).

---

## Architecture en une image

```
[4 boîtes email IMAP — 3 Infomaniak + 1 OVH]
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
    RAG sur paires Q/R historiques (sqlite-vec + text-embedding-3-small) — ⚠️ en pause v1.24.2 (rag_enabled=False) : remplacé par l'approche déterministe + few-shot Daniel
    Génération LLM few-shot style Daniel + personnalité
  Chat AI Charlie (cockpit + Slack) :
    Recherche SQL + archives historiques (boite1/2/3) + Cerveau2 vault + mémoire
    Modèle chat : openai/gemma4:31b (Ollama Pro Cloud, non-reasoning)
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

> ⚠️ **RAG en pause (v1.24.2)** : le RAG est désactivé par défaut (`rag_enabled=False`) car l'approche déterministe (`qualification_builder` + few-shot Daniel) est plus fiable. Les tables `pairs_vec` n'ont pas été réindexées depuis la bascule embedder (2026-05-28). Voir HANDOVER §9 point de vigilance #1. Réactivable via `RAG_ENABLED=true` après `python -m scripts.bootstrap_embeddings`.

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

## Stack technique (état v1.27.0)

| Couche | Choix |
|---|---|
| Runtime | Python 3.11+ (VPS = 3.11, Mac CDAL = 3.14) |
| Concurrence | `asyncio` |
| IMAP | `aioimaplib` |
| LLM router | **LiteLLM** + post-traitement `_clean_reasoning()` (30+ patterns regex, v1.21.2 — utile pour le fallback reasoning glm-5.2:cloud) |
| LLM principal (classifier + generator + chat) | **`openai/gemma4:31b`** via Ollama Pro Cloud (20€/mois), `api_base=https://ollama.com/v1` — **non-reasoning** (réponse dans `message.content`) |
| LLM fallback | **`openai/glm-5.2:cloud`** via Ollama Pro Cloud (reasoning model, thinking High/Max) |
| Embeddings | **`openai/text-embedding-3-small`** via OpenRouter (API stateless, image Docker ~800MB au lieu de ~4GB) — ⚠️ RAG en pause v1.24.2 (`rag_enabled=False`) |
| Vector store | **`sqlite-vec`** (extension SQLite, vit dans les DB existantes) — ⚠️ `pairs_vec` non réindexées depuis 2026-05-28 |
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
| Version | Source unique `app/_version.py` (`VERSION = "1.27.0"`) — `pyproject.toml` figé en 1.9.5 (volontaire) |

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
│   ├── cerveau_dossier.py       # Helpers liste dossiers Cerveau2
│   ├── settings_store.py        # Overrides runtime (app_settings)
│   ├── telegram_bot.py          # Module Telegram (conservé inactif en prod)
│   ├── alerts.py                # Notifications Slack + Resend
│   ├── healthcheck.py           # FastAPI /health + startup/shutdown hooks
│   ├── workers/
│   │   ├── imap_poller.py       # 1 task asyncio par boîte (5 min)
│   │   ├── newsletter_digest.py # Digest quotidien Slack
│   │   └── disk_watcher.py      # Surveillance espace disque VPS
│   ├── pipeline/
│   │   ├── prefilter.py         # Règles headers/expéditeurs
│   │   ├── classifier.py        # LLM → 8 catégories avec few-shots
│   │   ├── case_classifier.py   # Classification fine du cas métier
│   │   ├── priority.py          # Priorité intelligente (high/normal/low)
│   │   ├── language.py          # Détection langue (toutes BCP-47)
│   │   ├── objective_check.py   # Vérification objectif clair (utilisé par generator)
│   │   ├── document_extract.py  # Extraction texte pièces jointes
│   │   ├── rag.py               # Embed + retrieve sqlite-vec (⚠️ en pause v1.24.2, rag_enabled=False)
│   │   ├── generator.py         # Assemblage prompt + appel LLM + _load_daniel_fewshot()
│   │   ├── qualification_builder.py  # Brouillon qualifiant déterministe + refus illégal v1.25.21
│   │   ├── subject_fixer.py     # Nettoyage sujet + masque forwarders WP v1.25.18
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
│   │   ├── admin.py             # Dashboard + settings + simulateur brouillon
│   │   ├── deps.py              # Dependencies auth + DB
│   │   ├── utils.py             # Audit log
│   │   ├── models.py            # Modèles Pydantic web
│   │   ├── db_migrate.py        # Migrations/création schéma SQLite
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
│   ├── backfill_reclassify.py             # Re-classement + régénération d'un mail donné
│   ├── regenerate_and_deliver_drafts.py   # Régénération + livraison groupée de brouillons
│   ├── review_missed_demande_client.py   # (v1.25.17) Audit périodique faux négatifs demande_client
│   ├── cleanup_drafts_by_uid.py           # Nettoyage brouillons par UID IMAP
│   ├── cleanup_drafts_without_email_id.py # Nettoyage brouillons orphelins
│   ├── dedup_drafts_by_email_id.py        # Dédoublonnage brouillons par Message-ID
│   ├── test_pipeline.py                   # Smoke test pipeline complet (mock IMAP)
│   ├── test_draft_qualification.py        # Simulateur CLI brouillon qualifiant
│   ├── smoke_test_llm.py                  # Vérifie connectivité LLM
│   ├── smoke_test_sqlite_vec.py           # Vérifie sqlite-vec
│   ├── check_imap.py                      # Diagnostic connexion IMAP brute
│   ├── ingest_sent_to_cerveau2.py         # Ingestion manuelle dossier Sent vers Cerveau2
│   ├── backfill_historical_to_cerveau2.py # Backfill historique vers Cerveau2
│   ├── fix_charlie_memory.py              # Outils maintenance mémoire Charlie
│   ├── fix_prompts.py                     # Maintenance prompts
│   ├── build_bible_pdf.py                 # Génération PDF bible projet
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

✅ **Production active** — `detective.digitalhs.biz` — **v1.27.0**

- **Pipeline IMAP** : polling 4 boîtes toutes les 5 min (3 Infomaniak + 1 OVH), classification 8 catégories, priorité intelligente, flag `AgentProcessed` (succès) + `AgentAttempted` (libère la queue même en cas de crash, v1.21.3).
- **Génération brouillon** : gemma4:31b (non-reasoning), style Daniel imité via few-shot learning (v1.22.0) + personnalité Cerveau2. **RAG sqlite-vec en pause (v1.24.2)** — remplacé par le brouillon qualifiant déterministe (`qualification_builder`) pour les `demande_client`/`prise_contact`.
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
- **LLM gemma4:31b principal + glm-5.2:cloud fallback (v1.25.0)** : bascule depuis kimi-k2.6:cloud (v1.21.1→v1.24.x) vers gemma4:31b (non-reasoning) comme modèle principal sur toutes les tâches. glm-5.2:cloud remplace glm-5.1:cloud comme fallback (reasoning model). Extraction `reasoning_content` + post-traitement `_clean_reasoning()` v1.21.2 (filtre 30+ patterns, utile pour le fallback reasoning glm-5.2:cloud).
- **Hardening classifier v1.24.0** : 3 règles déterministes où le body l'emporte sur le sujet (#515, #606, #614).
- **Brouillon hors-légalité v1.24.1 → v1.25.21** : refus clair des méthodes illégales (piratage, extraction WhatsApp, localisation via GSM) + qualification commerciale (but ultime, contexte, éléments, alternative légale). 19 tests illégaux, 278 tests verts.
- **Masque forwarders WP v1.25.18 → v1.25.20** : `NO_EMAIL_IN_THE_FORM` affiché dans brouillons IMAP, cockpit et Slack quand un formulaire WordPress n'expose pas l'email client. Fix P0 cockpit 500 + badge brouillon HTMX + test de non-régression cockpit.
- **Réconcilieur Drafts IMAP v1.25.22 + bug P0 corrigé v1.25.23** : `app/workers/drafts_reconciler.py` (15 min) garantit la présence physique de chaque brouillon dans `Drafts` — recherche par header `X-Detective-Mail-Id` (posé par `append_draft`) puis body `EMAIL #<id>`. Bug P0 : `_draft_present` confondait la ligne de status aioimaplib `Search completed` avec un match → corrigé via `_has_search_match()`. Anti-doublon `_fetch_candidates` (`delivered_at IS NULL` uniquement).
- **Expéditeur = vrai client v1.25.24 → v1.25.26** : `mask_forwarder_sender` s'appuie **uniquement sur le Reply-To** (décision CDAL — extraction body ambiguë `info@`/`support@`/`retail@` = faux clients) → `NO_EMAIL_IN_THE_FORM` si sender technique → sender direct sinon. `_is_technical_sender` capte `newsletter@`/`noreply@` sur tout domaine. `_persist` stocke le sender masqué. **Backfill prod** : 224 senders techniques → `NO_EMAIL_IN_THE_FORM`, 353 vrais clients intacts. **#629 finalisé** (Christèle Kremp-Voinova, Reply-To `ckremp@vo.lu`, brouillon UID 6540).
- **Audit périodique faux négatifs v1.25.17** : `scripts/review_missed_demande_client.py` détecte les formulaires WP passés à travers (#519).
- **Investigation successorale v1.25.27** : nouveau cas métier `investigation_successorale` (`case_classifier.py` + `qualification_builder._build_succession_draft`) + `objective_check.py` enrichi (objectifs patrimoniaux reconnus clairs sans appel LLM). #643 (Boeteman) — le brouillon flou générique est remplacé par un brouillon dédié (accusé réception + 8 questions succession + coordination notaire).
- **Sujet de brouillon lisible v1.25.28 → v1.26.0** : `suggested_subject` persisté en DB par le poller (`_persist` INSERT/UPDATE COALESCE) + écrit dans le sujet du brouillon IMAP par `append_draft` (le tag `[NO_EMAIL_IN_THE_FORM]` et les templates WP absurdes ne polluent plus le sujet vu par Daniel). v1.26.0 : le cockpit affiche ce sujet lisible dans l'inbox et la conversation (`display_subject = suggested_subject or subject`, zéro modif template). Symétrique IMAP/cockpit.
- **4ème boîte mail OVH v1.27.0** : ajout de `info@detectives-belgique.be` (brand Detectives Belgique, code cockpit `D_DS`, marque Cerveau2 `detectivesbelgique`, DB `boite4.sqlite`, serveur IMAP `ex5.mail.ovh.net`). Architecture IMAP host par boîte : `MailboxConfig` enrichi avec `imap_host`, `imap_port`, `short_code`, `cerveau2_marque`. Templates cockpit et mappings métier mis à jour. 323 tests verts.
- **Header `X-Detective-Mail-Id` v1.25.22** : identifie un brouillon précis en IMAP (réconcilieur + `append_draft`).
- **Dashboard admin** : stats, settings LLM, audit logs, télémétrie poller, backup Cerveau2.
- **4 niveaux anti-crash silencieux** : Slack + Resend in-app + cron watchdog externe + Healthchecks.io.

Voir `docs/ROADMAP.md` pour la roadmap V2b/V2c (polishing cockpit, feedback loop qualité Daniel).

---

## Versions

Version source de vérité : **`app/_version.py`** (`VERSION = "1.27.0"`).

Le badge affiché dans le cockpit est lu dynamiquement depuis `app/_version.py`. **Tolérance zéro** sur la désynchronisation.

Voir [`CHANGELOG.md`](CHANGELOG.md) pour l'historique détaillé (1.18.x → 1.24.x).

---

## Documentation Cerveau2

- [`docs/CERVEAU2_RECHERCHE_FACTUELLE.md`](docs/CERVEAU2_RECHERCHE_FACTUELLE.md) — **Recherche factuelle via Cerveau2** (dense search = implicit AND, faux négatifs LLM, normalisation numéros, déduplication)
- [`docs/CERVEAU2_EXTRACTION.md`](docs/CERVEAU2_EXTRACTION.md) — **Comment traiter et extraire les informations** (fiches entreprise, contact, wikilinks, ingestion PJ)
- [`docs/CERVEAU2_API.md`](docs/CERVEAU2_API.md) — Référence API interne (endpoints, formats, mappings)
- [`docs/CERVEAU2_INTEGRATION.md`](docs/CERVEAU2_INTEGRATION.md) — Guide d'intégration pour agents externes (Hermes, etc.)
