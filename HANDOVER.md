# HANDOVER — Detective.be Agent (Charlie)

> **Date** : 2026-05-18
> **Version** : 1.9.0
> **Intégrateur** : CDAL (`cdal@digitalhs.biz`)
> **Client** : Daniel Hurchon — Detective.be (3 marques : Detective Belgique FR, Detective Belgium EN/multi, DPDH Investigations)
> **Repo** : https://github.com/cdal-dhs/Charlie-detective-ai

---

## 1. Vue d'ensemble

Agent IA Python (asyncio) qui poll 3 boîtes mail Infomaniak toutes les 5 min, classifie les emails entrants en 8 catégories, assigne une priorité intelligente, et génère des brouillons de réponse "à la Daniel" via RAG sur 1200 paires Q/R historiques anonymisées. Le tout est supervisé via un cockpit web sécurisé.

**Environnements** :
- **Local (Mac CDAL)** : développement, tests
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
  Pré-filtre règles      → newsletter / facture / phishing / rappel / demande_client évidents → tag & skip
  Classification LLM     → 8 catégories avec few-shots (phishing, rappel, demande_client, facture, newsletter, spam, urgent, autre)
  Priorité intelligente  → demande client chaude = HIGH
  Si demande_client :
    Détection langue (FR/NL/EN)
    RAG sqlite-vec (multilingual-e5-large)
    Génération brouillon (Kimi K2 via LiteLLM + OpenRouter fallback)
         ↓
[Flag IMAP AgentProcessed]     → évite les doublons (sans $, Infomaniak rejette les flags avec $)
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
| LLM principal | **Kimi K2 via Ollama Pro** (abonnement 20€/mois CDAL) |
| LLM fallback | **OpenRouter** (Claude / GPT-4o à la demande) |
| Embeddings | `intfloat/multilingual-e5-large` (sentence-transformers, local CPU) |
| Vector store | **`sqlite-vec`** (extension SQLite, vit dans les DB existantes) |
| Détection langue | `langdetect` (FR/NL/EN) |
| Email outbound | **Resend API** → `cdal@digitalhs.biz` (validation humaine) |
| Slack Bot (interactif) | **Slack Bolt** (async, HTTP mode via FastAPI `/slack/events`) |
| Web framework | **FastAPI** + **Jinja2** |
| Frontend | HTMX + Alpine.js + Tailwind CSS (CDN) |
| Auth | Magic link email (Resend) + sessions `itsdangerous` |
| Healthcheck | FastAPI sur `127.0.0.1:8765` |
| Logs | `structlog` (JSON structuré, fichier journalier + rotation 7j) |
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

### 4.2 Déploiement (Mac CDAL → VPS)

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

### 4.3 Développement local (Mac CDAL)

```bash
# Setup (à faire une fois)
python3 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # puis éditer avec les secrets

# Lancer l'agent
python -m app.main
```

### 4.4 Slack App (Charlie Detective)

| Paramètre | Valeur |
|---|---|
| App name | Charlie Detective |
| Event Subscriptions URL | `https://detective.digitalhs.biz/slack/events` |
| Bot Token | `SLACK_BOT_TOKEN` (xoxb-...) dans `.env` |
| Signing Secret | `SLACK_SIGNING_SECRET` dans `.env` |
| Scopes bot | `channels:read`, `chat:write`, `chat:write.public`, `reactions:write`, `app_mentions:read`, `im:history`, `im:read`, `im:write` |
| Events | `app_mention`, `message.im` |

Le bot répond aux @mentions dans les canaux où il est invité, et en DM.

---

## 5. Structure du repo (état au 2026-05-16)

