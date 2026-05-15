# Changelog — Detective.be Agent

> Format : [Keep a Changelog](https://keepachangelog.com/fr/1.0.0/)

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
- **Poller IMAP** : traite maintenant **tous les emails** (lus et non lus), pas seulement `UNSEEN`. Le flag `$AgentProcessed` évite les doublons.

## [1.1.0] — 2026-05-15

### Ajouté
- **8 catégories de classification** (au lieu de 6) : `phishing` (menace sécurité), `rappel` (relance/échéance/rdv)
- **Priorité intelligente** (`app/pipeline/priority.py`) : demande client chaude (formulaire, ton insistant) = `high`
- **Pré-filtre renforcé** (`app/pipeline/prefilter.py`) :
  - Détection phishing par spoofing Reply-To, headers suspects, mots-clés menaces, pièces jointes dangereuses (`.exe`, `.zip`)
  - Détection rappel par keywords (échéance, impayé, relance, convocation, deadline)
  - Facture enrichie (fournisseurs connus : OVH, Infomaniak, Stripe, etc.)
- **Prompt classifier avec few-shots** : 8 exemples (un par catégorie), règles de décision précises, règle d'or de hiérarchie
- **Cockpit web** mis à jour avec les nouvelles catégories `phishing` et `rappel` dans les filtres

### Modifié
- **Poller IMAP** : traite maintenant **tous les emails** (lus et non lus), pas seulement `UNSEEN`. Le flag `$AgentProcessed` évite les doublons.

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
- Classification 6 catégories (`demande_client`, `facture`, `newsletter`, `spam`, `urgent`, `autre`)
- RAG sur 2042 paires Q/R historiques (`sqlite-vec` + `multilingual-e5-large`)
- Génération brouillon style Daniel Hurchon, multilingue FR/NL/EN
- Livraison brouillon via Resend API → `cdal@digitalhs.biz`
- Notifications Slack (webhook) pour les nouveaux brouillons
- Flag IMAP `$AgentProcessed` pour idempotence
- Healthcheck FastAPI sur `127.0.0.1:8765`
- Bootstrap embeddings + extraction personnalité
