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

## ✅ S3 — Cœur intelligent : RAG + génération (TERMINÉ)

**Objectif** : pour chaque `demande_client` détecté, générer un brouillon de qualité et l'envoyer à CDAL via Resend.

### Tâches
- [x] Brancher `pipeline.language.detect_language` sur les mails entrants
- [x] Brancher `pipeline.rag.retrieve` (déjà codé, validé sur vraie DB — 2042 paires)
- [x] Brancher `pipeline.generator.generate_draft` (déjà codé, validé end-to-end)
- [x] Brancher `delivery.resend_notifier.notify_draft` après génération
- [ ] Calibration qualité sur 50 mails réels, ajustements prompts *(en cours avec Daniel — sprint V2a)*
- [x] Vérifier multilingue : FR validé, NL validé, EN validé — détection via `langdetect`
- [x] Vérifier signatures par marque : boîte 1 validée, boîtes 2 et 3 en attente
- [ ] Tests d'intégration automatisés *(reporté V2)*

### Canal Slack Boss ↔ Charlie
- [x] Webhook Slack + module `app/delivery/slack_notifier.py`
- [x] Notification push à chaque nouveau brouillon (métadonnées + lien cockpit)
- [x] Newsletter digest quotidien sur Slack
- [x] **Slack Bot Charlie AI interactif** — @mention ou DM sur #detective

### Livrable S3 ✅
MVP opérationnel sur VPS. CDAL reçoit brouillons via Resend + notification Slack. Cockpit web accessible via `detective.digitalhs.biz`. Daniel interagit avec Charlie via Slack.

---

## ✅ S4 — Production sur KVM8 + supervision (TERMINÉ)

**Objectif** : agent déployé sur le VPS, tournant 24/7 avec supervision et backups.

### Tâches
- [x] Setup KVM8 : Docker + Docker Compose, structure `/opt/DETECTIVE/`
- [x] Build image + déploiement continu via `git pull + docker restart`
- [x] `.env.production` synchronisé sur VPS
- [x] Healthcheck FastAPI sur `:8765`
- [x] Bot Slack Charlie AI interactif déployé et fonctionnel
- [x] Cockpit web (inbox, conversation, chat AI, admin) — Traefik + TLS
- [x] Logs JSON structurés, rotation 7j
- [x] Backup nightly Cerveau2 vault via cron `0 1 * * *`
- [ ] Bot Telegram alertes système *(dépriorisé — Slack suffisant)*
- [ ] Backup SQLite → Backblaze B2 chiffré *(V2 nice-to-have)*
- [ ] Procédure de restore documentée *(à faire avant V2)*

### Livrable S4 ✅
MVP en production 24/7. Daniel interagit avec Charlie via Slack + cockpit web.

---

## 🔥 V2a — Bascule Drafts IMAP + boucle approbation Daniel (SPRINT LUNDI 2026-05-25)

**Contexte** : Accord du 2026-05-22. Daniel commence lundi à approuver les brouillons directement depuis sa boîte mail. Resend reste pour les alertes système uniquement.

### Objectif
Remplacer la livraison Resend (brouillon → CDAL par email) par un dépôt direct en **Brouillons IMAP** de la boîte qui a reçu le mail client. Daniel lit, édite si besoin, et envoie lui-même. Il donne du feedback par email séparé ou forward si correction nécessaire.

### Spécification technique

**Format du brouillon IMAP** :
- **De** : adresse de la boîte source (ex: `contact@detectivebelgique.be`)
- **À** : adresse du client (expéditeur du mail entrant)
- **Sujet** : `DEMANDE D'Approbation - Reponse Demande Client : [sujet original]`
- **Corps** : brouillon généré par Charlie (texte brut), précédé d'un bandeau contextuel :
  ```
  ⚠️ BROUILLON IA — À RELIRE AVANT ENVOI
  Dossier cockpit : https://detective.digitalhs.biz/app/conversation/{mail_id}
  ────────────────────────────────────────
  [texte du brouillon]
  ```
- **Flag IMAP** : `\Draft`
- **Dossier cible** : `Drafts` (fallback : `INBOX.Drafts` → `Brouillons` → premier dossier contenant "draft" ou "brouillon" insensible à la casse)

**Module à créer** : `app/delivery/imap_draft.py`
```python
async def append_draft(
    incoming: IncomingMail,
    mailbox: MailboxConfig,
    gen: GenerationResult,
    mail_id: int | None,
    settings: Settings,
) -> bool:
    """Dépose le brouillon dans les Drafts IMAP de la boîte source.
    Retourne True si succès, False si échec (fallback Resend activé)."""
```

