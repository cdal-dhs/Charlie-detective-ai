# HANDOVER — Detective.be Agent IA (Charlie)

> Document de transfert pour tout agent (Claude Sonnet/Opus 4.X, GPT, etc.).
> **Dernière mise à jour**: 2026-07-02 · **Version courante**: v1.30.0.13 · **Déployé sur** : `detective.digitalhs.biz`

---

## TABLE DES MATIÈRES (TL;DR)

1. [Qui, quoi, pourquoi](#1-qui-quoi-pourquoi)
2. [Architecture actuelle v1.29.0](#2-architecture-actuelle-v1290)
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
| **Client** | Daniel Hurchon — détective privé belge, cabinet **Detective.be** (4 marques : Detective Belgique FR, Detective Belgium EN/multi, DPDH Investigations, Detectives Belgique) |
| **Intégrateur & ops** | CDAL (`cdal@digitalhs.biz`) — c'est l'utilisateur que tu assistes |
| **Produit** | Agent IA Python qui poll 4 boîtes email toutes les 5 min (3 Infomaniak + 1 OVH), classifie les mails en 8 catégories, et **uniquement** pour les `demande_client` génère un brouillon "à la Daniel" via RAG sur ~2000 paires Q/R historiques (4 DB SQLite + sqlite-vec) |
| **Livraison V2a** (depuis v1.17) | Brouillons déposés **directement dans Drafts IMAP de la boîte source** (flag `\Draft`, sujet `DEMANDE D'Approbation - Reponse Demande Client : ...`). Resend conservé **uniquement** comme fallback. Daniel approuve/rejette depuis sa boîte mail. |
| **Cerveau2-Det** | Vault Markdown + API FastAPI sur `cerveau2-det.digitalhs.biz` (sqlite-vec + E5-large, ingestion 100% emails + PJ) |
| **Cockpit web** | `detective.digitalhs.biz` — FastAPI + HTMX + Tailwind CDN. Inbox filtrable, conversation détaillée, chat AI Charlie, dashboard admin |
| **Canal Boss ↔ Charlie** | Slack Bot interactif (`slack_bolt`) sur `#detective` — @mention ou DM. **Telegram module conservé inactif** (Slack suffit) |
| **Urgence** | Fiabilité critique — bugs "pas trouvé" malgré données existantes = tolérance zéro |

---

## 2. Architecture actuelle (v1.29.0)

```
[4 boîtes email IMAP — 3 Infomaniak + 1 OVH] ──polling 5min──► [Worker asyncio Python]
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
| `app/_version.py` | **Source unique de vérité** version | `VERSION = "1.26.0"` — tolérance zéro, ne JAMAIS utiliser `importlib.metadata`. |
| `app/charlie.py` | **Cœur intelligent Charlie AI** | `ask_charlie()` : extraction entités → SQL programmatique (bypass LLM) + vault Cerveau2 (fallback direct GET) + archives + corrections + mémoire → nuage de liaison familial → **résumé de dossier narratif LLM** (v1.19.1) → garde anti-vide + garde anti-"pas trouvé" |
| `app/charlie_memory.py` | **Mémoire persistante** | Table `charlie_memory` (feedback good/bad, corrections, auto-save) |
| `app/cerveau_client.py` | **Client HTTP Cerveau2** | `query_vault()`, `get_vault_note()` (fallback direct), `feed_correspondance()`, `feed_document()`. Bearer Token statique. **Dégradation silencieuse** (retourne `[]` si Cerveau2 down) |
| `app/config.py` | **Configuration pydantic-settings** | `llm_model_default = "openai/gemma4:31b"` (Ollama Pro, cloud). Provider `openai/` + `api_base=https://ollama.com/v1` |
| `app/llm/router.py` | **Wrapper LiteLLM** | `complete()` avec fallback automatique + extraction `reasoning_content` (fallback glm-5.2:cloud reasoning) + post-traitement `_clean_reasoning()` (30+ patterns pour traces raisonnement) |
| `app/pipeline/translator.py` | **Aide lecture multilingue (v1.21.0)** | `translate_to_fr()` + `translate_from_fr()` avec try/except, troncature 12K. Utilisé si langue mail ≠ FR |
| `app/pipeline/draft_renderer.py` | **Rendu brouillon enrichi (v1.21.0 → v1.24.1)** | Compose 4 blocs pour langues étrangères ; pour le FR, proposition FR + message original du client en dessous |
| `app/pipeline/generator.py` | **Génération brouillon** | Pour `demande_client`/`prise_contact` : branche `app/pipeline/qualification_builder.py` (brouillon déterministe, v1.22.8). Pour les autres catégories : flux LLM few-shot + Cerveau2 + RAG (⚠️ RAG en pause v1.24.2, `pairs` retourné vide). Appelle `translate_to_fr` + `translate_from_fr` en parallèle. **`_load_daniel_fewshot()` (v1.22.4)** : récupère 200 candidats SQL, parse date RFC 2822 en Python, garde top 4 dans fenêtre 30j |
| `app/pipeline/rag.py` | **RAG retrieval (sqlite-vec)** — ⚠️ **en pause v1.24.2** | `retrieve()` court-circuite (retourne `[]`) si `settings.rag_enabled=False`, **avant** tout appel embedding. Code (embed, `_connect`, query sqlite-vec) conservé intact pour réactivation. Tables `pairs_vec` non réindexées depuis 2026-05-28. Voir point de vigilance #1. |
| `app/pipeline/qualification_builder.py` | **Brouillon qualifiant intelligent (v1.22.16, + hors-légalité v1.24.1 → v1.25.21)** | Détection des informations client + spécifiques au cas déjà fournies dans le mail, section "Merci pour les éléments suivants", filtrage des questions redondantes, closing adapté. Gère les cas filature, recherche personne, incapacité, dette, passé violences, micros. **v1.24.1** : `_detect_illegal_request()` (11 regex FR/NL/EN) court-circuite le brouillon standard si le client demande un piratage / accès non autorisé aux communications. **v1.25.21** : refus transformé en outil de qualification commerciale — détection élargie aux localisations via numéro de téléphone/GSM et « savoir avec qui elle/il parle », 11 questions de requalification systématiques, alternative légale détaillée. Cf. mail #614 (Serge M). |
| `app/web/admin.py` | **Simulateur brouillon** (v1.22.9) | `GET /admin/draft-simulator` + `POST /admin/api/draft-simulator/run` : permet à CDAL de coller sujet/corps d'un email simulé et de voir le brouillon généré sans envoyer de vrai mail. RAG/Cerveau2 mockés, classifier LLM réel. |
| `app/web/templates/admin/draft_simulator.html` | **UI Simulateur brouillon** | Formulaire HTMX super-admin : boîte, catégorie, sujet, corps, affichage du résultat. |
| `app/pipeline/classifier.py` | **Classification LLM** (v1.24.0 hardened) | 8 catégories avec few-shots. **`_enforce_recall_over_precision`** : post-traitement qui force `demande_client` en cas de doute. **v1.24.0** — 3 règles déterministes prioritaires où le body l'emporte sur le sujet : (1) `_is_wp_contact_form()` (formulaires WordPress toutes boîtes, force depuis toute catégorie), (2) `_is_reply_to_daniel()` (Re: + citation signée Daniel + expéditeur humain), (3) `_has_strong_human_demand()` (prénom signé + vocabulaire enquête + question tarif, sans marqueur phishing actif — exception au « jamais remonter depuis phishing »). Règle d'or : faux positifs acceptables, faux négatifs intolérables. Cf. mails #515, #606, #614. |
| `app/pipeline/language.py` | **Détection langue** | `Language = str` (toutes BCP-47), `language_label()` pour affichage humain |
| `app/web/api.py` | **Endpoints HTMX + Charlie** | `charlie_ask()`, `charlie_feedback()`, `draft_generate()`, **`POST /api/drafts/{id}/retry`** (régénération manuelle) |
| `app/workers/imap_poller.py` | **Polling IMAP** | 1 task asyncio par boîte, flag `AgentProcessed` (sans `$`) + flag `AgentAttempted` (libère la queue même en cas de crash, v1.21.3). Appelle `generate_draft()` pour `demande_client` → brouillon enrichi |
| `app/delivery/imap_draft.py` | **Dépôt brouillon IMAP** (V2a) | `append_draft()` : flag `\Draft`, SELECT probe pour auto-découverte dossier Drafts (v1.21.9 fix Infomaniak), header custom `X-Detective-Mail-Id` posé si `mail_id` (v1.25.22) pour identifier un brouillon précis en IMAP + `_verify_draft_present` post-APPEND (garde-fou anti-crash silencieux dans la minute). Sujet du brouillon = `suggested_subject or subject` (v1.25.28 — le tag `[NO_EMAIL_IN_THE_FORM]` et les templates WP absurdes ne polluent plus le sujet vu par Daniel). Bandeau affiche `mask_forwarder_sender(reply_to)` (vrai client via Reply-To, v1.25.26). |
| `app/pipeline/subject_fixer.py` | **Nettoyage sujet + masquage expéditeur** (v1.25.7 → v1.25.28) | Nettoie les sujets pollués par des homoglyphes (`itsme` cyrillique) + calcule `suggested_subject` (sujet lisible persisté en DB par le poller, affiché cockpit + IMAP — v1.25.28/v1.26.0). `mask_forwarder_sender()` : **Reply-To uniquement** (v1.25.26, décision CDAL — extraction body ambiguë `info@`/`support@`/`retail@` = faux clients) → `NO_EMAIL_IN_THE_FORM` si `_is_technical_sender` (capte `newsletter@`/`noreply@`/`bounce@` sur tout domaine, plus large que `is_wp_forwarder`) → sender direct sinon. `_extract_client_email_from_body` conservé pour `has_client_email_in_body`/`tag_no_email` (tag du sujet) uniquement. |
| `app/workers/drafts_reconciler.py` | **Réconcilieur Drafts IMAP** (v1.25.22 + bug P0 v1.25.23) | Tâche 15 min garantit la présence physique de chaque brouillon dans `Drafts` de la boîte source — recherche par header `X-Detective-Mail-Id` puis body `EMAIL #<id>`. Bug P0 v1.25.23 : `_draft_present` confondait la ligne de status aioimaplib `Search completed` (non-vide) avec un match → `_has_search_match()` (token numérique uniquement). Anti-doublon `_fetch_candidates` n'accepte que `delivered_at IS NULL` (le workflow V2a ne notifie pas le cockpit, status reste `pending` même après envoi Daniel). |
| `app/pipeline/case_classifier.py` | **Classification fine du cas métier** | Détermine le cas métier (`infidelite_filature`, `recherche_personne`, `incapacite_travail`, `recuperation_dette`, `investigation_successorale` v1.25.27, etc.) pour orienter le `qualification_builder`. Fallback keywords si le LLM ne reconnaît pas le cas. |
| `app/pipeline/priority.py` | **Priorité intelligente** | `HIGH` pour les demandes client chaudes (prénom signé, question tarif, vocabulaire enquête), `LOW` pour les newsletters/factures. |
| `app/pipeline/prefilter.py` | **Pré-filtre règles** | Règles headers/expéditeurs : newsletters, factures, phishing, rappels, demandes évidentes. |
| `app/settings_store.py` | **Overrides runtime** | Persistance/lecture des `app_settings` en DB (LLM model, etc.). ⚠️ purge obligatoire si modification des défauts dans `app/config.py`. |
| `app/cerveau_dossier.py` | **Helpers dossiers Cerveau2** | Fonctions utilitaires pour interroger la liste des dossiers Cerveau2. |
| `app/workers/disk_watcher.py` | **Surveillance disque VPS** | Alerte si espace disque > 75% (niveau anti-crash silencieux). |
| `app/web/db_migrate.py` | **Migrations DB cockpit** | Crée/met à jour le schéma SQLite au boot. |
| `app/web/models.py` | **Schémas Pydantic web** | Modèles de requête/réponse pour le cockpit. |
| `app/telegram_bot.py` | **Module Telegram conservé inactif** | Code présent pour fallback/futur, mais non utilisé en prod (Slack suffit). |
| `scripts/deploy-to-vps.sh` | **Déploiement one-shot** | Pre-flight checks, sync data (exclut `agent_state.db`), build, healthcheck |
| `scripts/backfill_demande_client.py` | **(v1.22.1)** Re-classifie + génère brouillons pour les mails historiques manqués | |
| `scripts/deliver_pending_drafts.py` | **(v1.22.2)** Livre les brouillons existants en IMAP Drafts (idempotent via `delivered_at`) | |
| `scripts/cleanup_old_drafts.py` | **(v1.22.3)** Supprime vieux brouillons IMAP Drafts (SELECT probe + SEARCH SUBJECT) | |
| `scripts/review_missed_demande_client.py` | **(v1.25.17)** Audit périodique des faux négatifs `demande_client` | Lance le pré-filtre et le classifier sur les mails non-`demande_client` des 7 derniers jours pour détecter ceux qui devraient l'être. |
| `scripts/backfill_reclassify.py` | **Re-classement d'un mail spécifique** | Permet de reclassifier et régénérer un brouillon pour un ID donné (ex: #614 phishing → demande_client). |
| `scripts/regenerate_and_deliver_drafts.py` | **Régénération + livraison groupée** | Pour backfill : régénère les brouillons et les dépose en IMAP Drafts. |
| `scripts/test_draft_qualification.py` | **Simulateur CLI brouillon** (v1.22.9) | Teste le brouillon qualifiant en local sans envoyer de vrai mail. |
| `scripts/test_pipeline.py` | **Smoke test pipeline complet** | Mock IMAP, teste pré-filtre → classifier → génération end-to-end. |
| `scripts/smoke_test_llm.py` | **Smoke test connectivité LLM** | Vérifie que le LLM principal et fallback répondent. |
| `scripts/smoke_test_sqlite_vec.py` | **Smoke test sqlite-vec** | Vérifie l'extension sqlite-vec. |
| `scripts/bootstrap_embeddings.py` | **Indexation RAG** | 2042 paires Q/R → `pairs_vec` (sqlite-vec). ⚠️ RAG en pause v1.24.2. |
| `scripts/extract_personality.py` | **Extraction personnalité Daniel** | Génère `app/prompts/personality_daniel.txt` depuis `sent_emails`. |

---

## 3. Le pipeline Charlie AI (état v1.29.0)

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
- `complete(model=settings.llm_model_chat, ...)` — `gemma4:31b` via Ollama Pro Cloud.

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

## 4. Stack technique détaillée (v1.29.0)

| Couche | Outil | Version / Détail |
|---|---|---|
| Python | 3.11+ | VPS = 3.11, Mac CDAL = 3.14 |
| Concurrence | `asyncio` | Tout est `async def` |
| IMAP | `aioimaplib` | 2.0.1 |
| LLM router | **LiteLLM** | + post-traitement `_clean_reasoning()` (filtre traces raisonnement — utile pour le fallback glm-5.2:cloud) |
| LLM principal (chat + pipeline : classifier + generator + Charlie) | **`gemma4:31b`** via Ollama Pro Cloud | `openai/gemma4:31b` (non-reasoning, réponse dans `message.content`) |
| LLM fallback | **`glm-5.2:cloud`** via Ollama Pro Cloud | `openai/glm-5.2:cloud` (reasoning model, thinking High/Max — extraction `reasoning_content`) |
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
- **Modèles actuels (v1.29.0)** : `gemma4:31b` (principal, non-reasoning — réponse dans `message.content`), `glm-5.2:cloud` (fallback, reasoning model — réponse dans `reasoning_content`, extrait automatiquement par `complete()`).
- **JAMAIS** `kimi-k2` (404), `ollama_chat/<model>` (Ollama local inexistant sur le VPS).
- Si un nouveau modèle ne répond pas, vérifier immédiatement provider + api_base + nom.

### Règle 9 — JAMAIS builder Docker sur le VPS de prod
- VPS `69.62.110.165` = VPS de PROD. Build Docker consomme tout CPU/RAM, site inaccessible 10-30 min.
- Builder en local Mac M4 Max, puis `docker save | ssh ... docker load`.
- Le script `deploy-to-vps.sh` intègre ce workflow. **Ne jamais utiliser `docker compose up -d --build` directement sur le VPS.**

### Règle 10 — Pas d'ajouts de nouveaux services d'orchestration
- Périmètre Docker actuel (1 service Compose + Traefik externe `root_default`) est figé.
- Pas de Kubernetes, Swarm, Nomad, Celery, Redis, etc. sans discussion explicite.

---

## 9. Bugs résolus et points de vigilance (état au 2026-07-01, v1.29.0)

### ✅ Bugs résolus récents (v1.22.0 → v1.29.0)

| # | Problème | Statut | Fichier | Notes |
|---|---|---|---|---|
| 1 | **Le LLM n'a JAMAIS vu le vrai Daniel depuis v1.22.0** — bug latent : `_load_daniel_fewshot()` utilisait `date(received_at) >= ?` en SQL, mais `received_at` est stocké en RFC 2822 (`Sat, 13 Jun 2026 05:41:38 +0000`), fonction SQLite `date()` ne parse pas → 0 candidat retourné | ✅ **Corrigé v1.22.4** | `app/pipeline/generator.py` | Le filtre temporel est FAIT EN PYTHON (regex RFC 2822) après récupération d'un panel de 200 candidats SQL. Pattern mutualisé de `scripts/cleanup_old_drafts.py`. Test live : 2 corrections Daniel injectées (mail #561 Soldermann 1990 chars + mail #83 Wastiau 997 chars) → 6122 chars dans le system prompt au lieu de 0. Le LLM imite enfin le format Daniel (intro "Monsieur X,", estimations HTVA × scénarios, mention "On vous téléphonera...", "Bien cordialement, Daniel Hurchon"). |
| 13 | **Tests rouges + robustesse mémoire Charlie** — `query_vault` retourne un tuple `(notes, answer)` mais les tests mockaient une liste ; `charlie_memory` plantait avec `no such table` si la DB n'était pas initialisée ; `_is_vault_relevant` référençait `_VAULT_KEYWORDS` indéfini ; `_extract_dossier_id` ne capturait pas "affaire XYZ123" | ✅ **Corrigé v1.22.5** | `app/charlie_memory.py` + `app/charlie.py` + `tests/test_cerveau_client.py` + `tests/test_charlie_vault.py` + `tests/test_cerveau_feed.py` | `init_memory_table()` appelée dans toutes les fonctions publiques de `charlie_memory.py` avec dégradation silencieuse sur `OperationalError`. `_VAULT_KEYWORDS` défini. Pattern `affaire` ajouté. **75/75 tests verts**. |
| 14 | **Bouton Copier cassé + traçabilité actions Daniel** — le bouton Copier sur `/conversation` ne copiait pas (Alpine.js inline peu fiable) ; CDAL suspectait des demandes clients `detective_belgium` approuvées automatiquement | ✅ **Corrigé v1.22.6** | `app/web/templates/app/conversation.html` + `app/web/admin.py` + `app/web/templates/admin/audit.html` | Listener vanilla JS délégué pour le copier. Section "Dernières actions de Daniel" dans `/admin/audit` montre les `draft_approve`/`draft_reject`/`status_update` de `user_id=2`. Investigation VPS a confirmé : les mails arrivent `pending`/`high`, Daniel les approuve via le cockpit. |
| 15 | **Qualification prospect insuffisante dans les brouillons** — les réponses de Charlie ne posaient pas assez de questions métier pour permettre à Daniel de faire un appel de clôture/devis solide. **Test prod v1.22.7 (#582) = 0/10** : pas de salutation, pas de questions, ton robotique. Root cause : les LLM disponibles (`gemma4:31b`, `kimi-k2.6:cloud`, `glm-5.1:cloud`) ne suivent pas une consigne de liste numérotée. | ✅ **Corrigé v1.22.8** | `app/pipeline/qualification_builder.py` + `app/pipeline/generator.py` + `app/pipeline/case_classifier.py` | Passage à un **brouillon qualifiant déterministe** : questions de base + questions par cas + tarifs + règle des 2 détectives + relais Daniel construits par code. Détection du cas par LLM dédié (`LLM_MODEL_QUALIFIER`, défaut `openai/gemma4:31b`). Extraction du prénom du signataire pour personnaliser la salutation. Brouillons générés aussi pour `prise_contact`. **Tests locaux verts**. |
| 16 | **Pas de moyen de tester les brouillons sans envoyer de vrai email** — CDAL veut itérer sur la qualité des brouillons localement/prod sans risquer d'envoyer des emails. | ✅ **Corrigé v1.22.9** | `app/web/admin.py` + `app/web/templates/admin/draft_simulator.html` + `scripts/test_draft_qualification.py` | Simulateur super-admin `/admin/draft-simulator` (HTMX, mocks RAG/Cerveau2, vrai classifier). Script CLI local `scripts/test_draft_qualification.py` avec cas prédéfinis et `--subject/--body` custom. Tests ajoutés. |
| 17 | **Wording brouillon déterministe pas assez "Daniel"** — premier test prod du simulateur : intro et closing trop génériques. | ✅ **Corrigé v1.22.10** | `app/pipeline/qualification_builder.py` | Intro : "Afin de préparer votre dossier dans les meilleures conditions, et pouvoir vous donner une estimation de devis fiable...". Closing : "Dès réception de ces éléments, je reprendrai contact avec vous pour finaliser le devis et convenir d'un échange téléphonique sur ce nouveau dossier." |
| 18 | **Nouveau cas métier : récupération de dette** — CDAL partage une vraie demande client (Eunice, membre d'entourage devant une somme). Besoin d'un brouillon spécifique. | ✅ **Corrigé v1.22.11 → v1.22.13** | `app/pipeline/case_classifier.py` + `app/pipeline/qualification_builder.py` | Ajout du cas `recuperation_dette`. Brouillon structuré : intro dette, question créance, infos sur la personne concernée, closing légal. **v1.22.13** : extraction auto des infos client déjà reçues (nom, prénom, GSM, email, heure, profil) pour ne plus les redemander. **94/94 tests verts**. |
| 19 | **3 clients ratés par le classifier (body ignoré au profit du sujet)** — meeting Daniel 2026-06-22. #515 (Nathalie Hairemans, formulaire WP classé facture à cause sujet « Réinitialisation mot de passe »), #606 (Van Houtte, Re:+citation devis classé facture), #614 (Serge M, homoglyphes itsme classé phishing). | ✅ **Corrigé v1.24.0** | `app/pipeline/classifier.py` + `tests/test_classifier_hardening.py` | 3 règles déterministes prioritaires où le body l'emporte sur le sujet : `_is_wp_contact_form()` (formulaires WordPress toutes boîtes), `_is_reply_to_daniel()` (Re:+citation signée Daniel), `_has_strong_human_demand()` (exception au « jamais remonter depuis phishing » si prénom signé + vocabulaire enquête + question tarif, sans marqueur phishing actif). Règle d'or : faux positifs acceptables, faux négatifs intolérables. **#515 et #606 reclassés + brouillons livrés en prod** (IMAP Drafts). **136/136 tests hardening verts, 123/123 suite complète**. |
| 20 | **Demande hors-légalité sans réponse adaptée** — #614 (Serge M) demande de « faire sortir les conversations WhatsApp » du téléphone de son épouse = accès non autorisé = infraction pénale en BE. Le brouillon qualifiant infidélité standard est inadapté. Daniel demande une réponse polie expliquant le cadre légal. | ✅ **Corrigé v1.24.1 → v1.25.21** | `app/pipeline/qualification_builder.py` + `scripts/backfill_reclassify.py` + `tests/test_illegal_request.py` | v1.24.1 : `_detect_illegal_request()` (11 regex FR/NL/EN : piratage, extraction conversations, logiciel espion, mise sur écoute, relevés, mot de passe) court-circuite le brouillon standard → `_build_illegal_refusal_draft()` = refus poli + cadre légal belge + alternative légale. **v1.25.21** : refus transformé en outil de qualification commerciale (brief Daniel 260623) — détection élargie aux localisations via numéro de téléphone/GSM et « savoir avec qui elle/il parle », 11 questions de requalification systématiques (but, lien, contexte, éléments, type d'investigation légale, délai, usage du rapport), alternative légale détaillée, tarifs en indication. `backfill_reclassify.py --only-id` ne filtre plus par catégorie (permet de remonter #614 phishing → demande_client). **19 tests, 278 suite verte**. |
| 21 | **Audit périodique des faux négatifs demande_client** — #519 (formulaire WP NL classé `autre`) a passé entre les mailles car `_is_wp_contact_form` était exécuté APRÈS le filtre `_is_service_sender`. | ✅ **Corrigé v1.25.17** | `scripts/review_missed_demande_client.py` | Détection WP faite AVANT le filtre service sender. Si le body est structuré en champs WordPress (`Voornaam`, `Achternaom`, `Telefoonnummer`), c'est un signal INCONTESTABLE de `demande_client` quel que soit l'expéditeur. |
| 22 | **Forwarders WordPress sans email client visible** — les formulaires WP arrivent via des expéditeurs techniques (`mail@/wordpress@/contact@detective*`) et ne contiennent pas l'email du client final. Risque : Daniel répondrait à l'adresse technique au lieu d'appeler le client. | ✅ **Corrigé v1.25.18 → v1.25.20** | `app/pipeline/subject_fixer.py` + `app/delivery/imap_draft.py` + `app/workers/imap_poller.py` + `app/web/api.py` + `app/web/app_routes.py` + `tests/test_subject_fixer.py` + `tests/test_web_inbox_render.py` | v1.25.18 : `is_wp_forwarder()`, `has_client_email_in_body()`, `mask_forwarder_sender()` → affichage `NO_EMAIL_IN_THE_FORM` dans les brouillons IMAP, notifications Slack, cockpit. Tag `[NO_EMAIL_IN_THE_FORM]` dans le sujet si pas d'email client. v1.25.19/20 : fix P0 cockpit 500 (désalignement SQL `cols` après ajout de `body`/`ai_draft`) + fix badge brouillon HTMX + test de non-régression cockpit. **54 tests ciblés verts**. |
| 24 | **Brouillons V2a jamais réconciliés avec les Drafts IMAP** — aucun worker ne vérifiait que les brouillons en DB étaient bien présents dans `Drafts`. Les crashs silencieux du poller (mail #629) laissaient des `delivered_at IS NULL` orphelins sans re-livraison. | ✅ **Corrigé v1.25.22** | `app/workers/drafts_reconciler.py` + `app/delivery/imap_draft.py` + `app/workers/imap_poller.py` | Réconcilieur 15 min : pour chaque brouillon `demande_client` JAMAIS livré (`delivered_at IS NULL`), recherche dans `Drafts` via header custom `X-Detective-Mail-Id: <id>` (SEARCH HEADER), fallback body `EMAIL #<id>` pour les legacy. Colonne `reply_to` propagée depuis la DB vers l'`IncomingMail` reconstruit. Si manquant → re-livraison IMAP Drafts. |
| 25 | **Réconcilieur inopérant (P0) — faux positif systématique** — `_draft_present` confondait la ligne de status `b"Search completed (X secs)."` (toujours présente dans `resp.lines` aioimaplib) avec un vrai match → tout brouillon paraissait « présent » → zéro re-livraison, crashs silencieux non rattrapés. | ✅ **Corrigé v1.25.23** | `app/workers/drafts_reconciler.py` + `tests/test_v1_25_22_fixes.py` | `_has_search_match()` filtre les lignes `Search completed`/`completed` et exige au moins un token numérique (`b"1"`, `b"42"`) comme vrai UID de match. `_draft_present` l'utilise. + Anti-doublon : `_fetch_candidates` ajoute `AND delivered_at IS NULL` (un brouillon déjà livré puis envoyé par Daniel ne doit JAMAIS être re-livré). **5 tests régression** (status seule = False, UID 1 = True, fallback body legacy, exclusion delivered_at). |
| 26 | **Expéditeur forwarder affiché en cockpit (wordpress@/mail@detective, newsletter@, noreply@)** — malgré v1.25.18, des senders techniques ressortaient encore dans l'inbox car `_persist` stockait le sender brut. CDAL : « ne doit plus jamais arriver ». | ✅ **Corrigé v1.25.24 → v1.25.26** | `app/pipeline/subject_fixer.py` + `app/workers/imap_poller.py` + `tests/test_subject_fixer.py` + `tests/test_v1_25_22_fixes.py` | `mask_forwarder_sender(sender, body, reply_to)` réécrite — **Reply-To uniquement** (v1.25.26) : Reply-To valide non-interne → email client ; sinon `_is_technical_sender()` (capte `newsletter@`/`noreply@`/`bounce@`/`wordpress@` sur tout domaine, plus large que `is_wp_forwarder`) → `NO_EMAIL_IN_THE_FORM` ; sinon sender direct. `_persist` applique le mask après coercion `str` (prévient crash `Header`→sqlite). v1.25.25 : regex email body durcie (`[A-Za-z0-9._%+\-]+@...\.[A-Za-z]{2,}`) — élimine faux positifs `@URL markdown`/`@media CSS`. v1.25.26 : suppression de l'extraction body (ambiguë : mélangeait vrais clients et emails de service trompeurs) — seul le Reply-To identifie le vrai client. **308 tests verts**. Backfill prod one-shot (`scripts/backfill_sender.py`) : 224 senders techniques → `NO_EMAIL_IN_THE_FORM`, 353 vrais clients intacts, 0 techniques restants. |
| 27 | **#629 — sujet brouillon non modifié + proposition non régénérée** — le mail #629 (Christèle Kremp-Voinova) affichait encore le sujet template WP et le sender forwarder, malgré la livraison V2a. | ✅ **Corrigé v1.25.23 (prod, one-shot)** | DB `mail_processed` (UPDATE subject + sender) | Script one-shot `imaplib` stdlib + `email.message_from_bytes` pour récupérer le sujet du brouillon IMAP UID 6540 (aioimaplib ne remonte pas les littéraux `{NNN}` dans `resp.lines`). UPDATE DB : subject « Recherche de personne — Christele Kremp-voinova » (préfixe V2a retiré), sender `ckremp@vo.lu` (Reply-To valide). Proposition `ai_draft` 3598 chars régénérée. Cas de référence pour les tests `test_v1_25_22_fixes.py`. |
| 28 | **Extraction email body — faux positifs @URL et @CSS** — `_extract_client_email_from_body` capturait `@lab9be` (URL markdown YouTube) et `@@-ms-viewport{` (règle CSS `@media`) comme emails. | ✅ **Corrigé v1.25.25** | `app/pipeline/subject_fixer.py` + `tests/test_v1_25_22_fixes.py` | Regex strict : local part alphanumérique + ponctuation limitée, TLD ≥ 2 lettres. 2 tests de régression (`test_extract_client_email_ignores_markdown_url_at`, `test_extract_client_email_ignores_css_at_rule`). Fonction conservée pour `has_client_email_in_body`/`tag_no_email` uniquement (PAS dans `mask_forwarder_sender` après v1.25.26). |
| 23 | **Brouillon hors-légalité trop sec** — brief Daniel 260623 : au lieu d'un simple refus, il faut qualifier la vraie mission (but ultime, contexte, éléments disponibles) et proposer une alternative légale adaptée. | ✅ **Corrigé v1.25.21** | `app/pipeline/qualification_builder.py` + `tests/test_illegal_request.py` | `_build_illegal_refusal_draft()` réécrit : refus clair et non négociable, pivot vers qualification, 11 questions systématiques, alternative légale détaillée, tarifs en indication. **19 tests illégaux, 278 suite verte**. |
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
| 29 | **#643 — investigation successorale classée « demande floue »** (Boeteman, 24/06) : « connaître l'ampleur de sa succession et réserver nos droits ». Aucun cas métier `investigation_successorale` → `classify_case` retournait `non_determine` + `objective_check` ne couvrait pas les objectifs patrimoniaux → gemma répondait `OBJECTIF_FLOU` → brouillon générique qui redemandait ce que le client vient d'écrire. Faux négatif intolérable. | ✅ **Corrigé v1.25.27** | `app/pipeline/case_classifier.py` + `app/pipeline/objective_check.py` + `app/pipeline/qualification_builder.py` + `app/pipeline/generator.py` | Nouveau cas métier `investigation_successorale` (CASE_TYPES, fallback keywords `succession`/`héritage`/`patrimoine`) + `_build_succession_draft` (accusé réception + restitution infos + 8 questions succession + coordination notaire, modèle `_build_dette_draft`). `objective_check.py` : `_CLEAR_OBJECTIVE_RE` enrichi (succession, héritier, patrimoine, défunt, décès, réserver ses droits, legs, testament) → objectif reconnu clair **sans appel LLM**. Exclusion du flou dans `_is_vague_request` (le brouillon dédié pose ses questions d'office). **314 tests verts**. Re-classement #643 en prod à valider avec CDAL (`backfill_reclassify.py --only-id 643 --apply` puis `deliver_pending_drafts.py --only-id 643 --apply`). |
| 30 | **Sujet de brouillon IMAP moche (template WP + tag `[NO_EMAIL_IN_THE_FORM]`)** — malgré les brouillons propres, le sujet IMAP restait `Nouveau Message De Détective privé Belgique - Prenons contact [NO_EMAIL_IN_THE_FORM]`. Le sujet lisible `suggested_subject` (déjà calculé par `subject_fixer`) n'était jamais persisté ni écrit dans le sujet du brouillon. | ✅ **Corrigé v1.25.28** | `app/pipeline/subject_fixer.py` + `app/workers/imap_poller.py` + `app/delivery/imap_draft.py` + `scripts/deliver_pending_drafts.py` | `suggested_subject` persisté en DB : `_persist` (INSERT + UPDATE COALESCE), `_fetch_pending` le retourne, `_ensure_column` l'ajoute idempotemment. `append_draft` écrit `suggested_subject or subject` comme sujet du brouillon IMAP → le tag `[NO_EMAIL_IN_THE_FORM]` et les templates WP absurdes ne polluent plus le sujet vu par Daniel. Livreur backfill `_update_db` écrit `suggested_subject` (non-écrasement). **320 tests verts**. |
| 31 | **Cockpit affichait encore le sujet original moche** (inbox + conversation) alors que le brouillon IMAP avait déjà son sujet propre depuis v1.25.28. Incohérence entre le sujet vu par Daniel (Drafts IMAP) et celui vu par CDAL (cockpit). | ✅ **Corrigé v1.26.0** | `app/web/app_routes.py` + `tests/test_web_inbox_suggested_subject.py` | `_fetch_mails` (inbox) et `_fetch_mail` (conversation) sélectionnent `suggested_subject` et l'affichent en priorité (`display_subject = suggested_subject or subject`, symétrique à `append_draft` côté IMAP). Zéro modif template Jinja. Le bouton `fix-subject` (v1.25.4, correction LLM manuelle) reste intact (lit `subject` DB via son propre SELECT). **323 tests verts**. Les anciens `demande_client` d'avant v1.25.28 (sans `suggested_subject`) affichent encore le sujet original — backfill bulk possible plus tard. |
| 32 | **4ème boîte mail OVH (`detectives-belgique.be`)** — Daniel a fourni une nouvelle boîte `info@detectives-belgique.be` hébergée chez OVH (`ex5.mail.ovh.net`), avec brand `Detectives Belgique`, code cockpit `D_DS`, marque Cerveau2 `detectivesbelgique`, DB historique `boite4.sqlite`. Impossible de réutiliser l'IMAP host global `mail.infomaniak.com` pour cette boîte. | ✅ **Corrigé v1.27.3** | `app/config.py` + `app/workers/imap_poller.py` + `app/delivery/imap_draft.py` + `app/workers/drafts_reconciler.py` + `app/charlie.py` + `app/web/*` + scripts | `MailboxConfig` enrichi avec `imap_host`, `imap_port`, `short_code`, `cerveau2_marque`. Connexion IMAP host par boîte (fallback global si vide). Mappings statiques à 3 entrées supprimés/centralisés. Domaines propres étendus à `detectives-belgique.be`. Templates cockpit dynamiques (`mb.brand`, `mb.short_code`). `.env.example` + `docker-compose.yml` mis à jour. **328 tests verts**. |
| 33 | **OVH SEARCH rejete charset / `UNKEYWORD AgentProcessed`** — dès le premier cycle poller sur `detectives_belgique`, `client.search()` avec charset UTF-8 implicite a été rejeté par `ex5.mail.ovh.net` : `[BADCHARSET (US-ASCII)]`. Le fallback `charset="us-ascii"` a aussi échoué avec `Command Argument Error. 11`. L'usage de `UNKEYWORD AgentProcessed` semblait également rejeté. La 4ème boîte ne pouvait donc pas être lue. | ✅ **Corrigé v1.27.3** | `app/workers/imap_poller.py` | `_search_unprocessed()` : (1) SEARCH normal, (2) SEARCH sans charset, (3) `SEARCH ALL` + filtrage côté DB via `_mail_exists()` pour écarter les UIDs déjà traités. `needs_db_filter=True` propagé dans `_process_mailbox()`. **328 tests verts**. |
| 34 | **Brouillon « vague request » insultant pour un avocat (#656 Jennifer Das, 2026-06-26)** — Charlie classait correctement le mail en `infidelite_filature` mais `_is_vague_request()` déclenchait le brouillon flou parce qu'aucune info opérationnelle n'était extractible (un avocat ne donne pas les détails techniques dans le premier contact : il définit la **mission**, pas les données). Résultat : brouillon qui demandait à l'avocate de préciser l'objectif qu'elle avait formulé 3 fois explicitement. Faux négatif intolérable (rater un pro du droit = brouillon insultant). | ✅ **Corrigé v1.27.4** | `app/pipeline/qualification_builder.py` + `app/pipeline/objective_check.py` | Nouveau `_OPERATIONAL_SIGNAL_RE` capturant 5 catégories de signaux forts (mission déléguée par conseil, livrable opérationnel, question de mission déguisée, annonce d'éléments, indicateurs temporels). Court-circuit dans `_is_vague_request()` AVANT le check « cas classé sans info opérationnelle ». `_CLEAR_OBJECTIVE_RE` enrichi avec les mêmes patterns pour court-circuiter l'appel LLM sur les mails d'avocats (gain latence). **340 tests verts** (12 nouveaux). |
| 35 | **Brouillon avocat socialement maladroit (#656 Jennifer Das, suite)** — même après le fix v1.27.4, le brouillon restait maladroit : salutation **« Bonjour Jennifer, »** au lieu de **« Bonjour Maître, »** ; wording **« vous souhaitez… »** au lieu de **« votre client »** ; rappel au GSM du client final (qu'on ne doit PAS contacter directement — c'est l'avocat qui gère le dossier). Un professionnel du droit écrit rarement à la première personne « je » : il définit la mission **de son client**. | ✅ **Corrigé v1.27.5** | `app/pipeline/qualification_builder.py` | Nouveau `_is_legal_counsel_email(body, sender)` combinant indices body (`\bavocat[ée]?\b`, `\bma[îi]tre\s+[A-ZÀ-Ÿ]`, `\bnotaire\b`, `\bhuissier(?:\s+de\s+justice)?\b`, `\bagissant\s+(?:pour|au\s+nom)\b`, `\b(?:son|notre|votre|mon)\s+client\b`, `\bPour\s+Me\b`, `\b[ée]tude\s+de\s+Ma[îi]tre\b`) + indices sender (domaine `avocat|notaire|huissier|legal|juridique|juris`). Nouveau `_rephrase_need_for_counsel()`. Salutation « Bonjour Maître, » générique. Wording « votre client » partout dans `_build_standard_draft` / `_build_vague_request_draft` / `_build_illegal_refusal_draft`. Skip des questions identitaires + identité client final dans `_format_received_info`. Rappel téléphonique au GSM de l'avocat uniquement. **351 tests verts** (11 nouveaux). Patch bonus `scripts/dedup_drafts_by_email_id.py` (3 commits) : `--mailbox` / `--skip-mailbox` filtres + try/except par UID/mailbox + throttle OVH (`asyncio.sleep(0.1)` tous les 5 FETCH) + fix OVH SEARCH ALL `charset=None` + filtre `isdigit()` (le serveur renvoie `[BADCHARSET (US-ASCII)] The specified charset...` au lieu d'UIDs). 10 brouillons obsolètes supprimés en prod (4 sur Infomaniak dont les 2 doublons #656, 6 sur OVH). |
| 36 | **Brouillon « qualifiant médiocre » sur mission datée (#672 Olivier Kirara, 2026-06-27)** — Charlie classait correctement en `infidelite_filature` mais `_build_standard_draft` posait les questions identitaires (« Vos nom et prénom complets », « Votre GSM de contact direct », « Votre adresse complète ») alors que **toutes les coordonnées étaient déjà reçues** dans le formulaire (nom, GSM, email, profil) ET que la mission était **explicitement datée** (« filature le 02/07 à Tournai »). Résultat : brouillon qui demandait au client ce qu'il souhaitait, alors que la mission était claire et la date connue. Faux négatif intolérable (un client qui attend une date précise doit recevoir une réponse alignée sur le benchmark Daniel — capacité+date+réserve, urgence FR, Dans l'attente). | ✅ **Corrigé v1.28.0** | `app/pipeline/qualification_builder.py` + `tests/test_mission_dated_draft.py` (nouveau) + `tests/fixtures/mail_672_kirara.json` (nouveau) | **RC1** Pattern `relation_match` élargi (`fiancé`/`fiancée`/`compagne`/`compagnon`/`concubin`/`concubine`) avec lookahead restrictif. **RC2** `_OPERATIONAL_SIGNAL_RE` accepte « mission le 02 juillet » / « durant le week-end du 5 juillet » / « journée du 02/07 ». **RC3**+**RC4** Extraction `_extract_case_info` ajoute `date_cible` (formats JJ/MM, JJ mois, semaine/week-end, été YYYY, etc.) et `ville_cible` (pattern `à/a/au/aux/en/pour/sur/destination/vers` + mot capitalisé, filtrage stopwords mois/jours). **RC4 rendu** `_format_received_info` affiche « Date de mission souhaitée » + « Ville / lieu de surveillance » en tête de bloc éléments reçus. **RC5** Nouvelle brique `_build_mission_dated_draft()` (~190 lignes) alignée sur le benchmark Daniel : salutation → accusé chaleureux avec « confiance » → **capacité+date+réserve** (« Nous pouvons effectivement organiser une mission de filature le 02/07 à Tournai, sous réserve de recevoir rapidement les informations nécessaires… ») → méthode pédagogique 2 détectives → éléments reçus (avec date+ville) → questions strictement manquantes filtrées (photo/véhicule/adresse/horaires/habitudes — **jamais** nom/prénom/GSM/profil déjà reçus) → tarifs → **phrase urgence FR** si date < 30j (« Compte tenu du caractère urgent de votre demande et de la date très proche de l'intervention ») → clôture « Dans l'attente de votre retour, Bien à vous » → signature SRL. Helpers `_is_mission_dated` (filtre formulations vagues « durant cet été 2026 » pour préserver wording « pour le dossier de votre client » sur les mails avocat #656), `_is_date_urgent`, `_mission_dated_verb`. Wording véhicule aligné Daniel (« Caractéristiques de son véhicule (marque, modèle, couleur, immatriculation si connue) »). **17 nouveaux tests TDD** (368 verts). **#672 livré en prod v1.28.1** via `scripts/deliver_pending_drafts --only-id 672 --apply` (brouillon physique dans Drafts Infomaniak, sujet `DEMANDE D'Approbation - Reponse Demande Client : Filature / surveillance — Olivier Kirara`, header `X-Detective-Mail-Id: 672`). |
| 37 | **Brouillon aberrant sur mail interne (#686 CDAL→Daniel, 2026-06-29)** — CDAL a forwardé une note interne de réunion IT à la boîte `detective_belgique` (pour archive). Charlie a classé le mail en `demande_client` (le classifier v1.24+ est volontairement très permissif pour ne rater aucun vrai client) et a généré un brouillon client aberrant **livré en IMAP Drafts** : salutation « Bonjour PT » (extraction hallucinée du footer « PT Digital Highway Solutions »), accusé de réception d'une note de réunion interne comme si c'était un client. **5 autres mails internes CDAL→Daniel étaient déjà LIVRÉS** avec le même bug (#652, #582, #562, #474, #82 — tous des tests CDAL sauf #686 qui est une vraie note de fond). Cause racine : aucun filtre « sender interne » dans le préfiltre, le classifier, ni le générateur. Faux positif inacceptable (un brouillon aberrant livré dans la boîte Daniel = confusion garantie). | ✅ **Corrigé v1.28.2** | `app/pipeline/prefilter.py` + `app/pipeline/generator.py` + `tests/test_internal_sender_guard.py` (nouveau) | **Défense en profondeur, 3 maillons** : (1) `is_internal_sender()` dans `prefilter.py` détecte un mail interne selon 2 critères : **domaine interne** (`digitalhs.biz`) OU **local-part identifiant un membre** (`cdal`, `daniel` — n'importe quel domaine, ex `cdal@gmail.com`). **Whitelist d'exclusion** pour les préfixes techniques (`wordpress@`, `mail@`, `noreply@`, `no-reply@`, `contactform@`, `postmaster@`, `abuse@`, `newsletter@`, `contact@`, `info@`) pour ne pas casser `is_wordpress_contact_form()`. `quick_classify()` retourne `"autre"` en première position (avant WP). (2) `generate_draft()` court-circuite aussi (défense en profondeur) : si `_is_internal_email(sender)` est True, retourne `GenerationResult(raw_draft="", note="Sender interne — brouillon skipped (v1.28.2)")` AVANT d'invoquer RAG/case_classifier/LLM. Log `warning generator.internal_sender_skip` posé. (3) `GenerationResult` enrichi avec champ optionnel `note: str = ""` (debug). **11 nouveaux tests** (379 verts). **Backfill prod appliqué** : #686 brouillon supprimé des Drafts IMAP (UID 38) + DB rollback (`status=pending, draft_generated=0, ai_draft=NULL`) ; #652/#582/#562/#474/#82 reclassifiés `autre` + `draft_generated=0` (déjà absents des Drafts — Daniel les avait approuvés/rejetés). |
| 38 | **Inbox polluée par cascade de doublons `Re: Votre reçu Apple` (#719-#722, 2026-07-01)** — depuis 2 jours, ~10 mails identiques (sender `dpdhuinvestigations@gmail.com`, sujet `Re: Votre reçu Apple`, brand-mais-pas-officiel — non capturé par `is_internal_sender()`) étaient persistés en `demande_client`/`high` dans l'inbox. Chaque doublon déclenchait un brouillon fantôme en Drafts IMAP. Cause racine : aucun check de dédup logique au poller — 10 `message-id` IMAP distincts = 10 ingestions + 10 brouillons candidats. Le préfiltre matchait `Re:` (`_FOLLOWUP_SUBJECT_RE`) et `_enforce_recall_over_precision` remontait tout en `demande_client` (règle d'or : faux positif acceptable). | ✅ **Corrigé v1.28.3** | `app/pipeline/dedup.py` (nouveau) + `app/workers/imap_poller.py` + `tests/test_dedup.py` (nouveau) + `tests/test_cerveau_feed.py` (patch) + `scripts/backfill_dedup_apple.py` (nouveau) | Nouveau module `is_logical_duplicate()` déterministe (< 5ms/mail, sans LLM) avec clé `(sender_normalized, subject_normalized)` sur fenêtre glissante 48h. Normalisation sujet : strip préfixes `Re:`/`Fwd:`/`AW:`/`TR:`/`SV:` multi-niveaux. Injection dans `_process_single_mail()` juste après le filtre `system_email_skipped` et AVANT `is_subject_suspect()` → 0 coût LLM, 0 brouillon, flag IMAP posé. Nouveau helper `_persist_duplicate()` marque les doublons en `status=duplicate`, `category=autre`, `priority=low`, `draft_generated=0`, `ai_draft=NULL` (audit only). **Cascade guard** : la requête SQL filtre `status != 'duplicate'` pour éviter qu'un doublon d'un doublon soit re-marqué (le parent le plus ancien reste la référence). **22 nouveaux tests TDD** (401 verts). Patch `test_cerveau_feed.py` : mock `is_logical_duplicate → (False, None)` sur 2 tests d'intégration pour que le flux nominal complet (classify → Cerveau2 feed) reste testable. Script de backfill `--dry-run` / `--apply` pour nettoyer les doublons pré-existants en prod (DB + suppression brouillons Drafts IMAP via header `X-Detective-Mail-Id`). Idempotent (un second run ne fait rien). **Note** : `dpdhuinvestigations@gmail.com` reste NON-interne (la dédup est le bon filet, pas l'extension de `is_internal_sender()` — risque de faux positif sur un vrai client nommé "DPDH"). |
| 39 | **Cockpit inbox affiche 1 mail = 1 ligne même pour les fils de discussion (#740/748/746, 2026-07-01)** — quand un client envoie un mail initial puis des replies ping-pong avec sujet qui change (`Dossier Dupont : 740` → `Re: Dossier Dupont : 748` → `ajout au dossier : 746`), l'inbox cockpit montre 3 lignes non liées au lieu d'1 fil clair type Gmail/Outlook. La dédup v1.28.3 ne matche plus quand le sujet change (les Re: sont strippés mais les reformulations cassent la clé). Pas de groupement en DB. | ✅ **Corrigé v1.29.0** | `app/pipeline/threading.py` (nouveau) + `app/workers/imap_poller.py` + `app/web/app_routes.py` + `app/web/templates/app/inbox_rows.html` (macro `thread_row`) + `app/web/templates/app/inbox.html` (tabs view) + `app/web/api.py` + `tests/test_threading.py` (nouveau) + `tests/test_cerveau_feed.py` (patch) + `tests/test_imap_poller_resilience.py` (patch) + `tests/test_v1_25_22_fixes.py` (patch) + `tests/test_suggested_subject_v1_25_28.py` (patch) + `app/web/db_migrate.py` | Nouveau module `threading.py` (regex + heuristiques déterministes, pas de LLM) : `extract_dossier_name()` regex "Dossier Dupont" étendue (accents, composé "Dossier de la Rue", filtre anti-ref via `_is_name_with_lowercase`), `derive_dossier_id_threading()` hiérarchie name > ref > hash sha1[:16] stable, `compute_thread_id()` `f"{dossier_id}::{sender_n}"` ou `adhoc::...`, `pick_thread_subject()` sujet du plus ancien. **6 nouvelles colonnes `mail_processed`** : `message_id`, `in_reply_to`, `dossier_id`, `thread_id`, `thread_subject` + `references` (mot-clé SQL, ALTER séparé) + index `idx_mail_processed_thread`. Poller enrichi : capture headers IMAP `Message-ID`/`In-Reply-To`/`References`, dérive `dossier_id`/`thread_id` AVANT la dédup, helper `_refresh_thread_subject()` propage le sujet canonique. Cockpit : `_group_into_threads()` + macro `thread_row(t)` (parent + replies indentées, **border-l-4 sur le 1er `<td>`** — piège CSS respecté, badge `↳` sur replies, opacity réduite + line-through sur doublons) + tabs `?view=threads|flat|duplicates`. **19 nouveaux tests threading + 0 régression sur 401** = **420 verts**. Dédup v1.29.0 reste stricte : `thread_id` seul ne suffit PAS à marquer duplicate (sinon le parent + reply légitime serait zappé) — filet `subject_EXACT_lowercase` OU `Message-ID` identique OU `In-Reply-To` identique dans 60s. |

### 🔴 Points de vigilance ouverts (état au 2026-06-29)

#### Point de vigilance #1 — RAG mis en pause depuis v1.24.2 (décision CDAL)
> Statut changé le 2026-06-23 : ce n'est plus un **bug à corriger en urgence**, c'est une **fonctionnalité volontairement mise en pause**. L'approche déterministe (`qualification_builder` + few-shot Daniel) est plus fiable et remplace le RAG pour la génération des brouillons.

**Historique (pour mémoire)** : le RAG était cassé sur les **4 boîtes** depuis le 2026-05-28 :
- `boite1.sqlite` : table `pairs` existe mais **0 rows** (était censé en avoir 2042)
- `boite2.sqlite` : table `pairs` **n'existe pas**
- `boite3.sqlite` : table `pairs` **n'existe pas**
- `boite4.sqlite` : table `pairs` **n'existe pas** (nouvelle boîte v1.27.3 — pas d'historique RAG non plus)

Cause : le bootstrap a crashé le 2026-05-28 avec `litellm.BadRequestError: LLM Provider NOT provided. ... You passed model=intfloat/multilingual-e5-large` — le script utilisait encore l'ancien embedder local `e5-large` alors que la v1.18.0 avait basculé vers `openai/text-embedding-3-small` via OpenRouter. Le bootstrap n'a jamais été ré-exécuté après la bascule. Tous les brouillons générés depuis le 2026-05-28 avaient RAG=0.

**Décision v1.24.2 (2026-06-23, CDAL)** : plutôt que de re-bootstrapper `pairs_vec` en urgence, on **met le RAG en pause** :
- Nouveau setting `rag_enabled: bool = False` dans `app/config.py` (env `RAG_ENABLED`).
- `retrieve()` (`app/pipeline/rag.py`) court-circuite immédiatement (retourne `[]`) si `rag_enabled=False`, **avant** tout appel à l'API embedding. Log `rag.disabled_skip`.
- Le code RAG (embed, `_connect`, query sqlite-vec) est **conservé intact** pour réactivation ultérieure.

**Pourquoi c'est sans impact sur la qualité des brouillons** :
1. Pour les `demande_client` / `prise_contact`, le résultat du RAG (`pairs`) **n'était de toute façon pas utilisé** — la branche `build_qualification_draft` (déterministe) ignore `pairs`. Le RAG n'était exploité que dans la branche `else` (catégories hors `draft_categories`).
2. Le few-shot Daniel (v1.22.4, 2 corrections injectées dans le system prompt) donne déjà au LLM le vrai style de Daniel.
3. On supprime un appel embedding inutile (coût + latence) sur chaque mail traité.

**Réactivation (si un jour on le décide)** :
```bash
# 1. Re-indexer pairs_vec sur les 4 boîtes
ssh root@69.62.110.165
cd /opt/DETECTIVE
docker compose exec detective python -m scripts.bootstrap_embeddings
# 2. Activer le flag
echo "RAG_ENABLED=true" >> /opt/DETECTIVE/.env.production
docker compose restart detective
```
Vérifier aussi les catégories de `boite2` (10 catégories dont `PRISE_CONTACT:182` majoritaire) et `boite3` (12 catégories dont `INVESTIGATION_ENTREPRISE:399`) avant de relancer.

**Hors-scope tant que l'approche déterministe donne satisfaction.** Le RAG n'est plus un point bloquant pour V2c.

#### Point de vigilance #2 — Provider litellm pour Ollama Cloud (CRITIQUE v1.21.1)
`ollama_chat/<model>` force litellm vers `localhost:11434` (Ollama **local**). Le provider correct pour Ollama **Cloud** est `openai/<model>` avec `api_base=https://ollama.com/v1`.
**Modèles actuels (v1.29.0)** : `openai/gemma4:31b` (principal + classifier + chat, non-reasoning), `openai/glm-5.2:cloud` (fallback, reasoning).
**Si un nouveau modèle ne répond pas** → vérifier immédiatement provider (openai/ vs ollama_chat/), l'URL api_base (`/v1` pas `/api`), et que le nom de modèle existe sur ollama.com/library.

#### Point de vigilance #3 — glm-5.2:cloud (fallback) est un reasoning model
Sa réponse finale est dans `message.reasoning_content`, pas dans `message.content` (vide). Le wrapper `complete()` extrait automatiquement, MAIS :
- Soit utiliser un autre modèle non-reasoning si on veut du contenu direct
- Soit accepter le coût (raisonnement = plus de tokens) + le post-traitement `_clean_reasoning()`
- **gemma4:31b (modèle principal depuis v1.25.0) est non-reasoning** : réponse directe dans `message.content`, pas d'extraction ni de cleaning nécessaires.

#### Point de vigilance #4 — Traces de raisonnement glm-5.2:cloud (CRITIQUE v1.21.2)
Le fallback reasoning produit des métadiscours parasites : "L'utilisateur demande...", "Let me analyze...", "Points importants :", "Refonte :", "Version plus X :", "C'est mieux.", etc. Le post-traitement `_clean_reasoning()` filtre ~30 patterns, **MAIS** si un nouveau type d'artefact apparaît, il faut **enrichir `_REASONING_LINE_PATTERNS`** dans `app/llm/router.py`. Le cleaning n'est jamais "complet" — c'est une bataille continue.

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

#### Point de vigilance #10 — OVH IMAP + Cerveau2 (nouveau v1.27.3)
Intégration de la 4ème boîte `info@detectives-belgique.be` chez OVH (`ex5.mail.ovh.net`) fonctionne, mais deux comportements spécifiques sont apparus en prod le 2026-06-26 :

1. **OVH ne supporte pas les keywords IMAP**  
   Log observé : `NO Keywords are not supported!`. Conséquence :
   - `UNKEYWORD AgentProcessed` est rejeté (d'où le fallback `SEARCH ALL` + filtrage DB, voir bug #33 ci-dessus).
   - La vérification post-`APPEND` (`_verify_draft_present`) peut échouer immédiatement (`imap_draft.unverified`).
   - Le réconcilieur Drafts IMAP a dû **re-livrer** le brouillon #652 (log `reconcile.redelivered`).
   - **Risque** : doublons de brouillons OVH si le réconcilieur et la vérification post-APPEND ne parviennent pas à identifier le brouillon par header. Surveiller `Brouillons` OVH.

2. **Cerveau2 rejette la nouvelle marque `detectivesbelgique` et le type `fiche_entreprise`**  
   ✅ **Corrigé v0.8.3** — déployé sur `cerveau2-det.digitalhs.biz` le 2026-06-26.
   - Réponses API 422 observées en prod :
     - `"Input should be 'detectivebelgique', 'detectivebelgium' or 'dpdhu'"` pour le champ `marque`.
     - `"Input should be 'document', 'note', 'correspondance' or 'fiche_contact'"` pour le champ `type` (le code Charlie envoie `fiche_entreprise`).
   - **Fix** : `api/models.py` — ajout de `"detectivesbelgique"` dans les `Literal` `marque` de `IngestEmailRequest` et `IngestNoteRequest`, ajout de `"fiche_entreprise"` dans le `Literal` `type`. Mise à jour de la doc `GET /dossiers` dans `api/routes/dossiers.py`. Tests manuels en conteneur (`/ingest-email` + `/ingest-note`) → **200 OK**.
   - **Incident sous-jacent découvert et corrigé** : le backup automatique du vault (`scripts/backup-vault.sh`) avait supprimé TOUS les fichiers source de la branche `main` de Cerveau2 sur GitHub (bug ligne 32 `git reset --soft origin/$BRANCH` + `git pull`). Le script a été corrigé pour pusher le vault sur une branche dédiée `vault-backups` et ne jamais toucher à `main`. Le repo source a été restauré en v0.8.3.

**Mémoire dédiée** : [[ovh-imap-search-quirks]].

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
- [ ] **Vérifier `app/_version.py`** — est-ce bien `1.26.0` ?
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

État au **2026-07-02** : **v1.30.0.12** livrée et déployée en prod (**491 tests verts** sur la branche inbox, 2 pre-existing failures sur `test_mission_dated_draft.py` non liées — date 02/07 stale). **Dernières versions** (sprint en cours) :
- **v1.30.0.12 (2026-07-02)** — Fix "Re: jamais 1ère ligne de fil". CDAL a vu 3 mails "Re: Votre reçu Apple" (même thread_id, in_reply_to=NULL) affichés comme 3 lignes plates individuelles — "un enfant d'un fil parent ne peut pas démarrer seul". `_is_orphan_reply()` : un subject préfixé "Re:/AW:/TR:/Fwd:" = reply orphelin systématique (signal sémantique fort, même si in_reply_to=NULL). `_group_into_threads()` cas 2 (TOUS les mails sont des replies) : parent = plus ancien, `parent_is_orphan=True`, replies triés DESC par date. `app_index()` : RE-GROUPAGE de `other_mails` (rollback partiel du rollback v1.30.0.11 sur la double-grouping) — 3 mails "Re: ..." même thread_id sont désormais 1 fil. Suppression de `is_orphan_reply_subject` (1-mail "Re:" orphelins restent visibles dans la liste d'origine). 4 nouveaux tests TDD + 2 tests existants mis à jour. 36 tests inbox verts.
- **v1.30.0.11 (2026-07-02)** — Rollback worklist "Toutes" : on remet TOUS les mails visibles. CDAL a perdu patience avec le mode worklist (introduit v1.30.0.7, durci v1.30.0.8/.9) qui masquait la bande OTHER. Comportement attendu (avant v1.30.0.7) : "Toutes" = liste COMPLÈTE avec hot band verte (demande_client+urgent pending) en haut + other band grise (TOUT LE RESTE) en dessous. Aucun masquage. `app/web/app_routes.py` : suppression du paramètre `worklist: bool` dans `_fetch_mails()` et `app_index()` (rebranche `other_threads = []` supprimée). `app/web/api.py` : symétrique (`_fetch_mails_partial()` et `inbox_partial()`). `_group_into_threads()` : suppression de la 2e passe v1.30.0.9 (qui déplaçait les 1-mail threads avec `in_reply_to` orphelin ou sujet "Re:" dans `final_move`). Fix bug : le parent déjà traité (status=approved/rejected/sent/duplicate) était droppé silencieusement par le code (commentaire "n'est pas dans la liste d'origine" FAUX) — MAINTENANT on ajoute le parent lui-même en `final_move`. `app_index()` : suppression de la double-grouping sur `other_mails` (passe `other_mails` comme `all_thread_siblings` pour les cross-band moves, puis convertit `other_mails` en 1-mail threads pour le rendu). Garde-fous anti-bruit de la hot band (Pluxee, Reçu Apple, e-Box, @digitalhs.biz, @cvfconsult) conservés. 32 tests inbox verts.
- **v1.30.0.10 (2026-07-02)** — CSS inbox : colonnes 8 tiennent dans 1280px viewport (max-w-md Sujet → max-w-[240px], max-w-[140px] Expéditeur → max-w-[120px], w-32 Catégorie → w-28, w-24 Priorité → w-20, w-24 Date → w-20). Padding `px-3` → `px-2` sur la colonne Sujet. 8 colonnes visibles intégralement sur 1024px table area. Cosmétique pure.
- **v1.30.0.8 (2026-07-02)** — Anti-orphelin-reply + split bucket adhoc fourre-tout. Cause 1 (DB) : bucket `adhoc::unknown::50d8b4a9` regroupait 207 mails sans dossier en un seul fil → parent "Je hebt een nieuw belangrijk bericht" + 205 replies enfilés (Pluxee, Demande mission, changements propriétaire domaine, etc.). Backfill prod sur VPS : 205 rows re-threadées avec le hash v1.29.0.7 (sender|subject) → **120 fils distincts**. Cause 2 (algo) : un mail avec `in_reply_to` pointant vers un message_id absent du système (mail de Daniel hors-système) était promu parent à tort. `_fetch_mails()` ajoute `m.in_reply_to` + `m.message_id` à la projection SELECT. Nouvelle helper `_is_orphan_reply(mail, known_message_ids, same_thread_message_ids)`. `_group_into_threads()` : nouveau re-parenting — le parent = le plus ancien non-orphelin, ou le plus ancien avec `parent_is_orphan=True` si 100% orphelins. Champs thread ajoutés : `parent_is_orphan`, `all_orphans`. 4 tests TDD. Garde-fou permanent contre futures corruptions du bucket adhoc.
- **v1.30.0.7 (2026-07-02)** — Worklist mode : onglet "Toutes" = liste de travail de Daniel. `_fetch_mails()` + `_fetch_mails_partial()` : nouveau paramètre `worklist: bool = False`. En worklist : (1) exclut les doublons (`status != 'duplicate'`) du WHERE racine, (2) supprime la bande OTHER (retourne `(hot_mails, [])`). `worklist = (category is None and priority is None and status is None)` dans `/app/` et `/api/inbox`. Les onglets de catégorie explicite gardent le comportement 2 bandes (hot + other + move-to-other). Template `inbox.html` : compteur "Toutes" = `hot_threads|length` (au lieu de la somme des 2 bandes). 7 tests TDD. Cohérence `/app/` ↔ `/api/inbox`. Critère "parfait" CDAL : "Toutes" affiche ~3-23 lignes max (uniquement demande_client + urgent pending, sans doublons, sans bruit).
- **v1.30.0.5 (2026-07-02)** — Anti-reply-orphelin en hot band. `app/web/app_routes.py:_group_into_threads()` retourne maintenant `tuple[list[dict], list[dict]]` = `(keep, move_to_other)`. Logique : (1) `is_reply_in_other` = reply_count==0 + parent a un thread_id + sibling pending existe dans `all_thread_siblings` (cross-band) ; (2) `is_orphan_reply_subject` = reply_count==0 + sujet matche `^\s*(re|réponse|fwd|tr|fw|aw)\s*:` + parent pending. Helper `_looks_like_reply_subject()` + regex `_REPLY_SUBJECT_PREFIX`. Fix bug `reply_count` jamais incrémenté. Caller `app_index()` passe `all_thread_siblings=other_mails` et merge `hot_move` dans `other_threads`. **Cohérence `/api/inbox`** : `app/web/api.py:_fetch_mails_partial()` projette `has_draft`/`suggested_subject`/`thread_id` et utilise la nouvelle signature tuple. 8 tests TDD. **Vérification prod** : 5 threads NEW en hot (aucun Re:), 10 replies Re: déplacées en other. Section hot du test client = (730, 725, 672, 699, 692) — tous sans Re:.
- **v1.30.0.4 (2026-07-02)** — Garde-fou anti-bruit hot band. `app/web/app_routes.py:_fetch_mails()` + `app/web/api.py:_fetch_mails_partial()` : filtre anti-bruit dans `hot_where` exclut `@digitalhs.biz` (internes CDAL), `@cvfconsult.be` (comptable externe), sujets `pluxee`/`reçu apple`/`e-box`. `other_where = NOT(hot_where)` pour conserver les mails filtrés visibles en 2ème bande. 4 tests TDD. Une vraie demande client reste dans la hot.
- **v1.30.0.3 (2026-07-02)** — Hot étendu (`demande_client` + `urgent`, toutes priorités, pending).
- **v1.30.0.2 (2026-07-02)** — Tri prioritaire `demande_client + pending TOUJOURS en premier` (ORDER BY CASE WHEN 0..4).
- **v1.30.0.1 (2026-07-02)** — Middleware anti-cache navigateur forcé.
- **v1.30.0 (2026-07-02)** — Badge version sidebar défense contre troncature visuelle. `app/web/templates/base.html` (l. 46-58) : `whitespace-nowrap` ajouté sur le badge pour empêcher tout wrap, `v` (gris foncé `text-gray-600`) et chiffres (`text-gray-400 font-mono`) visuellement distincts. Cosmétique pure, bump majeur par convention rupture.
- **v1.29.1 (2026-07-02)** — Hourly check brouillon manquant + fix visuel replies cockpit. `app/workers/hourly_draft_check.py` (nouveau worker asyncio, tourne toutes les 60 min, 10 min après le boot) : query `category='demande_client' AND status='pending' AND IFNULL(ai_draft, '')='' AND draft_generated=0 AND processed_at >= now()-7j` (LIMIT 100), re-check idempotence pre-APPEND, `generate_draft()` max 3 retries (backoff 1s/2s/4s, timeout 60s/tentative), `append_draft()` header `X-Detective-Mail-Id` v1.25.22, UPDATE DB. 1 connexion IMAP par mailbox par cycle. Wiring dans `app/main.py` (task `hourly-draft-check`). `tests/test_hourly_draft_check.py` : 12 tests TDD. Fix visuel replies cockpit (`inbox_rows.html` l. 103-107) : flèche `↳` passe de `text-gray-500 text-[10px]` (invisible) à `text-purple-300 text-base font-bold` ; `border-l-4 border-l-purple-500/60` → `border-l-4 border-l-purple-400 bg-purple-500/10` (le "I" et le "_" parasites signalés par CDAL sur la 2ème ligne du tableau sont maintenant bien visibles). 5 régressions tests post-v1.29.0.7 corrigées (ajout colonnes threading v1.29.0 + `duplicate_of` v1.29.0.6 dans 2 fixtures de test web, mot-clé SQL `references` entouré de guillemets).
- **v1.29.0 (2026-07-01)** — Fil de discussion cockpit inbox (groupement parent + replies). `app/pipeline/threading.py` (extract_dossier_name regex "Dossier Dupont" + accents + composé, derive_dossier_id_threading hiérarchie name > ref > hash, compute_thread_id `f"{dossier_id}::{sender}"`, pick_thread_subject = sujet du plus ancien). DB : 6 nouvelles colonnes (message_id, in_reply_to, references, dossier_id, thread_id, thread_subject) + index `idx_mail_processed_thread`. Poller enrichi : capture headers IMAP, dérive thread_id AVANT dédup, `_refresh_thread_subject()` propage le sujet canonique. Cockpit : `_group_into_threads()` + macro `thread_row()` (parent + replies indentées, border-l-4 sur 1er `<td>`, badge `↳`, line-through sur doublons) + tabs `?view=threads|flat|duplicates`. **420 tests verts** (19 nouveaux threading + 0 régression). Fix inbox 740/748/746 (3 mails `Dossier Dupont` = 1 fil).
- **v1.28.3 (2026-07-01)** — Déduplication logique runtime (fix #719-#722 inbox polluée). `app/pipeline/dedup.py` (is_logical_duplicate, fenêtre 48h, normalize sujet + sender), poller enrichi avec 2 helpers (_persist_duplicate + injection avant quick_classify), backfill script dry-run/apply pour les 11 Apple doublons historiques.
- **v1.28.2 (2026-06-29)** — Garde-fou anti-brouillon-interne (#686). `is_internal_sender()` dans `prefilter.py` détecte un mail interne (domaine `digitalhs.biz` OU local-part `cdal`/`daniel`), whitelist d'exclusion pour préfixes techniques. `quick_classify()` retourne `"autre"` en première position, `generate_draft()` court-circuite avec `note="Sender interne — brouillon skipped"`. Backfill prod : #686 brouillon supprimé des Drafts IMAP (UID 38) + DB rollback ; #652/#582/#562/#474/#82 reclassifiés `autre` + `draft_generated=0`. 11 nouveaux tests.

Contexte technique stable : **Le RAG est mis en pause** (v1.24.2, `rag_enabled=False`) — ce n'est plus un bug à corriger (voir point de vigilance #1). **Bascule LLM v1.25.0** : `gemma4:31b` (non-reasoning) principal sur toutes les tâches ; `glm-5.2:cloud` (reasoning) fallback. **Brouillon qualifiant déterministe** couvre tous les cas (infidélité, recherche personne, incapacité, dette, succession, violences, micros, indéterminé + **mission datée**) avec questions structurées, refus poli hors-légalité (v1.24.1), wording avocat (v1.27.5), exclusion des éléments déjà reçus.

**Historique récent** : **v1.27.5** = brouillon avocat/conseil (#656 Jennifer Das) — `_is_legal_counsel_email()` détecte les pros du droit écrivant pour un client, salutation « Bonjour Maître, » + wording « votre client » + rappel au GSM de l'avocat uniquement. **v1.27.3** = 4ème boîte OVH `info@detectives-belgique.be` (`ex5.mail.ovh.net`, code `D_DS`, marque Cerveau2 `detectivesbelgique`, DB `boite4.sqlite`) — architecture IMAP host par boîte. **v1.25.22 → v1.26.0** = réconcilieur Drafts IMAP + expéditeur forwarder masqué Reply-To + sujet de brouillon lisible partout (`suggested_subject`).

**Chantiers ouverts restants** : reclassement #614 (validation brouillon refus poli avec CDAL — **PAS finalisé**), re-classement #643 en prod (brouillon succession propre — à valider avec CDAL), Task #4 (vrai contact client formulaires WP via Reply-To), V2b (polishing cockpit), V2c (feedback loop qualité Daniel). Pour le reste, voir HANDOVER §12 (checklist reprise) et §13 (4 niveaux anti-crash silencieux opérationnels).

**Philosophie CDAL** : MVP simple d'abord, V2 quand qualité prouvée. Pas d'over-engineering. ROI client : "solde 24/7" sans surdimensionner. Communique court en français, écrit parfois avec des fautes de frappe rapides — décoder l'intention.

---

*Document mis à jour le 2026-07-01 pour la v1.29.0 de Detective.be Agent IA.*

