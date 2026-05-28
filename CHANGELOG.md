# Changelog Charlie AI — Detective.be

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