Logique interne :
1. Construire le message RFC 2822 via `email.message.EmailMessage` (text/plain)
2. Ouvrir connexion `aioimaplib.IMAP4_SSL(host, port)`
3. `login(user, app_password)`
4. `LIST "" "*"` pour trouver le dossier Drafts
5. `APPEND mailbox_name (\Draft) {date} {message_bytes}`
6. `logout()`

**Modification `imap_poller.py`** (ligne ~845) :
```python
# Ancien :
await notify_draft(incoming, mailbox, gen, mail_id=mail_id)

# Nouveau :
draft_ok = await append_draft(incoming, mailbox, gen, mail_id, settings)
if not draft_ok:
    # Fallback Resend si IMAP APPEND échoue
    await notify_draft(incoming, mailbox, gen, mail_id=mail_id)
```

**Config `.env`** : aucun nouveau paramètre requis — la boîte source a déjà `user` + `app_password`.

### Tâches lundi

- [x] **1. Créer `app/delivery/imap_draft.py`** — fonction `append_draft()` avec :
  - Construction email RFC 2822 (`email.message`, text/plain + UTF-8)
  - Découverte auto du dossier Drafts via `LIST`
  - IMAP APPEND avec flag `\Draft`
  - Logging structuré (`imap_draft.ok`, `imap_draft.failed`, `imap_draft.folder_found`)
  - Timeout 15s, propagation propre des exceptions

- [x] **2. Modifier `app/workers/imap_poller.py`** :
  - Remplacer l'appel `notify_draft()` par `append_draft()` + fallback Resend si échec
  - Garder import `notify_draft` (utilisé pour les alertes système)

- [x] **3. Test sur boîte de dev** :
  - ~~Test dev~~ → bugs découverts sur email 121 en production (sujet faux, fallback inconditionnel, corps pollué)
  - Corrigés en v1.16.14 et redéployés

- [x] **4. Bump version** : `1.15.1 → 1.16.0` (changement livraison = minor bump)

- [x] **5. Déployer sur VPS** et surveiller les logs du premier vrai mail

### Resend — rôle post-V2a
Resend reste actif **uniquement** pour :
- Alertes système (disque VPS > 75%, erreurs critiques)
- Fallback si IMAP APPEND échoue
- N'envoie plus de brouillons de réponse client

### Points d'attention
- Infomaniak peut nommer le dossier Drafts différemment selon la locale du compte (`Brouillons` en FR, `Drafts` en EN) → découverte dynamique obligatoire via `LIST`
- Le flag IMAP `\Draft` est standard RFC 3501 — testé sur Infomaniak ?
- Si la connexion IMAP Append échoue (timeout, auth), le fallback Resend garantit qu'aucun brouillon n'est perdu

---

## ✅ Hotfixes v1.24.x — Hardening détection (meeting Daniel 2026-06-22)

