# Intégration Cerveau2 — Guide pour Agents Externes

> Document technique : comment connecter un agent tiers (Hermes, copilote interne, etc.) au vault Cerveau2-Detective.
> Version : v1.13.4
> Vault : https://cerveau2-det.digitalhs.biz

---

## 1. Architecture Cerveau2 en 30 secondes

Cerveau2 est un **vault Markdown** structuré + une **API FastAPI** qui expose :
- Ingestion d'emails et documents
- Recherche sémantique + keyword
- Anonymisation avant tout appel LLM

```
[Agent externe] ──HTTP──> [Cerveau2 API] ──> [Vault Markdown]
                              ↓
                         [sqlite-vec] ← embeddings E5-large
```

### Structure du vault
```
vault/
├── 00_system/       ← Logs, index, config AGENTS.md
├── 01_inbox/        ← Raw (jamais édité manuellement)
├── 02_dossiers/     ← Dossiers d'enquête actifs
├── 03_doctrine/     ← Méthodologie, jurisprudence
├── 04_entities/     ← CRM transversal (personnes, sociétés, lieux)
├── 05_clients/      ← Coordonnées clients + facturation
├── 99_archives/     ← Dossiers clos
└── 99_attachments/  ← Binaires originaux
```

---

## 2. Endpoints API disponibles

Tous les endpoints sont protégés par **Bearer Token** (voir §3).

### 2.1 Recherche — `POST /query`

**Requête :**
```json
{
  "question": "Qui est l'épouse de CDAL ?",
  "dossier_id": "Dutry",
  "limit": 5
}
```

**Réponse :**
```json
{
  "status": "ok",
  "question": "Qui est l'épouse de CDAL ?",
  "dossier_id": "Dutry",
  "context": [
    {
      "path": "02_dossiers/Dutry/2026-05-20_rapport.md",
      "content": "...CDAL est aidé par son épouse : Sarah..."
    }
  ],
  "total_found": 1,
  "answer": "L'épouse de CDAL s'appelle Sarah."
}
```

**Statuts possibles :**
| Status | Signification |
|---|---|
| `ok` | Recherche + réponse LLM générée |
| `context_only` | Pas de clé LLM configurée → retourne uniquement le contexte brut |
| `zone_rouge` | Document sensible détecté → LLM bloqué, contexte quand même retourné |

### 2.2 Ingestion Email — `POST /ingest-email`

**Requête :**
```json
{
  "message_id": "<abc123@detective.be>",
  "direction": "inbound",
  "date": "2026-05-21",
  "heure": "14:30",
  "expediteur": "client@example.com",
  "destinataire": "contact@detective.be",
  "objet": "Demande d'enquête",
  "body": "Je souhaite ...",
  "marque": "detectivebelgique",
  "dossier_id": "Dutry",
  "categorie": "surveillance",
  "zone": "jaune",
  "langue": "fr",
  "priorite": "high"
}
```

**Réponse :** `{"created": true}` ou HTTP 409 si doublon.

### 2.3 Ingestion Document — `POST /ingest-note`

**Requête :**
```json
{
  "id": "PJ-2026-001",
  "type": "document_scanned",
  "dossier_id": "Dutry",
  "marque": "detectivebelgique",
  "date": "2026-05-21",
  "titre": "Rapport terrain",
  "body": "...",
  "metadata": {"source": "scanner", "pages": 3},
  "zone": "jaune",
  "langue": "fr"
}
```

### 2.4 Anonymisation — `POST /anonymize`

**Requête :** `{"text": "Monsieur Dupont habite à Paris..."}`

**Réponse :** `{"text": "PERSONNE_A habite à LIEU_A...", "mapping": {"PERSONNE_A": "Dupont", "LIEU_A": "Paris"}}`

### 2.5 Audit — `GET /audit/{dossier_id}`

Retourne les 50 dernières requêtes sur un dossier (traçabilité judiciaire).

### 2.6 Health — `GET /health`

```json
{"status": "ok", "version": "...", "vault_ok": true}
```

---

## 3. Authentification

### 3.1 Mécanisme
Cerveau2 utilise un **Bearer Token statique** (pas de OAuth, pas de JWT — un seul secret partagé).

```http
Authorization: Bearer <cerveau_api_secret>
```

Le secret est défini dans `.env` de Cerveau2 :
```bash
CERVEAU_API_SECRET="sk-c2d-..."
```

### 3.2 Comment obtenir le secret
1. **CDAL** (administrateur VPS) lit le fichier `/opt/CERVEAU2/.env` sur le serveur
2. Il communique le secret à l'agent externe via canal sécurisé (pas par email)
3. L'agent externe stocke le secret dans **sa propre variable d'environnement** (jamais en dur dans le code)

