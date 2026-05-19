# SPEC — Agent IA email Detective.be (MVP)

> Document figé issu du brainstorm de cadrage 2026-05-13.
> Toute déviation doit être discutée avec CDAL.

---

## 1. Contexte

Daniel Hurchon (Detective.be) gère seul 3 boîtes mail Infomaniak correspondant à 3 marques (Detective Belgique FR, Detective Belgium EN/multi, DPDH Investigations). Volume : ~50 mails/jour entrants, mélangeant demandes clients, factures, newsletters, spam et urgences. Daniel répond à tout lui-même depuis Outlook ou Apple Mail, en FR/NL/EN selon la langue du client. Il perd un temps considérable et la qualité varie selon sa charge.

**Objectif MVP** : un agent qui poll les 3 boîtes toutes les 5 min, classifie les mails entrants, et **uniquement pour les demandes clients** génère un brouillon de réponse "à la Daniel" en exploitant un corpus de 1200 paires Q/R historiques déjà anonymisées dans 3 DB SQLite. **Au MVP, les brouillons sont livrés par email à `cdal@digitalhs.biz`** (CDAL, l'intégrateur) qui valide la qualité avant transfert à Daniel. La bascule vers dépôt direct dans Drafts IMAP de Daniel se fera en V2.

**Canal Telegram Boss ↔ Charlie** : en parallèle du pipeline email, Charlie (l'agent IA) dispose d'un canal Telegram direct avec Daniel. Daniel peut interagir avec Charlie en direct (résumés, validations, questions rapides). En phase test, le bot est connecté au compte Telegram de CDAL ; en prod, il sera migré sur le VPS Hostinger.

---

## 2. Scope

### Inclus dans le MVP

- Polling IMAP des 3 boîtes Infomaniak (toutes les 5 min, hybride pré-filtre règles + LLM)
- Classification 6 catégories : `demande_client` / `facture` / `newsletter` / `spam` / `urgent` / `autre`
- Génération de brouillon **uniquement** pour `demande_client`
- RAG sur les 1200 paires Q/R anonymisées (`sqlite-vec` + `multilingual-e5-large`)
- Multilingue FR/NL/EN avec détection langue + réponse dans la langue du client
- Style "Daniel Hurchon" via system prompt personnalité + few-shot RAG
- Livraison brouillon via **Resend API → `cdal@digitalhs.biz`**
- Flag IMAP `$AgentProcessed` sur le mail entrant pour idempotence
- Supervision 24/7 : systemd auto-restart + healthcheck HTTP local + alertes Telegram
- Backup quotidien des DB SQLite

### Hors MVP (V2 ou plus tard)

- Dépôt direct dans Drafts IMAP de Daniel
- Feedback loop (capture diff brouillon généré vs version envoyée)
- Module factures, newsletters, automatisations supplémentaires
- Dashboard web de supervision
- Bot WhatsApp client
- Suppression mails > 28 jours
- Multi-utilisateurs

---

## 3. Architecture cible

```
[3 boîtes Infomaniak IMAP via app passwords]
         ↓ polling 5 min (aioimaplib)
[Worker Python asyncio, 1 task par boîte]
         ↓ nouveau mail non-flaggé $AgentProcessed
[Pipeline]
  1. Pré-filtre règles  → headers (List-Unsubscribe), expéditeurs
                          connus → tag IMAP + skip
  2. Classification LLM → 6 catégories (Kimi K2 via LiteLLM)
  3. Si != demande_client → tag IMAP catégorie + skip
  4. Si demande_client :
     a. Détection langue (fasttext FR/NL/EN)
     b. Embedding du mail (multilingual-e5-large local CPU)
     c. RAG → top 5 paires Q/R similaires (sqlite-vec)
     d. Génération brouillon (LiteLLM → Kimi K2 par défaut, OpenRouter fallback)
        Prompt : system "personnalité Daniel" + few-shot RAG + mail entrant
         ↓
[Resend API] → email formaté à cdal@digitalhs.biz
  Contient : mail original, brouillon proposé, métadonnées
  (boîte source, langue détectée, paires RAG utilisées)
         ↓
[IMAP STORE] flag $AgentProcessed sur le mail entrant
         ↓
[CDAL valide → forward à Daniel pour envoi]

[Canal parallèle : Telegram Boss ↔ Charlie]
Bot Telegram (python-telegram-bot / aiogram)
  - Commandes : /status, /resume, /approve, /reject, /ask
  - Notifications push : nouveau brouillon généré (résumé + lien)
  - Réponses conversationnelles : RAG + style Daniel pour questions directes
  - Phase test : compte CDAL  |  Phase prod : compte Daniel sur KVM8
```

---

## 4. Stack technique

| Couche | Choix | Justification |
|---|---|---|
| Runtime | Python 3.11+ | écosystème mail/RAG/LLM riche |
| Concurrence | asyncio (1 task par boîte) | suffisant pour 50 mails/jour |
| IMAP | `aioimaplib` | async, gère reconnexions |
| LLM router | **LiteLLM** (proxy OpenAI-compat) | switch facile Kimi K2 / OpenRouter, future-ready sub-agents |
| LLM principal | **Kimi K2 via Ollama Pro** (20€/mois fixe) | qualité top, coût plafonné |
| LLM fallback / spécialisation | **OpenRouter** (Claude, GPT-4o, etc. à la demande) | flexibilité par tâche |
| Embeddings | `intfloat/multilingual-e5-large` (sentence-transformers) | gratuit, local CPU, excellent FR/NL/EN |
| Vector store | **sqlite-vec** (extension SQLite) | vit dans les DB existantes, zéro service ajouté |
| Détection langue | `fasttext` (lid.176.bin) | local, instantané, FR/NL/EN parfait |
| Email outbound MVP | **Resend API** | simple, fiable, free tier suffisant |
| État/queue/logs | 4ème SQLite `agent_state.db` | pas besoin de Redis |
| Secrets | `.env` chmod 600 | suffisant single-tenant |
| Service | systemd unit (auto-restart) | natif Linux, pas besoin de Docker au MVP |
| Healthcheck | endpoint HTTP localhost (FastAPI minimal) | sondé par systemd timer |
| Alertes | bot Telegram (down, IMAP timeout, taux erreur > seuil) | gratuit, push immédiat |
| Backup | cron quotidien → Backblaze B2 ou Hetzner Storage Box | restore-tested |
| Logs | `journalctl` + rotation | natif systemd |

---

## 5. Layout filesystem (cible prod KVM8)

```
/opt/detective-agent/
├── app/
│   ├── main.py                # entrypoint asyncio
│   ├── workers/imap_poller.py # 1 task par boîte
│   ├── pipeline/
│   │   ├── prefilter.py
│   │   ├── classifier.py
│   │   ├── language.py
│   │   ├── rag.py
│   │   └── generator.py
│   ├── delivery/resend_notifier.py
│   ├── llm/router.py
│   ├── healthcheck.py
│   └── prompts/
│       ├── personality_daniel.txt
│       └── classifier_prompt.txt
├── data/
│   ├── boite1.sqlite          # DB existantes (anonymisées)
│   ├── boite2.sqlite
│   ├── boite3.sqlite
│   └── agent_state.db         # nouveau : queue, logs, télémétrie
├── scripts/
│   ├── bootstrap_embeddings.py
│   ├── extract_personality.py
│   └── deploy.sh
├── .env                        # secrets, chmod 600
├── venv/
├── logs/
└── deploy/
    ├── detective-agent.service
    └── detective-agent-healthcheck.timer
```

En dev local (Mac CDAL), le code vit dans `/Users/cdal/DEV_APP_CLAUDE/DETECTIVE_BE/` avec la même structure relative.

---

## 6. Cœur intelligent — RAG + Style Daniel

### Bootstrap one-shot (`scripts/bootstrap_embeddings.py`)
- Lecture des 1200 paires `[entrant] → [réponse Daniel]` depuis les 3 DB
- Calcul embedding du mail entrant via `e5-large` (préfixe `passage:`)
- Stockage dans table `pairs_vec` (sqlite-vec) avec métadonnées (boîte, langue, date)
- À adapter au schéma réel des DB une fois inspecté

### Bootstrap one-shot (`scripts/extract_personality.py`)
- Échantillonne ~50 paires représentatives (réparties FR/NL/EN, marques variées)
- Demande au LLM principal de produire un guide de style "personnalité Daniel"
  (ton, formules récurrentes, longueur typique, signature par marque, registre selon type de client)
- Sauvegarde dans `app/prompts/personality_daniel.txt`
- **Reproductible** : ré-exécutable quand le corpus s'enrichit ou que le ton de Daniel évolue

### Génération à chaque demande client

```
[SYSTEM PROMPT]
{contenu de personality_daniel.txt}
Marque/boîte source : {Detective Belgique | Detective Belgium | DPDH Investigations}
Langue de réponse OBLIGATOIRE : {langue détectée}

[USER PROMPT]
Voici 5 cas similaires où Daniel a déjà répondu :
1. [entrant 1] → [réponse 1]
... (top 5 par similarité cosine)

--- NOUVEAU MAIL À TRAITER ---
De : {sender}
Sujet : {subject}
Corps : {body}

Génère UN brouillon de réponse en {langue}, signé au nom de {marque},
dans le style de Daniel illustré par les cas ci-dessus.
Renvoie UNIQUEMENT le corps du message.
```

---

## 7. Workflow de livraison MVP

### Livraison email (principale)

Le brouillon est envoyé par email à `cdal@digitalhs.biz` via Resend, formaté ainsi :

- **Sujet** : `[AGENT][${marque}] ${sujet du mail original}`
- **Corps HTML** :
  - Métadonnées : boîte source, expéditeur, date reçue, langue détectée, modèle utilisé
  - **Brouillon proposé** (encadré, copiable)
  - Mail original (intégral)
  - Top 3 cas RAG utilisés (extraits + scores similarité)

CDAL relit, ajuste si besoin, transmet à Daniel via forward standard. Quand qualité stabilisée (mesurée sur 2-4 semaines), bascule en V2 vers dépôt IMAP direct dans le dossier `Drafts` natif de Daniel.

### Canal Telegram Boss ↔ Charlie (parallèle)

Un bot Telegram notifie **Daniel** en direct pour les événements importants et lui permet d'interagir avec Charlie :

- **Notifications push** : résumé du brouillon généré (pas le texte complet, juste un aperçu + lien vers l'email complet chez CDAL)
- **Commandes disponibles** :
  - `/status` — état de l'agent, dernière classification, queue en attente
  - `/resume [n]` — résumé des `n` derniers mails traités
  - `/approve [id]` — valider un brouillon (marque pour envoi)
  - `/reject [id] [raison]` — rejeter un brouillon (log pour calibration)
  - `/ask <question>` — poser une question libre à Charlie (RAG + style Daniel)
- **Ton** : Charlie est professionnel mais accessible, utilise le vouvoiement, signe parfois "— Charlie" pour les réponses conversationnelles
- **Multilingue** : répond dans la langue de la question (FR/NL/EN)
- **Phase test** : bot connecté au Telegram de CDAL pour itérer sans déranger Daniel
- **Phase prod** : migré sur KVM8, token reconfiguré pour le compte de Daniel

---

## 8. Supervision 24/7

- **systemd unit** avec `Restart=always`, `RestartSec=10`
- **Healthcheck** : endpoint `/health` (FastAPI sur 127.0.0.1:8765) renvoie OK si toutes les connexions IMAP actives + dernier cycle < 10 min
- **systemd timer** sonde `/health` chaque minute, déclenche restart si KO
- **Bot Telegram — alertes système** (canal séparé des messages conversationnels) :
  - Agent down, IMAP timeout > 3 tentatives consécutives
  - Échec génération > 5/heure, taux d'erreur LLM > 10%
  - Exception non rattrapée, restart brutal
  - **Séparation** : thread/canal `alertes` vs thread/canal `conversation` pour ne pas polluer Daniel avec du bruit technique
- **Bot Telegram — conversation métier** (canal Boss ↔ Charlie) :
  - Notifications push des nouveaux brouillons (résumé + commandes `/approve`, `/reject`)
  - Commandes `/status`, `/resume`, `/ask` pour interagir avec l'agent en direct
- **Logs structurés JSON** dans journalctl, rotation 7 jours
- **Tableau de bord léger** (V1.5) : page HTML statique générée par cron, accessible via SSH tunnel, montrant stats journalières (mails reçus/classés/draftés)

---

## 9. Sécurité

- 3 app passwords Infomaniak + clé Resend + clé Ollama + clé OpenRouter + **token Telegram bot** dans `.env` (chmod 600, propriétaire dédié `detective-agent` en prod)
- TLS partout (IMAPS 993, SMTP 587 STARTTLS pour le futur outbound, HTTPS pour API LLM, Telegram Bot API via HTTPS)
- Healthcheck bind 127.0.0.1 uniquement, aucune API exposée publiquement
- Backups chiffrés (age) avant push vers Backblaze
- **Pas de log du contenu des mails en clair** (uniquement IDs + métadonnées). Flag `LOG_MAIL_BODY=true` requis pour debug.
- **Pas de log du contenu des conversations Telegram** (uniquement commande + métadonnées). Flag `LOG_TG_CONVERSATION=true` requis pour debug.
- App passwords séparés permettent révocation individuelle
- Token Telegram bot facilement révocable via @BotFather si compromis

---

## 10. Coûts estimés mensuels

| Poste | Coût |
|---|---|
| VPS Hostinger KVM8 | déjà payé |
| Infomaniak (3 boîtes) | déjà payé |
| Ollama Pro (Kimi K2) | 20 €/mois |
| OpenRouter (fallback ponctuel) | < 5 €/mois |
| Resend (free tier 3000 mails/mois) | 0 € |
| Backblaze B2 (~5 Go) | < 1 €/mois |
| Bot Telegram | 0 € |
| **Total** | **~25-30 €/mois** |

---

## 11. Vérification end-to-end (à exécuter avant de déclarer MVP livré)

1. **IMAP** : injecter un mail test depuis un compte externe vers chacune des 3 boîtes → vérifier détection < 5 min + tag `$AgentProcessed` posé
2. **Classification** : envoyer 1 mail de chaque catégorie → vérifier classification correcte dans logs
3. **RAG** : pour un mail "demande client" connu (similaire à un cas historique), vérifier que le top-5 retrouvé contient bien le cas attendu
4. **Génération** : vérifier que le brouillon respecte la langue du mail entrant et contient la signature de la bonne marque
5. **Livraison** : email reçu sur `cdal@digitalhs.biz` avec format complet (métadonnées + brouillon + mail original + cas RAG)
6. **Multilingue** : 3 mails identiques en sens (FR / NL / EN) → 3 brouillons cohérents chacun dans sa langue
7. **Robustesse** : kill brutal du process → systemd redémarre dans les 10s, reprend sans doublon (idempotence via flag IMAP)
8. **Supervision** : couper IMAP volontairement (firewall) → alerte Telegram reçue
9. **Backup** : exécuter restore sur une copie → DB lisibles
10. **Charge** : injecter 100 mails simultanés → tous traités sans crash, latence moyenne < 30s par mail
