# ROADMAP — Detective.be Agent

> Tenir cet état à jour. Cocher les cases au fur et à mesure. Quand une phase est complète, proposer à CDAL de passer à la suivante.

---

## ✅ Phase 0 — Brainstorm & cadrage (TERMINÉ 2026-05-13)

- [x] Spec technique figée (`docs/SPEC.md`)
- [x] Choix LLM : Kimi K2 via Ollama Pro + LiteLLM router
- [x] Choix vector store : sqlite-vec
- [x] Choix livraison MVP : Resend → cdal@digitalhs.biz
- [x] Scaffolding code complet en place

---

## ✅ S1 — Infra & data (TERMINÉ)

**Objectif** : environnement local opérationnel + 1200 paires indexées + guide de style Daniel généré.

### Pré-requis bloquants (côté CDAL)
- [x] Déposer `boite1.sqlite`, `boite2.sqlite`, `boite3.sqlite` dans `data/`
- [x] Partager le schéma de chaque DB (`sqlite3 data/boiteX.sqlite ".schema"`)
- [x] Remplir `.env` :
  - [x] 3 `MAILBOX_*_APP_PASSWORD` (Infomaniak)
  - [x] `OLLAMA_PRO_API_KEY` (optionnel — fallback OpenRouter actif)
  - [x] `RESEND_API_KEY` + domaine `noreply@resend.digitalhs.biz` vérifié
  - [x] `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` (test : compte CDAL ; prod : compte Daniel)

### Tâches code
- [x] Créer le venv et installer les deps (fasttext en attente, fallback lang detect)
  ```bash
  python3 -m venv venv && source venv/bin/activate && pip install -e ".[dev]"
  ```
- [x] Smoke test LLM : OK via OpenRouter (Claude Sonnet 4 — réponse "pong")
- [x] Smoke test embeddings : charger `multilingual-e5-large`, encoder une phrase, vérifier dimension (dim=1024)
- [x] Smoke test sqlite-vec : créer une DB jouet, indexer 5 vecteurs, retrieve top-1
- [x] **Adapter `scripts/bootstrap_embeddings.py`** au schéma réel des DB (matching sujet+date, nettoyage citations)
- [x] **Adapter `scripts/extract_personality.py`** au schéma réel (sampling depuis `sent_emails`, nettoyage citations)
- [x] Exécuter `python -m scripts.bootstrap_embeddings` → 2042 paires indexées (1971 boite1, 71 boite2, 0 boite3)
- [x] Exécuter `python -m scripts.extract_personality` → guide généré dans `app/prompts/personality_daniel.txt` (relu et validé)

### Livrable S1
Les 3 DB indexées avec embeddings, le guide de style validé, l'environnement local prêt à coder le pipeline.

---

## ✅ S2 — Pipeline ingestion IMAP (TERMINÉ)

**Objectif** : worker IMAP fonctionnel qui détecte les nouveaux mails, applique le pré-filtre + classification, et flag les mails traités.

### Tâches
- [x] Implémenter `app/workers/imap_poller.py::_poll_once` :
  - Connexion IMAPS via `aioimaplib` (login app password)
  - SELECT INBOX, SEARCH `UNSEEN UNKEYWORD $AgentProcessed`
  - FETCH RFC822, parser via `email` stdlib
  - Pour chaque mail : appeler le pipeline (prefilter → classifier)
  - STORE `+FLAGS $AgentProcessed`
- [x] Mode `--dry-run` : ne pas poser le flag, juste logger ce qui serait fait
- [x] Reconnexion automatique si IMAP timeout (retry 3x avec backoff)
- [x] Test sur 1 boîte d'abord (dev), puis les 3
- [x] Persister les classifications dans `agent_state.db` (table `mail_processed`)
- [ ] Tests unitaires : mock IMAP, vérifier le filtrage sur 5 mails-fixtures *(reporté V2)*

### Livrable S2
3 boîtes pollées toutes les 5 min, mails classés et taggés `$AgentProcessed`. Pipeline stable en local. Newsletter digest branché sur Slack.

---

## 🚧 S3 — Cœur intelligent : RAG + génération (EN COURS — MVP fonctionnel)

**Objectif** : pour chaque `demande_client` détecté, générer un brouillon de qualité et l'envoyer à CDAL via Resend.

### Tâches
- [x] Brancher `pipeline.language.detect_language` sur les mails entrants
- [x] Brancher `pipeline.rag.retrieve` (déjà codé, validé sur vraie DB — 2042 paires)
- [x] Brancher `pipeline.generator.generate_draft` (déjà codé, validé end-to-end)
- [x] Brancher `delivery.resend_notifier.notify_draft` après génération
- [ ] Calibration qualité sur 50 mails réels, ajustements prompts *(à faire avec Daniel)*
- [x] Vérifier multilingue : FR validé, NL validé, EN validé — détection via `langdetect` (remplace `fasttext` qui ne build pas sur Mac ARM)
- [x] Vérifier signatures par marque : boîte 1 validée, boîtes 2 et 3 en attente
- [ ] Tests d'intégration automatisés *(reporté V2)*

