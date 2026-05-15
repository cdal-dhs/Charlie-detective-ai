# HANDOVER — Detective.be Agent (Charlie)

> **Date** : 2026-05-15
> **Version** : 1.5.3
> **Intégrateur** : Cyril Dal (`cdal@digitalhs.biz`)
> **Client** : Daniel Hurchon — Detective.be (3 marques : Detective Belgique FR, Detective Belgium EN/multi, DPDH Investigations)
> **Repo** : https://github.com/cdal-dhs/Charlie-detective-ai

---

## 1. Vue d'ensemble

Agent IA Python (asyncio) qui poll 3 boîtes mail Infomaniak toutes les 5 min, classifie les emails entrants en 8 catégories, assigne une priorité intelligente, et génère des brouillons de réponse "à la Daniel" via RAG sur 1200 paires Q/R historiques anonymisées. Le tout est supervisé via un cockpit web sécurisé.

**Environnements** :
- **Local (Mac Cyril)** : développement, tests
- **Production (VPS Hostinger KVM8)** : Docker + Traefik + Let's Encrypt

**URL cockpit** : https://detective.digitalhs.biz

---

## 2. Architecture actuelle

```
[3 boîtes Infomaniak IMAP]  detective_belgique | detective_belgium | dpdh_investigations
         ↓ polling 5 min
[Worker asyncio Python — app/main.py]
         ↓
[Pipeline]
  Pré-filtre règles      → newsletter / facture / phishing / rappel évidents → tag & skip
  Classification LLM     → 8 catégories avec few-shots (phishing, rappel, demande_client, facture, newsletter, spam, urgent, autre)
  Priorité intelligente  → demande client chaude = HIGH
  Si demande_client :
    Détection langue (FR/NL/EN)
    RAG sqlite-vec (multilingual-e5-large)
    Génération brouillon (Kimi K2 via LiteLLM + OpenRouter fallback)
         ↓
[Flag IMAP $AgentProcessed]   → évite les doublons
[DB SQLite mail_processed]    → stockage + cockpit web
[Cockpit web FastAPI]         → detective.digitalhs.biz
  - Auth magic link (Resend)
  - Inbox filtrable (tabs, checkboxes boîtes, recherche texte, tri)
  - Édition inline catégorie/statut/priorité (HTMX)
  - Conversation détaillée avec génération inline brouillon IA
  - Chat AI Charlie (SQL read-only natural language)
  - Dashboard admin (stats, settings LLM, audit logs)
```

---

## 3. Stack technique

| Couche | Choix |
|---|---|
| Runtime | Python 3.11+ |
| Concurrence | `asyncio` |
| IMAP | `aioimaplib` |
| LLM router | **LiteLLM** (proxy OpenAI-compat) |
| LLM principal | **Kimi K2 via Ollama Pro** (abonnement 20€/mois Cyril) |
| LLM fallback | **OpenRouter** (Claude / GPT-4o à la demande) |
| Embeddings | `intfloat/multilingual-e5-large` (sentence-transformers, local CPU) |
| Vector store | **`sqlite-vec`** (extension SQLite, vit dans les DB existantes) |
| Détection langue | `langdetect` (FR/NL/EN) |
| Email outbound | **Resend API** → `cdal@digitalhs.biz` (validation humaine) |
| Web framework | **FastAPI** + **Jinja2** |
| Frontend | HTMX + Alpine.js + Tailwind CSS (CDN) |
| Auth | Magic link email (Resend) + sessions `itsdangerous` |
| Healthcheck | FastAPI sur `127.0.0.1:8765` |
| Logs | `structlog` (JSON structuré) |
| Config | `pydantic-settings` depuis `.env` |
| Prod hosting | Docker + Docker Compose + Traefik + Let's Encrypt |

---

## 4. Accès et connexions

### 4.1 VPS Hostinger (Production)

