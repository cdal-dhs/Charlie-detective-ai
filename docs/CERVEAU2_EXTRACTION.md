# Cerveau2 — Extraction et Traitement des Informations

> Document technique : comment les emails, pièces jointes et conversations sont transformés en connaissances structurées dans Cerveau2-Det.
> Version : v1.18.6
> Dépendances : `app/workers/imap_poller.py`, `app/charlie.py`, `app/cerveau_client.py`

---

## 1. Vue d'ensemble du pipeline

```
[Email IMAP]
    ↓
[Poller] ──fetch──┬──> Classification (8 catégories)
                  ├──> Extraction texte brut (body + PJ)
                  ├──> Fiche entreprise (regex)
                  ├──> Fiche contact (LLM classifier)
                  └──> Ingestion Cerveau2 (fire-and-forget)
                         ↓
                  [Vault Markdown]
                         ↓
[Charlie AI] ──query──> Recherche sémantique
                  ↓
         Nuage de liaison (wikilinks + YAML relations)
```

**Règle d'or** : **100% des emails traités** sont ingérés dans Cerveau2 via `feed_correspondance()` — **zéro tolérance sur le skip**, peu importe la catégorie (newsletter et phishing inclus, contrairement à la spec d'origine). **Toute pièce jointe** est ingérée via `feed_document()`, même non extractable (fallback métadonnées). Cut-off initial : 20/05/2026.

---

## 2. Ingestion email — `feed_correspondance()`

### Déclencheur
Dans `app/workers/imap_poller.py`, après classification d'un email (hors newsletter/phishing) :

```python
asyncio.create_task(
    feed_correspondance(
        message_id=f"{mailbox.name}_{uid}",
        direction="in",
        date=date_str,          # "2026-05-28"
        heure=heure_str,        # "14:30"
        expediteur=sender,
        destinataire=mailbox.user,
        objet=subject,
        body=body,              # texte brut (HTML détagué si besoin)
        marque=_marque,         # "detectivebelgique" | "detectivebelgium" | "dpdhu"
        dossier_id=dossier_id,  # ref sujet ou "marque_slug-email"
        categorie=category,     # "demande_client", "facture"...
        zone="jaune",
        langue=language,        # "fr" | "nl" | "en"
        priorite=priority,      # high → "urgent", normal → "normal", low → "faible"
        base_url=settings.cerveau2_base_url,
        api_secret=settings.cerveau2_api_secret,
    )
)
```

### Mapping des slugs `marque`

| Nom interne (code) | Slug Cerveau2 |
|---|---|
| `detective_belgique` | `detectivebelgique` |
| `detective_belgium` | `detectivebelgium` |
| `dpdh_investigations` | `dpdhu` |

> **Piège historique** : envoyer `detective_belgique` au lieu de `detectivebelgique` provoquait un rattachement incorrect.

### Mapping priorité (fix v1.18.6)

| Priorité interne | Valeur Cerveau2 |
|---|---|
| `high` | `urgent` |
| `normal` | `normal` |
| `low` | `faible` |

> **Pourquoi** : Cerveau2 valide `priorite` via un enum FastAPI (`urgent|normal|faible`). Les valeurs `high`/`low` provoquaient un **HTTP 422** rejetant l'email entier.

### `dossier_id` — dérivation

1. **Référence explicite dans le sujet** : regex `\b([A-Z][A-Z0-9]{2,})\b` (ex: ADF, PRJ2024). Ignoré si dans la liste d'exclusion (RE, FW, TEST...).
2. **Nom anonymisé du client** : slug ASCII safe (ex: `detective_belgique_dusza_virginie`).
3. **Partie locale de l'email** : `virginiedusza439` pour `virginiedusza439@gmail.com`.
4. **Fallback** : `GENERAL` (si tout est vide — Cerveau2 rejette les `dossier_id` vides).

> **Internes rejetés** : emails `@digitalhs.biz`, `@detectivebelgique.be`, `@detectivebelgium.com`, `@dpdhuinvestigations.be` retournent `dossier_id=""` (pas de dossier créé pour Daniel ou CDAL).

### Body — troncage (fix v1.18.6)

Cerveau2 met 40-120s par email (chunking + indexation embeddings). Un body de 500K caractères fait planter le timeout.

- **Limite** : 150 000 caractères
- **Suffixe** : `\n\n[... tronqué, taille originale : {len} caractères]`
- **Timeout ingestion** : 120s (retry 3x avec backoff exponentiel)

---

## 3. Extraction fiche entreprise — regex, pas de LLM

### Déclencheur
Après ingestion de l'email ET après ingestion de chaque pièce jointe texte-extractable.

### Signaux recherchés
Regex sur le texte brut (`_extract_entreprise_from_text`) :

