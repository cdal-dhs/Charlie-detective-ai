# CLAUDE.md — Instructions projet pour Claude Code

> **À lire en premier dans toute session.** Ce fichier donne le contexte, les conventions, les commandes et les garde-fous.

---

## 1. Contexte projet (TL;DR)

**Client** : Daniel Hurchon, détective privé belge — cabinet **Detective.be** avec 3 marques (Detective Belgique FR, Detective Belgium EN/multi, DPDH Investigations).

**Intégrateur** : CDAL (`cdal@digitalhs.biz`) — c'est l'utilisateur que tu assistes.

**But du projet** : agent IA Python qui poll les 3 boîtes mail Infomaniak toutes les 5 min, classifie les mails entrants en **8 catégories** (pré-filtre règles + LLM), et **uniquement pour les `demande_client`** génère un brouillon de réponse "à la Daniel" via RAG sur ~2000 paires Q/R historiques anonymisées (3 DB SQLite + `sqlite-vec`).

**Livraison V2a (livrée v1.17+)** : les brouillons sont **déposés directement dans le dossier `Drafts` IMAP de la boîte source** (auto-découverte via `LIST`, flag `\Draft`, sujet `DEMANDE D'Approbation - Reponse Demande Client : ...`). Resend est conservé **uniquement** comme fallback si l'APPEND IMAP échoue, et pour les alertes système. Daniel approuve/rejette depuis sa propre boîte mail — pas de transfert par CDAL.

**Cerveau2-Det — second cerveau** (livré v1.9+) : vault Markdown + API FastAPI sur `cerveau2-det.digitalhs.biz`. **Ingestion 100% des emails traités + toutes les pièces jointes** (zéro tolérance sur le skip). Charlie interroge le vault en parallèle de SQL local + archives historiques + mémoire pour répondre aux questions de Daniel (recherche sémantique + keyword + nuage de liaison YAML).

**Cockpit web** (livré v1.16+) : `detective.digitalhs.biz` — FastAPI + HTMX + Tailwind CDN. Inbox filtrable (tabs + checkboxes boîtes + tri), conversation détaillée avec viewer PJ, chat AI Charlie (question/réponse avec feedback), dashboard admin (stats, settings LLM, audit logs, télémétrie poller).

**Canal Slack Boss ↔ Charlie** (livré v1.16+) : Slack Bot interactif (`slack_bolt`) sur `#detective` — @mention ou DM, résumés de dossiers narratifs, recherche factuelle, feedback loop. **Le module Telegram est conservé dans le code mais inactif** (dépriorisé — Slack suffit).

**Vision long terme (post-V2c)** : module factures, bot WhatsApp client, dashboard supervision dédié, architecture multi-sub-agents avec LLM différencié par tâche, suppression mails > 28 jours.

---

## 2. Spec & roadmap (lecture obligatoire avant d'écrire du code)