| Paramètre | Valeur |
|---|---|
| IP | `69.62.110.165` |
| User SSH | `root` |
| Répertoire projet | `/opt/DETECTIVE` |
| Domaine | `detective.digitalhs.biz` |
| DNS A record | `detective.digitalhs.biz` → `69.62.110.165` |
| Reverse proxy | Traefik (réseau externe `root_default`) |
| HTTPS | Let's Encrypt auto (Traefik labels) |

**SSH** : `ssh root@69.62.110.165` (clé SSH déjà configurée via `ssh-copy-id`)

**Commandes courantes sur le VPS** :
```bash
# Logs temps réel
ssh root@69.62.110.165 'cd /opt/DETECTIVE && docker compose logs -f'

# Redémarrer le conteneur
ssh root@69.62.110.165 'cd /opt/DETECTIVE && docker compose up -d --build'

# Vérifier le statut
ssh root@69.62.110.165 'cd /opt/DETECTIVE && docker compose ps'
```

### 4.2 Déploiement (Mac Cyril → VPS)

```bash
# One-shot depuis le repo local
bash scripts/deploy-to-vps.sh
```

Le script inclut des **pre-flight checks systématiques** :
- Vérifie qu'on est sur `main`
- Bloque si des modifications non commitées existent
- Push automatiquement les commits locaux sur `origin/main`
- Sync `data/` (DB SQLite) et `.env`
- Build Docker et restart conteneur

### 4.3 Développement local (Mac Cyril)

```bash
# Setup (à faire une fois)
python3 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # puis éditer avec les secrets

# Lancer l'agent
python -m app.main
```

---

## 5. Structure du repo (état au 2026-05-15)

```
DETECTIVE_BE/
├── CLAUDE.md                    # Instructions projet pour Claude Code (LIRE EN 1ER)
├── README.md                    # Vue d'ensemble publique
├── CHANGELOG.md                 # Historique versions (format Keep a Changelog)
├── HANDOVER.md                  # Ce fichier (état + contexte pour nouvel agent)
├── pyproject.toml               # Version source de vérité (1.5.3), deps, ruff, pytest
├── .env.example                 # Template config (sans secrets)
├── Dockerfile                   # Python 3.11 slim
├── docker-compose.yml           # Service + labels Traefik
├── .dockerignore
├── docs/
│   ├── SPEC.md                  # Spec technique complète et figée
│   ├── ROADMAP.md               # Découpage S1→S4 + V2/V3 + état courant
│   ├── CONTEXT.md               # Contexte business Daniel + sensibilités
│   ├── HANDOVER.md              # État projet au handover (obsolète → ce fichier)
│   └── Bible_DetectiveBE.pdf    # Doc client
├── app/
│   ├── __init__.py              # __version__ lit dynamiquement depuis pyproject.toml
│   ├── main.py                  # Entrypoint : pollers + serveur web
│   ├── config.py                # pydantic-settings depuis .env
│   ├── healthcheck.py           # FastAPI /health
│   ├── workers/
│   │   ├── imap_poller.py       # Boucle polling 3 boîtes
│   │   └── newsletter_digest.py # Digest quotidien newsletters
│   ├── pipeline/
│   │   ├── prefilter.py         # Règles rapides (newsletter, spam, phishing, rappel)
│   │   ├── classifier.py        # LLM → 8 catégories avec few-shots
│   │   ├── priority.py          # Priorité intelligente (high/normal/low)
│   │   ├── language.py          # Détection langue FR/NL/EN
│   │   ├── rag.py               # Embed + retrieve sqlite-vec
│   │   └── generator.py         # Assemblage prompt + appel LLM
│   ├── delivery/
│   │   ├── resend_notifier.py   # Email brouillon → Cyril
│   │   └── slack_notifier.py    # Webhook Slack #detective
│   ├── llm/
│   │   └── router.py            # Wrapper LiteLLM avec fallback OpenRouter
│   ├── web/                     # Cockpit web FastAPI
│   │   ├── app.py               # Application FastAPI
│   │   ├── auth.py              # Magic link + sessions
│   │   ├── app_routes.py        # /app/inbox, /app/conversation/{id}
│   │   ├── api.py               # Endpoints HTMX (drafts, inline edits, Charlie AI)
│   │   ├── admin.py             # Dashboard + settings LLM + audit
│   │   ├── deps.py              # Dependencies auth + DB
│   │   ├── utils.py             # Audit log
│   │   ├── models.py            # Schéma SQLite
│   │   ├── static/              # (CSS/JS inline via CDN)
│   │   └── templates/           # Jinja2
│   │       ├── base.html
│   │       ├── app/inbox.html         # Inbox + Charlie AI modal
│   │       ├── app/inbox_rows.html    # Lignes HTMX inbox
│   │       ├── app/conversation.html  # Vue conversation détaillée
│   │       ├── auth/login.html
│   │       └── admin/*.html
│   └── prompts/
│       ├── classifier_prompt.txt
│       └── personality_daniel.txt
├── scripts/
│   ├── bootstrap_embeddings.py  # Indexe les paires Q/R (one-shot S1)
│   ├── extract_personality.py   # Extrait style Daniel (one-shot S1)
│   └── deploy-to-vps.sh        # Deploy one-shot Mac → VPS (pre-flight checks)
├── deploy/
│   └── detective-agent.service  # systemd unit (legacy)
├── data/                        # DB SQLite (gitignored)
│   ├── boite1.sqlite            # DB historique anonymisée (NE PAS MODIFIER)
│   ├── boite2.sqlite            # DB historique anonymisée (NE PAS MODIFIER)
│   ├── boite3.sqlite            # DB historique anonymisée (NE PAS MODIFIER)
│   └── agent_state.db           # Traçabilité mails traités
├── logs/
└── tests/                       # (peuplé partiellement)
```

