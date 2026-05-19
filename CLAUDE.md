# CLAUDE.md — Instructions projet pour Claude Code

> **À lire en premier dans toute session.** Ce fichier donne le contexte, les conventions, les commandes et les garde-fous.

---

## 1. Contexte projet (TL;DR)

**Client** : Daniel Hurchon, détective privé belge — cabinet **Detective.be** avec 3 marques (Detective Belgique FR, Detective Belgium EN/multi, DPDH Investigations).

**Intégrateur** : CDAL (`cdal@digitalhs.biz`) — c'est l'utilisateur que tu assistes.

**But du projet** : agent IA Python qui poll les 3 boîtes mail Infomaniak toutes les 5 min, classifie les mails entrants en 6 catégories, et **uniquement pour les `demande_client`** génère un brouillon de réponse "à la Daniel" via RAG sur 1200 paires Q/R historiques anonymisées (3 DB SQLite). **Au MVP, les brouillons sont envoyés par email à CDAL via Resend** (validation humaine avant transfert à Daniel). Bascule en Drafts IMAP natifs en V2.

**Canal Telegram Boss ↔ Charlie** : en parallèle du pipeline email, Charlie (l'agent IA) dispose d'un bot Telegram direct avec Daniel (le Boss) pour notifications push, résumés, validations et questions conversationnelles. En test, connecté au Telegram de CDAL ; en prod, migré sur le VPS Hostinger avec le compte de Daniel.

**Vision long terme (post-MVP)** : module factures, dashboard supervision, bot WhatsApp client, architecture multi-sub-agents avec LLM différencié par tâche.

---

## 2. Spec & roadmap (lecture obligatoire avant d'écrire du code)

- **Spec technique complète** : `docs/SPEC.md`
- **Roadmap par semaine** : `docs/ROADMAP.md` (l'état courant y est tenu à jour — coche les items au fur et à mesure)
- **Contexte business** : `docs/CONTEXT.md` (Daniel, marques, langues, sensibilités client)

> Si tu hésites sur un choix d'archi, c'est la spec qui tranche. Si la spec ne couvre pas le cas, demande à CDAL avant d'inventer.

---

## 3. Stack technique

| Couche | Choix |
|---|---|
| Runtime | Python ≥ 3.11 (le Mac de CDAL a Python 3.14) |
| Concurrence | `asyncio` |
| IMAP | `aioimaplib` |
| LLM router | **LiteLLM** (proxy OpenAI-compat) |
| LLM principal | **Kimi K2 via Ollama Pro** (abonnement 20€/mois de CDAL) |
| LLM fallback | **OpenRouter** (Claude / GPT-4o à la demande) |
| Embeddings | `intfloat/multilingual-e5-large` (sentence-transformers, local CPU) |
| Vector store | **`sqlite-vec`** (extension SQLite, vit dans les DB existantes) |
| Détection langue | `fasttext` (lid.176.bin) |
| Email outbound MVP | **Resend API** |
| Canal Boss ↔ Charlie | **Telegram Bot** (aiogram / python-telegram-bot) |
| Healthcheck | FastAPI sur `127.0.0.1:8765` |
| Service prod | systemd (le VPS Hostinger KVM8 de CDAL) |
| Logs | `structlog` (JSON structuré) |
| Config | `pydantic-settings` depuis `.env` |

**Ne PAS introduire** sans discussion explicite : Docker, Celery, Redis, Postgres, Kubernetes, ORM lourd. L'architecture est volontairement légère pour rester maintenable par 1 personne.

---

## 4. Commandes courantes

```bash
# Setup initial (à faire une fois)
python3 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # puis éditer

# Lancer l'agent en local
python -m app.main

# Bootstrap one-shot (à exécuter en S1, après que les DB sont en place)
python -m scripts.bootstrap_embeddings
python -m scripts.extract_personality

# Tests
pytest

# Lint / format
ruff check .
ruff format .
```

---

## 5. Conventions de code

- **Python ≥ 3.11**, type hints partout, `from __future__ import annotations` pas nécessaire
- **`async def`** pour tout ce qui touche IMAP, LLM, HTTP — éviter le sync bloquant dans la hot path
- **Logs structurés via `structlog`** : `log.info("event.name", key=value)` — jamais de `print()`
- **Pas de docstrings verbeux**. Une ligne courte si l'intention métier n'est pas évidente. Le nom doit suffire.
- **Pas de commentaires explicatifs inutiles**. Garder uniquement les `TODO S2/S3` qui marquent ce qui reste à coder par phase de roadmap.
- **Imports** : stdlib → tiers → projet (groupes séparés par ligne vide). `ruff` les trie.
- **Erreurs** : laisser remonter sauf si on sait quoi en faire. Pas de `try/except: pass`.
- **Secrets** : jamais en dur dans le code. Toujours via `app.config.get_settings()`.
- **Chemins** : utiliser `pathlib.Path`, pas de `os.path`.
- **Tests** : `pytest-asyncio`, mode auto. Mocker les appels IMAP/LLM externes.
- **Bot Telegram** : commandes en minuscules avec underscore (`/approve`, `/reject`), réponses courtes, vouvoiement, jamais de contenu mail entier dans le chat (uniquement résumés et métadonnées).

---

## 6. Garde-fous IMPORTANTS

⚠️ **Ne JAMAIS commit le `.env`** (le `.gitignore` le bloque, mais double-check si tu touches au gitignore).

⚠️ **Ne JAMAIS écrire dans les vraies boîtes Infomaniak en dev**. Les modules IMAP doivent supporter un mode `--dry-run` ou un compte mail de test. La première vraie connexion en prod sera surveillée par CDAL.

⚠️ **Ne JAMAIS envoyer de mail réel via Resend pendant les tests automatisés** — utiliser un mock ou `RESEND_API_KEY` vide (le module skip alors avec un warning).

⚠️ **Ne JAMAIS modifier les 3 DB SQLite anonymisées sources sans confirmation**. Le bootstrap ajoute des tables `pairs` et `pairs_vec` — c'est OK, mais ne touche pas aux tables existantes.

⚠️ **Ne JAMAIS logger le contenu intégral d'un mail entrant** — uniquement métadonnées (message-id, expéditeur, sujet, classification). Pour debug, ajouter un flag `LOG_MAIL_BODY=true` explicite.

⚠️ **Ne JAMAIS logger le contenu intégral des conversations Telegram** — uniquement commande reçue + métadonnées (user_id, chat_id, timestamp). Pour debug, ajouter un flag `LOG_TG_CONVERSATION=true` explicite.

⚠️ **Ne JAMAIS commit le token Telegram bot** (dans `.env`, déjà protégé par `.gitignore`, mais double-check).

⚠️ **Pas de Docker au MVP**. Si tu es tenté, relis `docs/SPEC.md` section "Stack technique".

⚠️ **Flag IMAP = `AgentProcessed`** (sans `$`). Infomaniak rejette les flags IMAP avec préfixe `$`. Ne jamais remettre `$AgentProcessed`.

⚠️ **Multilingue obligatoire** : la réponse générée DOIT être dans la langue détectée du mail entrant (FR/NL/EN). Tester systématiquement les 3 langues.

---

## 7. État courant du projet

Voir `docs/ROADMAP.md` pour l'état détaillé. À l'instant T (v1.6.1) :

- ✅ **MVP opérationnel en production** — `detective.digitalhs.biz`
- ✅ **Pipeline IMAP complet** : polling 3 boîtes, classification 8 catégories, priorité intelligente, RAG + génération brouillon
- ✅ **Cockpit web** : inbox, conversation, chat AI Charlie, dashboard admin
- ✅ **Slack Bot Charlie AI** : @mention + DM sur #detective
- ✅ **Logs quotidiens** : JSON structuré, rotation 7j
- ⏳ **Calibration qualité** : affiner prompt Daniel avec retours terrain
- ⬜ **V2 — Drafts IMAP natifs** : basculer livraison Resend → Drafts boîte mail
- ⬜ **V3 — Bot WhatsApp client** : canal client direct

---

## 8. Workflow de collaboration

1. **Avant d'écrire du code**, lis `docs/SPEC.md` et `docs/ROADMAP.md`. Identifie la phase en cours (S1/S2/S3/S4).
2. **Si une décision n'est pas dans la spec**, demande à CDAL. N'invente pas d'archi parallèle.
3. **Avant de toucher à un module existant**, lis-le en entier. La plupart sont des stubs avec des TODO clairs marqués par phase.
4. **Tests d'abord** quand c'est un module pur (RAG retrieve, prefilter, language detect, prompt assembly). IMAP/LLM peuvent rester en intégration manuelle au début.
5. **Quand tu termines une étape de la roadmap**, coche la case correspondante dans `docs/ROADMAP.md` et propose à CDAL de passer à la suivante.

---

## 9. Pré-requis bloquants attendus de CDAL

Pour avancer sur S1, j'ai besoin de :
- [ ] Les 3 fichiers `.sqlite` anonymisés déposés dans `data/`
- [ ] Le schéma de chaque DB (`sqlite3 data/boiteX.sqlite ".schema"`)
- [ ] `.env` rempli avec : 3 `MAILBOX_*_APP_PASSWORD` Infomaniak, `OLLAMA_PRO_API_KEY`, `RESEND_API_KEY`
- [ ] Domaine Resend `agent@digitalhs.biz` vérifié (sinon adapter `RESEND_FROM`)
- [ ] Token Telegram bot créé via @BotFather + `TELEGRAM_CHAT_ID` du compte de test (CDAL) pour la phase S1-S3

---

## 10. Mémoire et préférences user

L'utilisateur (CDAL) :
- Communique en français, écrit court, parfois fautes de frappe rapides — décoder l'intention.
- Préfère brainstormer avant d'implémenter.
- N'aime pas l'over-engineering : MVP simple d'abord, V2 quand qualité prouvée.
- Pense ROI client : "solide 24/7" sans surdimensionner.
- Veut une archi multi-sub-agents à terme avec LLM différencié — d'où le choix LiteLLM dès le départ.
