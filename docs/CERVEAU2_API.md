# Cerveau2-Det — Documentation API interne

> **À jour** : 2026-05-20 (v1.9.9)
> **URL base** : `https://cerveau2-det.digitalhs.biz`
> **Auth** : `Authorization: Bearer <CERVEAU2_API_SECRET>`

---

## Endpoints

### 1. `POST /query` — Recherche RAG dans le vault

Interroge le vault vectoriel de Cerveau2 et retourne les notes les plus pertinentes.

#### Requête

```json
{
  "question": "string",      // obligatoire — question en langage naturel
  "limit": 3,                // optionnel — défaut 3, **MAX 20** (422 si >20)
  "dossier_id": "string"     // optionnel — filtre par dossier
}
```

#### Réponse 200

```json
{
  "status": "ok",
  "question": "string",
  "dossier_id": "string|null",
  "context": [
    {
      "path": "string",
      "content": "string"
    }
  ],
  "answer": "string",        // réponse LLM générée (souvent vide en mode RAG pur)
  "total_found": 5
}
```

#### Erreurs connues

- **422 Unprocessable Entity** : `limit` supérieur à 20.
- **zone_rouge** : retourne `status: "zone_rouge"` → le système refuse de répondre (garde-fou contenu sensible).

#### Garde-fous côté client (`app/cerveau_client.py`)

- Timeout : **4 secondes** (dégradation silencieuse → `[]`).
- Si `base_url` ou `api_secret` absents : retourne `[]` sans appel.

---

### 2. `POST /ingest-note` — Ingestion d'un document

Ajoute un document (PDF, DOCX, image OCR, TXT, etc.) au vault Cerveau2. Appel **fire-and-forget** depuis l'agent Charlie.

#### Requête

```json
{
  "id": "string",               // obligatoire — identifiant unique stable
  "type": "document",           // "document" | "note" | "correspondance"
  "dossier_id": "string",       // obligatoire — dossier client (ex: ADF)
  "marque": "string",           // obligatoire — slug Cerveau2
  "date": "YYYY-MM-DD",         // obligatoire
  "titre": "string",            // titre/description du document
  "body": "string",             // obligatoire — texte extrait du document
  "metadata": {},               // optionnel — {filename, source, size_bytes...}
  "zone": "jaune|rouge",        // défaut "jaune"
  "langue": "fr|nl|en"          // défaut "fr"
}
```

#### Réponse 200

```json
{
  "status": "created",
  "path": "02_dossiers/ADF/documents/2026-05-20_document_rapport.md",
  "doc_id": "doc-upload-12345678",
  "duplicate": false
}
```

- **duplicate: true** → le `id` existait déjà dans le vault (considéré comme succès).

#### Garde-fous côté client (`app/cerveau_client.py` → `feed_document()`)

- Timeout : **120 secondes** (Cerveau2 met 40-120s pour indexer les embeddings + fallback LLM).
- Retry : **3 tentatives** avec backoff exponentiel (2s, 4s, 8s).
- Body tronqué à **150 000 caractères** si trop long (évite les timeouts sur gros emails).
- `dossier_id` vide remplacé par `"GENERAL"` (Cerveau2 rejette les vides).
- Stockage vault : `02_dossiers/{dossier_id}/documents/{date}_{type}_{slug}.md`

---

### 3. `POST /ingest-email` — Ingestion d'un email

Ajoute un email au vault Cerveau2. Appel **fire-and-forget** depuis l'agent.

#### Requête

```json
{
  "message_id": "string",        // obligatoire — identifiant unique IMAP
  "direction": "in|out",         // obligatoire
  "date": "YYYY-MM-DD",          // obligatoire
  "heure": "HH:MM",              // obligatoire
  "expediteur": "string",        // obligatoire
  "destinataire": "string",      // obligatoire
  "objet": "string",             // obligatoire
  "body": "string",              // obligatoire — corps texte brut
  "marque": "string",            // obligatoire — slug Cerveau2 (voir mapping ci-dessous)
  "dossier_id": "string",        // obligatoire — laisser "" si inconnu
  "categorie": "string",         // ex: "demande_client", "facture"...
  "zone": "jaune|rouge",         // défaut "jaune"
  "langue": "fr|nl|en",          // défaut "fr"
  "priorite": "urgent|normal|faible"  // défaut "normal"
}
```

> **Mapping interne → Cerveau2** : `high` → `urgent`, `normal` → `normal`, `low` → `faible`. Cerveau2 valide via enum FastAPI — les valeurs `high`/`low` provoquaient un HTTP 422 avant v1.18.6.

#### Réponse 200

```json
{
  "success": true,
  "created": true|false,   // false si l'email existait déjà
  "message_id": "string"
}
```

#### Garde-fous côté client

- Timeout : **120 secondes** (indexation embeddings + fallback LLM peuvent prendre 40-120s).
- Retry : **3 tentatives** avec backoff exponentiel (2s, 4s, 8s).
- Body tronqué à **150 000 caractères** avec suffixe `[... tronqué]`.
- `dossier_id` vide → remplacé par `"GENERAL"` (rejet 422 si vide).
- Si `created: false` → l'email était déjà ingéré, ce n'est **pas** une erreur.

