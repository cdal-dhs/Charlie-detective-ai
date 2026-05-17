# Detective.be — Agent IA email

Agent IA Python qui assiste **Daniel Hurchon** (Detective.be, cabinet d'enquêtes privées) dans le traitement de ses emails clients.

L'agent surveille 3 boîtes Infomaniak (3 marques : Detective Belgique, Detective Belgium, DPDH Investigations), classifie les mails entrants en 8 catégories, assigne une priorité intelligente, et génère des brouillons de réponse "à la Daniel" pour les demandes clients — multilingue FR/NL/EN.

> **Pour Claude Code** : lis `CLAUDE.md` en premier pour le contexte, les conventions et les garde-fous.  
> **Pour un nouvel agent** : lis `HANDOVER.md` pour l'état complet du projet, l'architecture et les accès.

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
  Si demande_client :
    Détection langue (FR/NL/EN)
    RAG sur 1200 paires Q/R historiques (sqlite-vec + multilingual-e5-large)
    Génération brouillon (Kimi K2 via LiteLLM, style "Daniel")
         ↓
[Flag IMAP AgentProcessed]       → idempotence
[DB SQLite mail_processed]      → stockage + cockpit web
[Cockpit web FastAPI]           → detective.digitalhs.biz
  - Auth magic link
  - Inbox filtrable (tabs, checkboxes boîtes, recherche texte, tri)
  - Édition inline catégorie/statut/priorité (HTMX)
  - Conversation détaillée avec génération brouillon inline
  - Chat AI Charlie (SQL read-only natural language)
  - Dashboard admin (stats, settings LLM, audit logs)
```

Spec complète : [`docs/SPEC.md`](docs/SPEC.md). Roadmap : [`docs/ROADMAP.md`](docs/ROADMAP.md).

---

## Setup local (Mac de CDAL)

```bash
# 1. Environnement Python
python3 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"

# 2. Config
cp .env.example .env
# → éditer .env avec : app passwords Infomaniak, clé Ollama Pro, clé Resend, PUBLIC_BASE_URL

# 3. Données : déposer les 3 DB SQLite anonymisées dans data/
#   data/boite1.sqlite
#   data/boite2.sqlite
#   data/boite3.sqlite

# 4. Bootstrap one-shot (S1) — après que les DB soient là
python -m scripts.bootstrap_embeddings   # indexe les paires dans pairs_vec
python -m scripts.extract_personality    # génère app/prompts/personality_daniel.txt

# 5. Lancer l'agent
python -m app.main
```

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

**Manuellement sur le VPS** (si besoin) :
```bash
ssh root@69.62.110.165
cd /opt/DETECTIVE
git pull
docker compose up -d --build
```

**Prérequis sur le VPS** :
- Docker + Docker Compose
- Réseau Traefik externe `root_default`
- DNS A record `detective.digitalhs.biz` → `69.62.110.165`

---

## Stack

Python 3.11+ · asyncio · aioimaplib · LiteLLM (Kimi K2 / Ollama Pro + OpenRouter fallback) · sentence-transformers (e5-large) · sqlite-vec · langdetect · Resend · FastAPI · uvicorn · HTMX · Alpine.js · Tailwind CSS · structlog · pydantic-settings.

Hébergement : VPS Hostinger KVM8, Docker + Traefik + Let's Encrypt.

---

## Layout

```
DETECTIVE_BE/
├── CLAUDE.md                    # Instructions Claude Code (à lire en 1er)
├── README.md                    # Ce fichier
├── HANDOVER.md                  # État complet + contexte pour nouvel agent
├── CHANGELOG.md                 # Historique des versions
├── pyproject.toml               # Version source de vérité, deps, ruff, pytest
├── .env.example                 # Template config
├── Dockerfile                   # Image Docker Python 3.11
├── docker-compose.yml           # Traefik + labels
├── .dockerignore
├── docs/
│   ├── SPEC.md                  # Spec technique complète et figée
│   ├── ROADMAP.md               # Découpage S1→S4 + V2/V3 + état courant
│   ├── CONTEXT.md               # Contexte business client
│   └── HANDOVER.md              # (obsolète — voir HANDOVER.md racine)
├── app/
│   ├── main.py                  # Entrypoint asyncio (poller + web)
│   ├── config.py                # pydantic-settings depuis .env
│   ├── healthcheck.py           # FastAPI /health
│   ├── workers/
│   │   ├── imap_poller.py       # 1 task asyncio par boîte
│   │   └── newsletter_digest.py # Digest quotidien Slack
│   ├── logging_config.py        # structlog : console + fichier journalier (rotation 3j)
│   ├── charlie.py               # Logique partagée Charlie AI (prompt, SQL, résultat)
│   ├── pipeline/
│   │   ├── prefilter.py         # Règles headers/expéditeurs + détection demande_client
│   │   ├── classifier.py        # LLM → 8 catégories avec few-shots
│   │   ├── priority.py          # Priorité intelligente (high/normal/low)
│   │   ├── language.py          # Détection langue FR/NL/EN
│   │   ├── rag.py               # Embed + retrieve sqlite-vec
│   │   └── generator.py         # Assemblage prompt + appel LLM
│   ├── delivery/
│   │   ├── resend_notifier.py   # Email brouillon → CDAL
│   │   ├── slack_notifier.py    # Notifications webhook Slack
│   │   └── slack_bot.py         # Slack Bot Charlie AI interactif
│   ├── llm/router.py            # Wrapper LiteLLM avec fallback
│   ├── web/                     # Cockpit web FastAPI
│   │   ├── app.py               # Application FastAPI
│   │   ├── auth.py              # Magic link login
│   │   ├── app_routes.py        # Inbox + conversation
│   │   ├── api.py               # Endpoints HTMX + Charlie AI
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
│   ├── bootstrap_embeddings.py
│   ├── extract_personality.py
│   └── deploy-to-vps.sh         # Deploy one-shot Mac → VPS
├── deploy/
│   └── detective-agent.service  # systemd unit (legacy)
├── data/                        # DB SQLite (gitignored)
├── logs/
└── tests/
```

---

## Statut

✅ **MVP opérationnel en production** — `detective.digitalhs.biz`
- Backend IMAP + génération IA : Docker sur VPS Hostinger
- Cockpit web : FastAPI via Traefik + HTTPS
- Classification enrichie : 8 catégories (phishing, rappel, demande_client, facture, newsletter, spam, urgent, autre)
- Priorité intelligente : demande client chaude = HIGH
- Chat AI Charlie : SQL read-only, liens cliquables, resizeable
- Slack Bot Charlie AI : @mention et DM sur #detective
- Voir `docs/ROADMAP.md` pour les phases restantes (S4 supervision, V2 Drafts IMAP, V3 WhatsApp).

---

## Versions

Version source de vérité : **`pyproject.toml`** (`version = "1.8.1"`).

Le badge affiché dans le cockpit (`v1.8.0`) est lu dynamiquement depuis `pyproject.toml` via `importlib.metadata`. Ne modifier la version que dans `pyproject.toml`.

Voir [`CHANGELOG.md`](CHANGELOG.md) pour l'historique détaillé.
