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

### 2. `POST /ingest-email` — Ingestion d'un email

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
  "priorite": "normal|high|low"  // défaut "normal"
}
```

#### Réponse 200

```json
{
  "success": true,
  "created": true|false,   // false si l'email existait déjà
  "message_id": "string"
}
```

#### Garde-fous côté client

- Timeout : **15 secondes**.
- Retry : **3 tentatives** avec backoff implicite (boucle `for attempt`).
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

Les emails ingérés sont stockés sous forme de notes Markdown avec frontmatter :

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

- Le champ `type` est toujours `"correspondance"`.
- Le parsing du frontmatter se fait par split sur `---` + lecture clé:valeur.

---

## Commandes de diagnostic

```bash
# Tester /query manuellement
curl -s -X POST https://cerveau2-det.digitalhs.biz/query \
  -H "Authorization: Bearer $CERVEAU2_API_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"question":"test","limit":3}'

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
| 2026-05-20 | 422 sur `/query` | `limit: 100` > max 20 | Cap à 20 côté client |
| 2026-05-20 | Emails non retrouvés dans Cerveau2 | Mauvais slug `marque` | Mapping `detective_belgique` → `detectivebelgique` |
| 2026-05-20 | Ingestion lente/bloquée | FETCH IMAP batch multi-messages | Passage au FETCH un par un (fiabilité) |