### Canal Slack Boss ↔ Charlie (remplace Telegram — MVP)
- [x] Webhook Slack configuré dans `.env`
- [x] Module `app/delivery/slack_notifier.py`
- [x] Notification push quand un nouveau brouillon est généré (métadonnées + référence email)
- [x] Newsletter digest quotidien sur Slack
- [x] **Slack Bot Charlie AI interactif** — @mention ou DM sur #detective, même pipeline que le cockpit web (module `app/delivery/slack_bot.py`, route `/slack/events`)
- [ ] Approbation/rejet depuis Slack *(V2 — nécessite Slack App interactive avec Block Kit buttons)*

### Livrable S3
CDAL reçoit un email Resend formaté + une notification Slack pour chaque demande client. L'agent tourne en local sur 3 boîtes. Qualité à valider avec Daniel sur vrais cas.

---

## ⬜ S4 — Production sur KVM8 + supervision

**Objectif** : agent déployé sur le VPS, tournant 24/7 avec supervision et backups.

### Tâches
- [x] Setup KVM8 : Docker + Docker Compose, structure `/opt/DETECTIVE/`
- [x] Copier le code et build l'image
- [x] `.env` prod synchronisé via `scripts/deploy-to-vps.sh`
- [x] Healthcheck FastAPI sur `127.0.0.1:8765`
- [x] Bot Slack Charlie AI interactif déployé et fonctionnel
- [ ] Bot Telegram — canal **alertes système** : brancher `healthcheck` + erreurs critiques
- [ ] Bot Telegram — canal **conversation Boss ↔ Charlie** : migrer le bot test vers le compte Daniel
- [ ] Cron quotidien de backup → Backblaze B2 (les 4 SQLite, chiffrés via `age`)
- [ ] Procédure de restore documentée et testée
- [ ] Documentation opérationnelle (procédure restart, restore, ajout boîte mail)
- [ ] Lancement officiel + monitoring 1 semaine

### Livrable S4
MVP en production, monitoring actif, CDAL reçoit les brouillons à mesure que les vrais mails arrivent. Daniel peut interagir avec Charlie via Slack en direct.

---

## ⬜ V2 — Bascule Drafts IMAP + feedback loop

**Pré-requis** : MVP stable depuis ≥ 2 semaines avec qualité validée par CDAL/Daniel.

- [ ] Module `delivery/imap_drafts.py` : `IMAP APPEND` du brouillon dans `Drafts` de la boîte d'origine
- [ ] Switch config `DELIVERY_MODE=resend|imap_drafts`
- [ ] Capture feedback : détection des mails envoyés depuis `Sent`, diff avec brouillon stocké, persistance dans `agent_state.db`
- [ ] Tableau de bord léger : taux d'acceptation, distance moyenne édit, top éditions par catégorie

---

## ⬜ V3 — Extensions

- [ ] Module factures : extraction montant/échéance/fournisseur, création tâche comptable
- [ ] Bot WhatsApp client (Twilio ou WhatsApp Business API) — réutilise pipeline RAG
- [ ] Dashboard web supervision (FastAPI + HTMX, accessible via SSH tunnel ou réseau privé)
- [ ] Suppression mails > 28 jours (politique de rétention)
- [ ] Architecture multi-sub-agents : router orchestrateur qui dispatch par tâche, chaque agent sa config LLM
- [ ] **Pipeline Cerveau2 — ingestion continue** : alimenter Cerveau2 en temps réel depuis IMAP (v1.9.4 lancé, à stabiliser)
- [ ] **Charlie AI temps réel** : court-circuiter le LLM pour 80% des requêtes (SQL programmatique) ou basculer vers Claude Sonnet 4 via OpenRouter pour fiabilité maximale

---

## 📝 Notes de session 2026-05-19 (v1.9.6)

**Problèmes résolus** :
- Charlie répondait "zéro" alors que des dossiers existaient dans les archives → garde archives débloquée
- Faux dossier_id ("entreprise", "infidelite") → regex stricte
- Fuite de données (sender, body_preview visibles) → `_sanitize_rows_for_prompt()`
- Dump technique visible dans Slack → supprimé
- SQL trop permissif (LIKE OR attrapait des factures) → Mode A (category exacte) vs Mode B (LIKE OR)
- Latence ~35s → parallélisation + timeout réduit → ~5-13s

**Décisions en attente** :
- Basculer le LLM principal vers Claude Sonnet 4 via OpenRouter (coût ~0.003€/req, fiabilité 99%) ou rester sur Gemma4:31b local
- Implémenter le SQL programmatique pour court-circuiter le LLM sur les requêtes standard

**Prochaine session** : choix LLM + stabilisation Cerveau2 pipeline + tests terrain avec Daniel
