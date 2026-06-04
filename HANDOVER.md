# HANDOVER — Detective.be Agent IA (Charlie)

> Document de transfert pour Claude Opus 4.7 ou tout agent ultérieur.  
> Dernière mise à jour : **2026-06-04** · Version courante : **V1.21.5** · Déployé sur : `detective.digitalhs.biz`

---

## 1. Qui, quoi, pourquoi

| | |
|---|---|
| **Client** | Daniel Hurchon — détective privé belge, cabinet **Detective.be** |
| **Intégrateur & ops** | CDAL (`cdal@digitalhs.biz`) — c'est l'utilisateur que tu assistes |
| **Produit** | Agent IA Python qui poll 3 boîtes mail Infomaniak, classifie, et génère des brouillons de réponse "à la Daniel" |
| **Canal Boss** | Bot Slack direct Daniel ↔ Charlie (notifications, résumés, validations) |
| **Second cerveau** | **Cerveau2-Det** — vault Markdown + API FastAPI sémantique (sqlite-vec + E5-large) |
| **Cockpit web** | `detective.digitalhs.biz` — inbox, conversation, chat AI Charlie, dashboard admin |
| **Urgence** | Fiabilité des réponses Charlie est critique — les bugs "pas trouvé" malgré données existantes sont tolérance zéro |

---

## 2. Architecture actuelle (V1.21.5)

```
[3 boîtes Infomaniak IMAP] ──polling 5min──► [Worker asyncio Python]
                                                    │
                    ┌───────────────────────────────┼───────────────────────────────┐
                    ▼                               ▼                               ▼
        [Pipeline IMAP]                    [Cockpit web FastAPI]              [Cerveau2 API]
          prefilter ──► classifier          /inbox, /conversation, /admin      /query, /ingest
          priority ──► generator            /api/charlie/ask                 vault Markdown
          translator (NL/EN/DE/ES...)      /api/drafts/{id}/retry            sqlite-vec
          renderer (4 blocs multilingues)  /api/charlie/feedback
          delivery (IMAP Drafts / Resend)
                                                    │
                                            [agent_state.db]
                                            mail_processed
                                            charlie_memory
                                            email_attachment
```

### Fichiers clés et rôles

| Fichier | Rôle critique | À savoir |
|---|---|---|
| `app/_version.py` | **Source unique de vérité** version | `VERSION = "1.21.2"`. Tolérance zéro sur la désynchronisation. |
| `app/charlie.py` | **Cœur intelligent Charlie AI** | Pipeline `ask_charlie()` : extraction entités → SQL programmatique (bypass LLM pour comptages + statuts) + vault Cerveau2 (fallback direct GET pour entités non indexées) + archives + corrections + mémoire → nuage de liaison familial → **résumé de dossier narratif LLM** (v1.19.1) → garde anti-vide + garde anti-"pas trouvé" |
| `app/charlie_memory.py` | **Mémoire persistante** | Table `charlie_memory` (feedback good/bad, corrections, auto-save). |
| `app/cerveau_client.py` | **Client HTTP Cerveau2** | `query_vault()`, `get_vault_note()` (fallback direct par chemin), `feed_correspondance()`, `feed_document()`. Bearer Token statique. |
| `app/config.py` | **Configuration pydantic-settings** | `llm_model_chat = "openai/kimi-k2.6:cloud"` (Ollama Pro, cloud). Provider `openai/` + `api_base=https://ollama.com/v1`. **v1.21.1** : bascule depuis `gemma4:31b` (obsolète) + correction du nom du modèle. |
| `app/llm/router.py` | **Wrapper LiteLLM** | `complete()` avec fallback automatique vers `llm_model_fallback` + extraction `reasoning_content` (kimi-k2.6 reasoning) + post-traitement `_clean_reasoning()` (30+ patterns pour filtrer les traces de raisonnement). |
| `app/pipeline/translator.py` | **Aide lecture multilingue (v1.21.0)** | `translate_to_fr()` + `translate_from_fr()` avec garde-fous try/except, troncature 12K. Utilisé si langue mail ≠ FR. |
| `app/pipeline/draft_renderer.py` | **Rendu brouillon enrichi (v1.21.0)** | Compose 4 blocs : email d'origine + traduction FR + proposition FR + traduction langue source. |
| `app/pipeline/generator.py` | **Génération brouillon** | Appelle `translate_to_fr` + `translate_from_fr` en parallèle (`asyncio.gather`) si langue ≠ FR, puis `render_draft_with_translations`. Retourne `GenerationResult(draft, raw_draft)`. |
| `app/pipeline/language.py` | **Détection langue** | `Language = str` (toutes BCP-47), `language_label()` pour affichage humain. |
| `app/web/api.py` | **Endpoints HTMX + Charlie** | `charlie_ask()`, `charlie_feedback()`, `draft_generate()` (utilise body complet + force=True), **NOUVEAU** `POST /api/drafts/{id}/retry` (force la régénération). |
| `app/workers/imap_poller.py` | **Polling IMAP** | 1 task asyncio par boîte, flag `AgentProcessed` (sans `$` — Infomaniak rejette `$`). Appelle `generate_draft()` pour `demande_client` → brouillon enrichi. |
| `scripts/deploy-to-vps.sh` | **Déploiement one-shot** | Pre-flight checks, sync data (exclut `agent_state.db`), build, healthcheck. |

