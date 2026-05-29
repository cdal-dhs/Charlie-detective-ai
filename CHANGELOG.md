# Changelog Charlie AI — Detective.be

## [1.18.6] — 2026-05-29 (hotfix Cerveau2 ingestion)

### Fixé
- **Cerveau2 — mapping priorité** : `high` → `urgent`, `low` → `faible`. Cerveau2 rejetait tous les emails avec priorité `high` ou `low` avec une erreur 422 (validation FastAPI). C'était la cause racine des échecs d'ingestion des demandes clients urgentes.
- **Cerveau2 — `dossier_id` vide** : remplacé par `"GENERAL"` avant envoi. Cerveau2 rejette les dossier_id vides.
- **Cerveau2 — body trop long** : tronqué à 150 000 caractères avec mention `[... tronqué]`. Évite les payloads JSON massifs qui peuvent causer des timeouts ou rejets.
- **Cerveau2 — log du body d'erreur HTTP** : en cas d'échec HTTP (422, 500, etc.), le texte de la réponse Cerveau2 est logué (tronqué à 500 caractères) pour un diagnostic immédiat.
- **Cerveau2 — skip newsletter/phishing** : les pièces jointes des newsletters et phishing ne sont plus envoyées à Cerveau2 (bruit inutile). Le bloc `feed_correspondance` l'était déjà, mais pas les PJ.
- **Cerveau2 — timeout ingestion 15s → 120s** : Cerveau2 met 40-120s par email (indexation embeddings + fallback LLM). Le timeout de 15s provoquait des échecs systématiques. Passage à 120s avec retry 3x.

---

## [1.18.5] — 2026-05-28 (hotfix Drafts monitoring + telemetry)

### Fixé
- **Table `agent_telemetry` manquante dans migrations** : ajoutée dans `db_migrate.py`. Sur le VPS elle existait déjà (créée manuellement), mais les nouveaux setups plantaient.
- **Log visible des brouillons IMAP** : `_log_telemetry("draft_deposited")` ou `"draft_failed")` inséré à chaque brouillon. Visible dans le cockpit web → Audit Logs → Télémétrie poller.
- **Vérification post-dépôt Drafts** : `_verify_draft_present()` fait un SELECT + SEARCH SUBJECT "DEMANDE" dans Drafts après l'APPEND pour confirmer que le brouillon est indexé. Si non retrouvé, log `imap_draft.unverified` (warning) mais considéré comme succès (APPEND a réussi).
- **Re-sélection INBOX après Drafts** : quand `append_draft()` emprunte la connexion du poller, il re-sélectionne INBOX après la vérification pour ne pas casser le prochain `fetch`.

---

## [1.18.4] — 2026-05-28 (hotfix critique poller)

### Fixé
- **Poller traite les mails récents en priorité** : avec 7701 vieux mails sans flag, `uids[:MAX_PER_CYCLE]` traitait les 10 plus anciens à chaque cycle. Les nouveaux mails n'auraient jamais été vus avant ~64h. Correction : `uids[::-1][:MAX_PER_CYCLE]` pour traiter les UIDs les plus élevés (donc les plus récents) en premier.

---

## [1.18.3] — 2026-05-28 (hotfix critique Drafts IMAP)