- **État complet du projet pour un nouvel agent** : `HANDOVER.md` (à la racine) — c'est **le document de référence** : architecture, pipeline Charlie, fichiers clés, bugs résolus, points de vigilance, procédures d'urgence. À lire en priorité absolue.
- **Spec technique complète** : `docs/SPEC.md` (figée 2026-05-13 — désalignée sur certains points avec la prod actuelle, voir HANDOVER pour la vérité opérationnelle).
- **Roadmap par semaine** : `docs/ROADMAP.md` (l'état courant y est tenu à jour — coche les items au fur et à mesure).
- **Contexte business** : `docs/CONTEXT.md` (Daniel, marques, langues, sensibilités client).
- **Runbook incidents** : `docs/RUNBOOK.md` (post-mortems + procédures d'urgence).
- **Documentation Cerveau2** :
  - `docs/CERVEAU2.md` — vue d'ensemble du second cerveau.
  - `docs/CERVEAU2_API.md` — référence API.
  - `docs/CERVEAU2_INTEGRATION.md` — guide d'intégration pour agents externes.
  - `docs/CERVEAU2_EXTRACTION.md` — comment traiter/extraire les informations.
  - `docs/CERVEAU2_RECHERCHE_FACTUELLE.md` — **recherche factuelle via Cerveau2** (dense search = implicit AND, normalisation numéros, faux négatifs LLM, déduplication — patterns et pièges, à lire avant toute recherche par numéro/nom propre).
  - `docs/PATTERNS_FROM_CHARLIE_V1.21.3.md` — **patterns réutilisables pour Second Cerveau Pro** (3 bugs IMAP génériques + observabilité + 19 tests, à backporter dans tout nouveau client).

> Si tu hésites sur un choix d'archi, **c'est `HANDOVER.md` qui tranche** sur l'état réel. Si une décision n'est documentée nulle part, demande à CDAL avant d'inventer.

---

## 3. Stack technique

État au **2026-06-22 (v1.24.1)**. La SPEC.md d'origine est désalignée sur certains points — **cette section fait foi**.

| Couche | Choix |
|---|---|
| Runtime | Python ≥ 3.11 (VPS = 3.11, Mac CDAL = 3.14) |
| Concurrence | `asyncio` |
| IMAP | `aioimaplib` |
| LLM router | **LiteLLM** + post-traitement `_clean_reasoning()` (filtre les traces de raisonnement kimi-k2.6) |
| LLM principal (pipeline : classifier, generator) | **`openai/kimi-k2.6:cloud`** via Ollama Pro Cloud (20€/mois), provider `openai/` + `api_base=https://ollama.com/v1` — **raisonnement activé, voir §6 "LLM Ollama"** |
| LLM chat Charlie (cockpit + Slack Bot) | **`openai/kimi-k2.6:cloud`** (même provider — bascule v1.19.3, gemma4:31b faisait des refus systématiques) |
| LLM fallback | **`openai/glm-5.1:cloud`** via Ollama Pro Cloud (Claude Sonnet 4 est 404 sur OpenRouter) |
| Embeddings | **`openai/text-embedding-3-small`** via OpenRouter (API stateless — image Docker ~800MB au lieu de ~4GB avec sentence-transformers local) |
| Vector store | **`sqlite-vec`** (extension SQLite, vit dans les DB existantes) |
| Détection langue | **`langdetect`** (remplace fasttext qui ne build pas sur Mac ARM) — supporte **toutes langues BCP-47** (v1.21.0+) |
| Aide lecture multilingue | **`app/pipeline/translator.py` + `app/pipeline/draft_renderer.py`** (v1.21.0) — mail ≠ FR → 4 blocs dans brouillon (NL original + FR + proposition FR + NL traduite) |
| Email outbound — **livraison brouillon** | **IMAP Drafts** (V2a livrée) : `app/delivery/imap_draft.py`, flag `\Draft`, auto-découverte dossier via `LIST` |
| Email outbound — fallback + alertes | **Resend API** (`agent@digitalhs.biz`) |
| Canal Boss ↔ Charlie | **Slack Bot** (`slack_bolt`) sur `#detective` — Telegram module conservé inactif |
| Cerveau2-Det | **Vault Markdown + API FastAPI** sur `cerveau2-det.digitalhs.biz` (sqlite-vec + E5-large côté serveur, ingestion 100% emails + PJ) |
| Cockpit web | **FastAPI + HTMX + Tailwind CDN** sur `:8080` exposé via Traefik |
| Healthcheck | FastAPI sur `127.0.0.1:8765` (interne) |
| Service prod | **Docker + Docker Compose + Traefik** (VPS Hostinger KVM8) |
| Logs | `structlog` (JSON structuré, rotation 7j) |
| Config | `pydantic-settings` depuis `.env` |
| Version | Source unique `app/_version.py` (`VERSION = "1.21.5"`) — **tolérance zéro** sur `pyproject.toml` qui reste figé en `1.9.5` (volontaire) |

**Ne PAS introduire** sans discussion explicite : Kubernetes, Swarm, Celery, Redis, Postgres, ORM lourd, framework JS front (React/Vue/Angular). Le périmètre Docker actuel (1 service Compose + Traefik externe) est figé.

---

## 4. Commandes courantes

```bash
# Setup initial (une seule fois)
python3 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # puis éditer avec les secrets Infomaniak / Ollama / Resend / Cerveau2

# Lancer l'agent en local
python -m app.main

# Bootstrap one-shot (déjà fait en S1 — ne re-exécuter que si on réindexe)
python -m scripts.bootstrap_embeddings   # 2042 paires Q/R → pairs_vec (sqlite-vec)
python -m scripts.extract_personality    # → app/prompts/personality_daniel.txt
python -m scripts.bootstrap_cerveau2     # initialise Cerveau2 (à exécuter une fois)

# Tests
pytest

# Lint / format
ruff check .
ruff format .

# Déploiement prod (depuis le Mac de CDAL)
bash scripts/deploy-to-vps.sh            # pre-flight checks + push + healthcheck
# ⚠️ JAMAIS `docker compose up -d --build` directement sur le VPS (cf. §6)

# Debug local
python -m scripts.test_pipeline.py       # smoke test pipeline complet (mock IMAP)
python -m scripts.smoke_test_llm.py      # vérifie la connectivité LLM
python -m scripts.smoke_test_sqlite_vec.py

# Module Telegram (INACTIF en prod — Slack est utilisé)
# Le code est conservé pour fallback / futur, ne pas activer sans discussion.
python -m scripts.run_telegram_bot.py
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
- **Templates web** : **Jinja2 + HTMX**, pas de framework JS lourd (pas de React/Vue/Angular). Alpine.js autorisé pour le micro-interactive côté client.
- **Cerveau2** : tout client Cerveau2 doit être **dégradation silencieuse** (retourner `[]` / `None` en cas d'erreur, ne jamais faire crasher Charlie).

---

## 6. Garde-fous IMPORTANTS

### Sécurité & secrets
⚠️ **Ne JAMAIS commit le `.env`** (le `.gitignore` le bloque, mais double-check si tu touches au gitignore).
⚠️ **Ne JAMAIS commit le token Telegram bot** ni le token Slack Bot ni `CERVEAU2_API_SECRET` (dans `.env`, déjà protégés par `.gitignore`, mais double-check).

### IMAP — prod
⚠️ **Ne JAMAIS écrire dans les vraies boîtes Infomaniak en dev**. Les modules IMAP doivent supporter un mode `--dry-run` ou un compte mail de test. La première vraie connexion en prod sera surveillée par CDAL.
⚠️ **Flag IMAP = `AgentProcessed`** (sans `$`). Infomaniak rejette les flags IMAP avec préfixe `$`. Ne jamais remettre `$AgentProcessed`.
⚠️ **Flag brouillon = `\Draft`** (standard RFC 3501). Le dossier cible est auto-découvert via `LIST` car Infomaniak localise : `Drafts` (EN) ou `Brouillons` (FR) ou tout dossier contenant "draft"/"brouillon" insensible à la casse. Si l'APPEND échoue, fallback Resend automatique.

### Resend
⚠️ **Ne JAMAIS envoyer de mail réel via Resend pendant les tests automatisés** — utiliser un mock ou `RESEND_API_KEY` vide (le module skip alors avec un warning).

### Données
⚠️ **Ne JAMAIS modifier les 3 DB SQLite anonymisées sources sans confirmation**. Le bootstrap ajoute des tables `pairs` et `pairs_vec` — c'est OK, mais ne touche pas aux tables existantes.
⚠️ **Ne JAMAIS logger le contenu intégral d'un mail entrant** — uniquement métadonnées (message-id, expéditeur, sujet, classification). Pour debug, ajouter un flag `LOG_MAIL_BODY=true` explicite.
⚠️ **Ne JAMAIS logger le contenu intégral des conversations Slack** (Charlie Bot) — uniquement commande reçue + métadonnées (user_id, channel, timestamp). Pour debug, ajouter un flag `LOG_SLACK_CONVERSATION=true` explicite.

### LLM — Ollama Pro Cloud
⚠️ **Provider litellm pour Ollama Cloud = `openai/<model>` + `api_base=https://ollama.com/v1`**. **JAMAIS** `ollama_chat/<model>` qui force litellm vers `localhost:11434` (Ollama local — inexistant sur le VPS). Si un nouveau modèle ne répond pas, vérifier immédiatement le provider et l'`api_base`.

⚠️ **kimi-k2.6:cloud est un *reasoning model*** (v1.21.1+) : sa réponse finale est dans `message.reasoning_content`, pas dans `message.content` (qui reste vide). Le wrapper `complete()` dans `app/llm/router.py` extrait automatiquement `reasoning_content` quand `content` est vide.

⚠️ **Post-traitement `_clean_reasoning()`** (v1.21.2, toujours actif) : 30+ patterns regex filtrent les traces de raisonnement typiques (FR + EN + listes + guillemets + auto-critique "Version plus X :", "C'est mieux.", "Refonte :", etc.). Sans ce nettoyage, les brouillons sont pollués par des métadiscours du LLM ("L'utilisateur demande...", "The user wants...", "Let me analyze..."). **Si un nouveau modèle reasoning est ajouté, enrichir les patterns dans `_REASONING_LINE_PATTERNS`**.

⚠️ **Nom du modèle = `kimi-k2.6:cloud`** (avec `.6` et `:cloud`). **JAMAIS** `kimi-k2` (404), `gemma4:31b` (obsolète), `claude-sonnet-4` (404 OpenRouter).

### Cerveau2
⚠️ **Ingestion 100% des emails + PJ** : tout mail traité (peu importe la catégorie) doit être envoyé à Cerveau2 via `feed_correspondance()`, et toute pièce jointe via `feed_document()`. Zéro tolérance sur le skip — c'est ce qui alimente le second cerveau de Daniel.
⚠️ **Recherche factuelle** : pour recherche par numéro/nom propre, n'envoyer à Cerveau2 **que l'identifiant précis** (ex: `"0488411192"`, pas `"retrouve le dossier avec téléphone 0488/411192"`). Le dense retrieval calcule un vecteur moyen de TOUS les concepts (implicit AND) — les documents qui ne contiennent pas tous les mots ont un score faible. Voir `docs/CERVEAU2_RECHERCHE_FACTUELLE.md`.
⚠️ **Faux négatifs du LLM de synthèse Cerveau2** : le LLM peut écrire "je ne trouve pas" alors que le document est dans le `context`. Charlie vérifie si le keyword recherché est présent dans `vault_answer` brut et court-circuite la réponse contradictoire (cf. `_bad_vault` dans `app/charlie.py`).
⚠️ **Mapping priorité Cerveau2** : `high` → `urgent`, `low` → `faible`, `dossier_id` vide → `"GENERAL"`. Cerveau2 rejette les autres valeurs (422).
⚠️ **Body tronqué à 150K** avant envoi Cerveau2 (évite payloads massifs / timeouts).
⚠️ **Timeout Cerveau2** : 120s avec retry 3x (l'ingestion embeddings + fallback LLM prend 40-120s par email).
⚠️ **Entités Cerveau2 non indexées** : les fiches `04_entities/personnes/*.md` créées manuellement ne sont PAS dans `chunk_embeddings` (sqlite-vec). Le fallback direct `GET /notes/{path}` dans Charlie contourne ce problème (cf. `_vault_task()`). La vraie réindexation a échoué plusieurs fois sur le VPS (volume mount, extension sqlite3 vec0) — ne pas retenter sans s'être coordonné avec CDAL.

### Multilingue
⚠️ **Multilingue obligatoire** : la réponse générée est TOUJOURS en **français** (langue de travail de Daniel). Si le mail entrant est en NL/EN/DE/ES/autre, le brouillon est enrichi (v1.21.0) avec 4 blocs : email d'origine + traduction FR + proposition FR + traduction dans la langue source. Voir `app/pipeline/translator.py` et `app/pipeline/draft_renderer.py`.

⚠️ **Détection langue étendue** (v1.21.0+) : `Language = str` au lieu de `Literal["fr","nl","en"]` — toutes langues BCP-47 supportées (allemand, espagnol, italien, portugais, etc.). Libellé humain via `language_label()`.

### Déploiement
⚠️ **JAMAIS builder l'image Docker sur le VPS de production.** Le VPS (`69.62.110.165`) est un VPS de PROD — un build Docker consomme tout le CPU/RAM et le site devient inaccessible pendant 10-30 min. Toujours builder en local (Mac CDAL, M4 Max 48 GB) puis pousser l'image compilée vers le VPS :
```bash
# 1. Builder en local (rapide sur M4 Max)
docker build -f Dockerfile.base -t detective-agent:base .
docker build -t detective_detective .

# 2. Pousser l'image vers le VPS sans rebuild distant
docker save detective_detective | ssh root@69.62.110.165 'docker load'

# 3. Redémarrer le service sur le VPS (utilise l'image locale chargée)
ssh root@69.62.110.165 'cd /opt/DETECTIVE && docker compose up -d'
```
Le script `scripts/deploy-to-vps.sh` intègre ce workflow. **Ne jamais utiliser `docker compose up -d --build` directement sur le VPS.**

⚠️ **Pas d'ajout de nouveaux services d'orchestration** (Kubernetes, Swarm, Nomad). Le périmètre Docker actuel (1 service Compose + Traefik externe `root_default`) est figé.

### Version
⚠️ **Tolérance zéro sur la version** : source unique `app/_version.py`. **Jamais** `importlib.metadata`. À chaque release (nouveauté, bugfix, correction) → bump `app/_version.py` + entrée dans `CHANGELOG.md`. `pyproject.toml` reste volontairement en `1.9.5` (figé). Le badge du cockpit est lu dynamiquement depuis `app/_version.py`.

---

## 7. État courant du projet

État au **2026-06-22 — v1.24.1** déployée en prod. Voir `HANDOVER.md` pour le détail complet, `CHANGELOG.md` pour l'historique, `docs/ROADMAP.md` pour la roadmap à jour.

### ✅ Livré
- **S1 → S4 terminées** : infra & data, pipeline IMAP, RAG + génération, prod 24/7 sur KVM8.
- **V2a — Livraison Drafts IMAP** (v1.17+) : Charlie dépose les brouillons dans `Drafts` de la boîte source, flag `\Draft`. Daniel approuve/rejette depuis sa boîte mail. Resend conservé en fallback.
- **Cerveau2-Det** : vault + API sur `cerveau2-det.digitalhs.biz`, ingestion 100% emails + PJ, recherche sémantique + keyword, nuage de liaison YAML (frontmatter `epouse`, `mari`, etc.).
- **Cockpit web** : `detective.digitalhs.biz` — inbox filtrable, conversation détaillée avec viewer PJ, chat AI Charlie, dashboard admin (stats, settings LLM, audit logs, télémétrie, backup Cerveau2).
- **Slack Bot Charlie AI** : @mention + DM sur `#detective` (le module Telegram est conservé inactif).
- **Charlie — recherche robuste** : SQL programmatique bypass (comptages, statuts pending, listes dossiers), résumé de dossier narratif LLM, garde anti-hallucination, garde anti-"pas trouvé" malgré données présentes, scoring mots-clés avec bonus noms concrets / pénalité verbes, déduplication probants, recherche numérique par numéro.
- **Aide lecture multilingue v1.21.0** : pour les mails NL/EN/DE/ES/etc., brouillon enrichi avec 4 blocs (email d'origine + traduction FR + proposition FR + traduction langue source). Daniel n'a plus à déchiffrer une langue qu'il ne maîtrise pas.
- **Endpoint retry-draft v1.21.0** : `POST /api/drafts/{id}/retry` permet de régénérer un brouillon manquant (cas deadlock poller).
- **LLM kimi-k2.6:cloud stable v1.21.1+** : bascule depuis gemma4:31b (obsolète) + claude-sonnet-4 (404). Extraction `reasoning_content` + post-traitement `_clean_reasoning()` v1.21.2 (filtre 30+ patterns de traces de raisonnement).
- **Hardening classifier v1.24.0** (meeting Daniel 2026-06-22) : 3 règles déterministes où le body l'emporte sur le sujet — `_is_wp_contact_form()` (formulaires WordPress toutes boîtes), `_is_reply_to_daniel()` (Re:+citation signée Daniel), `_has_strong_human_demand()` (exception au « jamais remonter depuis phishing »). Rattrape #515 (formulaire WP classé facture), #606 (Re:+devis classé facture), #614 (homoglyphes itsme classé phishing). #515 + #606 reclassés et livrés en prod. Règle d'or : faux positifs acceptables, faux négatifs intolérables.
- **Brouillon hors-légalité v1.24.1** : `_detect_illegal_request()` (11 regex FR/NL/EN) court-circuite le brouillon qualifiant si le client demande un piratage / accès non autorisé aux communications (WhatsApp, téléphone, compte, logiciel espion) → `_build_illegal_refusal_draft()` = refus poli + cadre légal belge (infractions pénales) + alternative légale (filature/surveillance/constat). Pour #614 (Serge M / « faire sortir les conversations WhatsApp »).

### ⏳ En cours
- **V2b — Polishing cockpit** : filtres inbox (3 boîtes cochées par défaut), latence Charlie < 5s (parallélisation Cerveau2 + SQL déjà faite).
- **V2c — Feedback loop qualité Daniel** : détecter les mails `Sent` qui correspondent à un brouillon V2a, calculer le diff, taux d'acceptation, affiner `personality_daniel.txt` avec les patterns d'édition. **Démarrage lundi 2026-05-25** (cf. mémoire `project_v2_drafts_approval`).
- **Reclassement #614 (post-v1.24.1)** : `backfill_reclassify.py --apply --only-id 614` puis `deliver_pending_drafts.py --only-id 614 --apply` — à valider avec CDAL (brouillon = refus poli, confronter au ton de Daniel).
- **Task #4 — Extraction vrai contact client formulaires WP** : les formulaires WP ne demandent jamais l'email — le vrai contact = téléphone (`Telefoonnummer`). Le brouillon doit dire « je vous appelle au 04xx » plutôt que répondre par email au forwarder `mail@/wordpress@/contact@detective*`.
- **Point de vigilance #10** : `case_classifier` + `translator` tournent encore sur `gemma4:31b` (obsolète) — basculer `LLM_MODEL_QUALIFIER` sur `kimi-k2.6:cloud` + purger `app_settings`. Hors-scope v1.24.1.

### ⬜ À venir
- **V3** : module factures (extraction montant/échéance/fournisseur), bot WhatsApp client, dashboard web supervision dédié, suppression mails > 28 jours, architecture multi-sub-agents avec LLM différencié par tâche.
- **Bug connu** : `pairs_vec` table missing sur `boite2.sqlite` — RAG retrieval échoue (rag=0) sur cette boîte. À investiguer hors-scope v1.21.x.

---

## 8. Workflow de collaboration

1. **Avant d'écrire du code**, lis `HANDOVER.md` (état réel) + `docs/SPEC.md` (intentions) + `docs/ROADMAP.md` (phase en cours) + `CHANGELOG.md` (dernières versions). Identifie la phase en cours (S1/S2/S3/S4/V2a/V2b/V2c).
2. **Si une décision n'est ni dans HANDOVER, ni dans la spec, ni dans le CHANGELOG**, demande à CDAL. N'invente pas d'archi parallèle.
3. **Avant de toucher à un module existant**, lis-le en entier. La plupart sont des stubs avec des TODO clairs marqués par phase.
4. **Tests d'abord** quand c'est un module pur (RAG retrieve, prefilter, language detect, prompt assembly, SQL programmatique, scoring keywords, garde-fous). IMAP/LLM peuvent rester en intégration manuelle au début.
5. **Quand tu termines une étape de la roadmap**, coche la case correspondante dans `docs/ROADMAP.md` et propose à CDAL de passer à la suivante.
6. **Avant tout déploiement prod** → `bash scripts/deploy-to-vps.sh` (pre-flight checks obligatoires : branche `main`, working tree clean, build local OK, push GitHub, smoke test Docker, healthcheck post-deploy).
7. **À chaque release** (nouveauté, bugfix, correction) → bump `app/_version.py` (pas `pyproject.toml`) + entrée détaillée dans `CHANGELOG.md` (sections : Ajouté / Changé / Fixé).

---

## 9. État des pré-requis (au 2026-06-22)

Tous les pré-requis S1 sont livrés :

- [x] 3 fichiers `boite1.sqlite`, `boite2.sqlite`, `boite3.sqlite` dans `data/` (gitignored, synchronisés via `rsync` au deploy)
- [x] `.env` complet : 3 `MAILBOX_*_APP_PASSWORD` Infomaniak, `OLLAMA_PRO_API_KEY`, `RESEND_API_KEY`, `CERVEAU2_API_SECRET`, `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`
- [x] Domaine Resend `agent@digitalhs.biz` vérifié
- [x] Slack Bot configuré (Telegram module conservé inactif, dépriorisé)
- [x] Cerveau2-Det vault + API déployés sur `cerveau2-det.digitalhs.biz`
- [x] VPS Hostinger KVM8 : Docker + Traefik + Let's Encrypt, DNS `detective.digitalhs.biz` → `69.62.110.165`
- [x] Cockpit web exposé en HTTPS via Traefik

Voir `HANDOVER.md` section 11 pour les contacts, URLs et procédures d'urgence.

---

## 10. Mémoire et préférences user

L'utilisateur (CDAL) :
- Communique en français, écrit court, parfois fautes de frappe rapides — décoder l'intention.
- Préfère brainstormer avant d'implémenter.
- N'aime pas l'over-engineering : MVP simple d'abord, V2 quand qualité prouvée.
- Pense ROI client : "solide 24/7" sans surdimensionner.
- Veut une archi multi-sub-agents à terme avec LLM différencié — d'où le choix LiteLLM dès le départ.