---

## 6. Conventions de code (non-négociables)

Lis impérativement **`CLAUDE.md`** pour les conventions complètes. Points clés :

- **Python ≥ 3.11**, type hints partout
- **`async def`** pour tout ce qui touche I/O (IMAP, LLM, HTTP)
- **Logs structurés** : `structlog` — `log.info("event.name", key=value)`, jamais `print()`
- **Pas de docstrings verbeux**. Une ligne courte si l'intention métier n'est pas évidente.
- **Pas de commentaires explicatifs** — le nom doit suffire. Garder uniquement les `TODO S2/S3`.
- **Imports** : stdlib → tiers → projet (groupes séparés par ligne vide). `ruff` les trie.
- **Secrets** : jamais en dur. Toujours via `app.config.get_settings()`.
- **Tests** : `pytest-asyncio`, mode auto. Mocker les appels IMAP/LLM externes.
- **Version** : **unique source de vérité = `pyproject.toml`**. `app/__init__.py` lit dynamiquement via `importlib.metadata`.

---

## 7. Points de vigilance IMPORTANTS

⚠️ **Ne JAMAIS commit le `.env`** (bloqué par `.gitignore`, mais double-check si tu touches au gitignore).

⚠️ **Ne JAMAIS écrire dans les vraies boîtes Infomaniak en dev** — mode `--dry-run` ou compte mail de test.

⚠️ **Ne JAMAIS envoyer de mail réel via Resend pendant les tests** — utiliser un mock ou `RESEND_API_KEY` vide.

⚠️ **Ne JAMAIS modifier les 3 DB SQLite sources** (`data/boite{1,2,3}.sqlite`) sans confirmation — ce sont les données historiques de Daniel.

⚠️ **Ne JAMAIS logger le contenu intégral d'un mail entrant** — uniquement métadonnées. Pour debug, utiliser `LOG_MAIL_BODY=true` explicite.

⚠️ **Multilingue obligatoire** : la réponse générée DOIT être dans la langue détectée du mail entrant (FR/NL/EN).

⚠️ **Pas de Docker au MVP** — le projet est déjà en prod Docker. Ne pas introduire d'autres infra lourde (Redis, Postgres, K8s) sans discussion explicite.

---

## 8. État des fonctionnalités (2026-05-15)

### ✅ Opérationnel en production

