# Changelog — Detective.be Agent

> Format : [Keep a Changelog](https://keepachangelog.com/fr/1.0.0/)

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