### 3.3 Vérification d'accès
Avant d'autoriser un nouvel agent, CDAL doit :
- [ ] Vérifier que l'agent a un **besoin métier légitime** (ex: Hermes = agent facturation, besoin d'accéder aux dossiers clients)
- [ ] Créer une **ligne dans `00_system/AGENTS.md`** du vault avec : nom agent, date d'autorisation, scope autorisé (query / ingest / both)
- [ ] Optionnel : définir un **scope restreint** via code (ex: Hermes = uniquement `POST /query` sur `04_entities/` et `05_clients/`)

---

## 4. Exemple — Connecter un agent Hermes (Python)

```python
import os
import httpx

CERVEAU_BASE = os.getenv("CERVEAU2_BASE_URL", "https://cerveau2-det.digitalhs.biz")
CERVEAU_SECRET = os.getenv("CERVEAU2_API_SECRET")  # Jamais en dur !


async def ask_cerveau(question: str, dossier_id: str | None = None) -> dict:
    """Agent Hermes interroge Cerveau2 pour récupérer du contexte."""
    if not CERVEAU_SECRET:
        raise RuntimeError("CERVEAU2_API_SECRET manquant")

    async with httpx.AsyncClient(timeout=12.0) as client:
        resp = await client.post(
            f"{CERVEAU_BASE}/query",
            json={"question": question, "dossier_id": dossier_id, "limit": 3},
            headers={"Authorization": f"Bearer {CERVEAU_SECRET}"},
        )
        resp.raise_for_status()
        return resp.json()


async def save_email_to_cerveau(payload: dict) -> bool:
    """Agent Hermes alimente Cerveau2 avec un email traité."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{CERVEAU_BASE}/ingest-email",
            json=payload,
            headers={"Authorization": f"Bearer {CERVEAU_SECRET}"},
        )
        return resp.status_code in (200, 409)  # 409 = doublon, OK quand même
```

---

## 5. Garde-fous de sécurité (à respecter impérativement)

### 5.1 Path Traversal — dossier_id
- **Regex obligatoire** : `^[A-Za-z0-9_-]+$`
- **Rejet silencieux** : si un `dossier_id` invalide est envoyé, Cerveau2 retourne `[]` ou dépose en `01_inbox/` (fallback)
- **Jamais de concaténation directe** : ne faites pas `vault_path / dossier_id` sans validation

### 5.2 Zone Rouge
- Si un document avec `#zone-rouge` est trouvé dans le contexte :
  - Le LLM **n'est pas appelé**
  - Le contexte est **quand même retourné** (statut `zone_rouge`)
  - L'agent externe peut décider de rediriger vers un humain

### 5.3 Anonymisation
- **Tout contenu sortant vers un LLM cloud** (OpenRouter, Claude, GPT-4) doit passer par `POST /anonymize` d'abord
- Les vrais noms ne doivent **jamais** quitter le VPS en clair
- Cerveau2 le fait automatiquement sur `/query`, mais si vous appelez le LLM directement, c'est **votre responsabilité**

### 5.4 Rate Limiting (recommandé)
- Mettre en place un rate-limit côté agent externe : max 10 req/min sur `/query`
- Cerveau2 n'a pas de rate-limit natif actuellement — c'est au consommateur de se comporter

### 5.5 Audit
- Chaque requête est loguée dans `00_system/audit.log` du vault
- L'agent externe doit s'identifier clairement dans ses logs structurés

---

## 6. Checklist avant de brancher un nouvel agent

- [ ] Agent identifié (nom, rôle, propriétaire)
- [ ] Scope défini (query seul ? ingest seul ? les deux ?)
- [ ] Secret généré / partagé via canal sécurisé
- [ ] Ligne ajoutée dans `vault/00_system/AGENTS.md`
- [ ] Test `/health` → 200
- [ ] Test `/query` avec question simple → retourne contexte
- [ ] Test `/query` avec `dossier_id` invalide (`../../../etc/passwd`) → rejeté ou `[]`
- [ ] Test d'ingestion avec doublon → HTTP 409
- [ ] Test de zone rouge → statut `zone_rouge`, pas d'appel LLM
- [ ] Documentation de l'intégration mise à jour

---

## 7. Différences avec le client Charlie interne

| Aspect | Charlie (interne) | Agent externe (Hermes, etc.) |
|---|---|---|
| Auth | Même secret via `.env` | Doit obtenir le secret de CDAL |
| Endpoints | `/query`, `/ingest-email`, `/ingest-note` | Idem, selon scope accordé |
| Parallélisme | SQL + vault + mémoire en parallèle | Généralement vault seul |
| Fallback | SQL local si vault down | Doit gérer son propre fallback |
| Logging | `structlog` JSON | Doit logger dans son propre système |

---

## 8. Contact

Intégrateur : CDAL (`cdal@digitalhs.biz`)
Responsable vault : CDAL
Changements d'architecture : toujours via PR + review avant merge
