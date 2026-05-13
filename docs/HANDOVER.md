# HANDOVER — Detective.be Agent (Charlie)

> Date : 2026-05-13
> Handover par : Claude (assistant IA) → Cyril Dal (`cdal@digitalhs.biz`)
> Projet : Agent IA email pour Daniel Hurchon (Detective.be)

---

## État du projet au handover

### Ce qui fonctionne (MVP opérationnel en local)

- **Polling IMAP** : 3 boîtes Infomaniak (`detective_belgique`, `detective_belgium`, `dpdh_investigations`) scannées toutes les 5 min via `aioimaplib`
- **Classification** : 6 catégories (`demande_client`, `facture`, `newsletter`, `spam`, `urgent`, `autre`) via LLM gemma4:31b + pré-filtre règles
- **RAG** : retrieval sur 2042 paires Q/R historiques indexées (`sqlite-vec` + `multilingual-e5-large`)
- **Génération brouillons** : style Daniel Hurchon, signé par marque, langue détectée du mail entrant (FR/NL/EN)
- **Livraison** : email Resend → `cdal@digitalhs.biz` avec sujet `TRIAL DETECTIVE AI : {sujet}`
- **Notifications Slack** : webhook `#detective` avec métadonnées + référence email à valider
- **Digest newsletter** : résumé quotidien des newsletters sur Slack
- **Persistance** : `agent_state.db` (SQLite) trace chaque mail traité (uid, catégorie, brouillon généré)
- **Idempotence** : flag IMAP `$AgentProcessed` — un mail n'est jamais traité 2x
- **Healthcheck** : serveur FastAPI sur `127.0.0.1:8765`
- **Retry IMAP** : 3 tentatives avec backoff exponentiel

### Ce qui a été testé et validé

| Test | Résultat |
|---|---|
| Mail FR (detective_belgique) | `demande_client` → brouillon FR envoyé ✅ |
| Mail EN (detective_belgium, UID=10) | `demande_client` → brouillon EN envoyé ✅ |
| Mail NL (detective_belgium, UID=11) | `demande_client` → brouillon NL envoyé ✅ |
| Slack notification | Reçue avec référence email ✅ |
| Newsletter digest | Pas encore testé en conditions réelles (pas de newsletter hier) |

### Ce qui reste à faire (S3 calibration + S4 prod)

- [ ] **Calibration qualité** : faire traiter 20-50 vrais mails par Daniel/Cyril, noter les corrections, affiner le prompt de personnalité
- [ ] **Signature par marque** : vérifier que boîte 2 (`Detective Belgium`) et boîte 3 (`DPDH Investigations`) génèrent la bonne signature (testé uniquement boîte 1)
- [ ] **Slack App interactive (V2)** : rendre les boutons Approuver/Rejeter fonctionnels (nécessite endpoint + tokens Slack)
- [ ] **Tests unitaires automatisés** : mock IMAP/LLM, fixtures de classification
- [ ] **Déploiement S4** : VPS Hostinger KVM8, systemd, `.env` prod, backups Backblaze B2
- [ ] **Bot WhatsApp client (V3)** : réutilise le pipeline RAG pour un canal client direct

---

## Structure du repo

```
detective-agent/
├── app/
│   ├── config.py              # pydantic-settings depuis .env
│   ├── delivery/
│   │   ├── resend_notifier.py # Envoi brouillons par email Resend
│   │   └── slack_notifier.py  # Notifications Slack (remplace Telegram)
│   ├── healthcheck.py         # Serveur FastAPI /health
│   ├── llm/
│   │   └── router.py          # Proxy LiteLLM vers Ollama Pro / OpenRouter
│   ├── main.py                # Point d'entrée : 3 pollers + healthcheck
│   ├── pipeline/
│   │   ├── classifier.py      # Classification LLM 6 catégories
│   │   ├── generator.py       # Génération brouillon RAG + style Daniel
│   │   ├── language.py        # Détection langue (langdetect : FR/NL/EN)
│   │   ├── prefilter.py       # Filtre rapide règles (newsletter, spam...)
│   │   └── rag.py             # Retrieval sqlite-vec + embeddings
│   ├── prompts/
│   │   ├── classifier_prompt.txt      # Prompt classification (à affiner)
│   │   └── personality_daniel.txt     # Guide de style Daniel (généré + validé)
│   ├── telegram_bot.py        # Gardé en référence (abandonné — instable)
│   └── workers/
│       ├── imap_poller.py     # Boucle polling + traitement mail
│       └── newsletter_digest.py # Digest quotidien Slack
├── data/
│   ├── boite1.sqlite          # DB historique anonymisée (NE PAS MODIFIER)
│   ├── boite2.sqlite          # DB historique anonymisée (NE PAS MODIFIER)
│   ├── boite3.sqlite          # DB historique anonymisée (NE PAS MODIFIER)
│   └── agent_state.db         # Traçabilité mails traités (créé auto)
├── docs/
│   ├── SPEC.md                # Spec technique figée
│   ├── ROADMAP.md             # État des phases S1-S4
│   ├── CONTEXT.md             # Contexte business Daniel
│   └── HANDOVER.md            # Ce fichier
├── scripts/
│   ├── bootstrap_embeddings.py # Indexation initiale RAG (déjà exécuté)
│   ├── extract_personality.py  # Extraction style Daniel (déjà exécuté)
│   └── simulate_trial.py      # Simulation manuelle d'un mail
├── tests/                     # (vide — à peupler en V2)
├── .env                       # Secrets — JAMAIS commit
├── .env.example               # Template .env sans secrets
├── .gitignore                 # Bloque .env, data/*.sqlite, venv/
├── pyproject.toml             # Dépendances (langdetect, litellm, etc.)
└── venv/                      # Python 3.14 (Mac de Cyril)
```

---

## Comment lancer l'agent

### En local (développement)

```bash
# Setup (déjà fait)
python3 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"

# Lancer
python -m app.main
```

L'agent tourne en foreground. Ctrl+C pour arrêter proprement.

### En prod (V4 — systemd)

```bash
# Sur le VPS Hostinger
sudo systemctl start detective-agent
sudo systemctl status detective-agent
```

Voir `deploy/detective-agent.service` (à créer en S4).

---

## Points de vigilance IMPORTANTS

1. **Ne JAMAIS commit `.env`** — le `.gitignore` le bloque, mais double-check avant chaque push
2. **Ne JAMAIS modifier les 3 DB sources** (`data/boite{1,2,3}.sqlite`) sans confirmation — ce sont les données historiques de Daniel
3. **Ne JAMAIS envoyer de mail réel via Resend en test** — utiliser `DRY_RUN=true` dans `.env` ou laisser `RESEND_API_KEY` vide
4. **Langue du client = règle P0** : si un brouillon sort dans la mauvaise langue, c'est un bug critique. `langdetect` est fiable mais à surveiller sur les vrais cas
5. **API keys à renouveler** :
   - Ollama Pro : abonnement 20€/mois de Cyril
   - OpenRouter : pay-as-you-go, vérifier le solde
   - Resend : domaine `resend.digitalhs.biz` à garder validé
6. **Telegram abandonné** : le module `app/telegram_bot.py` est présent mais non importé. Slack remplace tout pour le MVP. Si on revient à Telegram en V2, il faudra résoudre les problèmes de processus zombies/getUpdates

---

## Contact

- **Cyril Dal** (intégrateur) : `cdal@digitalhs.biz`
- **Daniel Hurchon** (client) : detectivebelgique.be / detectivebelgium.com / dpdhuinvestigations.be