---

## Mapping marques

Cerveau2 attend des **slugs spécifiques** dans le champ `marque`. Ne PAS utiliser les noms internes du projet.

| Nom interne (code Python) | Slug Cerveau2 (`marque`) |
|---|---|
| `detective_belgique` | `detectivebelgique` |
| `detective_belgium` | `detectivebelgium` |
| `dpdh_investigations` | `dpdhu` |

> **Piège rencontré** : envoyer `detective_belgique` au lieu de `detectivebelgique` provoque un rejet silencieux ou une ingestion sans rattachement correct.

---

## Notes Cerveau2 (format interne)

Les emails et documents sont stockés sous forme de notes Markdown avec frontmatter :

### Correspondance (email)
```markdown
---
type: "correspondance"
direction: "out"
date: "2023-02-15"
heure: "14:30"
expediteur: "daniel@detectivebelgique.be"
destinataire: "client@example.com"
objet: "RE: Demande de surveillance"
langue: "fr"
marque: "detectivebelgique"
zone: "jaune"
priorite: "normal"
categorie: "demande_client"
---

Corps du message ici...
```

### Document (PDF, DOCX, image OCR, upload cockpit, pièce jointe)
```markdown
---
type: "document"
date: "2026-05-20"
titre: "Rapport surveillance Dupont.pdf"
marque: "detectivebelgique"
dossier: "[[ADF/_index]]"
zone: "jaune"
langue: "fr"
doc_id: "doc-upload-12345678"
metadata: {"source": "cockpit_upload", "filename": "rapport.pdf", "size_bytes": 124000}
---

# Rapport surveillance Dupont.pdf

## Contenu

texte extrait du document ici...
```

- Le champ `type` distingue `"correspondance"` et `"document"`.
- Le parsing du frontmatter se fait par split sur `---` + lecture clé:valeur.
- Les documents sont rangés dans `02_dossiers/{id}/documents/`.
- Les correspondances sont rangées dans `02_dossiers/{id}/correspondances/`.
- `/query` avec `dossier_id` recherche dans **les deux dossiers** et retourne emails + documents.

---

## Commandes de diagnostic

```bash
# Tester /query manuellement
curl -s -X POST https://cerveau2-det.digitalhs.biz/query \
  -H "Authorization: Bearer $CERVEAU2_API_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"question":"test","limit":3}'

# Tester /ingest-note manuellement
curl -s -X POST https://cerveau2-det.digitalhs.biz/ingest-note \
  -H "Authorization: Bearer $CERVEAU2_API_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"id":"test-doc-123","type":"document","dossier_id":"ADF","marque":"detectivebelgique","date":"2026-05-20","titre":"Test rapport","body":"Contenu extrait du document...","metadata":{"source":"test"},"zone":"jaune","langue":"fr"}'

# Tester /ingest-email manuellement
curl -s -X POST https://cerveau2-det.digitalhs.biz/ingest-email \
  -H "Authorization: Bearer $CERVEAU2_API_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"message_id":"test-123","direction":"out","date":"2026-05-20","heure":"10:00","expediteur":"a@b.com","destinataire":"c@d.com","objet":"Test","body":"Corps","marque":"detectivebelgique","dossier_id":"","categorie":"test","zone":"jaune","langue":"fr","priorite":"normal"}'
```

---

## Dépendances projet

- `app/cerveau_client.py` — client HTTP asynchrone (httpx)
- `scripts/extract_soul.py` — consommateur de `/query` pour analyse de style
- `scripts/ingest_sent_to_cerveau2.py` — producteur vers `/ingest-email` (emails sortants)

---

## Historique des pièges résolus

| Date | Problème | Cause | Fix |
|---|---|---|---|
| 2026-05-29 | Emails `high`/`low` non ingérés | Enum Cerveau2 : `urgent\|normal\|faible` | `_map_priority()` : `high→urgent`, `low→faible` (v1.18.6) |
| 2026-05-29 | Timeout ingestion systématique | Cerveau2 met 40-120s (embeddings) | Timeout client 15s → **120s** (v1.18.6) |
| 2026-05-29 | `dossier_id` vide rejeté | Validation FastAPI `min_length=1` | Fallback `"GENERAL"` (v1.18.6) |
| 2026-05-29 | Body trop long → timeout | Emails de 300K+ caractères | Troncage à 150K caractères (v1.18.6) |
| 2026-05-20 | 422 sur `/query` | `limit: 100` > max 20 | Cap à 20 côté client |
| 2026-05-20 | Emails non retrouvés dans Cerveau2 | Mauvais slug `marque` | Mapping `detective_belgique` → `detectivebelgique` |
| 2026-05-20 | Ingestion lente/bloquée | FETCH IMAP batch multi-messages | Passage au FETCH un par un (fiabilité) |
