# Detective.be — Agent IA email

Agent IA Python qui assiste **Daniel Hurchon** (Detective.be, cabinet d'enquêtes privées) dans le traitement de ses emails clients.

L'agent surveille 3 boîtes Infomaniak (3 marques : Detective Belgique, Detective Belgium, DPDH Investigations), classifie les mails entrants, et génère des brouillons de réponse "à la Daniel" pour les demandes clients — multilingue FR/NL/EN.

> **Pour Claude Code** : lis `CLAUDE.md` en premier pour le contexte, les conventions et les garde-fous.

---

## Architecture en une image

```
[3 boîtes Infomaniak IMAP]
         ↓ polling 5 min
[Worker asyncio Python]
         ↓
[Pipeline]
  pré-filtre règles  → newsletter / facture évidente → tag & skip
  classification LLM → 6 catégories
  si demande_client :
    détection langue (FR/NL/EN)
    RAG sur 1200 paires Q/R historiques (sqlite-vec)
    génération brouillon (Kimi K2 via LiteLLM, style "Daniel")
         ↓
[Resend API → cdal@digitalhs.biz]
[Flag IMAP $AgentProcessed sur le mail entrant]
```

Spec complète : [`docs/SPEC.md`](docs/SPEC.md). Roadmap : [`docs/ROADMAP.md`](docs/ROADMAP.md).

---

## Setup local (Mac de Cyril)

```bash
# 1. Environnement Python
python3 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"

# 2. Config
cp .env.example .env
# → éditer .env avec : app passwords Infomaniak, clé Ollama Pro, clé Resend

# 3. Données : déposer les 3 DB SQLite anonymisées dans data/
#   data/boite1.sqlite
#   data/boite2.sqlite
#   data/boite3.sqlite

# 4. Bootstrap one-shot (S1) — après que les DB soient là
python -m scripts.bootstrap_embeddings   # indexe les 1200 paires dans pairs_vec
python -m scripts.extract_personality    # génère app/prompts/personality_daniel.txt

# 5. Lancer l'agent
python -m app.main
```

---

## Stack

Python 3.11+ · asyncio · aioimaplib · LiteLLM (Kimi K2 / Ollama Pro + OpenRouter fallback) · sentence-transformers (e5-large) · sqlite-vec · fasttext · Resend · FastAPI healthcheck · structlog · pydantic-settings.

Hébergement cible : VPS Hostinger KVM8, déploiement systemd.

Coût LLM mensuel estimé : **~25-30 €** (Ollama Pro 20€ + OpenRouter ponctuel + Backblaze backup).

---

## Layout

```
DETECTIVE_BE/
├── CLAUDE.md                    # instructions Claude Code (à lire en 1er)
├── README.md                    # ce fichier
├── pyproject.toml               # deps + ruff + pytest
├── .env.example                 # template config
├── docs/
│   ├── SPEC.md                  # spec technique complète et figée
│   ├── ROADMAP.md               # découpage S1→S4 + V2/V3 + état courant
│   └── CONTEXT.md               # contexte business client
├── app/
│   ├── main.py                  # entrypoint asyncio
│   ├── config.py                # pydantic-settings
│   ├── healthcheck.py           # FastAPI /health
│   ├── workers/imap_poller.py   # 1 task asyncio par boîte
│   ├── pipeline/
│   │   ├── prefilter.py         # règles headers/expéditeurs
│   │   ├── classifier.py        # LLM → 6 catégories
│   │   ├── language.py          # fasttext FR/NL/EN
│   │   ├── rag.py               # embed + retrieve sqlite-vec
│   │   └── generator.py         # assemblage prompt + appel LLM
│   ├── delivery/resend_notifier.py  # email HTML formaté → Cyril
│   ├── llm/router.py            # wrapper LiteLLM avec fallback
│   └── prompts/
│       ├── classifier_prompt.txt
│       └── personality_daniel.txt   # généré par extract_personality.py
├── scripts/
│   ├── bootstrap_embeddings.py
│   └── extract_personality.py
├── deploy/detective-agent.service   # systemd unit pour KVM8
├── data/                            # DB SQLite (gitignored)
├── logs/
└── tests/
```

---

## Statut

🚧 **MVP en construction** — voir `docs/ROADMAP.md` pour l'avancement détaillé.
