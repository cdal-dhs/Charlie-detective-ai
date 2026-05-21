# Changelog — Detective.be Agent

> Format : [Keep a Changelog](https://keepachangelog.com/fr/1.0.0/)

---

## [1.13.10] — 2026-05-21

### Ajouté
- **Alerte email crédit Ollama Pro épuisé** : quand le modèle principal (`gemma4:31b`) retourne une erreur 429 / rate limit, Charlie envoie immédiatement un email d'alerte sur `cdal@digitalhs.biz` puis bascule automatiquement sur le fallback OpenRouter.
  - Nouveau module `app/alerts.py` : fonction `alert_ollama_credit_low()` envoyant un email HTML via Resend.
  - `app/llm/router.py` détecte les erreurs `429`, `ratelimit`, `rate limit`, `usage limit` et déclenche l'alerte (une seule fois par session jusqu'au redémarrage).

### Corrigé
- **Fallback OpenRouter non fonctionnel** : le modèle fallback `google/gemini-3.1-flash-lite` n'était pas reconnu par LiteLLM (provider manquant). Remplacé par `openrouter/google/gemini-2.5-flash-preview` dans les settings DB avec `api_base` et `api_key` explicites lors du fallback.

---

## [1.13.9] — 2026-05-21

### Corrigé
- **Renouvellement de facture classé "autre" au lieu de "facture"** : "renouvellement" retiré de `AUTRE_KEYWORDS` ; ajouté à `FACTURE_KEYWORDS` avec "régler", "paiement en ligne", etc.
- **Priorité facture avec lien de paiement** : `PAYMENT_LINK_KEYWORDS` déclenche `priority=high` pour les factures avec lien de règlement ou pièce jointe.
- **Invitations calendrier/meeting auto-approved** : `autre` + mots-clés invitation/ical/vcalendar/event → `status=approved` + `priority=low` (plus besoin de les traiter manuellement).

---

## [1.13.8] — 2026-05-21

### Ajouté
- **Édition des modèles LLM depuis le dashboard admin** : le superadmin peut modifier `llm_model_default`, `llm_model_classifier` et `llm_model_fallback` directement dans `/admin/settings` (onglet IA), sans SSH ni redémarrage.
  - Nouveau module `app/settings_store.py` : lecture synchrone de `app_settings` (DB) avec fallback `.env`.
  - `app/llm/router.py`, `app/pipeline/classifier.py`, `app/pipeline/generator.py` lisent désormais les modèles en temps réel depuis `app_settings`.
  - Le formulaire sauvegarde en DB via `POST /admin/api/settings` ; si un champ est vidé, la ligne DB est supprimée pour revenir au `.env`.

---

## [1.13.7] — 2026-05-21

### Ajouté
- **Visibilité backup Cerveau2 dans Audit Logs** : card en haut de page `/admin/audit` affichant la date/heure du dernier backup vault poussé sur GitHub.
  - Nouveau endpoint Cerveau2 `GET /admin/backup/status` qui lit `vault/00_system/.last_backup`.
  - Script `backup-vault.sh` écrit la date dans ce fichier après chaque push réussi.
  - Dashboard appelle cet endpoint via `get_backup_status()` dans `cerveau_client.py`.

---

## [1.13.6] — 2026-05-21

### Corrigé
- **Charlie répondait n'importe quoi quand on demandait sa version** : la question "quelle est la version actuelle de Charlie AI" déclenchait un SQL + vault qui renvoyait des documents sur CDAL/DIGITALHS au lieu du numéro de version.
  - `CHARLIE_SYSTEM_PROMPT` converti en f-string injectant `{VERSION}` depuis `app._version`.
  - Règle 6 ajoutée : "Quand Daniel demande ta version actuelle, réponds directement : 'Je suis Charlie AI version {VERSION}.' — pas besoin de SQL ni de vault."

---

## [1.13.5] — 2026-05-21

### Corrigé
- **Faux positif phishing sur expéditeur connu** : un email de test envoyé par CDAL (déjà présent dans `mail_processed`) a été classé `phishing` par le prefilter.
  - `_is_known_sender()` vérifie si l'expéditeur existe déjà dans `mail_processed` avant d'appliquer le prefilter phishing.
  - Si connu → le LLM classifier décide (garde-fou anti-faux-positif).
  - Prompt classifier enrichi : few-shot "email interne de test avec TEST dans le sujet + PJ PDF = autre (PAS phishing)".

---

## [1.13.4] — 2026-05-21