| Signal | Regex |
|---|---|
| Forme juridique | `\b(?:SA\|SRL\|BVBA\|SPRL\|ASBL\|SCS\|SCA\|SCRL\|NV\|VBA\|GIE\|SE)\b` |
| Nom + forme | `([A-Z][A-Za-z0-9\s\&\.\-]{1,40}?(?:\s+(?:SA\|SRL\|...)))` |
| TVA belge | `\bBE\s*0?\d{3}[\.\s]?\d{3}[\.\s]?\d{3}\b` |
| Adresse complète | `(?:Rue\|Avenue\|Av\.\|Chaussée\|...)[\s\w\-\.]+?\d+.*?\b\d{4}\b.*?[A-Za-zÀ-Ÿ\-]+` |
| CP + ville | `\b(\d{4})\s+([A-Za-zÀ-Ÿ\-]{3,})\b` |
| Email | `[\w\.-]+@[\w\.-]+\.[a-z]{2,}` (filtré contre les internes) |
| Téléphone belge | `(?:\+32\|0032\|0)(?:\s*\d){8,9}` (filtré contre le numéro de Daniel) |

### Faux positifs éliminés
- Signature Daniel : `detectivebelgique`, `daniel hurchon`, `0779.433.503`, `chaussée bara` → **ignoré**.
- Si le nom extrait contient la signature de Daniel → **ignoré**.
- Si aucun signal secondaire (TVA, adresse, email, téléphone) → **pas de fiche créée**.

### Format de la fiche créée (type=`fiche_entreprise`)

```markdown
# Nom Entreprise SA

**Dossier** : ADF
**Source** : email du 2026-05-28 — Sujet original

## Coordonnées
- **TVA** : BE0123456789
- **Adresse** : Rue ...
- **Emails** : contact@example.com
- **Téléphones** : +32 ...
```

Stockage Cerveau2 : `02_dossiers/{dossier_id}/documents/YYYY-MM-DD_fiche_entreprise_{slug}.md`

---

## 4. Extraction fiche contact — LLM classifier

### Déclencheur
Tous les emails **sauf newsletter/phishing** (car ils contiennent rarement un contact client pertinent).