| Fonctionnalité | État | Notes |
|---|---|---|
| Polling IMAP 3 boîtes | ✅ | Tous les emails (lus + non lus), flag `$AgentProcessed` |
| Classification 8 catégories | ✅ | Avec pré-filtre règles + LLM few-shots |
| Priorité intelligente | ✅ | `high`/`normal`/`low` |
| RAG + génération brouillon | ✅ | Style Daniel, multilingue FR/NL/EN |
| Livraison Resend | ✅ | → `cdal@digitalhs.biz` (validation humaine) |
| Notifications Slack | ✅ | Webhook `#detective` |
| Cockpit web auth | ✅ | Magic link via Resend |
| Inbox filtrable | ✅ | Tabs, checkboxes boîtes, recherche texte, tri |
| Édition inline | ✅ | Catégorie, statut, priorité via HTMX |
| Conversation détaillée | ✅ | Body preview + génération brouillon inline |
| Chat AI Charlie | ✅ | SQL read-only, liens cliquables, resizeable |
| Dashboard admin | ✅ | Stats, settings LLM, audit logs |
| Filtre date IMAP | ✅ | `PROCESS_SINCE_DATE` configurable |

### ⏳ En cours / À améliorer

| Fonctionnalité | État | Notes |
|---|---|---|
| Calibration qualité brouillons | ⏳ | Faire traiter 20-50 vrais mails par Daniel, noter corrections, affiner prompt |
| Signature par marque (boîte 2 & 3) | ⏳ | Testé uniquement boîte 1 (Detective Belgique) |
| Tests unitaires automatisés | ⏳ | Mocker IMAP/LLM, fixtures classification |
| Drafts IMAP natifs (V2) | ⬜ | Bascule livraison Resend → Drafts boîte mail |
| Bot Telegram Boss ↔ Charlie | ⬜ | Canal direct Daniel pour notifications push/validation |
| Bot WhatsApp client (V3) | ⬜ | Canal client direct réutilisant RAG |
| Supervision / monitoring S4 | ⬜ | Grafana ou alerting basique |

---

## 9. Workflow pour un nouvel agent de codage

1. **Lire `CLAUDE.md`** en entier — c'est la bible du projet.
2. **Lire `docs/ROADMAP.md`** — identifier la phase en cours (S1/S2/S3/S4/V2).
3. **Lire ce HANDOVER.md** — comprendre l'état actuel et les conventions.
4. **Lire le module concerné en entier** avant de le modifier.
5. **Si une décision n'est pas dans la spec**, demander à Cyril. N'invente pas d'archi parallèle.
6. **Tests d'abord** quand c'est un module pur (RAG, prefilter, language, prompt assembly).
7. **Avant déploiement** : vérifier que `pyproject.toml` version est à jour, mettre à jour `CHANGELOG.md`.
8. **Déployer** : `bash scripts/deploy-to-vps.sh` (pre-flight checks intégrés).
9. **Après déploiement** : vérifier le badge version en bas à gauche du cockpit.

---

## 10. Contacts et ressources

- **Intégrateur** : Cyril Dal (`cdal@digitalhs.biz`)
- **Client** : Daniel Hurchon — detectivebelgique.be / detectivebelgium.com / dpdhuinvestigations.be
- **Repo GitHub** : https://github.com/cdal-dhs/Charlie-detective-ai
- **Cockpit prod** : https://detective.digitalhs.biz
- **Mémoire projet Claude** : `/Users/cdal/.claude/projects/-Users-cdal-DEV-APP-CLAUDE-DETECTIVE-BE/memory/` (persiste entre sessions)

---

## 11. Mémoire projet (Claude Code)

Les fichiers suivants persistent entre sessions Claude Code et guident le comportement futur :

- `memory/feedback_code-clean.md` — README, CHANGELOG et versioning systématiques
- `memory/feedback_deploy-check-systematic.md` — Pre-flight checks avant tout déploiement VPS
- `memory/multilingual_rule.md` — Réponses DOIVENT être dans la langue du client
- `memory/MEMORY.md` — Index de toutes les mémoires

---

*Document maintenu à jour à chaque itération. Dernière mise à jour : 2026-05-15 (v1.5.3).*