### Corrigé
- **Feedback buttons ✅/❌ totalement inertes** : le `hx-vals` JSON avec `\'` (échappement d'apostrophe) produisait du JSON invalide — HTMX ne parsait rien et envoyait un POST vide.
  - Remplacé `hx-vals` par un vrai `<form>` avec `<input type=hidden>` pour le bouton "Bonne réponse".
  - Attributs `value` avec `&quot;` et `&#39;` pour résister à tout contenu.
  - Même correction appliquée au formulaire "À corriger".

---

## [1.13.3] — 2026-05-21

### Corrigé
- **HTMX ne processait pas les bulles Charlie injectées dynamiquement** : `insertAdjacentHTML` n'appelle pas `htmx.process()`, donc les `hx-post` des boutons feedback restaient sans écouteur.
  - Fix : après chaque injection, boucle `htmx.process(child)` sur les nouveaux nœuds (`inbox.html:318`).
- **Échappement JSON `hx-vals` amélioré** : backslash + single quote + double quote (`\`, `'`, `"`).

---

## [1.13.2] — 2026-05-21

### Ajouté
- **Bypass LLM pour les questions identitaires** : `_extract_identity_answer()` extrait directement les noms propres depuis le contenu du vault via regex ciblées.
  - Patterns : `"aidé par son épouse : Sarah"`, `"son épouse Sarah"`, `"épouse : Sarah"`, `"Sarah, épouse de CDAL"`.
  - Fallback par tokenisation voisine si aucun pattern ne match.
  - Quand SQL vide + question identitaire + vault retourne des notes → réponse directe sans appel LLM.
  - Élimine définitivement le bug "aucun résultat" malgré le document visible.

---

## [1.13.1] — 2026-05-21

### Corrigé
- **Vault content tronqué à 500 caractères** : le frontmatter YAML consommait ~350 caractères, le LLM ne voyait que "TEST PIECE JOINTE...".
  - Augmenté à **2000 caractères** dans `_summarize_results()`.
- **Prompt vault-only trop permissif** : `_SUMMARY_PROMPT_VAULT_ONLY` renforcé avec "INSTRUCTION CRITIQUE : la réponse se trouve DANS les notes... Ne dis JAMAIS 'je ne trouve rien'".

---

## [1.13.0] — 2026-05-21

### Ajouté
- **Forçage vault pour questions identitaires** : `_is_identity_query()` détecte "qui", "épouse", "mari", "fille", "fils", etc. et force `need_vault=True` indépendamment du SQL généré.
- **Prompt vault-only** : `_SUMMARY_PROMPT_VAULT_ONLY` sans section SQL, utilisé quand SQL vide + vault présent.

---

## [1.12.7] — 2026-05-20

### Corrigé
- **Ingestion pièces jointes dans Cerveau2 : zéro tolérance**
  - Bug : si `extract_text_bytes()` retournait vide (PDF illisible, image sans OCR), le code faisait `continue` → la PJ n'allait jamais dans Cerveau2.
  - Fix : fallback body avec métadonnées pour **toutes** les PJ, même non extractables.
  - `doc_id` déterministe : `hash()` non reproductible entre redémarrages remplacé par `hashlib.md5`.

### Modifié
- **Cerveau2 blindé** (repo séparé `Cerveau2-DEtective`) :
  - Recherche sans troncation (`_extract_keywords()` remplace `split()[:8]`).
  - Recherche insensible aux accents (`_normalize()` via `unicodedata.normalize("NFKD")`).
  - Path-traversal guards : `dossier_id` validé `^[A-Za-z0-9_-]+$` dans `query.py`, `vault_reader.py`, `vault_writer.py`, `email_ingester.py`, `note_ingester.py`.

---

## [1.12.5] — 2026-05-20

### Corrigé
- **Fix téléchargement pièces jointes** : le endpoint `/app/attachments/{id}/download` retournait 404/Internal Server Error car `storage_path` était stocké en **chemin absolu du Mac de développement** (`/Users/cdal/...`) dans la DB. Sur le VPS, ce chemin n'existe pas.
  - `_save_attachments()` : stocke désormais un chemin **relatif** à `data_dir` (`attachments/{mail_id}/{filename}`).
  - `download_attachment()` : reconstruit le chemin absolu avec `settings.data_dir / path`, tout en supportant les anciens chemins absolus pour la rétrocompatibilité.

---

## [1.12.4] — 2026-05-20

### Corrigé
- **Timeout Cerveau2 passé de 4s à 12s** : Cerveau2 met ~5.5s à répondre sur des questions complexes (recherche globale + LLM). Le timeout de 4s dans `query_vault()` causait un timeout silencieux : Charlie recevait `vault: 0` alors que Cerveau2 avait bien trouvé le PDF. Fix : `timeout=12.0` dans `app/cerveau_client.py`.

---

## [1.12.3] — 2026-05-20

### Corrigé
- **Synthèse vault quand SQL est vide** : résout le cas où Charlie fetchait les notes Cerveau2 mais ne les synthétisait jamais.
  - La condition `_summarize_results` exigeait `sql and ...` ce qui bloquait la synthèse conversationnelle quand le LLM ne générait pas de requête SQL.
  - Fix : la synthèse est maintenant déclenchée dès qu'il y a des données (SQL, vault, ou mémoire), indépendamment de la présence d'une requête SQL.

---

## [1.12.2] — 2026-05-20

### Corrigé
- **Charlie interroge Cerveau2 pour les questions d'identité** : résout le cas "Comment se nomme l'épouse de CDAL ?" où Charlie répondait "Aucun résultat" alors que le PDF était bien dans Cerveau2.
  - Ajout de mots-clés d'identité dans `_VAULT_KEYWORDS` (`qui`, `personne`, `nom`, `prenom`, `client`, `epouse`, `mari`, `conjoint`, `contact`, `sappelle`) pour déclencher `query_vault` quand la question porte sur une personne.
  - Ajout d'un fallback `vault_fallback` : si SQL retourne 0 résultat, pas de catégorie d'enquête détectée, mais la question est une requête d'identité → Charlie interroge Cerveau2 a posteriori et synthétise la réponse.

---

## [1.12.1] — 2026-05-20

### Corrigé
- **Fix SQL cleanup attachments** : correction du paramètre bind SQLite dans `cleanup_old_attachments()`.  
  `datetime('now', '-? days')` ne fonctionne pas avec `?` à l'intérieur d'une string SQLite → remplacé par `datetime('now', '-' || ? || ' days')`.  
  Empêche l'erreur `Incorrect number of bindings supplied` qui bloquait la purge quotidienne des pièces jointes.

---

## [1.12.0] — 2026-05-20

### Ajouté
- **Dashboard Pièces Jointes** : les PJ extraites des emails sont maintenant visibles dans le cockpit.
  - **Badge 📎 dans l'inbox** : colonne "PJ" indiquant le nombre de pièces jointes par email (HTMX filter/sort compatible).
  - **Bloc "Pièces jointes" dans la conversation** : liste des fichiers avec nom, taille, bouton **Télécharger**, et aperçu texte extractible (collapsible).
  - **Endpoint `/app/attachments/{id}/download`** : serve les fichiers bruts via `FileResponse` avec auth operator.
- **Stockage local des PJ** : `_save_attachments()` dans `app/workers/imap_poller.py` écrit les fichiers sur disque (`data/attachments/{mail_id}/{filename}`) et insère une ligne par PJ dans la nouvelle table `email_attachment` (preview texte inclus).
- **Cleanup automatique 30 jours** : tâche de fond `run_attachment_cleanup()` dans `app/main.py` qui purge quotidiennement les PJ de plus de 30 jours (disque + DB), conformément au principe que Cerveau2 conserve l'index vectoriel à long terme.

### Architecture
- Table `email_attachment` : `id`, `mail_processed_id`, `filename`, `storage_path`, `size_bytes`, `extracted_text_preview`, `created_at`. Index sur `mail_processed_id`. Clé étrangère `ON DELETE CASCADE`.
- Les PJ continuent d'être ingérées dans **Cerveau2** (unchanged) ; le stockage local sert uniquement au dashboard visuel et au téléchargement immédiat.

---

## [1.11.0] — 2026-05-20

### Ajouté
- **UPLOAD Documents** : nouvelle section colorée dans la sidebar admin (`/admin/documents`) avec drag-and-drop pour ingérer des fichiers dans Cerveau2. Interface : sélection du dossier client, marque, et titre. Formats supportés : TXT, MD, CSV, JSON, XML, HTML, PDF, DOCX, JPG, PNG, TIFF (OCR via Tesseract). Architecture extensible pour MP3/MP4 en V2.
- **Module `app/pipeline/document_extract.py`** : extracteur universel de texte. Une fonction par format (`_extract_pdf`, `_extract_docx`, `_extract_txt`, `_extract_image`). Chunking intelligent avec chevauchement de 10% pour les textes >4000 tokens. Détection de doublons via hash MD5.
- **Endpoint Cerveau2 `POST /ingest-note`** : nouveau client `feed_document()` dans `app/cerveau_client.py`. Même pattern fire-and-forget que `feed_correspondance` (retry 3x, timeout 15s, 409 = doublon).
- **Pièces jointes emails → Cerveau2** : dans `app/workers/imap_poller.py`, après l'ingestion de l'email, extraction automatique des PJ supportées (`_extract_attachments()`). Ignore les exécutables, les formats non supportés, et les mini-images <2KB (logos/signatures). Chaque PJ devient une note `type: "document"` dans Cerveau2, rattachée au même `dossier_id` que l'email parent.
- **Table `document_scanned`** dans `agent_state.db` (migration auto) : tracking local des documents uploadés (doc_id, dossier_id, marque, format, taille, date, sync status).

### Architecture
- Principe : **Cerveau2 est le seul vault**. Charlie ne stocke aucun contenu documentaire en local (hors tracking minimal dans `document_scanned`). Tous les documents, qu'ils viennent d'un upload cockpit ou d'une pièce jointe email, sont indexés vectoriellement par Cerveau2.

---

## [1.11.1] — 2026-05-20

### Corrigé
- **`derive_dossier_id()` — expéditeurs internes** : les emails de CDAL (`cdal@digitalhs.biz`), Daniel ou tout domaine interne (`detectivebelgique.be`, `detectivebelgium.com`, `dpdhuinvestigations.be`, `digitalhs.biz`) généraient un `dossier_id` du type `detective_belgique_cdal`, provoquant un **422 Cerveau2** car le dossier n'existe pas. Désormais ces expéditeurs retournent `dossier_id=""`, ce qui permet à Cerveau2 de les ingérer sans rattachement forcé.
- **`_INTERNAL_SENDERS` et `_INTERNAL_DOMAINS`** : garde-fou explicite dans `app/cerveau_dossier.py` pour traiter tous les emails entrants sans exception, y compris les internes.

---

## [1.10.5] — 2026-05-20

### Corrigé
- **RAG robuste — fallback silencieux** : `retrieve()` dans `app/pipeline/rag.py` capturait `sqlite3.OperationalError` (table `pairs_vec` absente) et plantait le pipeline → aucun brouillon généré pour DPDH. Désormais toute erreur SQL ou d'embedding retourne `[]` avec un warning logué, et le générateur continue avec SOUL.md + personality seuls.
- **Faux positifs dossier_id** : `extract_dossier_ref()` dans `app/cerveau_dossier.py` acceptait "TEST" comme référence de dossier, provoquant un 422 sur Cerveau2. Ajout d'une liste `_IGNORE_REFS` (TEST, TESTING, DEMO, URGENT, etc.) pour filtrer les mots courants.

---

## [1.10.4] — 2026-05-20

### Modifié
- **Seuil alerte disque** : `THRESHOLD_PERCENT` passé de 24% à **25%** dans `app/workers/disk_watcher.py`.
- **Nettoyage VPS** : `docker system prune -af` sur le VPS Hostinger → libération de **~120 GB** (images inutilisées 27 GB + build cache 90 GB). Espace libre passé de 19.5% à ~81%.

---

## [1.10.3] — 2026-05-20

### Ajouté
- **Surveillance espace disque VPS** : nouveau module `app/workers/disk_watcher.py` qui vérifie l'espace libre du filesystem racine toutes les 60 minutes. Si l'espace libre passe sous **24%**, envoie automatiquement un email d'alerte URGENT via Resend à `draft_recipient` (cdal@digitalhs.biz) avec les détails (total, utilisé, libre en GB et %). One-shot : une seule alerte par crise jusqu'à retour au dessus du seuil. Log structuré à chaque check.

---

## [1.10.2] — 2026-05-20

### Ajouté
- **Auto-évolution SOUL.md (tâche de fond)** : nouveau script `scripts/evolve_soul.py` qui compare le SOUL.md actuel avec les emails sortants récents de Cerveau2 et génère une version enrichie via LLM. Garde-fous : backup automatique `SOUL.md.bak.YYYYMMDD`, mode `--dry-run`, détection des suppressions risquées (section RÈGLES ABSOLUES, règles critiques). Si trop de suppressions, l'évolution est bloquée et un aperçu est écrit dans `/tmp/SOUL_proposed.md`.
- **Scheduler d'évolution** : coroutine `run_soul_evolver` dans `app/main.py` qui lance `evolve_soul.py` toutes les **72 heures** via subprocess isolé. Attend 5 min au boot pour ne pas saturer le LLM.
- **Persistance SOUL.md dans `data/`** : `app/main.py` copie `app/prompts/SOUL.md` → `data/SOUL.md` au boot si absent. Tous les modules (`generator.py`, `admin.py`, `extract_soul.py`, `evolve_soul.py`) lisent/écrivent désormais `data/SOUL.md`, ce qui garantit la persistance entre redémarrages Docker (volume `data/` monté en read-write).

---

## [1.10.1] — 2026-05-20

### Ajouté
- **Onglet SOUL.md dans le panel admin** : nouvel onglet "SOUL.md" dans `/admin/settings`, accessible uniquement aux super-admins. Affiche le contenu de `app/prompts/SOUL.md` dans un textarea éditable (hauteur 60vh, monospace). Bouton "Sauvegarder" qui persiste le fichier sur disque et logue l'action en audit. Chargement asynchrone du contenu via `fetch()` + Alpine.js.

---

## [1.10.0] — 2026-05-20

### Ajouté
- **SOUL.md — Guide de style Daniel par marque** : nouveau script `scripts/extract_soul.py` qui interroge Cerveau2 pour récupérer les emails sortants (`direction="out"`) de chaque marque, analyse le style d'écriture via LLM, et génère `app/prompts/SOUL.md` structuré par marque (Detective Belgique, Detective Belgium, DPDH Investigations). Couvre ton, registre, formules récurrentes, signature, règles absolues.
- **Intégration SOUL.md dans le générateur** : `app/pipeline/generator.py` injecte désormais la section correspondant à `mailbox.brand` dans le prompt système, en complément de `personality_daniel.txt`. Permet à Charlie de calquer le style spécifique de Daniel selon la marque concernée.
- **Documentation API Cerveau2** : `docs/CERVEAU2_API.md` référence complète des endpoints `/query` et `/ingest-email`, payloads, mapping marques, format des notes, pièges résolus et commandes de diagnostic.
- **Ingestion batch des emails sortants** : nouveau script `scripts/ingest_sent_to_cerveau2.py` qui balaie les dossiers "Sent" des 3 boîtes IMAP et les injecte dans Cerveau2 (`direction="out"`) pour enrichir le corpus d'analyse de style.

### Corrigé
- **Contrainte `limit` Cerveau2** : le endpoint `/query` rejette `limit > 20` (422). `extract_soul.py` et `query_vault()` ont été ajustés pour respecter ce plafond.

---

## [1.9.9] — 2026-05-20

### Corrigé
- **Fix newsletters classées comme "autre"** : `quick_classify()` testait `is_service_email()` **avant** `is_newsletter()`. Les newsletters envoyées via SendGrid (Cercle Wallonie, etc.) contenaient des mots-clés dans leur corps qui matchaient `AUTRE_KEYWORDS` → classées "autre" au lieu de "newsletter". Deux corrections : (1) `is_newsletter` est désormais testé **avant** `is_service_email` dans `quick_classify()`, (2) `is_service_email()` a une garde `if is_newsletter(msg): return False` pour éviter les faux positifs.
- **Version bump 1.9.9** : `_version.py`, CHANGELOG synchronisés.

---

## [1.9.8] — 2026-05-20

### Corrigé
- **Fix décodage entités HTML dans les emails** : `_get_body_text()` dans `app/workers/imap_poller.py` retirait les balises HTML (`re.sub(r"<[^>]+>", "", html)`) mais ne décodait pas les entités HTML (`&#039;`, `&eacute;`, etc.). Résultat : les newsletters et emails HTML étaient illisibles (`l&#039;équipe` au lieu de `l'équipe`). Ajout de `html.unescape()` après le détaggage pour les cas multipart et non-multipart.
- **Version bump 1.9.8** : `_version.py`, CHANGELOG synchronisés.

---

## [1.9.7] — 2026-05-20

### Corrigé
- **Fix liens conversation 404 sur résultats archives** : `_format_rows_html()` dans `app/web/api.py` créait systématiquement des liens `/app/conversation/{id}` pour toute colonne `id`, même quand les résultats venaient des archives historiques (`boite1/2/3.sqlite`). Les IDs des archives ne correspondent pas à la table `mail_processed` → 404. Désormais, la présence de la colonne `source_db` (exclusives aux résultats historiques) désactive les liens : `id` et `subject` sont affichés comme texte simple sans lien.
- **Version bump 1.9.7** : `_version.py`, CHANGELOG synchronisés.

---

## [1.9.6] — 2026-05-19

### Corrigé
- **Fix dump technique Slack** : `slack_bot.py` n'affiche plus les rows bruts en dessous du texte LLM. Le LLM synthétise déjà la réponse — le dump technique gâchait l'expérience et fuitait des données sensibles.
- **Fix SQL trop permissif — garde anti-faux-positif** : quand Daniel demande un type d'enquête (filature, adultère, disparition...), le LLM générait un SQL avec LIKE OR sur `subject`, `body`, `ai_draft` qui attrapait n'importe quoi (ex: facture Arval pour "filature"). Le prompt distingue désormais deux modes : **Mode A** (recherche par `category` exacte pour les types d'enquête) vs **Mode B** (LIKE OR pour les mots-clés spécifiques). Garde post-SQL : si les résultats n'ont pas la `category` attendue, ils sont considérés comme faux-positifs → recherche archives automatique.
- **Version bump 1.9.6** : `_version.py`, CHANGELOG, HANDOVER synchronisés. Tolérance zéro respectée.

---

## [1.9.5] — 2026-05-19

### Ajouté
- **Charlie devient bibliothécaire — mémoire persistante** :
  - `app/charlie_memory.py` (nouveau) : table SQLite `charlie_memory` avec `save_memory()`, `query_memory()`, `is_save_request()`, `is_memory_query()`. Garde-fou de la grande bibliothèque de Daniel.
  - `app/charlie.py` : Phase 2.5 (`query_memory()` si question de mémoire ou `dossier_id`). Phase 4 (`save_memory()` si Daniel demande de retenir/enregistrer/noter). Réponse confirmée : *"C'est noté dans ma mémoire, Daniel !"*
  - `_summarize_results()` : injecte les souvenirs Charlie dans le prompt de synthèse via `_SUMMARY_PROMPT_VAULT` (placeholders `memory_count`, `memory_notes`).
  - `app/main.py` : `init_memory_table()` au démarrage.
- **Charlie détecte filature/surveillance** : mots-clés `filature`, `surveillance`, `observation`, `terrain` ajoutés aux déclencheurs vault et summary. Règle 11 du prompt : filature = surveillance, cherche catégorie DB ET interroge Cerveau2.
- **Charlie la précieuse moitié** : prompts réécrits avec personnalité marquée ("prolongement de ton cerveau", ton direct/chaleureux, humour détective bienvenu).
- **Date visible dans le tableau résultats** : colonne `received_at` en première position, formatée `2026-05-15 10:30`.
- **Patience aléatoire** : message immédiat aléatoire avec spinner animé ("OK je vérifie", "Je consulte ton second cerveau", etc.) avant la réponse réelle.
- **Mémoire conversationnelle** : `history[]` côté client (20 messages), envoyé à chaque requête. Charlie enchaîne les questions de suite avec contexte.
- **Garde anti-hallucination** : si SQL retourne 0 ligne et vault vide, réponse forcée à *"Aucun email trouvé pour cette recherche."* sans appeler le LLM summary.

### Corrigé
- **Fix garde archives — vault ne bloque plus la recherche historique** : si SQL retourne 0 (même `COUNT(*)=0`), Charlie cherche TOUJOURS dans les archives boite1/2/3, indépendamment du vault ou de la mémoire. Ces sources sont du contexte, pas des données structurées. Règle de garde : `if sql and not has_sql_data`.
- **Fix faux `dossier_id`** : `_DOSSIER_RE` utilise `(?i:dossier|affaire|...)` (case-insensitive uniquement sur le préfixe) avec un groupe de capture strict `[A-Z][a-zA-Z0-9]{2,}`. Empêche l'extraction de `"entreprise"` ou `"infidelite"` comme faux dossier_id.
- **Fix fuite de données CRITIQUE** : `_sanitize_rows_for_prompt()` ne garde que `subject`, `received_at`, `category` avant d'envoyer au LLM. Masque ABSOLUMENT : `id`, `sender`, `body_preview`, `body`, `source_db`. Plus jamais d'expéditeurs réels ou de contenu brut dans les réponses Charlie.
- **Fix format réponse archives** : `_format_historical_response()` formate une réponse propre et conversationnelle pour les résultats historiques. Liste à puces avec liens cliquables vers l'inbox, sans dump technique.
- **Filtre archives par année** : `_extract_year()` extrait `20xx` de la question. `_search_historical_by_category()` filtre par `date LIKE '%2026%'` quand une année est détectée. Évite de lister 15 ans d'archives quand Daniel demande "pour 2026".
- **Fix tri chronologique archives** : `_search_historical_by_category()` parse les dates RFC 2822 via `email.utils.parsedate_to_datetime()` et trie globalement par date décroissante (`reverse=True`). Fini le tri lexicographique qui mettait mars 2026 avant janvier 2026.
- **Fix tri datetime offset-naive vs offset-aware** : `_parse_date()` normalise toutes les dates en offset-naive (`replace(tzinfo=None)`) avant comparaison. Résout le `TypeError` sur les dates RFC 2822 avec timezone.
- **Parallélisation SQL + vault + mémoire** : `asyncio.gather()` exécute les trois appels en parallèle au lieu de séquentiellement. Gain de latence : ~30 s → ~5 s sur les requêtes COUNT.
- **Timeout vault réduit 10 s → 4 s** : `app/cerveau_client.py` — le vault Cerveau2 ne bloque plus Charlie pendant 10 secondes si le réseau est lent.
- **Skip vault sur COUNT(*)** : `app/charlie.py` `_is_count_query()` — inutile d'interroger le vault pour un simple comptage. Économise 4-5 s supplémentaires.
- **Fix JS Alpine.js** : apostrophe dans `'C'est noté...'` rompait la chaîne JS, plantant `charlieData()`. La modale restait visible et impossible à fermer. Remplacé par des double-quotes + ajout `x-cloak`.
- **Filtre archives historiques** : `_search_historical_by_category()` exclut désormais les emails génériques (`subject NOT LIKE '%Nouveau Message De Détective%'`, `%Formulaire%`, `%Contact%`), les expéditeurs `noreply`, et exige un `body_preview` significatif (`LENGTH(body_preview) > 30`). Empêche Charlie de présenter du spam comme un dossier client.

---

## [1.9.4] — 2026-05-19

### Ajouté
- **Pipeline Cerveau2 — ingestion continue + migration historique** :
  - `app/cerveau_dossier.py` (nouveau) : logique de dérivation `dossier_id` avec finesse maximale. Priorité : (1) référence de dossier extraite du sujet via regex (`ADF`, `PRJ2024`), (2) nom anonymisé du client, (3) partie locale de l'email. Helper `derive_dossier_id_from_state()` pour lire `agent_state.db`.
  - `app/cerveau_client.py` : nouvelle fonction `feed_correspondance()` qui POST sur `/ingest-email` de Cerveau2. Retry 3× avec backoff exponentiel, gestion silencieuse des doublons (HTTP 409), dégradation silencieuse si Cerveau2 est down.
  - `app/workers/imap_poller.py` : hook post-`persist()` qui alimente Cerveau2 en fire-and-forget pour **tout mail entrant sauf newsletter et phishing**. La langue détectée est réutilisée pour les `demande_client`.
  - `scripts/bootstrap_cerveau2.py` (nouveau) : script one-shot d'import historique depuis les 3 DB SQLite. Mappe les 12 catégories DB historiques vers les catégories fines Cerveau2 (`infidelite`, `surveillance`, `enquete_famille`, `recherche_personne`, `controle_residence`, `investigation_entreprise`, `test_materiel`, `collaboration`, `harcelement`, etc.). Supporte `--dry-run`, `--limit`, `--batch-size`. Gère aussi la table `sent_emails` (direction `out`).
  - `tests/test_cerveau_feed.py` (nouveau) : 8 tests anti-régression couvrant extraction de `dossier_id`, dérivation par référence/anonymisé/sender, et le hook poller (feed activé pour `demande_client`/`facture`, désactivé pour `newsletter`/`phishing`).

---

## [1.9.3] — 2026-05-18

### Corrigé
- **Charlie AI : le summary écrasait les vault notes** — dans `app/charlie.py`, `ask_charlie()` interroge désormais le vault Cerveau2 **avant** de générer le summary. Le prompt de summary (`_SUMMARY_PROMPT_VAULT`) intègre explicitement les notes du vault quand elles existent, évitant que Charlie ne mentionne que les résultats SQL.
- Ajout de logs `charlie.dossier_detected` et `charlie.vault_fetched` pour tracer le `dossier_id` extrait et le nombre de notes retournées.

---

## [1.9.2] — 2026-05-18

### Ajouté
- **Recherche par dossier spécifique dans Charlie AI** :
  - `app/charlie.py` : extraction automatique d'un `dossier_id` (ex: "ADF") via regex sur la question utilisateur (`_extract_dossier_id()`). Quand un dossier est détecté, le prompt système est enrichi pour forcer le LLM à chercher ce terme dans les emails via `LIKE`. Le vault Cerveau2 est toujours interrogé avec `dossier_id` passé à `query_vault()`.
  - `app/cerveau_client.py` : supporte déjà `dossier_id` dans `query_vault()`.
  - **Tests** : 4 nouveaux tests dans `tests/test_charlie_vault.py` couvrant l'extraction de `dossier_id` (syntaxe `dossier : ADF`, `affaire XYZ`, `#PROJ42`) et l'appel forcé au vault malgré un SQL généré.

---

## [1.9.1] — 2026-05-18

### Ajouté
- **Charlie AI chat × Cerveau2 vault (Sprint 5 extension)** :
  - `app/charlie.py` : `CharlieResult` enrichi avec `vault_notes: list[VaultNote]`. Ajout du helper `_is_vault_relevant()` et appel `query_vault()` dans `ask_charlie()` pour les questions conversationnelles (sans SQL) ou contenant des mots-clés historique/dossier/similaire.
  - `app/web/api.py` : le endpoint `charlie_ask` génère désormais un bloc HTML `vault_html` injecté dans la bulle AI du chat, affichant les notes du vault avec prévisualisation.
  - `app/delivery/slack_bot.py` : `format_charlie_response()` ajoute les vault notes en blocs Slack (context + sections) avant le divider final.
  - **Tests** : `tests/test_charlie_vault.py` — 5 tests couvrant la détection de pertinence vault et les appels conditionnels.

---

## [1.9.0] — 2026-05-18

### Ajouté
- **`app/cerveau_client.py`** (nouveau module) : client async `query_vault()` vers l'API Cerveau2-Det. Retourne une liste de `VaultNote` (path + content). Dégradation silencieuse : retourne `[]` si config absente, erreur réseau, zone rouge, ou HTTP 4xx/5xx.
- **Intégration Cerveau2 dans `generate_draft()`** : après le retrieve RAG sqlite-vec, un appel `query_vault()` enrichit le contexte LLM avec les correspondances historiques du vault. Les notes sont injectées sous `=== Correspondances historiques du vault ===` dans le prompt.
- **Configuration** : 3 nouvelles variables `.env` — `CERVEAU2_BASE_URL`, `CERVEAU2_API_SECRET`, `CERVEAU2_LIMIT` (défaut 3).
- **Tests** : `tests/test_cerveau_client.py` — 10 tests couvrant dégradation silencieuse, réponses nominales, headers auth, zone rouge, context_only.

---

## [1.8.1] — 2026-05-17

### Corrigé
- **Hot-row visuel totalement absent** : `border-l-4 border-l-green-500` appliqué sur `<tr>` ne s'affiche pas car les navigateurs ignorent les bordures latérales sur les lignes de table en mode `border-collapse: separate` (défaut Tailwind). Déplacé sur le premier `<td>`. Fond renforcé `bg-green-900/40` (au lieu de `/20`) pour meilleure lisibilité sur le fond sombre.

---

## [1.8.0] — 2026-05-16

### Corrigé
- **Hot-row visuel non appliqué sur les vieux mails** : seule la première ligne `demande_client + pending + high` affichait le trait vert (`border-l-green-500`). Les mails persistés avant l'ajout de la colonne `status` avaient `status = NULL`. Le template affichait visuellement "pending" dans le `<select>`, mais la condition `m.status == 'pending'` était fausse. `effective_status = m.status or 'pending'` corrige le critère `is_hot`, et le `<select>` pré-sélectionne désormais "pending" aussi quand `m.status` est vide.
- **Fond hot-row renforcé** : `bg-green-900/20` au lieu de `/10` pour plus de contraste sur le fond sombre.
- **Crash serveur web en production** : `app.mount("/static", ...)` échouait silencieusement car le répertoire `app/web/static` n'était pas copié dans l'image Docker (jamais commité dans Git). Le serveur uvicorn ne démarrait jamais → Traefik retournait 502. Corrigé : mount conditionnel avec vérification `Path("app/web/static").exists()`.
- **Garde-fous deploy** : `scripts/deploy-to-vps.sh` vérifie désormais que les répertoires montés dans `docker-compose.yml` existent localement, effectue un dry-run build Docker avant envoi, et crée `app/web/static/.gitkeep` si absent. Voir `docs/RUNBOOK.md#INC-001`.

---

## [1.7.9] — 2026-05-16

### Corrigé
- **BUG CRITIQUE : écrasement des champs cockpit** : le poller IMAP réécrivait `category`, `priority` et `status` à chaque cycle via `ON CONFLICT DO UPDATE`, écrasant les modifications faites par l'opérateur dans le cockpit web. `_persist()` utilise désormais un `SELECT` préalable : si le mail existe déjà, seuls `body_preview`, `body`, `ai_draft` et `processed_at` sont mis à jour — jamais les champs métier.
- **Re-notification et re-génération de brouillon** : pour un mail déjà en base (flag IMAP manquant ou retiré), le poller regénérait un brouillon et renvoyait une notification Resend/Slack à chaque cycle. Ajout de `_mail_exists()` : la génération de brouillon et les notifications ne se déclenchent désormais que pour les mails **nouveaux** (`is_new`).
- **Counts des tabs non dynamiques** : quand l'opérateur changeait une catégorie/priorité/statut via HTMX, les compteurs dans les tabs (Urgent, Demandes client, Factures, etc.) restaient figés à leur valeur du chargement initial. Le endpoint `/api/inbox` recalcule désormais les `counts` à chaque requête HTMX et les injecte dans les tabs via `hx-swap-oob`.

---

## [1.7.8] — 2026-05-16

### Corrigé
- **Lien cockpit dans Slack** : `draft_id` passé à Slack est désormais le vrai `id` SQLite auto-incrémenté (retourné par `_persist`) au lieu de l'`uid` IMAP. Le lien `/app/conversation/{id}` fonctionne correctement.
- **Bouton Slack invisible** : remplacé le bloc `actions` (button Block Kit non supporté par les webhooks) par une section `mrkdwn` avec un lien cliquable.
- **Lien cockpit dans l'email Resend** : ajout d'un bouton bleu "Ouvrir le dossier #{id}" dans l'email de notification brouillon.
- **Faux positifs `demande_client` massifs** : le pré-filtre rules-based capturait des emails automatiques (renouvellement Infomaniak, confirmations, reçus) comme demandes client. Retiré `demande_client` du pré-filtre rapide — seuls les formulaires de contact du site passent en pré-filtre. Le LLM classifier prend le relai pour tout le reste.
- **Garde-fou post-classification** : avant de notifier Slack, `_is_verified_demande_client()` vérifie que l'email n'est pas automatique (expéditeur service, headers `Auto-Submitted`, sujets transactionnels). Les brouillons sont toujours générés pour la trace en DB, mais le canal #detective n'est plus pollué par les faux positifs.
- **Garde-fou priorité inconditionnel** : `if category == "demande_client": priority = "high"` hard-codé dans le poller — même si `assign_priority` venait à être modifié, les demandes client restent toujours HIGH.
- **Variable `body_preview` utilisée avant sa définition** dans `imap_poller.py` — déplacée avant le bloc `if category == "demande_client"`.
- **Syntaxe `_persist`** : parenthèse mal placée lors de l'édition précédente, corrigée.
- **Route /app/inbox 404** : ajout d'une route `/app/inbox` qui redirige vers `/app/`.
- **Checkbox DPDH manquante dans l'inbox** : `_fetch_mailboxes()` scannait la DB (`SELECT DISTINCT mailbox_name`) pour afficher les checkboxes. Si DPDH n'avait encore aucun mail, elle n'apparaissait pas. Corrigé : la fonction retourne désormais les 3 boîtes configurées via `settings.mailboxes`, indépendamment de la présence de mails en base.

### Ajouté
- **Télémétrie des cycles IMAP** : chaque cycle de polling écrit un événement `poller_cycle` dans `agent_telemetry` (DB `agent_state.db`) avec le nombre de mails traités et le breakdown par catégorie.
- **Dashboard "Cycles IMAP récents"** : le panel admin affiche les 10 derniers cycles IMAP avec la boîte, les détails et l'heure. Permet de vérifier visuellement quand l'agent a dernièrement travaillé.
- **Résumé de cycle polling** : à chaque cycle IMAP, un log `poller.cycle_summary` affiche le nombre de mails traités et le breakdown par catégorie (`{"demande_client": 1, "autre": 2}`). Si aucun mail, log `poller.cycle_empty`.
- **Body preview dans les notifications Slack** : la notification de nouveau brouillon inclut désormais un aperçu du contenu du mail (tronqué à 400 caractères).
- **Logs journaliers lisibles** : les fichiers `logs/agent-YYYY-MM-DD.log` utilisent désormais un format lisible (ConsoleRenderer sans couleurs) au lieu de JSON brut.
- **Migration DB automatique** : `app/main.py` appelle `migrate()` au démarrage. La colonne `body` manquante sur les DB existantes est ajoutée automatiquement.
- **Script de test traçable** : `scripts/test_pipeline.py` génère un `batch_id` horodaté (ex: `#20260516-053710`) injecté dans le sujet, le corps et les headers X-Test-* pour reconnaître facilement les emails de test.

### Modifié
- **Ordre pipeline** : le mail est persisté en DB *avant* la notification Slack/Resend, garantissant que `mail_id` existe au moment de construire les liens.
- **Rétention logs** : `cleanup_old_logs` passe de 7 jours à **3 jours** (72h).
- **Healthcheck post-deploy** : `deploy-to-vps.sh` vérifie désormais `/health=200` et `/auth/login=200` après le build Docker. S'échoue avec les logs d'erreur si le cockpit ne revient pas dans les 60s.
- **Pré-filtre `prefilter.py`** : ajout de `is_service_email()` qui détecte les expéditeurs de services connus + keywords automatiques. Testé en PREMIER dans `quick_classify`.
- **Prompt classifier LLM** : définition plus stricte de `demande_client` (HUMAIN qui sollicite une ENQUÊTE/DEVIS/CONSULTATION, jamais un email automatique). Ajout de contre-exemples.

---

## [1.7.0] — 2026-05-15

### Ajouté
- **Charlie AI : synthèse en langage naturel** — quand l'opérateur demande un résumé, un détail ou le contenu d'un dossier/mail, Charlie exécute le SQL puis lance un second appel LLM pour produire une vraie synthèse au lieu d'afficher juste un tableau brut.
- **Détection automatique** des requêtes de type résumé (résume, synthèse, détail, contenu, que dit...) avec normalisation Unicode pour les accents français.

---

## [1.6.1] — 2026-05-15

### Ajouté
- **Colonne `body`** : stockage du contenu complet des emails dans la DB (en plus de `body_preview` tronqué à 2000 chars). Les nouveaux mails auront le contenu complet accessible par Charlie AI et la web UI.
- **Affichage complet dans Slack** : les champs `body`, `ai_draft`, `human_draft` s'affichent jusqu'à 800 chars dans les réponses Block Kit (au lieu de 60 chars).
- **ID cliquable dans les notifications Slack** : chaque notification de nouveau brouillon inclut l'ID du mail avec un lien vers la conversation dans le cockpit web.
- **Logs quotidiens** : fichiers `logs/agent-YYYY-MM-DD.log` en JSON structuré, avec rotation automatique (suppression après 7 jours). Variable `LOG_DIR` configurable.
- **Module `app/logging_config.py`** : configuration structlog avec sortie console + fichier journalier.
- **Prefilter `demande_client`** : règle rapide qui attrape les sujets contenant "demande", "filature", "surveillance", "investigation", etc. avant le LLM classifier — évite les mauvaises classifications (ex: "DEMANDE TEST / CDAL - Filature" classé facture).

### Modifié
- **Prompt Charlie** : `body` documenté dans le schéma, règle de déflexion supprimée — Charlie peut désormais afficher le contenu complet d'un mail.
- **Web UI** : la page conversation affiche `body` en priorité, `body_preview` en fallback.
- **IMAP poller** : `_persist()` stocke désormais `body` en plus de `body_preview`.
- **Docker Compose** : volume `./logs:/app/logs` monté, variable `LOG_DIR=/app/logs`.
- **Priorité `demande_client`** : toujours HIGH — c'est du business vital pour Detective.be.

### Corrigé
- **IMAP flag critique** : le flag `$AgentProcessed` était rejeté par le serveur IMAP d'Infomaniak (erreur BAD), ce qui bloquait **tout** le pipeline email. Remplacé par `AgentProcessed` (sans `$`). C'est ce bug qui empêchait les mails d'être détectés.
- **Slack Bot route** : l'import `from ... import slack_handler` capturait `None` au chargement du module au lieu de lire la valeur à l'exécution. Corrigé en important le module (`slack_bot_module.slack_handler`).

---

## [1.6.0] — 2026-05-15

### Ajouté
- **Slack Bot Charlie AI** : interrogation de la DB directement depuis le canal #detective via l'app Slack "Charlie Detective". Le bot répond aux @mentions et aux DM en utilisant le même pipeline Charlie AI que le cockpit web (question → LLM → SQL → exécution → réponse Block Kit).
- **Module `app/charlie.py`** : logique partagée du pipeline Charlie AI (prompt système, parsing SQL, validation sécurité, exécution, `CharlieResult` dataclass) — refactorisé depuis `app/web/api.py` pour réutilisation par le bot Slack.
- **Module `app/delivery/slack_bot.py`** : handler Slack Bolt async (HTTP mode intégré à FastAPI sur `/slack/events`), avec rate limit (10 req/min/user) et réaction :eyes: comme accusé de réception.
- **Variables d'environnement** : `SLACK_BOT_TOKEN` (xoxb-...), `SLACK_SIGNING_SECRET`.

### Modifié
- **Refactor `app/web/api.py`** : l'endpoint Charlie AI web utilise désormais `app.charlie.ask_charlie()` au lieu de fonctions inline. Le formatage HTML reste spécifique au web.

---

## [1.5.3] — 2026-05-15

### Ajouté
- **Chat AI Charlie** dans l'inbox : bouton flottant violet qui ouvre un panneau de chat. L'opérateur pose des questions en langage naturel sur les emails en base (ex: "recherche les factures depuis le 05/05/26"). Charlie génère du SQL SELECT, l'exécute en read-only et retourne la réponse sous forme de tableau + texte.
- **Badge version** (`v1.5.3`) affiché dans le coin inférieur gauche de toutes les pages du cockpit — source unique de vérité : `pyproject.toml`.
- **Liens cliquables dans les résultats Charlie** : les colonnes `id` et `subject` des tableaux SQL sont des liens `<a href="/app/conversation/{id}" target="_blank">` qui ouvrent la conversation dans un nouvel onglet sans fermer le chat.
- **Redimensionnement manuel** de la fenêtre Charlie : handle visible en bas à droite (curseur ↘) qui permet d'agrandir/réduire la fenêtre avec la souris.
- **Abréviations boîtes** dans les résultats Charlie : `detective_belgique` → D_FR, `detective_belgium` → D_NL, `dpdh_investigations` → D_PD.
- **Guard anti-double-submit** sur le bouton Envoyer de Charlie.

### Modifié
- **Chat Charlie : soumission via `fetch()` pur + Alpine.js** au lieu de HTMX (`hx-post`) : résout le bug où Enter et le bouton Envoyer ne déclenchaient aucune action dans le modal caché par `x-show`.
- **Source unique de vérité pour la version** : `pyproject.toml` → `app/__init__.py` lit dynamiquement via `importlib.metadata`.
- **Prompt Charlie enrichi** : règles pour inclure `id`+`subject` dans les SELECT, et expliquer que `body_preview` est tronqué.
- **Script de déploiement** : pre-flight checks systématiques (branche main, pas de modifs non commitées, push auto des commits locaux).

---

## [1.1.8] — 2026-05-15

### Ajouté
- **Zone de recherche texte** dans la barre de filtres de l'inbox : filtre sur sujet, expéditeur et contenu du mail (`body_preview`). Recherche via `LOWER() LIKE` SQL (insensible à la casse : `dutest` trouve aussi `DUTEST`).

---

## [1.1.7] — 2026-05-15

### Ajouté
- **Dropdowns Statut & Priorité** dans la barre de filtres de l'inbox : à côté des checkboxes boîtes, avec changement immédiat via HTMX.

---

## [1.1.6] — 2026-05-15

### Ajouté
- **Tab "Urgent" en premier** dans l'inbox : filtre sur la priorité `high` (et non plus la catégorie `urgent`).
- **Tabs Phishing et Rappels** ajoutés au bandeau de catégories.
- **Filtre boîtes par checkboxes** : 3 cases à cocher côte à côte (Detective Belgique, Detective Belgium, DPDH) avec multi-sélection supportée en backend (`IN` clause SQL).

### Modifié
- **Suppression des dropdowns filtres** (catégorie, statut, priorité) : remplacés par les tabs et l'édition inline dans les lignes.
- **Backend** : `_fetch_mails`, `_fetch_mails_partial` et `_fetch_counts` supportent désormais une liste de boîtes (`mailbox_names`).

---

## [1.1.5] — 2026-05-15

### Ajouté
- **Édition inline de la catégorie** dans le listing inbox : dropdown HTMX identique à statut/priorité.
- **Bouton "Réinitialiser"** dans la barre de filtres de l'inbox : efface tous les filtres et recharge la liste complète.

### Modifié
- **Inbox : suppression de la colonne Action** (redondante car le sujet est déjà un lien vers la conversation).

---

## [1.1.4] — 2026-05-15

### Ajouté
- **Édition inline statut & priorité** dans le Cockpit : dropdowns HTMX dans la vue conversation (`/app/conversation`) et dans le listing inbox (`/app/inbox`). Changement immédiat sans rechargement de page.

---

## [1.1.3] — 2026-05-15

### Corrigé
- **Faux positif phishing** sur les formulaires de contact : `is_phishing()` exclut désormais les mails avec sujet "Nouveau Message De...", "Contact Form", etc. et les mails auto-générés par les propres domaines (detectivebelgique.be, detectivebelgium.com, dpdhuinvestigations.be).
- **Invitations calendrier** : nouvelle fonction `is_autre()` qui classe automatiquement les "updated invitation", "calendar", "ical", etc. comme `autre` avant qu'ils ne passent par le classifier.

## [1.1.2] — 2026-05-15

### Ajouté
- **Filtre date IMAP** (`PROCESS_SINCE_DATE`) : Charlie ne traite que les mails reçus depuis une date configurable (ex: `2026-05-01`). Évite la retraitement massif de l'historique.

## [1.1.1] — 2026-05-15

### Modifié
- **Poller IMAP** : traite maintenant **tous les emails** (lus et non lus), pas seulement `UNSEEN`. Le flag `AgentProcessed` évite les doublons.

## [1.1.0] — 2026-05-15

### Ajouté
- **8 catégories de classification** (au lieu de 6) : `phishing` (menace sécurité), `rappel` (relance/échéance/rdv)
- **Priorité intelligente** (`app/pipeline/priority.py`) : demande client = `high` (business vital)
- **Pré-filtre renforcé** (`app/pipeline/prefilter.py`) :
  - Détection phishing par spoofing Reply-To, headers suspects, mots-clés menaces, pièces jointes dangereuses (`.exe`, `.zip`)
  - Détection rappel par keywords (échéance, impayé, relance, convocation, deadline)
  - Facture enrichie (fournisseurs connus : OVH, Infomaniak, Stripe, etc.)
  - Détection demande client (demande, filature, surveillance, investigation)
- **Prompt classifier avec few-shots** : 8 exemples (un par catégorie), règles de décision précises, règle d'or de hiérarchie
- **Cockpit web** mis à jour avec les nouvelles catégories `phishing` et `rappel` dans les filtres

### Modifié
- **Poller IMAP** : traite maintenant **tous les emails** (lus et non lus), pas seulement `UNSEEN`. Le flag `AgentProcessed` évite les doublons.

### Corrigé
- Docker Compose `env_file` remplacé par volume mount direct (`.env.production:/app/.env`) pour éviter l'interpolation des `$` dans les mots de passe
- Magic link URL utilise `PUBLIC_BASE_URL` au lieu de `WEB_BIND_HOST:WEB_BIND_PORT`
- Redirection racine `/` → `/auth/login`

---

## [1.0.0] — 2026-05-15

### Ajouté
- **Déploiement production** sur VPS Hostinger KVM8
  - `Dockerfile` Python 3.11 slim
  - `docker-compose.yml` avec labels Traefik (`detective.digitalhs.biz`)
  - `.dockerignore` pour build rapide
  - `scripts/deploy-to-vps.sh` : workflow `git pull` → sync data/.env → `docker compose up -d --build`
- **Cockpit web FastAPI** :
  - Auth magic link par email (Resend)
  - Inbox filtrable/sortable avec filtres par boîte, catégorie, statut, priorité
  - Conversation détaillée avec génération inline de brouillon IA
  - Dashboard admin (stats, settings LLM, audit log)
  - API HTMX pour les actions (save/generate/approve/reject)
- **Fixes pré-déploiement** : import `RedirectResponse`, `PUBLIC_BASE_URL`

---

## [0.2.0] — 2026-05-14

### Ajouté
- **Cockpit conversation** : génération inline de brouillon, auto-save on approve
- **Layout conversation** : panneau collapsible, scroll, séparation IA/opérateur
- **Inbox sortable** : tri par sujet, expéditeur, catégorie, statut, priorité, date
- **UI improvements** : role badge, admin nav, settings warning
- **Contenu mail complet** dans la conversation (body preview)

---

## [0.1.0] — 2026-05-13

### Ajouté
- MVP initial — pipeline IMAP + classification + RAG + génération
- 3 boîtes Infomaniak pollées toutes les 5 min
- Classification catégories (`demande_client`, `facture`, `newsletter`, `spam`, `urgent`, `autre`)
- RAG sur 2042 paires Q/R historiques (`sqlite-vec` + `multilingual-e5-large`)
- Génération brouillon style Daniel Hurchon, multilingue FR/NL/EN
- Livraison brouillon via Resend API → `cdal@digitalhs.biz`
- Notifications Slack (webhook) pour les nouveaux brouillons
- Flag IMAP `AgentProcessed` pour idempotence
- Healthcheck FastAPI sur `127.0.0.1:8765`
- Bootstrap embeddings + extraction personnalité