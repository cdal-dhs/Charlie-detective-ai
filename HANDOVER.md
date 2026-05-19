# HANDOVER.md — Detective.be Agent IA

> **Document de transfert** : état complet du projet, accès, conventions et pièges pour un nouvel agent.
> **Dernière mise à jour** : 2026-05-18 (v1.9.3)
> **Auteur** : Cyril Dal (`cdal@digitalhs.biz`) — Digital Highway Solutions

---

## 1. Contexte métier (TL;DR)

**Client** : Daniel Hurchon, détective privé belge — cabinet **Detective.be** avec 3 marques :
- `detective_belgique` (D_FR) — Detective Belgique FR
- `detective_belgium` (D_NL) — Detective Belgium NL/multi
- `dpdh_investigations` (D_PD) — DPDH Investigations

**But** : agent IA Python qui poll les 3 boîtes mail Infomaniak toutes les 5 min, classifie les mails entrants en 8 catégories, et **uniquement pour les `demande_client`** génère un brouillon de réponse "à la Daniel" via RAG sur 1200 paires Q/R historiques anonymisées. Les brouillons sont envoyés par email à Cyril via Resend (validation humaine avant transfert à Daniel).

**Canaux** :
- **Pipeline email** : IMAP → classification → priorité → RAG → brouillon
- **Cockpit web** : `https://detective.digitalhs.biz`
- **Slack Bot Charlie AI** : @mention + DM sur #detective
- **Cerveau2 vault** (v1.9.x) : intégration Charlie AI chat × second cerveau via API Cerveau2-Det

---

## 2. Stack technique

| Couche | Choix |
|---|---|
| Runtime | Python ≥ 3.11 |
| Concurrence | `asyncio` |
| IMAP | `aioimaplib` |
| LLM router | **LiteLLM** (proxy OpenAI-compat) |
| LLM principal | **Kimi K2 via Ollama Pro** (abonnement 20€/mois de Cyril) |
| LLM fallback | **OpenRouter** (Claude / GPT-4o à la demande) |
| Embeddings | `intfloat/multilingual-e5-large` (sentence-transformers, local CPU) |
| Vector store | `sqlite-vec` (extension SQLite) |
| Détection langue | `fasttext` (lid.176.bin) |
| Email outbound MVP | **Resend API** |
| Canal Boss ↔ Charlie | **Telegram Bot** (python-telegram-bot) |
| Healthcheck | FastAPI sur `127.0.0.1:8765` |
| Web | FastAPI + Jinja2 + HTMX + Tailwind CSS |
| Service prod | Docker + Docker Compose sur VPS Hostinger |
| Logs | `structlog` (JSON structuré, rotation 7j) |
| Config | `pydantic-settings` depuis `.env` |

**Ne PAS introduire** sans discussion explicite : Docker supplémentaire, Celery, Redis, Postgres, ORM lourd, Kubernetes. L'architecture est volontairement légère.

---

## 3. Architecture fichiers clés

