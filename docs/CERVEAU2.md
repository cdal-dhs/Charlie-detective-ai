# Cerveau2-Detective — Guide d'intégration pour DETECTIVE_BE

> Référence rapide pour tout agent Claude Code travaillant sur charlie.py, cerveau_client.py ou le pipeline IMAP.
> Document complet : `/Users/cdal/DEV_APP_CLAUDE/CERVEAU2-DEtective/CERVEAU2-REF.md`
> Extraction d'informations (fiches entreprise, contact, wikilinks) : [`docs/CERVEAU2_EXTRACTION.md`](CERVEAU2_EXTRACTION.md)

---

## Accès production

| | |
|--|--|
| URL | `https://cerveau2-det.digitalhs.biz` |
| Auth | `Authorization: Bearer {CERVEAU2_API_SECRET}` |
| VPS | `root@69.62.110.165` → `/opt/CERVEAU2-Det` |
| Healthcheck | `GET /health` → `{"status":"ok","version":"0.8.2"}` |

**Variables `.env` DETECTIVE_BE** :
```env
CERVEAU2_BASE_URL=https://cerveau2-det.digitalhs.biz
CERVEAU2_API_SECRET=...   # Même clé que dans .env de CERVEAU2-DEtective
CERVEAU2_LIMIT=5
```

---

## Client Python — `app/cerveau_client.py`

| Fonction | Endpoint Cerveau2 | Utilisée par |
|----------|-------------------|--------------|
| `query_vault(question, base_url, api_secret, dossier_id, limit)` | `POST /query` | `charlie.py` → `_vault_task()` |
| `feed_correspondance(...)` | `POST /ingest-email` | Pipeline IMAP après traitement |
| `feed_document(...)` | `POST /ingest-note` | Ingestion pièces jointes |
| `query_dossiers(base_url, api_secret, since, client_type)` | `GET /dossiers` | `charlie.py` → `_dossiers_task()` |
| `get_backup_status(base_url, api_secret)` | `GET /admin/backup/status` | Dashboard admin |

Toutes les fonctions sont **fire-and-forget** avec dégradation silencieuse : retournent `[]` / `False` / `None` en cas d'erreur — jamais d'exception levée.

---

## Utilisation dans Charlie AI (`app/charlie.py`)

### Recherche sémantique (vault notes)

```python
vault_notes = await query_vault(
    question=question,
    base_url=settings.cerveau2_base_url,
    api_secret=settings.cerveau2_api_secret,
    dossier_id=dossier_id,   # filtre optionnel
    limit=settings.cerveau2_limit,
)
```
Retourne `list[VaultNote]` où `VaultNote.path` et `VaultNote.content` sont disponibles.

### Comptage dossiers clients

```python
# Déclenché si is_dossier_count = True (mots-clés : "nouveau dossier", "ouvert depuis"…)
dossier_list = await query_dossiers(
    base_url=settings.cerveau2_base_url,
    api_secret=settings.cerveau2_api_secret,
    since="2026-05-01",     # optionnel
    client_type="particulier",  # optionnel
)
# Retourne : [{"dossier_id": "ADF", "created_at": "2026-05-01T...", "client_type": "particulier", "marque": "..."}, ...]
```

---

## Pipeline IMAP → Cerveau2

Dans `app/workers/imap_poller.py` / pipeline, après classification (hors newsletter/phishing) :

```python
await feed_correspondance(
    message_id=msg.message_id,
    direction="in",
    date="2026-05-22",
    heure="10:30",
    expediteur="client@example.com",
    destinataire="daniel",
    objet="Sujet du mail",
    body="Corps (anonymisé si nécessaire)",
    marque="detectivebelgique",
    dossier_id="ADF",        # si identifié, sinon "GENERAL"
    categorie="demande_client",
    zone="jaune",
    langue="fr",
    priorite="normal",       # high→urgent, normal→normal, low→faible (mapping automatique v1.18.6)
    base_url=settings.cerveau2_base_url,
    api_secret=settings.cerveau2_api_secret,
)
```

**Important** :
- Si `dossier_id` est nouveau, Cerveau2 crée automatiquement son `_index.md` et l'ajoute au registre (`dossier_registry.jsonl`).
- `dossier_id` vide est remplacé par `"GENERAL"` avant envoi (Cerveau2 rejette les vides).
- Body tronqué à 150K caractères si trop long (timeout 120s).
- **Skip newsletter/phishing** : ces catégories ne sont pas ingérées (bruit inutile).

---

## Vault — structure clé

```
vault/
├── 00_system/
│   ├── message_index.jsonl      # Idempotence (message_id → path)
│   └── dossier_registry.jsonl  # Registre dossiers + date ouverture
├── 01_inbox/                    # Emails sans dossier_id
├── 02_dossiers/
│   └── {DOSSIER_ID}/
│       ├── _index.md            # created_at, client_type, statut
│       ├── correspondances/     # Emails
│       └── documents/           # Notes, pièces jointes
└── 99_archives/                 # Dossiers clos
```

---

## Zones de confidentialité

| Zone | Renvoi par /query | Usage |
|------|-------------------|-------|
| `vert` | Oui | Données publiques |
| `jaune` | Oui | Données client anonymisées (défaut) |
| `rouge` | NON (`status: zone_rouge`) | Données ultra-sensibles |

Quand `/query` retourne `zone_rouge`, `vault_notes = []` dans Charlie — c'est le comportement attendu.

---

## Déploiement Cerveau2 (si besoin)

```bash
cd /Users/cdal/DEV_APP_CLAUDE/CERVEAU2-DEtective
bash scripts/deploy-to-vps.sh
```

---

## Évolutions planifiées

- v0.5.0 : endpoint `PATCH /dossiers/{id}` pour mettre à jour `statut`, `client_type`
- v0.5.0 : endpoint `GET /dossiers/{id}` pour la fiche détaillée d'un dossier