### Pré-filtre regex
Si le body ne contient **aucun** signal de contact (téléphone belge, +32, code postal + ville, Rue/Avenue...) → **skip** (évite d'appeler le LLM pour rien).

### Prompt LLM (classifier dédié)

```
Extrait les coordonnées de la PERSONNE MENTIONNÉE dans ce message
(client/demandeur, pas l'expéditeur technique ni la signature de Daniel/Detective.be).
Réponds UNIQUEMENT en JSON, sans texte autour.
Format : {"nom":"...","prenom":"...","adresse":"...","code_postal":"...",
         "ville":"...","telephone":"...","email":"..."}
Mets null pour les champs absents. Si aucune coordonnée trouvée, réponds: {}
```

- **Modèle** : `llm_model_classifier` (défaut : même que le classifier de catégorie)
- **Max tokens** : 250
- **Temperature** : 0.0
- **Texte tronqué** : 2500 caractères (évite de surcharger le LLM)

### Parsing JSON robuste
Le LLM peut wrapper le JSON dans du markdown ou des backticks. On utilise :
```python
json_match = re.search(r'\{[^{}]*\}', raw, re.DOTALL)
```

### Faux positifs éliminés
- Si tous les champs sont vides/null → **pas de fiche**.
- Si l'email extrait appartient à un domaine interne → **ignoré**.
- Si le téléphone extrait termine par `433503` (numéro de Daniel) → **ignoré**.

### Format de la fiche créée (type=`fiche_contact`)

```markdown
# Fiche contact — Jean Dupont

**Dossier** : ADF
**Source** : email du 2026-05-28 — Sujet original

## Coordonnées
- **Nom** : Jean Dupont
- **Adresse** : Rue de la Loi 1
- **Localité** : 1000 Bruxelles
- **Téléphone** : +32 470 12 34 56
- **Email** : jean@example.com
```

### Clé stable (`doc_id`)
Hash déterministe basé sur `nom:email:téléphone:dossier_id` pour éviter les doublons Cerveau2 :
```python
doc_id = f"contact-{hashlib.md5(f'{nom}:{email}:{tel}:{dossier_id}'.encode()).hexdigest()[:12]}"
```

---

## 5. Ingestion des pièces jointes — 100%, zéro tolérance

### Règle
Toute pièce jointe d'un email (hors newsletter/phishing) est ingérée dans Cerveau2. **Même si le texte n'est pas extractable.**

### Flux
1. Extraction texte via `extract_text_bytes()` (OCR Tesseract pour images, pdfplumber pour PDF, python-docx pour DOCX...)
2. Si texte vide → fallback body avec métadonnées :
   ```
   [Pièce jointe non extractable automatiquement]
   Fichier : scan.pdf
   Taille : 124000 octets
   Type : .pdf
   ```
3. `doc_id` stable : `att-{mail_id}-{md5(mail_id:filename)[:12]}`
4. Type Cerveau2 : `document`

### Double extraction
Si une PJ contient du texte extractable, les fiches entreprise ET contact sont **aussi** extraites depuis la PJ (en plus de l'email).

---

## 6. Nuage de liaison — wikilinks et relations YAML

### Où ça se passe
`app/charlie.py` → `_resolve_links()` appelé après chaque `query_vault()`.

### Objectif
Quand Charlie reçoit une réponse Cerveau2 contenant des notes avec `[[wikilinks]]`, il va chercher les notes liées automatiquement pour enrichir le contexte (jusqu'à 5 liens max).

### Sources de liens scannées

1. **Wikilinks dans le contenu** : `[[Jean Dupont]]`, `[[ADF/_index]]`
2. **Clés relationnelles dans le frontmatter YAML** :
   - `employeur`, `adresse_principale`, `related`, `dossier`, `lieu`, `personne`, `entities`
   - **Familial** : `epouse`, `mari`, `conjoint`, `compagne`, `compagnon`, `fille`, `fils`, `enfant`, `pere`, `mere`, `parent`, `soeur`, `frere`, `cousin`, `cousine`, `oncle`, `tante`

### Résolution de slug → chemin

```
slug "Jean Dupont"
  → 04_entities/personnes/jean-dupont.md
  → 04_entities/societes/jean-dupont.md
  → 04_entities/lieux/jean-dupont.md
  → 02_dossiers/jean-dupont/_index.md
  → 03_doctrine/jean-dupont.md
  → jean-dupont.md
```

### Dédoublonnage
- Slugs déjà présents dans la réponse originale ne sont pas re-fetchés.
- Max 5 liens résolus par requête (limite pour éviter les explosions de contexte).

---

## 7. Types de documents Cerveau2 créés par Charlie

| Type Cerveau2 | Source | Créateur | Contenu |
|---|---|---|---|
| `correspondance` | Email entrant | `feed_correspondance()` | Body + metadata IMAP |
| `document` | Pièce jointe | `feed_document()` | Texte extrait ou fallback métadonnées |
| `fiche_entreprise` | Body ou PJ | `_extract_and_feed_entreprise()` | Nom, TVA, adresse, emails, tél |
| `fiche_contact` | Body ou PJ | `_extract_and_feed_contact()` | Nom, prénom, adresse, CP, ville, tél, email |
| `correction` | Feedback utilisateur | `push_correction()` | Question + réponse corrigée |
| `task` | Extraction auto | Cerveau2 interne | Tâches détectées dans les emails (ex: "les informations pour le devis") |

---

## 8. Garde-fous et pièges résolus (historique)

| Date | Problème | Cause | Fix |
|---|---|---|---|
| 2026-05-29 | Emails `high`/`low` non ingérés | Enum Cerveau2 : `urgent\|normal\|faible` | `_map_priority()` : `high→urgent`, `low→faible` (v1.18.6) |
| 2026-05-29 | Timeout ingestion systématique | Cerveau2 met 40-120s (embeddings) | Timeout client 15s → **120s** (v1.18.6) |
| 2026-05-29 | `dossier_id` vide rejeté | Validation FastAPI `min_length=1` | Fallback `"GENERAL"` (v1.18.6) |
| 2026-05-29 | Body trop long → timeout | Emails de 300K+ caractères | Troncage à 150K caractères (v1.18.6) |
| 2026-05-28 | Emails non ingérés (55 échecs) | Infomaniak rejetait 2e connexion IMAP | Réutilisation connexion poller pour Drafts (v1.18.3) |
| 2026-05-20 | 422 sur `/query` | `limit: 100` > max 20 Cerveau2 | Cap côté client à 20 |
| 2026-05-20 | Mauvais rattachement marque | `detective_belgique` au lieu de `detectivebelgique` | Mapping explicite `_MARQUE_CERVEAU2` |

---

## 9. Commandes de diagnostic

```bash
# Tester l'ingestion d'un email manuellement
curl -s -X POST https://cerveau2-det.digitalhs.biz/ingest-email \
  -H "Authorization: Bearer $CERVEAU2_API_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"message_id":"test-123","direction":"in","date":"2026-05-29","heure":"10:00","expediteur":"a@b.com","destinataire":"c@d.com","objet":"Test","body":"Corps","marque":"detectivebelgique","dossier_id":"TEST","categorie":"demande_client","zone":"jaune","langue":"fr","priorite":"urgent"}'

# Vérifier qu'un dossier existe et a des notes
curl -s "https://cerveau2-det.digitalhs.biz/dossiers?since=2026-05-20" \
  -H "Authorization: Bearer $CERVEAU2_API_SECRET"

# Query sur un dossier précis
curl -s -X POST https://cerveau2-det.digitalhs.biz/query \
  -H "Authorization: Bearer $CERVEAU2_API_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"question":"provision","dossier_id":"virginiedusza439","limit":3}'
```

---

## 10. Références

- Client HTTP : `app/cerveau_client.py`
- Poller / extraction : `app/workers/imap_poller.py`
- Dossier ID : `app/cerveau_dossier.py`
- Résolution liens : `app/charlie.py` → `_resolve_links()`
- API Cerveau2 : `docs/CERVEAU2_API.md`
- Intégration générale : `docs/CERVEAU2_INTEGRATION.md`