```
DETECTIVE_BE/
├── app/
│   ├── main.py                  # Entrypoint asyncio (poller + web + Telegram)
│   ├── config.py                # pydantic-settings depuis .env
│   ├── charlie.py               # Logique Charlie AI (prompt, SQL, vault, summary)
│   ├── cerveau_client.py        # Client HTTP async vers API Cerveau2-Det
│   ├── workers/
│   │   ├── imap_poller.py       # 1 task asyncio par boîte (intervalle 300s)
│   │   └── newsletter_digest.py # Digest quotidien newsletters → Slack
│   ├── pipeline/
│   │   ├── prefilter.py         # Règles headers + détection demande_client
│   │   ├── classifier.py        # LLM → 8 catégories
│   │   ├── priority.py          # HIGH / normal / low
│   │   ├── language.py          # FR / NL / EN
│   │   ├── rag.py               # Embed + retrieve sqlite-vec
│   │   └── generator.py         # Assemblage prompt + appel LLM
│   ├── delivery/
│   │   ├── resend_notifier.py   # Email brouillon → CDAL
│   │   ├── slack_notifier.py    # Webhook Slack (notifications)
│   │   ├── slack_bot.py         # Slack Bot Charlie AI interactif (@mention + DM)
│   │   └── telegram_bot.py      # Bot Telegram Boss ↔ Charlie
│   ├── llm/router.py            # Wrapper LiteLLM avec fallback OpenRouter
│   └── web/                     # Cockpit FastAPI
│       ├── app.py               # Application FastAPI
│       ├── api.py               # Endpoints HTMX + Charlie AI chat
│       ├── app_routes.py        # Inbox + conversation
│       ├── auth.py              # Magic link login
│       ├── admin.py             # Dashboard + settings LLM
│       ├── db_migrate.py        # Migration SQLite + seed users
│       └── templates/           # Jinja2 (inbox, conversation, chat, admin)
├── data/                        # DB SQLite (gitignored, ne JAMAIS commit)
│   ├── agent_state.db           # ⚠️ NE PAS écraser en déploiement
│   ├── boite1.sqlite            # Données anonymisées boîte 1
│   ├── boite2.sqlite            # Données anonymisées boîte 2
│   └── boite3.sqlite            # Données anonymisées boîte 3
├── scripts/
│   ├── deploy-to-vps.sh         # Déploiement one-shot Mac → VPS
│   ├── bootstrap_embeddings.py  # Indexe les paires dans pairs_vec
│   └── extract_personality.py   # Génère personality_daniel.txt
└── docs/
    ├── SPEC.md                  # Spec technique complète
    ├── ROADMAP.md               # Découpage S1→S4 + V2/V3
    └── CONTEXT.md               # Contexte business client
```

---

## 4. État des fonctionnalités (v1.9.4)

### ✅ Opérationnel en production
- Pipeline IMAP complet : polling 3 boîtes, classification 8 catégories, priorité intelligente
- Génération brouillon "style Daniel" via RAG + LLM (Kimi K2 / OpenRouter fallback)
- Cockpit web : inbox filtrable, conversation détaillée, édition inline catégorie/statut/priorité
- Chat AI Charlie : SQL read-only natural language, liens cliquables, resizeable
- Slack Bot Charlie AI : @mention et DM sur #detective
- **Charlie AI × Cerveau2 vault** : recherche dans le second cerveau depuis le chat web et Slack
- **Recherche par dossier spécifique** : extraction auto `dossier_id` (ex: "ADF"), prompt enrichi SQL + vault forcé
- **Pipeline Cerveau2 ingestion continue** : hook IMAP post-persist qui alimente Cerveau2 pour tout mail sauf newsletter/phishing. Migration historique one-shot via `scripts/bootstrap_cerveau2.py`.
- Logs JSON structurés, rotation 7j
- Newsletter digest quotidien Slack

### ⏳ En cours / à calibrer
- Calibration qualité : affiner prompt Daniel avec retours terrain
- Qualité des réponses vault : dépend du contenu indexé dans Cerveau2-Det

### ⬜ À venir (roadmap)
- V2 — Drafts IMAP natifs : basculer livraison Resend → Drafts boîte mail
- V3 — Bot WhatsApp client : canal client direct
- S4 — Dashboard supervision avancé

---

## 5. Accès et environnements

### Production
- **Cockpit** : `https://detective.digitalhs.biz`
- **VPS** : `root@69.62.110.165` (Hostinger KVM8)
- **Déploiement** : `bash scripts/deploy-to-vps.sh` (depuis le Mac de Cyril)
- **Docker** : `cd /opt/DETECTIVE && docker compose ps && docker compose logs -f`

### Local (Mac de Cyril)
```bash
source venv/bin/activate
python -m app.main
```