**Contexte** : meeting Daniel 2026-06-22 remonte 3 clients réels ratés (#515, #606, #614) — tous partagent le même défaut : le classifier se fiait au **sujet** alors que le **body** contenait une vraie demande client.

- [x] **v1.24.0 — 3 règles déterministes** où le body l'emporte sur le sujet :
  - [x] `_is_wp_contact_form()` — formulaires WordPress toutes boîtes (detectivebelgium.com NL, detectivebelgique.be FR), force `demande_client` depuis toute catégorie
  - [x] `_is_reply_to_daniel()` — Re: + citation signée Daniel + expéditeur humain
  - [x] `_has_strong_human_demand()` — exception au « jamais remonter depuis phishing » (prénom signé + vocabulaire enquête + question tarif, sans marqueur phishing actif)
  - [x] 36 tests hardening + 123 suite complète verts
- [x] **v1.24.0 — reclassement prod** : #515 (Nathalie Hairemans) + #606 (Van Houtte) reclassés `facture` → `demande_client`, brouillons générés + livrés en IMAP Drafts
- [x] **v1.24.1 — brouillon hors-légalité** : `_detect_illegal_request()` (11 regex FR/NL/EN) + `_build_illegal_refusal_draft()` = refus poli (cadre légal belge + infractions pénales) + alternative légale (filature/surveillance/constat). Pour #614 (Serge M / piratage WhatsApp). 14 tests + 137 suite verte.
- [ ] **v1.24.1 — reclassement prod #614** : `backfill_reclassify.py --apply --only-id 614` puis `deliver_pending_drafts.py --only-id 614 --apply`. **À valider avec CDAL** (le brouillon est un refus poli, à confronter au ton de Daniel) → démarrage dès reprise.
- [ ] **Task #4 — Extraction vrai contact client formulaires WP** : les formulaires WP ne demandent jamais l'email — le vrai contact = téléphone (`Telefoonnummer`). Le brouillon doit dire « je vous appelle au 04xx » plutôt que répondre par email au forwarder `mail@/wordpress@/contact@detective*`. Pour les mails directs : récupérer le vrai email client.

---

## ⬜ V2b — Polishing cockpit : latence Charlie + UX inbox

**Pré-requis** : V2a déployé et stable.

### Bug UI — Inbox : filtres boîtes mail non cochés par défaut
**Comportement actuel** : à l'ouverture de l'inbox, les cases de filtrage des 3 boîtes ne sont pas toutes cochées → l'inbox peut apparaître vide ou partiellement filtrée sans que Daniel l'ait voulu.

**Comportement attendu** :
- À l'ouverture, les **3 boîtes cochées par défaut**, filtre texte vide → affichage complet trié
- Daniel peut décocher 1 ou 2 boîtes pour isoler son périmètre
- L'état des filtres peut être mémorisé en `localStorage` pour la session

**Fichiers concernés** : `app/web/templates/inbox.html` + JavaScript de filtre côté client

**Fix** : s'assurer que les checkboxes sont `checked` par défaut dans le HTML et que le JS de filtrage applique l'état initial sans intervention utilisateur.

---

### Latence Charlie — cible < 5s (vs 5-13s actuel)
**Contexte** : la latence actuelle est acceptable mais perfectible. Causes principales :
1. Appel LLM pour génération de réponse (~3-8s selon deepseek-v4-pro)
2. Requête Cerveau2 + SQL en séquentiel

**Pistes d'amélioration** (à évaluer selon ROI) :

| Piste | Gain estimé | Complexité |
|---|---|---|
| Paralléliser SQL + Cerveau2 (`asyncio.gather`) | ~2-3s | Faible |
| SQL programmatique étendu (list, last, who) | bypass LLM ~80% requêtes simples | Moyen |
| Basculer LLM vers Claude Haiku 4.5 via OpenRouter | réponse ~1s, coût ~0.001€/req | Faible |
| Cache réponses fréquentes (TTL 5 min) | gain si questions répétées | Moyen |

**Approche recommandée lundi** :
- [ ] Paralléliser Cerveau2 + SQL dans `charlie.py` (`asyncio.gather`)
- [ ] Étendre le SQL programmatique aux questions de type "liste" et "dernier mail"
- [ ] Tester Claude Haiku 4.5 (`openrouter/anthropic/claude-haiku-4-5`) comme `llm_model_chat` → si qualité OK, adoption permanente

---

## ⬜ V2c — Feedback loop qualité Daniel

**Pré-requis** : V2a stable depuis ≥ 1 semaine, Daniel a approuvé ≥ 10 brouillons.

**Objectif** : apprendre des corrections de Daniel pour améliorer les prochains brouillons.

- [ ] Détecter les mails envoyés depuis `Sent` qui correspondent à un brouillon V2a (par sujet/Message-ID)
- [ ] Calculer la distance textuelle (diff) entre brouillon IA et version envoyée par Daniel
- [ ] Persister dans `agent_state.db` : taux d'acceptation, types d'éditions fréquentes
- [ ] Dashboard léger dans le cockpit : "Charlie — taux d'approbation cette semaine"
- [ ] Affiner le prompt `personality_daniel.txt` avec les patterns d'édition les plus fréquents

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

## 📝 Notes de session 2026-05-22 (v1.15.1)

**État prod au 22 mai** :
- Charlie v1.15.1 tourne sur VPS — 3 boîtes pollées, pipeline complet opérationnel
- Cerveau2 v0.4.6 — second cerveau alimenté en temps réel + 933 fiches contacts importées (batch extract terminé)
- Cockpit web accessible : `detective.digitalhs.biz` — inbox, conversation, chat Charlie, admin
- Backup nightly fonctionnel avec timestamp `.last_backup`

**Bugs résolus aujourd'hui** :
- Charlie timeout "Failed to fetch" → `context_only=True` élimine le double appel LLM Cerveau2
- "combien d'emails depuis le 20 mai ?" → SQL programmatique `_build_count_sql()` bypass deepseek-v4-pro (réponses SQL vides)
- Download PJ → logging path manquant + dossier `attachments/` créé au boot

**Décision clé** : bascule V2a IMAP Drafts lundi 2026-05-25 — Daniel approuve les brouillons depuis sa boîte mail directement.

**LLM chat actuel** : `openai/deepseek-v4-pro` (langage naturel OK, SQL generation vide — contourné par SQL programmatique)

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