```
DETECTIVE_BE/
├── CLAUDE.md                    # Instructions projet pour Claude Code (LIRE EN 1ER)
├── README.md                    # Vue d'ensemble publique
├── CHANGELOG.md                 # Historique versions (format Keep a Changelog)
├── HANDOVER.md                  # Ce fichier (état + contexte pour nouvel agent)
├── pyproject.toml               # Version source de vérité (1.8.1), deps, ruff, pytest
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
│   ├── logging_config.py        # structlog : console + fichier journalier (rotation 7j)
│   ├── healthcheck.py           # FastAPI /health
│   ├── workers/
│   │   ├── imap_poller.py       # Boucle polling 3 boîtes
│   │   └── newsletter_digest.py # Digest quotidien newsletters
│   ├── charlie.py              # Logique partagée Charlie AI (prompt, SQL, CharlieResult)
│   ├── pipeline/
│   │   ├── prefilter.py         # Règles rapides (newsletter, spam, phishing, rappel, demande_client)
│   │   ├── classifier.py        # LLM → 8 catégories avec few-shots
│   │   ├── priority.py          # Priorité intelligente (high/normal/low)
│   │   ├── language.py          # Détection langue FR/NL/EN
│   │   ├── rag.py               # Embed + retrieve sqlite-vec
│   │   └── generator.py         # Assemblage prompt + appel LLM
│   ├── delivery/
│   │   ├── resend_notifier.py   # Email brouillon → CDAL
│   │   ├── slack_notifier.py    # Webhook Slack #detective (notifications + ID cliquable)
│   │   └── slack_bot.py         # Slack Bolt App (Charlie AI interactif)
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
│   └── agent_state.db           # Traçabilité mails traités (+ colonne body complet)
├── logs/                        # Fichiers journaliers agent-YYYY-MM-DD.log (rotation 3j)
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

⚠️ **Flag IMAP = `AgentProcessed`** (sans `$`) — Infomaniak rejette les flags avec préfixe `$`.

⚠️ **Pas de Docker au MVP** — le projet est déjà en prod Docker. Ne pas introduire d'autres infra lourde (Redis, Postgres, K8s) sans discussion explicite.

---

## 8. État des fonctionnalités (2026-05-16)

### ✅ Opérationnel en production

| Fonctionnalité | État | Notes |
|---|---|---|
| Polling IMAP 3 boîtes | ✅ | Tous les emails (lus + non lus), flag `AgentProcessed` (sans $) |
| Classification 8 catégories | ✅ | Pré-filtre règles (newsletter, facture, phishing, rappel, **demande_client**) + LLM few-shots |
| Priorité intelligente | ✅ | `high`/`normal`/`low` |
| RAG + génération brouillon | ✅ | Style Daniel, multilingue FR/NL/EN, enrichi via Cerveau2 vault |
| Livraison Resend | ✅ | → `cdal@digitalhs.biz` (validation humaine) |
| Notifications Slack | ✅ | Webhook `#detective` |
| Cockpit web auth | ✅ | Magic link via Resend |
| Inbox filtrable | ✅ | Tabs, checkboxes boîtes, recherche texte, tri |
| Édition inline | ✅ | Catégorie, statut, priorité via HTMX |
| Conversation détaillée | ✅ | Body complet + body_preview fallback + génération brouillon inline |
| Chat AI Charlie | ✅ | SQL read-only, liens cliquables, resizeable |
| Slack Bot Charlie AI | ✅ | @mention ou DM sur #detective, même pipeline Charlie AI, ID cliquable dans notifications |
| Logs quotidiens | ✅ | JSON structuré, rotation 7j, `LOG_DIR` configurable |
| Dashboard admin | ✅ | Stats, settings LLM, audit logs |
| Filtre date IMAP | ✅ | `PROCESS_SINCE_DATE` configurable |
| Contenu complet mail | ✅ | Colonne `body` en DB, affichée par Charlie AI et cockpit |

### ⏳ En cours / À améliorer

| Fonctionnalité | État | Notes |
|---|---|---|
| **Sprint 5 ext — Charlie chat vault** | 🚧 | **PROCHAIN TICKET** — Voir section "Sprint 5 extension" ci-dessous |
| Calibration qualité brouillons | ⏳ | Faire traiter 20-50 vrais mails par Daniel, noter corrections, affiner prompt |
| Signature par marque (boîte 2 & 3) | ⏳ | Testé uniquement boîte 1 (Detective Belgique) |
| Tests unitaires automatisés | ⏳ | Mocker IMAP/LLM, fixtures classification |
| Drafts IMAP natifs (V2) | ⬜ | Bascule livraison Resend → Drafts boîte mail |
| Bot Telegram Boss ↔ Charlie | ⬜ | Canal direct Daniel pour notifications push/validation |
| Approbation/rejet depuis Slack | ⬜ | V2 — nécessite Slack App interactive |
| Bot WhatsApp client (V3) | ⬜ | Canal client direct réutilisant RAG |
| Supervision / monitoring S4 | ⬜ | Grafana ou alerting basique |

---

### 🚧 Sprint 5 extension — Charlie AI chat × Cerveau2 vault (PROCHAIN TICKET)

**Objectif** : quand l'opérateur pose une question à Charlie (via le cockpit web ou Slack), Charlie doit pouvoir consulter le vault Cerveau2-Det en plus de la DB SQLite `mail_processed`.

**Ce qui est déjà fait (v1.9.0)** :
- `app/cerveau_client.py` — `query_vault()` fonctionnel et testé
- `app/pipeline/generator.py` — vault enrichi dans `generate_draft()`
- `.env` — variables `CERVEAU2_BASE_URL`, `CERVEAU2_API_SECRET`, `CERVEAU2_LIMIT` configurées

**Ce qui reste à coder** :

#### 1. `app/charlie.py`