---

## 3. Le pipeline Charlie AI (état V1.16.13)

Le fichier `app/charlie.py` contient `ask_charlie()`. Flow exact :

### Phase 1 — Questions générales (bypass)
- `_general_response()` répond en dur à "salut", "version", "merci", "au revoir", "qui es-tu".
- **Aucun appel LLM** — latence nulle, coût nul.

### Phase 2 — Extraction entités
- `_extract_dossier_id()` : regex pour détecter un dossier (ex: ADF, #DPDH).
- `_extract_year()` : regex `20\d{2}`.
- `_enrichir_question()` : ajoute des synonymes métier si le type d'enquête est détecté.
- `_extract_date_filter()` : parse "depuis le 20 mai", "en mai 2026", etc. en SQL `processed_at`.

### Phase 3 — Génération SQL (bypass programmatique + LLM fallback)
**Bypass programmatique** (pas d'appel LLM, 100% déterministe) :
- `_build_count_sql()` : comptages d'emails (combien, nombre, total).
- `_build_status_sql()` : listes de statut (pending, urgent, demandes clients en attente) — fuzzy matching sur les mots-clés (tolère "deamdnes" → "demand").

**LLM fallback** : si le bypass ne match pas, le LLM génère `SQL: <SELECT>` via `CHARLIE_SYSTEM_PROMPT`.
- `parse_charlie_response()` extrait SQL + réponse.
- `is_safe_sql()` vérifie SELECT uniquement.

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

**Bases historiques** : `data/boite1.sqlite`, `boite2.sqlite`, `boite3.sqlite` (emails avant cutoff 2026-05-15).  
**Base courante** : `data/agent_state.db` → table `mail_processed` (emails post-cutoff).

### Phase 5 — Nuage de liaison (V1.16.12+)
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
- **Identités** : `_extract_identity_answer()` parse frontmatter YAML pour suivre les wikilinks relationnels (ex: `epouse: "[[sarah-dalla-valle]]"` → récupère prénom/nom de la fiche liée).
- **Dossier par ville** : `_extract_dossier_par_ville()`.
- **Entreprise (siège/adresse)** : `_extract_entreprise_info()`.

#### Résumé de dossier (v1.19.1)
- Détecté par `is_dossier_summary` (keywords : "résume", "synthèse", "infos", "détails" + `dossier_id` extrait).
- Bypass dédié : assemble les **contenus complets** des emails (body, pas preview) et appelle le LLM avec un prompt ultra-ciblé : "UN SEUL PARAGRAPHE FLUIDE ET NARRATIF".
- Le LLM doit raconter l'histoire du dossier (client, demande, dates, montants financiers).
- `hide_rows=True` dans `CharlieResult` → le template web **ne montre pas** le tableau SQL brut sous le résumé.
- 2 tentatives avec le modèle chat, fallback sur `llm_model_fallback`.

#### LLM final (questions spécifiques)
- `complete(model=settings.llm_model_chat, ...)` — gemma4:31b via Ollama Pro Cloud.

#### Garde-fous de secours (V1.16.13 — critique)
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

## 4. Stack technique détaillée

| Couche | Outil | Version / Détail |
|---|---|---|
| Python | 3.11+ | VPS = 3.11, Mac CDAL = 3.14 |
| Concurrence | `asyncio` | Tout est `async def` |
| IMAP | `aioimaplib` | 2.0.1 |
| LLM router | **LiteLLM** | 1.85.0 + post-traitement `_clean_reasoning()` (filtre traces raisonnement) |
| LLM chat + pipeline (Charlie AI) | **kimi-k2.6:cloud** via Ollama Pro Cloud | `openai/kimi-k2.6:cloud` (**v1.21.1** : reasoning model, extraction `reasoning_content`) |
| LLM fallback | **glm-5.1:cloud** via Ollama Pro Cloud | `openai/glm-5.1:cloud` |
| Embeddings | `text-embedding-3-small` via OpenRouter | API stateless, image Docker ~800MB au lieu de ~4GB avec sentence-transformers local |
| Vector store | `sqlite-vec` | 0.1.9, vit dans les DB existantes |
| Détection langue | `langdetect` | v1.21.0+ : `Language = str` (toutes BCP-47) |
| Aide lecture multilingue | `app/pipeline/translator.py` + `draft_renderer.py` | v1.21.0 : 4 blocs pour mails NL/EN/DE/ES/etc. |
| Email outbound principal | **IMAP Drafts** (V2a) | Brouillon dans `Drafts` de la boîte source, flag `\Draft` |
| Email outbound fallback | **Resend API** | `agent@digitalhs.biz`, alertes système |
| Web framework | **FastAPI** | 0.136.1 |
| Templating | **Jinja2** + HTMX | Pas de React |
| CSS | **Tailwind CSS** | CDN |
| Logs | `structlog` | JSON structuré, rotation 3j |
| Config | `pydantic-settings` | `.env` |
| Serveur | **uvicorn** | 0.47.0 |
| Reverse proxy | **Traefik** | Docker network `root_default` |

---

## 5. Cerveau2-Det — Le second cerveau

### Qu'est-ce que c'est
Cerveau2-Det est un **vault Markdown** structuré + une **API FastAPI** qui expose recherche sémantique, ingestion et anonymisation. Il vit sur le même VPS (`cerveau2-det.digitalhs.biz`) ou un sous-domaine séparé.

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
**Contournement** (V1.16.12) : `_vault_task()` dans `app/charlie.py` fait un `GET /notes/{path}` direct pour les slugs d'entités connus (christophe-dalla-valle, sarah-dalla-valle, daniel-hurchon, digitalhs-llc).

---

## 6. Déploiement production

### VPS
- **Host** : `root@69.62.110.165`
- **Répertoire** : `/opt/DETECTIVE`
- **Container** : `detective-agent` (service Docker Compose `detective`)
- **DNS** : `detective.digitalhs.biz` → A record `69.62.110.165`
- **Reverse proxy** : Traefik (network Docker `root_default` externe)
- **SSL** : Let's Encrypt via Traefik (`mytlschallenge`)

### Déployer depuis le Mac de CDAL
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

### Déploiement rapide (hotfix sans script)
```bash
# Depuis le Mac
cd /Users/cdal/DEV_APP_CLAUDE/DETECTIVE_BE
scp app/charlie.py app/_version.py root@69.62.110.165:/opt/DETECTIVE/app/
ssh root@69.62.110.165 "cd /opt/DETECTIVE && docker compose restart detective"
```

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
      - ./app:/app/app:ro          # dev mount
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

---

## 7. Données et bases SQLite

### `data/agent_state.db` (base courante — NE PAS ÉCRASER EN DEPLOY)
| Table | Rôle |
|---|---|
| `mail_processed` | Emails traités par le pipeline (post-cutoff 2026-05-15) |
| `charlie_memory` | Mémoire Charlie (feedback good/bad, corrections, faits auto-sauvés) |
| `email_attachment` | Pièces jointes détectées |
| `users` | Utilisateurs cockpit (auth magic link) |
| `audit_log` | Traçabilité actions cockpit |

### `data/boite1.sqlite`, `boite2.sqlite`, `boite3.sqlite` (archives historiques)
- Contiennent les emails **avant** le cutoff.
- **Ne pas modifier** sans confirmation de CDAL.
- Charlie les interroge via `_search_historical_by_keyword()` et `_search_historical_all()`.

### Cutoff date
`process_since_date = "2026-05-15"` dans `.env`.  
Le poller IMAP ne traite que les mails reçus depuis cette date. Les archives historiques contiennent tout l'historique.

---

## 8. Règles critiques (à respecter impérativement)

### Règle 1 — Tolérance zéro version
- Source unique : `app/_version.py`.
- **Jamais** `importlib.metadata`.
- À chaque release (nouveauté, bugfix, correction) → bump `app/_version.py` + mettre à jour `CHANGELOG.md`.
- La version affichée dans le cockpit est lue dynamiquement depuis `app/_version.py`.

### Règle 2 — Ne jamais écrire dans les vraies boîtes Infomaniak en dev
- Mode `--dry-run` disponible.
- En dev, utiliser un compte mail de test si besoin.
- La première vraie connexion en prod est surveillée par CDAL.

### Règle 3 — Ne jamais envoyer de mail réel via Resend en test
- Si `RESEND_API_KEY` est vide, le module skip avec un warning.
- En test automatisé, mocker ou laisser la clé vide.

### Règle 4 — Flag IMAP = `AgentProcessed` (sans `$`)
- Infomaniak rejette les flags avec préfixe `$`.  
- Le code utilise `AgentProcessed` (ligne confirmée dans `imap_poller.py`).

### Règle 5 — Multilingue obligatoire
- La réponse générée DOIT être dans la langue détectée du mail entrant (FR/NL/EN).
- Tester systématiquement les 3 langues.

### Règle 6 — Pas de Docker au MVP
- L'architecture est volontairement légère : Python natif + SQLite + Docker uniquement en prod.
- Ne pas introduire Docker Compose en dev sans discussion.

### Règle 7 — Ne jamais logger le contenu intégral d'un mail
- Métadonnées uniquement (message-id, expéditeur, sujet, classification).
- Pour debug, ajouter un flag explicite `LOG_MAIL_BODY=true`.

---

## 9. Bugs connus et points de vigilance (2026-06-04, V1.21.5)

| # | Problème | Statut | Fichier concerné | Notes |
|---|---|---|---|---|
| 1 | **Questions identitaires Cerveau2** (ex: "qui est l'épouse de CDAL") retournent "pas trouvé" | ✅ Corrigé V1.16.12 | `app/charlie.py` | Fallback direct `GET /notes/{path}` pour les fiches `04_entities/personnes/*.md` non indexées dans sqlite-vec. Nuage de liaison familial (`epouse`, `mari`, etc.) dans `_resolve_links()`. |
| 2 | **Boutons feedback Charlie** nécessitent plusieurs clics | ✅ Corrigé V1.16.11 | `app/web/api.py` | `hx-disabled-elt="find button[type=submit]"` + `hx-target="this" hx-swap="outerHTML"`. |
| 3 | **"Demandes clients en attente"** retourne "pas trouvé" malgré des pending en base | ✅ Corrigé V1.16.13 | `app/charlie.py` | `_build_status_sql()` génère `SELECT ... WHERE category='demande_client' AND status='pending'` automatiquement. Fuzzy matching sur "deamdnes" → "demand". Garde-fous secours reconstruit la réponse depuis les rows SQL si le LLM dit "pas trouvé". |
| 4 | **LLM retourne vide** sur comptages ADF | ✅ Corrigé V1.14.2 | `app/charlie.py` | Garde anti-vide + bypass programmatique comptage. |
| 5 | **Réponses list montrent des stats** au lieu de noms de dossiers | ✅ Corrigé V1.14.2 | `app/charlie.py` | Bypass Python pour list supprimé, contexte 50 emails. |
| 6 | **Count ADF = 0** car SQL cherchait `subject LIKE '%ADF%'` mais emails ADF viennent de `@groupeadf.com` | ✅ Corrigé V1.14.1 | `CHARLIE_SYSTEM_PROMPT` | Mode B recherche aussi dans `sender`. |
| 7 | **Corrections écrasaient les questions analytiques** | ✅ Corrigé V1.14.0 | `_summarize_results()` | Bypass correction ne s'applique que si `_is_identity_query()`. |
| 8 | **Mauvais mot-clé dans `_build_keyword_sql`** — verbes d'action ("retrouve", "donne") choisis à la place de noms concrets ("hotel", "facture") → SQL non pertinent, tableau affiché sous réponse vault | ✅ Corrigé V1.19.1 | `app/charlie.py` | Nouvelle fonction `_extract_keywords()` avec scoring sémantique : bonus +15 noms concrets, pénalité −15 verbes d'action. Dédoublonnage `_build_keyword_sql` + `_archive_task`. Masquage tableau quand réponse principale vient du vault (`hide_rows=True`). |
| 9 | **Recherche numérique non fonctionnelle** — `is_safe_sql()` rejetait les SQL avec `replace(...)` (normalisation des numéros), le tri `received_at DESC` était lexicographique (pas chronologique), et le `OR` avec "téléphone" polluait les résultats | ✅ Corrigé V1.20.6 | `app/charlie.py` | `is_safe_sql` ignore `replace(` avant le check de mots dangereux. `ORDER BY id DESC` remplace `received_at DESC`. Si keyword numérique, le WHERE ne garde que ce numéro. |
| 10 | **Doublons dans les probants** — un même email existait dans `mail_processed` et `boite2.sqlite` (sender anonymisé différemment) | ✅ Corrigé V1.20.8 | `app/charlie.py` | Déduplication par `(subject.lower(), received_at)` sans `sender`. |
| 11 | **Faux négatif Cerveau2** — le LLM de synthèse disait "pas trouvé" alors que le numéro était dans le `context` retourné | ✅ Corrigé V1.20.8 | `app/charlie.py` | Détection : si `_bad_vault` match mais que le numéro recherché est dans `vault_answer` → considérer que l'info est là. Court-circuit de la réponse contradictoire. |
| 12 | **Mail #430 (Beheydt) classifié `demande_client` mais sans `ai_draft`** | ✅ Corrigé V1.21.0 | `app/workers/imap_poller.py` | Cas deadlock poller — l'endpoint `POST /api/drafts/{id}/retry` permet de régénérer manuellement. `draft_generate` utilise désormais `body` complet (au lieu de `body_preview` 2K). |
| 13 | **Demande de Daniel : aide lecture mails non-FR** — Daniel a des difficultés avec NL/EN/DE/ES | ✅ Corrigé V1.21.0 | `app/pipeline/translator.py` + `draft_renderer.py` | Brouillon enrichi avec 4 blocs : email d'origine + traduction FR + proposition FR + traduction langue source. `Language = str` (toutes BCP-47). |
| 14 | **kimi-k2 inexistant sur Ollama Cloud** — `openai/kimi-k2` retournait 404 | ✅ Corrigé V1.21.1 | `app/config.py` + `.env.production` | Vrai nom = `kimi-k2.6:cloud`. Idem pour `glm-5.1` → `glm-5.1:cloud`. `ollama_pro_base_url` corrigé de `/api` vers `/v1`. Table `app_settings` prod purgée des 3 entrées obsolètes. |
| 15 | **kimi-k2.6:cloud est un reasoning model** — réponse dans `reasoning_content` pas dans `content` → fallback systématique vers glm-5.1 | ✅ Corrigé V1.21.1 | `app/llm/router.py` | Extraction : si `content` vide, fallback sur `reasoning_content`. |
| 16 | **Traces de raisonnement kimi-k2.6 polluent les brouillons** — "L'utilisateur demande...", "The user wants...", "Refonte :", "Version plus X :", "C'est mieux.", etc. | ✅ Corrigé V1.21.2 | `app/llm/router.py` | 30+ patterns regex dans `_clean_reasoning()` filtrent les artefacts. EN + FR + listes + guillemets + auto-critique post-mail. |
| 17 | **Poller IMAP crash en boucle sur la boîte `detective_belgique`** depuis ~26h (3 bugs cumulés) — 0 brouillon généré, 13 retries sur certains UIDs | ✅ Corrigé V1.21.3 | `app/workers/imap_poller.py` + `app/alerts.py` + `app/healthcheck.py` | Bug 1 : `_decode_header` crash sur charset `unknown-8bit` (LookupError). Bug 2 : `_persist` crash sur `Header` objects (sqlite3.ProgrammingError). Bug 3 : retry éternel (flag `AgentProcessed` posé qu'en cas de succès → crash = rejoué toutes les 5 min indéfiniment). Fix : 5 patches + try/except englobant + nouveau flag `AgentAttempted` (libère la queue après crash). 19 tests de résilience. **Visibilité** : compteur `consecutive_errors` + alerte Resend à `cdal@digitalhs.biz` si ≥5 crashes/boîte (anti-spam 1h/boîte). |
| 18 | **Filtre date hardcodé incohérent** — code dit `datetime(2026, 5, 20)`, `.env.example` dit `PROCESS_SINCE_DATE=2026-05-01`, Daniel veut 1er juin strict | ✅ Corrigé V1.21.4 | `app/workers/imap_poller.py` + `.env.example` | Date passée à `datetime(2026, 6, 1)`. Log `poller.date_skipped` reason=`before_2026-06-01`. `.env.example` aligné à `2026-06-01`. |

### Point de vigilance #1 — Provider litellm pour Ollama Cloud (CRITIQUE v1.21.1)
`ollama_chat/<model>` force litellm vers `localhost:11434` (Ollama **local**). Le provider correct pour Ollama **Cloud** est `openai/<model>` avec `api_base=https://ollama.com/v1`.
**Vrai nom des modèles** (v1.21.1+) : `openai/kimi-k2.6:cloud` (principal + classifier + chat), `openai/glm-5.1:cloud` (fallback). **JAMAIS** `kimi-k2` (404), `gemma4:31b` (obsolète), `claude-sonnet-4` (404 OpenRouter).
**Si un nouveau modèle ne répond pas** → vérifier immédiatement le provider (openai/ vs ollama_chat/), l'URL api_base (`/v1` pas `/api`), et que le nom de modèle existe sur ollama.com/library.

### Point de vigilance #2 — kimi-k2.6:cloud est un reasoning model
Sa réponse finale est dans `message.reasoning_content`, pas dans `message.content` (vide). Le wrapper `complete()` extrait automatiquement, MAIS il faut :
- Soit utiliser un autre modèle non-reasoning si on veut du contenu direct
- Soit accepter le coût (raisonnement = plus de tokens) + le post-traitement `_clean_reasoning()`

### Point de vigilance #3 — Traces de raisonnement kimi-k2.6 (CRITIQUE v1.21.2)
Le modèle produit des métadiscours parasites : "L'utilisateur demande...", "Let me analyze...", "Points importants :", "Refonte :", "Version plus X :", "C'est mieux.", etc. Le post-traitement `_clean_reasoning()` dans `app/llm/router.py` filtre ~30 patterns, **MAIS** si un nouveau type d'artefact apparaît (autre modèle, autre langue, etc.), il faut **enrichir `_REASONING_LINE_PATTERNS`** dans ce fichier. Le cleaning n'est jamais "complet" — c'est une bataille continue.

### Point de vigilance #4 — mail_processed ne contient que les emails post-cutoff
La base courante `agent_state.db/mail_processed` ne contient que les emails post-cutoff (2026-05-15). Les vraies données sont dans `boite1.sqlite`.  
**Conséquence** : pour les questions sur 2026, les archives historiques sont la source principale. Le SQL local retourne souvent 0.

### Point de vigilance #5 — Cerveau2 peut être down
Le client `query_vault()` est dégradation silencieuse. Si Cerveau2 est indisponible, Charlie répond avec SQL + mémoire seuls. Vérifier les logs `cerveau.query_failed`.

### Point de vigilance #6 — Entités Cerveau2 non indexées dans sqlite-vec
Les fiches `04_entities/personnes/*.md` créées manuellement ne sont pas dans l'index sémantique. Le fallback direct `GET /notes/{path}` contourne ce problème, mais la **vraie solution** serait de réindexer le vault Cerveau2. Toutes les tentatives sur le VPS ont échoué (problèmes volume mount, extension sqlite3 vec0 manquante).

### Point de vigilance #7 — Dense search Cerveau2 = implicit AND (CRITIQUE v1.20.10)
Quand on envoie une phrase complète à Cerveau2 (`"retrouve le dossier avec téléphone 0488/411192"`), le dense retrieval calcule un vecteur moyen de TOUS les concepts. Les documents qui ne contiennent pas tous les mots ont un score faible. **Solution** : pour les recherches factuelles, n'envoyer que les identifiants précis (`"0488411192"`) — voir `docs/CERVEAU2_RECHERCHE_FACTUELLE.md`.

### Point de vigilance #8 — Faux négatifs du LLM de synthèse Cerveau2
Le LLM de synthèse Cerveau2 peut écrire "je ne trouve pas" alors que le document est dans le `context`. C'est un biais du modèle, pas un bug du retrieval. **Solution** : vérifier si le numéro/nom recherché est présent dans `vault_answer` (la chaîne brute retournée par Cerveau2) malgré les patterns négatifs — voir `_bad_vault` et la logique de faux négatif dans `app/charlie.py`.

### Point de vigilance #9 — `pairs_vec` table missing sur `boite2.sqlite` (NOUVEAU v1.21.2)
Le RAG retrieval échoue (rag=0) sur la boîte `detective_belgium` car la table `pairs_vec` n'existe pas dans `boite2.sqlite`. Conséquence : mails de cette boîte → brouillon moins bon (pas de cas historiques similaires). À investiguer hors-scope. Cause probable : ancien `boite2.sqlite` créé avant l'activation de sqlite-vec, jamais réindexé.

### Point de vigilance #10 — Tables `mail_processed` `app_settings` peuvent être stale
Si tu modifies `app/config.py` (défauts `llm_model_*`), il faut aussi **purger la table `app_settings` en prod** (clé `llm_model_default`, `llm_model_classifier`, `llm_model_fallback`) — sinon les valeurs runtime en DB écrasent tes défauts. Procédure :
```python
import sqlite3
conn = sqlite3.connect("/app/data/agent_state.db")
conn.execute("DELETE FROM app_settings WHERE key LIKE 'llm_model%'")
conn.commit()
```

### Point de vigilance #11 — Périmètre Cerveau2 (NOUVEAU v1.21.3)
**Important** : le hotfix v1.21.3 (poller IMAP) et le fix v1.21.4 (filtre date) sont **100% côté Charlie** (instance Detective.be). Aucun changement n'a été fait dans :
- `app/cerveau_client.py` (wrapper HTTP Charlie → Cerveau2)
- `app/cerveau_feed.py` (wrapper d'ingestion)
- Le serveur `CERVEAU2-DEtective` (`/Users/cdal/DEV_APP_CLAUDE/CERVEAU2-DEtective/`, v0.8.2)
- Le produit `SECONDCERVEAU-PRO` (`/Users/cdal/DEV_APP_CLAUDE/SECONDCERVEAU-PRO/`)
- L'instance `CDAL2` (`/Users/cdal/DEV_APP_CLAUDE/CDAL2/`)

Le serveur Cerveau2 n'est pas affecté par ces fixes. Si tu réutilises Charlie comme base pour un nouveau client (via `SECONDCERVEAU-PRO`), les patches `_decode_header` / `_persist` / try-except poller / compteur erreurs / alerte Resend sont **réutilisables** — voir `docs/PATTERNS_FROM_CHARLIE_V1.21.3.md` pour le détail d'implémentation.

---

## 10. Procédures d'urgence

### Redémarrage container
```bash
ssh root@69.62.110.165
cd /opt/DETECTIVE
docker compose down
docker compose up -d --build
docker compose logs -f --tail 20
```

### Hotfix rapide (sans rebuild complet)
```bash
scp app/charlie.py app/_version.py root@69.62.110.165:/opt/DETECTIVE/app/
ssh root@69.62.110.165 "cd /opt/DETECTIVE && docker compose restart detective"
```

### Rollback rapide
```bash
cd /opt/DETECTIVE
git log --oneline -5
git reset --hard <COMMIT_PRÉCÉDENT>
docker compose up -d --build
```

### Vérifier l'état
```bash
# Health
curl -s -o /dev/null -w "%{http_code}" https://detective.digitalhs.biz/health
curl -s -o /dev/null -w "%{http_code}" https://detective.digitalhs.biz/auth/login

# Logs container
ssh root@69.62.110.165 "cd /opt/DETECTIVE && docker compose logs --tail 50"

# Version
ssh root@69.62.110.165 "cd /opt/DETECTIVE && docker compose exec detective python -c 'from app._version import VERSION; print(VERSION)'"
```

---

## 11. Contacts et ressources

| Ressource | Où trouver |
|---|---|
| Spec technique | `docs/SPEC.md` |
| Roadmap | `docs/ROADMAP.md` |
| Contexte business | `docs/CONTEXT.md` |
| Guide Cerveau2 | `docs/CERVEAU2_INTEGRATION.md` |
| API Cerveau2 | `docs/CERVEAU2_API.md` |
| Runbook incidents | `docs/RUNBOOK.md` |
| Checklist démo | `docs/DEMO_CHECKLIST.md` |
| Changelog | `CHANGELOG.md` |
| Instructions Claude Code | `CLAUDE.md` |
| Intégrateur | CDAL — `cdal@digitalhs.biz` |
| Client | Daniel Hurchon — Detective.be |

---

## 12. Pour le prochain agent (checklist reprise)

Avant de modifier quoi que ce soit :
- [ ] Lire `CLAUDE.md` (conventions, garde-fous, stack)
- [ ] Lire ce `HANDOVER.md` (contexte actuel, état des bugs)
- [ ] Vérifier `app/_version.py` — est-ce la bonne version ?
- [ ] Vérifier `CHANGELOG.md` — la dernière version est-elle documentée ?
- [ ] Lire les 100 dernières lignes de `app/charlie.py` pour comprendre le pipeline actuel (bypass SQL, garde-fous, nuage de liaison)
- [ ] Lire `docs/ROADMAP.md` pour savoir quelle phase est en cours
- [ ] Si une décision n'est pas dans la spec → demander à CDAL

---

## 13. 🔴 TODO DEMAIN (2026-06-05) — Sécurité anti-crash silencieux

**Contexte** : le 2026-06-04, CDAL a explicitement demandé « plus jamais de crash sans être prévenu : c'est impossible et interdit ». La v1.21.5 a ajouté Slack + heartbeat startup/shutdown (niveaux 1-2), mais il manque les niveaux 3-4 (watchdog externe + uptime checker). CDAL veut **ZÉRO crash silencieux** → finir ces items avant de passer à autre chose.

### Priorité 1 — Watchdog externe (cron VPS)

**Pourquoi** : si Charlie crash COMPLÈTEMENT (OOM, kill -9, deadlock asyncio), aucune alerte in-app ne s'exécute. Il faut un script externe au processus qui ping `/health` et alerte Resend si down.

**Comment** :
- Créer `/usr/local/bin/detective-healthcheck.sh` sur le VPS (analogue à `/usr/local/bin/magicreator-docker-clean.sh` qui existe déjà)
- Cron toutes les 60s : `* * * * * /usr/local/bin/detective-healthcheck.sh`
- Logique : si 3 checks consécutifs échouent → curl POST sur Resend avec `RESEND_API_KEY` (récupéré depuis `/opt/DETECTIVE/.env.production`) vers `cdal@digitalhs.biz`
- Endpoint à pinger : `http://127.0.0.1:8765/health` (déjà exposé par Charlie)
- ⚠️ **Important** : la commande s'exécute depuis l'host, pas dans le conteneur. Le port 8765 est exposé via Docker sur 127.0.0.1 du host (déjà mappé, à vérifier avec `docker port detective-agent`).

**État** : 0% — script à créer, cron à ajouter, Resend API à sourcer.

### Priorité 2 — Uptime checker externe (Healthchecks.io)

**Pourquoi** : le cron ci-dessus tourne SUR le VPS. Si le VPS lui-même crash, le cron ne s'exécute plus. Il faut un service externe qui ping Charlie depuis l'extérieur.

**Comment** :
- S'inscrire sur https://healthchecks.io (gratuit, 5 min de setup)
- Créer un check "Charlie" avec interval=2min, grace=3min
- Ajouter un ping automatique depuis Charlie : dans `app/main.py` après `notify_startup`, faire un `httpx.get(settings.healthchecks_ping_url)` best-effort
- Configurer Healthchecks pour alerter par email si ping manquant
- Optionnel : exposer `/healthz` via Traefik (déjà en place pour le cockpit, juste ajouter une route)

**État** : 0% — service à créer, intégration Charlie à coder.

### Priorité 3 — Cleanup auto disk + images Docker

**Pourquoi** : le VPS est à 60% disque (78GB libres). Si on accumule des images Docker / logs / vieux attachments, on risque de remplir le disque dans 2-3 mois, ce qui ferait crasher Charlie (sqlite + logs).

**Comment** :
- Créer `/usr/local/bin/detective-docker-clean.sh` sur le VPS (analogue à `magicreator-docker-clean.sh`) :
  - `docker image prune -af --filter "until=720h"` (supprime images > 30j)
  - `docker system prune -f` (volumes orphelins)
  - `find /opt/DETECTIVE/logs -name "*.log.*" -mtime +7 -delete` (rotation logs)
  - `find /opt/DETECTIVE/data/attachments -mtime +30 -delete` (PJ > 30j, mais vérifier qu'il n'y a pas de DB refs)
- Cron hebdo dimanche 4h : `0 4 * * 0 /usr/local/bin/detective-docker-clean.sh >> /var/log/detective-docker-clean.log 2>&1`
- ⚠️ **Sauvegarde avant** : `rsync` des 3 DB sqlite + `data/attachments/` vers backup externe avant prune.

**État** : 0% — script à créer, cron à ajouter, stratégie backup à définir.

### Priorité 4 — Mémoire projet

**Pourquoi** : CDAL a passé 14h+ sur ce projet le 2026-06-04 et demande que la fatigue ne coûte pas le contexte.

**Comment** :
- Écrire mémoire `feedback_fatigue_longue_sessione.md` : "CDAL fatigué après 14h+ → pause, ne pas démarrer nouveau chantier, finir ce qui est en cours"
- Écrire mémoire `feedback_zéro_crash_silencieux.md` : règle absolue = "toute absence d'alerte = bug à corriger en priorité P0"

**État** : 0% — fichiers à écrire dans `~/.claude/projects/.../memory/`.

### Fichiers à toucher demain

- `/opt/DETECTIVE/scripts/` (nouveau) → `detective-healthcheck.sh`, `detective-docker-clean.sh`
- `/etc/cron.d/detective` (nouveau) → entrée cron
- `app/main.py` → ajouter ping Healthchecks.io après notify_startup
- `app/config.py` → ajouter `healthchecks_ping_url: str = ""`
- `~/.claude/projects/.../memory/feedback_*.md` (nouveau) → 2 mémoires CDAL

### Note pour le prochain agent

Si tu reprends demain : lis cette section 13 EN PREMIER. Ne commence PAS de nouveau chantier tant que les 4 priorités ci-dessus ne sont pas résolues. CDAL veut un Charlie "production-grade 24/7" — les niveaux 1-2 actuels (Slack + Resend) ne suffisent pas pour un service critique.

---

*Document généré le 2026-06-02 pour la V1.20.10 de Detective.be Agent IA.*