### Fixé
- **IMAP Drafts — réutilisation connexion poller** : `append_draft()` accepte désormais un paramètre `imap_client` optionnel. Le poller passe sa connexion IMAP existante, éliminant la connexion secondaire qui était rejetée par Infomaniak (cause racine des 55 échecs `list_failed response=BAD` aujourd'hui).
- **Alerte monitoring Draft IMAP** : nouvelle alerte Resend `alert_imap_draft_failure()` envoyée à CDAL à chaque échec de dépôt Draft. Permet de monitorer que les brouillons de Daniel sont bien déposés.

---

## [1.18.2] — 2026-05-28

### Fixé
- **Date/heure du message original** ajoutée dans les brouillons de réponse (Resend email + IMAP Drafts). Auparavant, seuls l'expéditeur et le sujet étaient affichés — la date manquait absolument.

---

## [1.18.1] — 2026-05-28 (hotfix critique)

### Fixé
- **Poller saturation CPU** : le poller trouvait 7781 mails historiques sans flag `AgentProcessed`, saturant le VPS à 4604% CPU. Ajout d'un filtre date logiciel qui pose le flag et skip immédiatement tout mail avant le 20 mai 2026.
- **`MAX_PER_CYCLE` réduit à 10** (au lieu de 200) pour éviter de bloquer l'event loop asyncio.
- **Sleep 0.5s entre chaque mail** traité par le poller pour préserver la réactivité du cockpit web.
- **Script `deploy-to-vps.sh`** : ajout d'une vérification d'architecture (local arm64 vs VPS amd64) pour éviter le déploiement d'une image incompatible.

---

## [1.18.0] — 2026-05-28

### Changé
- **Suppression complète de `torch` / `sentence-transformers`** : passage de l'embedder local E5-large (CPU, 2GB+ RAM, JIT Triton) à un embedder API via `litellm` + `openai/text-embedding-3-small` (OpenRouter). L'image Docker passe de ~4GB à ~800MB.
- **Suppression du préchargement embedder au boot** : plus de task `embedder-preload` qui bloquait le démarrage web. L'embedder est maintenant stateless (appel API).
- **Dockerfile.base allégé** : suppression de `gcc`, `g++`, `libffi-dev`, `libssl-dev` et du `python -m compileall`. Seul `tesseract-ocr` reste (OCR pièces jointes).

### Ajouté
- **Dépendance `pytesseract>=0.3.10`** ajoutée explicitement dans `pyproject.toml` (déjà utilisé par `document_extract.py`, manquait dans les déps).
- **Variables d'environnement `EMBEDDING_API_BASE` et `EMBEDDING_API_KEY`** dans `app/config.py` et `.env.example`.

### Fixé
- **`writable_schema=ON` protégé par `try/finally`** dans `scripts/bootstrap_embeddings.py` pour éviter la corruption SQLite en cas d'exception.
- **`.env.example`** : `EMBEDDING_MODEL` aligné sur `openai/text-embedding-3-small` (au lieu de l'ancien E5 local).

---

## [1.17.4] — 2026-05-28

### Fixé
- **Ordre email Resend** : la proposition de réponse (brouillon Charlie) apparaît désormais **en haut** de l'email, et le message original du client **en dessous** — comme dans les brouillons IMAP. Daniel lit d'abord ce qu'il doit approuver, puis le contexte complet.
- **Sujet Resend** : correction faute de frappe `REPOSNE` → `REPONSE`.
- **Bandeau Resend** : correction faute de frappe `Assiatnt` → `Assistant`.

---

## [1.17.3] — 2026-05-28

### Fixé
- **Embedder préchargé au boot** : `SentenceTransformer` est chargé dans un thread séparé (`asyncio.to_thread`) au démarrage de l'agent, avant le poller. Évite le blocage de l'event loop asyncio pendant plusieurs minutes quand le premier `demande_client` déclenche le chargement du modèle.
- **Poller batch limité + yield** : maximum 200 emails traités par cycle de polling ; `await asyncio.sleep(0)` ajouté dans la boucle pour céder le contrôle à uvicorn/web server. Évite que le traitement d'un gros backlog (ex: suppression du SINCE) ne rende le cockpit web inaccessible.

---

## [1.17.2] — 2026-05-28

### Fixé
- **Poller IMAP — critère SINCE retiré** : le serveur IMAP Infomaniak rejette silencieusement le format de date RFC 3501 (`SINCE 01-May-2026`), provoquant un retour de 0 résultats et un arrêt total de détection des emails. Le critère `SINCE` est supprimé de la commande SEARCH ; l'idempotence est assurée par `UNKEYWORD AgentProcessed` + le check `_mail_exists` en base.
- **Sujet brouillon IMAP** (correction V1.17.1) : alignement définitif sur `DEMANDE D'Approbation - Reponse Demande Client : {subject}`.
- **Corps du brouillon IMAP** (correction V1.17.1) : la proposition de réponse apparaît en premier, suivie du message original du client en dessous (demande Daniel).

---

## [1.17.0] — 2026-05-27

### Ajouté
- **V2a — Livraison brouillons IMAP Drafts** : Charlie dépose les brouillons directement dans le dossier Drafts de la boîte source, avec le flag `\Draft`.
- **Script `scripts/manual_draft_deposit.py`** : dépose manuel un brouillon existant (`ai_draft` en base) dans les Drafts IMAP, sujet `PROPOSITION DE REPONSE EMAIL N° {id} / {subject}`.
- **Découverte auto du dossier Drafts** : `LIST` IMAP pour trouver `Drafts`, `Brouillons`, ou tout dossier contenant "draft" (compatibilité locale Infomaniak).

### Changé
- **UI cockpit conversation** : le bloc "Réponse proposée par Charlie" remonte en colonne droite, sous les boutons d'action `Approuver` / `Rejeter`. Numéro de mail `Email #{{ id }}` affiché en évidence verte dans le titre du bloc.
- **Corps du brouillon IMAP** : uniquement la réponse proposée + bandeau cockpit — suppression du message original du client intégré dans le corps.

### Fixé
- **Sujet brouillon IMAP** : `PROPOSITION REPONSE : ...` → `DEMANDE D'Approbation - Reponse Demande Client : ...` (spec V2a).
- **Fallback Resend conditionnel** : `notify_draft()` n'est appelé que si `append_draft()` échoue. Plus de doublon systématique email + Drafts.

---

## [1.16.13] — 2026-05-26

### Fixé
- **Charlie SQL statut** : `_build_status_sql()` génère automatiquement `SELECT ... WHERE status = 'pending'` pour les questions de demandes clients en attente (y compris avec fautes de frappe comme "deamdnes").
- **Prompt statut visible** : `_sanitize_rows_for_prompt()` expose désormais `status` et `priority` au LLM — plus de "aucun indicateur de statut" malgré des résultats SQL.
- **Garde-fous secours** : quand le LLM final dit "pas trouvé" malgré des rows en base, Charlie reconstruit la réponse directement à partir des résultats SQL.

### Changé
- **Prompt system Charlie** : règle 7b/7c ajoutée pour forcer l'inclusion de `status`/`priority` dans les SELECT de liste.

---

## [1.16.12] — 2026-05-26

### Fixé
- **Questions identitaires Cerveau2** : fallback direct `GET /notes/{path}` pour les fiches `04_entities/personnes/*.md` qui ne sont pas indexées dans sqlite-vec (Christophe, Sarah, Daniel).
- **Nuage de liaison familial** : `_resolve_links()` scanne désormais les clés relationnelles (`épouse`, `mari`, `conjoint`, `fille`, `fils`, `parent`, etc.) dans le frontmatter YAML.
- **dossier_id sur identités** : plus de filtrage par `dossier_id="CDAL"` sur les requêtes identitaires — les fiches entité ne portent pas de `dossier_id`.

---

## [1.16.11] — 2026-05-25

### Fixé
- **UX boutons feedback Charlie** : `hx-disabled-elt="find button[type=submit]"` empêche le double-clic ; le formulaire se remplace par le message de confirmation via `hx-target="this" hx-swap="outerHTML"`.
- **Bouton "À corriger"** : id stable `{feedback_id}-toggle` pour éviter les conflits HTMX.

---

## [1.16.10] — 2026-05-25

### Fixé
- **Classification IMAP** : mailbox_name correctement extrait dans `count_sql` pour les statistiques par marque.

---

## [1.16.0] — 2026-05-24

### Ajouté
- **Cockpit web v1.0** : inbox, conversation, chat AI Charlie, dashboard admin.
- **Bot Slack Charlie AI** : @mention + DM sur #detective.

### Changé
- **Génération SQL programmatique** pour les comptages d'emails (bypass LLM, +90% fiabilité).

---

*Format basé sur [Keep a Changelog](https://keepachangelog.com).*