### Secrets
Tous les secrets vivent dans `.env` (gitignored) :
- 3 `MAILBOX_*_APP_PASSWORD` (Infomaniak)
- `OLLAMA_PRO_API_KEY`
- `OPENROUTER_API_KEY`
- `RESEND_API_KEY`
- `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- `CERVEAU2_BASE_URL`, `CERVEAU2_API_SECRET`

---

## 6. Conventions de code

- Python ≥ 3.11, type hints partout
- `async def` pour tout ce qui touche IMAP, LLM, HTTP
- Logs structurés via `structlog` : `log.info("event.name", key=value)` — jamais de `print()`
- Pas de docstrings verbeux. Une ligne courte si l'intention n'est pas évidente.
- Imports : stdlib → tiers → projet (groupes séparés par ligne vide). `ruff` les trie.
- Erreurs : laisser remonter sauf si on sait quoi en faire. Pas de `try/except: pass`.
- Secrets : jamais en dur dans le code. Toujours via `app.config.get_settings()`.
- Chemins : `pathlib.Path`, pas `os.path`.
- Tests : `pytest-asyncio`, mode auto. Mocker les appels IMAP/LLM externes.

---

## 7. Garde-fous CRITIQUES

⚠️ **Ne JAMAIS commit le `.env`** (déjà dans `.gitignore`, mais double-check).

⚠️ **Ne JAMAIS écrire dans les vraies boîtes Infomaniak en dev**.

⚠️ **Ne JAMAIS envoyer de mail réel via Resend pendant les tests** — utiliser un mock ou `RESEND_API_KEY` vide.

⚠️ **Ne JAMAIS modifier les 3 DB SQLite anonymisées sources** sans confirmation.

⚠️ **Ne JAMAIS logger le contenu intégral d'un mail** — uniquement métadonnées.

⚠️ **Ne JAMAIS logger le contenu intégral des conversations Telegram** — uniquement commande + métadonnées.

⚠️ **Flag IMAP = `AgentProcessed`** (sans `$`). Infomaniak rejette les flags avec préfixe `$`.

⚠️ **Multilingue obligatoire** : la réponse générée DOIT être dans la langue détectée du mail (FR/NL/EN).

⚠️ **agent_state.db ne doit PAS être écrasée en déploiement** — elle contient les catégories, priorités et statuts modifiés via le cockpit. Le script `deploy-to-vps.sh` l'exclut désormais du rsync.

---

## 8. Pièges connus

### CSS hot-row invisible
- `border-l-4` sur `<tr>` ne s'affiche PAS dans les tables HTML. Il faut l'appliquer sur le **premier `<td>`** de la ligne.
- Fond hot-row minimum : `bg-green-900/40` (en dessous, invisible sur fond sombre).
- Voir commit V1.8.1 et `docs/RUNBOOK.md#hot-row-visuel`.

### Déploiement et agent_state.db
- Le script `deploy-to-vps.sh` faisait un `rsync -avz --delete ./data/` qui écrasait `agent_state.db` sur le VPS avec la copie locale (généralement obsolète).
- **Fix v1.9.3** : backup automatique + `--exclude='agent_state.db'` dans le rsync.

### Summary écrasait les vault notes (fix v1.9.3)
- Dans `app/charlie.py`, le `_summarize_results()` était appelé **avant** l'appel au vault. Le summary ne savait donc pas qu'il y avait des notes vault.
- **Fix** : vault interrogé **avant** le summary. Prompt `_SUMMARY_PROMPT_VAULT` intègre explicitement les notes du vault quand elles existent.

### Délai LLM (~30s)
- Le modèle `gemma4:31b` via Ollama Pro met ~30s pour répondre. C'est lié au modèle, pas au code.
- Si le délai est problématique, envisager un modèle plus rapide (Claude Haiku via OpenRouter) pour les requêtes simples.

---

## 9. Tests

```bash
# Tous les tests
pytest

# Tests Charlie vault spécifiques
pytest tests/test_charlie_vault.py -v

# Lint / format
ruff check .
ruff format .
```

---

## 10. Contact et escalade

- **Cyril Dal** (`cdal@digitalhs.biz`) — intégrateur, propriétaire du VPS
- **Daniel Hurchon** — client final, détective privé
- Problème de prod critique : SSH sur le VPS + `docker compose logs -f`
- Problème de modèle LLM : vérifier `OLLAMA_PRO_API_KEY` et fallback OpenRouter

---

*Fin du handover. Version 1.0 — 2026-05-18. À mettre à jour à chaque sprint majeur.*