Ajouter dans `CharlieResult` :
```python
from dataclasses import dataclass, field
from app.cerveau_client import VaultNote, query_vault

@dataclass
class CharlieResult:
    response_text: str
    sql: str
    rows: list[dict] | None
    sql_safe: bool
    sql_error: str | None
    vault_notes: list[VaultNote] = field(default_factory=list)  # NOUVEAU
```

Ajouter les helpers de détection de pertinence et l'appel vault dans `ask_charlie()` :
```python
_VAULT_KEYWORDS = (
    "similaire", "historique", "passe", "precedent",
    "anterieur", "archive", "contexte", "dossier",
    "affaire", "enquete", "investigation", "correspondance",
)

def _is_vault_relevant(question: str, sql: str) -> bool:
    if not sql:  # question conversationnelle → vault toujours utile
        return True
    q = _normalize(question)
    return any(kw in q for kw in _VAULT_KEYWORDS)
```

Dans `ask_charlie()`, après la logique SQL :
```python
if _is_vault_relevant(question, sql):
    result.vault_notes = await query_vault(
        question=question,
        base_url=settings.cerveau2_base_url,
        api_secret=settings.cerveau2_api_secret,
        limit=settings.cerveau2_limit,
    )
```

#### 2. `app/web/api.py` — endpoint `charlie_ask`

Après `results_html`, ajouter la génération de `vault_html` :
```python
vault_html = ""
if result.vault_notes:
    items_html = ""
    for note in result.vault_notes:
        filename = note.path.split("/")[-1].replace(".md", "")
        preview = (note.content[:300]
                   .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        items_html += (
            f'<div class="mt-2 text-xs bg-gray-900 rounded px-3 py-2">'
            f'<div class="text-purple-400 font-mono mb-1">{filename}</div>'
            f'<div class="text-gray-400 whitespace-pre-wrap">{preview}…</div>'
            f'</div>'
        )
    vault_html = (
        f'<div class="mt-4 border-t border-gray-700 pt-3">'
        f'<div class="text-xs text-purple-400 font-semibold mb-1">📚 Second cerveau '
        f'({len(result.vault_notes)} note(s))</div>'
        f'{items_html}'
        f'</div>'
    )
```

Dans `ai_bubble`, ajouter `{vault_html}` après `{results_html}` :
```python
ai_bubble = (
    ...
    f'<div class="charlie-text whitespace-pre-wrap">{safe_response}</div>'
    f'{results_html}'
    f'{vault_html}'     # ← AJOUTER ICI
    f'<div class="flex">{copy_btn}</div>'
    ...
)
```

#### 3. `app/delivery/slack_bot.py` — `format_charlie_response()`

Ajouter en fin de fonction, avant le dernier divider :
```python
if result.vault_notes:
    blocks.append({"type": "divider"})
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn",
                      "text": f":books: *Second cerveau* — {len(result.vault_notes)} note(s)"}],
    })
    for note in result.vault_notes:
        filename = note.path.split("/")[-1].replace(".md", "")
        preview = note.content[:300]
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{filename}*\n{preview}…"},
        })
```

#### 4. Tests à ajouter

Fichier `tests/test_charlie_vault.py` :
- `test_is_vault_relevant_no_sql()` — sans SQL → True
- `test_is_vault_relevant_with_sql_no_keyword()` — SQL + pas de mot-clé → False
- `test_is_vault_relevant_with_sql_and_keyword()` — SQL + "dossier" → True
- `test_ask_charlie_calls_vault_when_no_sql()` — mock LLM (no SQL) + mock vault → vault_notes rempli
- `test_ask_charlie_no_vault_for_pure_sql()` — mock LLM (avec SQL) + pas de keyword → vault_notes vide

---

## 9. Workflow pour un nouvel agent de codage

1. **Lire `CLAUDE.md`** en entier — c'est la bible du projet.
2. **Lire `docs/ROADMAP.md`** — identifier la phase en cours (S1/S2/S3/S4/V2).
3. **Lire ce HANDOVER.md** — comprendre l'état actuel et les conventions.
4. **Lire le module concerné en entier** avant de le modifier.
5. **Si une décision n'est pas dans la spec**, demander à CDAL. N'invente pas d'archi parallèle.
6. **Tests d'abord** quand c'est un module pur (RAG, prefilter, language, prompt assembly).
7. **Avant déploiement** : vérifier que `pyproject.toml` version est à jour, mettre à jour `CHANGELOG.md`.
8. **Déployer** : `bash scripts/deploy-to-vps.sh` (pre-flight checks intégrés).
9. **Après déploiement** : vérifier le badge version en bas à gauche du cockpit.

---

## 10. Contacts et ressources

- **Intégrateur** : CDAL (`cdal@digitalhs.biz`)
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
- `docs/RUNBOOK.md` — Incidents, post-mortems, procédures de secours

---

*Document maintenu à jour à chaque itération. Dernière mise à jour : 2026-05-18 (v1.9.0).*
