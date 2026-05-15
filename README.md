# Detective.be — Agent IA email

Agent IA Python qui assiste **Daniel Hurchon** (Detective.be, cabinet d'enquêtes privées) dans le traitement de ses emails clients.

L'agent surveille 3 boîtes Infomaniak (3 marques : Detective Belgique, Detective Belgium, DPDH Investigations), classifie les mails entrants en 8 catégories, assigne une priorité intelligente, et génère des brouillons de réponse "à la Daniel" pour les demandes clients — multilingue FR/NL/EN.

> **Pour Claude Code** : lis `CLAUDE.md` en premier pour le contexte, les conventions et les garde-fous.

---

## Architecture en une image

```
[3 boîtes Infomaniak IMAP]
         ↓ polling 5 min
[Worker asyncio Python]
         ↓
[Pipeline]
  pré-filtre règles  → newsletter / facture / phishing / rappel évidents → tag & skip
  classification LLM → 8 catégories avec few-shots
  priorité intelligente  → demande client chaude = HIGH
  si demande_client :
    détection langue (FR/NL/EN)
    RAG sur 1200 paires Q/R historiques (sqlite-vec)
    génération brouillon (Kimi K2 via LiteLLM, style "Daniel")
         ↓
[Resend API → cdal@digitalhs.biz]
[Flag IMAP $AgentProcessed sur le mail entrant]
[Cockpit web → detective.digitalhs.biz]
```

Spec complète : [`docs/SPEC.md`](docs/SPEC.md). Roadmap : [`docs/ROADMAP.md`](docs/ROADMAP.md).

---

## Setup local (Mac de Cyril)

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
python -m scripts.bootstrap_embeddings   # indexe les 1200 paires dans pairs_vec
python -m scripts.extract_personality    # génère app/prompts/personality_daniel.txt

# 5. Lancer l'agent
python -m app.main
```

---

## Déploiement production (VPS Hostinger + Docker + Traefik)

L'agent est containerisé et exposé via Traefik sur `detective.digitalhs.biz`.

```bash
# Depuis le Mac de Cyril — one-shot
./scripts/deploy-to-vps.sh
```

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

Python 3.11+ · asyncio · aioimaplib · LiteLLM (Kimi K2 / Ollama Pro + OpenRouter fallback) · sentence-transformers (e5-large) · sqlite-vec · langdetect · Resend · FastAPI · uvicorn · structlog · pydantic-settings.

Hébergement : VPS Hostinger KVM8, Docker + Traefik + Let's Encrypt.

Coût LLM mensuel estimé : **~25-30 €** (Ollama Pro 20€ + OpenRouter ponctuel + Backblaze backup).

---

## Layout

```
DETECTIVE_BE/
├── CLAUDE.md                    # instructions Claude Code (à lire en 1er)
├── README.md                    # ce fichier
├── CHANGELOG.md                 # historique des versions
├── pyproject.toml               # deps + ruff + pytest
├── .env.example                 # template config
├── Dockerfile                   # image Docker Python 3.11
├── docker-compose.yml           # Traefik + labels
├── .dockerignore
├── docs/
│   ├── SPEC.md                  # spec technique complète et figée
│   ├── ROADMAP.md               # découpage S1→S4 + V2/V3 + état courant
│   ├── CONTEXT.md               # contexte business client
│   └── HANDOVER.md              # état du projet au handover
├── app/
│   ├── main.py                  # entrypoint asyncio (poller + web)
│   ├── config.py                # pydantic-settings depuis .env
│   ├── healthcheck.py           # FastAPI /health
│   ├── workers/
│   │   ├── imap_poller.py       # 1 task asyncio par boîte
│   │   └── newsletter_digest.py # digest quotidien Slack
│   ├── pipeline/
│   │   ├── prefilter.py         # règles headers/expéditeurs
│   │   ├── classifier.py        # LLM → 8 catégories avec few-shots
│   │   ├── priority.py          # priorité intelligente (high/normal/low)
│   │   ├── language.py          # détection langue FR/NL/EN
│   │   ├── rag.py               # embed + retrieve sqlite-vec
│   │   └── generator.py         # assemblage prompt + appel LLM
│   ├── delivery/
│   │   ├── resend_notifier.py   # email HTML formaté → Cyril
│   │   └── slack_notifier.py    # notifications webhook Slack
│   ├── llm/router.py            # wrapper LiteLLM avec fallback
│   ├── web/                     # Cockpit web FastAPI
│   │   ├── app.py               # application FastAPI
│   │   ├── auth.py              # magic link login
│   │   ├── app_routes.py        # inbox + conversation
│   │   ├── api.py               # endpoints HTMX (drafts)
│   │   ├── admin.py             # dashboard + settings
│   │   ├── models.py            # schéma SQLite
│   │   ├── static/              # CSS/JS
│   │   └── templates/           # Jinja2
│   └── prompts/
│       ├── classifier_prompt.txt
│       └── personality_daniel.txt
├── scripts/
│   ├── bootstrap_embeddings.py
│   ├── extract_personality.py
│   └── deploy-to-vps.sh         # deploy one-shot Mac → VPS
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
- Cockpit web : FastAPI via Traefik + HTTPS, avec édition inline statut/priorité dans inbox et conversation
- Classification enrichie : 8 catégories (phishing, rappel, demande_client, facture, newsletter, spam, urgent, autre)
- Priorité intelligente : demande client chaude = HIGH
- Voir `docs/ROADMAP.md` pour les phases restantes (S4 supervision, V2 Drafts IMAP).

---

## Versions

Voir [`CHANGELOG.md`](CHANGELOG.md) pour l'historique détaillé.
