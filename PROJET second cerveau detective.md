# Cahier des charges — Second Cerveau IA pour cabinet de détective privé

> **Projet** : Plateforme de mémoire augmentée par IA pour Digital Highway Solutions
> **Cible métier** : cabinet de détective privé (dossiers d’enquête, OSINT, filatures, rapports)
> **Pattern de référence** : LLM Wiki d’Andrej Karpathy + structure PARA adaptée enquêtes
> **Stack** : Obsidian (vault Markdown) + Claude Code (agent) + Ollama Cloud / Claude API (inférence) + Syncthing (sync)
> **Hébergement** : VPS Hostinger, Docker, Traefik
> **Auteur du brief** : Chris — Digital Highway Solutions

-----

## 1. Vision & principes directeurs

### 1.1 Le problème à résoudre

Un cabinet de détective accumule **rapidement** un volume considérable d’informations hétérogènes :
rapports de filature, notes vocales transcrites, photos horodatées, captures écran OSINT,
relevés bancaires, plans cadastraux, copies de pièces d’identité, jurisprudence,
témoignages, correspondances clients, etc.

Trois douleurs critiques :

1. **Confidentialité absolue** : secret professionnel du détective + RGPD + données nominatives ultra-sensibles. Aucune fuite tolérable.
1. **Traçabilité judiciaire** : tout élément utilisé doit être citable, daté, sourcé. Audit possible à tout moment.
1. **Effet boule de neige** : à partir de 20-30 dossiers, retrouver « qui a croisé qui en mars 2024 » devient ingérable manuellement.

### 1.2 Principe central : le LLM est le bibliothécaire

Reprise directe du pattern Karpathy : **on ne demande pas au modèle de redécouvrir l’information à chaque session**. On lui fait maintenir un wiki Markdown structuré qui devient la source de vérité du cabinet. À chaque nouvelle pièce versée, l’agent met à jour 5 à 15 fichiers : la fiche personne concernée, la chronologie du dossier, l’index des sources, le graphe d’entités, etc.

L’humain (Chris ou un enquêteur) :

- **Capture** rapidement (drop d’un PDF, dictée vocale, clip web)
- **Direct** (pose des questions, donne des consignes)
- **Décide** (valide, corrige, classe sensible/non-sensible)

L’agent :

- **Lit** les pièces brutes
- **Extrait** entités (personnes, lieux, dates, événements)
- **Rédige** les fiches structurées
- **Met à jour** les index et liens croisés
- **Détecte** contradictions, doublons, fiches orphelines

### 1.3 Pourquoi pas de vector DB ?

À l’échelle d’un cabinet (estimé 50 à 500 dossiers actifs/archivés sur 2 ans), un `index.md` bien tenu tient dans la fenêtre de contexte d’un modèle moderne. L’agent **lit l’index, identifie les 5 à 15 fichiers pertinents, les charge en contexte**, et répond avec citations directes. C’est plus rapide, plus précis (contexte entier vs chunks tronqués), et infiniment plus léger sur VPS qu’une stack Qdrant + Postgres + MinIO.

Seuil de bascule : si le vault dépasse ~5000 fiches ou ~20M de mots, on ajoute `qmd` (hybrid search par-dessus les Markdown) sans rien changer à la structure. Évolution sans migration.

### 1.4 Trois zones de sensibilité

Toute donnée du vault est tagguée :

|Tag          |Zone             |Modèle utilisable                             |Données envoyées              |
|-------------|-----------------|----------------------------------------------|------------------------------|
|`#zone-rouge`|Local strict     |Ollama local (si présent) ou aucun appel cloud|Tout en clair, jamais hors VPS|
|`#zone-jaune`|Cloud no-training|Ollama Cloud / Claude API                     |**Anonymisé** avant envoi     |
|`#zone-verte`|Public/OSINT     |Tout modèle                                   |En clair                      |

La zone est déterminée par l’agent à l’ingestion, validée par l’humain, et **bloque techniquement** les appels sortants si rouge.

-----

## 2. Architecture technique

### 2.1 Vue d’ensemble

```
┌──────────────────────────────────────────────────────────┐
│  HUMAINS                                                 │
│  ├── Chris (Mac) → Obsidian Desktop + Claude Code        │
│  └── Enquêteurs (laptops) → Obsidian + Claude Code       │
└────────────────────┬─────────────────────────────────────┘
                     │ Syncthing (chiffré bout-en-bout)
                     ▼
┌──────────────────────────────────────────────────────────┐
│  VPS HOSTINGER — Docker + Traefik                        │
│                                                          │
│  ┌────────────────────────────────────────────────────┐  │
│  │ /data/vault/ ← le second cerveau (Markdown pur)    │  │
│  │ • Source de vérité                                 │  │
│  │ • Sauvegardé en git + snapshot quotidien chiffré   │  │
│  └────────────────────────────────────────────────────┘  │
│                                                          │
│  ┌──────────────────┐  ┌─────────────────────────────┐   │
│  │ Syncthing        │  │ API FastAPI (cerveau-api)   │   │
│  │ container        │  │ • POST /ingest              │   │
│  │ port 22000       │  │ • POST /query               │   │
│  │                  │  │ • POST /anonymize           │   │
│  │                  │  │ • GET  /audit/{dossier}     │   │
│  └──────────────────┘  └─────────────┬───────────────┘   │
│                                      │                   │
│                                      │ utilisée par      │
│                                      ▼                   │
│  ┌────────────────────────────────────────────────────┐  │
│  │ Agent IA détective (orchestrateur Claude Code      │  │
│  │ en mode serveur, ou n8n + MCP server du vault)     │  │
│  └────────────────────────────────────────────────────┘  │
└────────────────────┬─────────────────────────────────────┘
                     │ HTTPS sortant (anonymisé)
                     ▼
┌──────────────────────────────────────────────────────────┐
│  CLOUD (no-training, no-retention)                       │
│  • Ollama Cloud  → raisonnement, synthèse                │
│  • Claude API    → analyses complexes, OCR, vision       │
└──────────────────────────────────────────────────────────┘
```

### 2.2 Composants à déployer

|Composant          |Rôle                                                          |RAM    |Image Docker                |
|-------------------|--------------------------------------------------------------|-------|----------------------------|
|`cerveau-vault`    |Volume Docker hébergeant le vault                             |0      |(volume nommé)              |
|`cerveau-syncthing`|Sync avec laptops du cabinet                                  |~50 Mo |`syncthing/syncthing:latest`|
|`cerveau-api`      |API FastAPI : ingest, query, anonymize, audit                 |~150 Mo|custom (`python:3.12-slim`) |
|`cerveau-git`      |Cron qui commit le vault toutes les heures                    |~20 Mo |`alpine/git` + cron         |
|`cerveau-backup`   |Snapshot quotidien chiffré (restic vers B2 ou Hetzner Storage)|~50 Mo |`restic/restic`             |

**Total RAM : ~270 Mo**. Comparé à Qdrant+Postgres+MinIO (~2-4 Go), gain massif. Tu gardes la marge pour n8n et tes autres apps.

### 2.3 Réseau Docker & Traefik

Réseau dédié `cerveau-net` isolé. Seules deux choses sortent du réseau :

- **Syncthing** : port 22000 TCP/UDP, exposé en direct (NAT), pas de Traefik (protocole binaire)
- **API FastAPI** : derrière Traefik à `cerveau.digitalhs.biz`, **auth mTLS obligatoire** (certificats clients délivrés par toi à chaque enquêteur, révocables)

Pas d’exposition publique du vault lui-même. Les enquêteurs accèdent au contenu uniquement via Syncthing (chiffré E2E) ou via l’API authentifiée.

-----

## 3. Structure du vault

### 3.1 Arborescence

```
/data/vault/
├── 00_system/
│   ├── AGENTS.md              ← instructions canoniques pour Claude Code
│   ├── README.md              ← humains : comment utiliser ce vault
│   ├── index.md               ← carte de tout le contenu (maintenu par l'agent)
│   ├── log.md                 ← journal des opérations (append-only)
│   ├── glossary.md            ← termes métier (filature, surveillance discrète, etc.)
│   └── templates/             ← templates de fiches
│       ├── _dossier.md
│       ├── _personne.md
│       ├── _lieu.md
│       ├── _evenement.md
│       ├── _societe.md
│       └── _source.md
│
├── 01_inbox/                  ← raw, jamais édité manuellement
│   ├── 2026-05-15_rapport_filature_X.pdf
│   ├── 2026-05-14_dictee_terrain.m4a
│   └── ...
│
├── 02_dossiers/
│   ├── _index.md              ← liste tous les dossiers actifs/clos
│   └── 2026-001_MARTIN-vs-DURAND/
│       ├── _index.md          ← résumé exécutif du dossier
│       ├── _chronologie.md    ← timeline générée par l'agent
│       ├── _zone.md           ← #zone-rouge / #zone-jaune
│       ├── personnes/
│       │   ├── martin-jean.md
│       │   └── durand-sophie.md
│       ├── evenements/
│       │   └── 2026-05-12_rdv-cafe-flore.md
│       ├── lieux/
│       │   └── cafe-de-flore-paris-6.md
│       ├── societes/
│       ├── pieces/            ← rapports rédigés, attestations
│       │   └── rapport-final.md
│       └── sources/           ← copies de pièces avec frontmatter de provenance
│           ├── 2026-05-12_photo-001.md  ← contient pointer + métadonnées
│           └── ...
│
├── 03_doctrine/               ← méthodologie, jurisprudence, OSINT
│   ├── methodologie/
│   │   ├── filature-piedmobile.md
│   │   └── retrosurveillance.md
│   ├── jurisprudence/
│   └── osint/
│       ├── bce-belgique.md
│       └── infogreffe-france.md
│
├── 04_entities/               ← « CRM » transversal aux dossiers
│   ├── _index.md
│   ├── personnes/             ← fiche unique par personne, croise les dossiers
│   ├── societes/
│   └── lieux/
│
├── 05_clients/                ← coordonnées + facturation + accords
│   └── 2026-001_acme-corp.md
│
├── 99_archives/               ← dossiers clos > 1 an
│   └── 2024/
│
└── 99_attachments/            ← binaires (photos, audio, PDF originaux)
    └── 2026-001_MARTIN-vs-DURAND/
        ├── filature-2026-05-12-cafe-flore-001.jpg
        └── ...
```

### 3.2 Conventions de nommage

- **Dossiers** : `ANNÉE-NNN_TITRE-COURT` (ex: `2026-001_MARTIN-vs-DURAND`)
- **Personnes** : `nom-prenom.md` en minuscules avec tirets (ex: `martin-jean.md`)
- **Événements** : `YYYY-MM-DD_titre-court.md` (ex: `2026-05-12_rdv-cafe-flore.md`)
- **Pièces** : `YYYY-MM-DD_type-titre.md` (ex: `2026-05-12_photo-001.md`)

Toutes les fiches ont un **frontmatter YAML** structuré (cf. templates section 4).

### 3.3 Wikilinks et tags : règles d’or

- **Toute mention d’une personne** dans un événement → wikilink `[[martin-jean]]`
- **Tag du dossier** présent sur **chaque** fiche : `#dossier/2026-001`
- **Tag de zone** présent sur chaque fiche : `#zone-jaune`
- **Tag de statut** : `#statut/actif`, `#statut/archive`, `#statut/sous-scelles`
- Wikilink **toujours sans extension** (`[[martin-jean]]` pas `[[martin-jean.md]]`)
- Si une entité existe à la fois dans un dossier et dans `04_entities/`, les deux fiches se référencent mutuellement, l’entité globale étant la source de vérité.

-----

## 4. Templates de fiches

### 4.1 Template Dossier (`_index.md`)

```markdown
---
type: dossier
id: 2026-001
client: "[[2026-001_acme-corp]]"
statut: actif        # actif | suspendu | clos | sous-scelles
zone: jaune          # rouge | jaune | verte
date_ouverture: 2026-05-01
date_cloture: null
mots_cles: [filature, conjoint, soupcon-adultere]
detective_principal: chris
detectives_associes: []
budget_alloue_eur: 4500
budget_consomme_eur: 1240
---

# Dossier 2026-001 — MARTIN vs DURAND

## Mission
Vérification d'éléments factuels concernant les déplacements de [[durand-sophie]]
sur la période 2026-05-01 → 2026-06-30, à la demande de [[martin-jean]].

## Personnes impliquées
- Cible principale : [[durand-sophie]]
- Donneur d'ordre : [[martin-jean]]
- Tiers identifiés : [[lefevre-paul]] (rencontré le 2026-05-12)

## Chronologie
Voir [[_chronologie]] (mise à jour automatique par l'agent).

## Synthèse en cours
*Mise à jour par l'agent à chaque ingestion. Ne pas éditer manuellement.*

État au 2026-05-15 : 3 sorties documentées de la cible, dont 1 RDV répété
avec un tiers masculin identifié comme [[lefevre-paul]]. Aucune preuve
d'adultère matériel à ce stade. Filature à poursuivre selon plan.

## Pièces principales
- [[2026-05-12_rapport-filature]]
- [[2026-05-13_rapport-osint-lefevre-paul]]

## Notes opérationnelles
- Cible utilise probablement un VPN sur son téléphone (à confirmer)
- Pas de surveillance véhicule possible (parking sous-sol fermé)
```

### 4.2 Template Personne (`personnes/durand-sophie.md`)

```markdown
---
type: personne
nom: Durand
prenom: Sophie
date_naissance: 1985-03-14
nationalite: francaise
adresse_actuelle: "12 rue de la Paix, 75002 Paris"
profession: avocate
employeur: "[[cabinet-leblanc-associes]]"
zone: jaune
dossiers: ["[[2026-001_MARTIN-vs-DURAND/_index]]"]
sources_identification:
  - "[[2026-05-12_photo-001]]"
  - "[[2026-05-13_capture-linkedin]]"
status_juridique: cible-enquete-civile
---

# Sophie Durand

## Identification
Née le 14 mars 1985 à Lyon. Avocate au barreau de Paris depuis 2011.
Identifiée formellement par recoupement photo + LinkedIn + plaque immatriculation.

## Apparitions dans le dossier 2026-001
- 2026-05-12 : RDV [[cafe-de-flore-paris-6]] avec [[lefevre-paul]] de 14h30 à 16h05
- 2026-05-14 : sortie domicile → cabinet, comportement nominal

## Liens connus
- Conjoint : [[martin-jean]] (donneur d'ordre du dossier)
- Contact répété : [[lefevre-paul]] (au moins 2 rencontres en 7 jours)
- Collègue : [[leblanc-marc]]

## Habitudes observées
- Quitte le domicile en moyenne à 8h45 du lundi au vendredi
- Pratique le yoga le mercredi soir (studio à confirmer)

## À investiguer
- [ ] Confirmer studio yoga (potentiel point de rencontre régulier)
- [ ] Vérifier déplacements weekend
```

### 4.3 Template Événement (`evenements/2026-05-12_rdv-cafe-flore.md`)

```markdown
---
type: evenement
date: 2026-05-12
heure_debut: "14:30"
heure_fin: "16:05"
lieu: "[[cafe-de-flore-paris-6]]"
participants:
  - "[[durand-sophie]]"
  - "[[lefevre-paul]]"
observateur: chris
dossier: "[[2026-001_MARTIN-vs-DURAND/_index]]"
zone: jaune
fiabilite: haute    # haute | moyenne | basse
sources:
  - "[[2026-05-12_photo-001]]"
  - "[[2026-05-12_photo-002]]"
  - "[[2026-05-12_notes-terrain]]"
---

# RDV Café de Flore — 2026-05-12

## Description factuelle
À 14h28, [[durand-sophie]] arrive seule à pied par la rue Saint-Benoît.
S'installe en terrasse côté boulevard Saint-Germain.
À 14h33, [[lefevre-paul]] arrive en taxi, embrassade prolongée sur la joue
(≈4 secondes, captée sur [[2026-05-12_photo-002]]), s'installe en face.

Conversation soutenue, ton détendu. Aucun document échangé visible.
Trois consommations partagées (deux cafés, un verre de vin blanc, un kir).

Départ séparé : Lefèvre à 16h02 (sud, vers métro Saint-Germain),
Durand à 16h05 (nord-ouest, à pied).

## Interprétation
**Aucune interprétation enregistrée dans ce fichier — pour synthèse, voir
[[_chronologie]] et synthèse dossier.**

## Pièces
- Photo 001 : arrivée séparée, horodatée par boîtier
- Photo 002 : salutation
- Notes terrain : déroulé minute par minute
```

### 4.4 Template Source / Pièce (`sources/2026-05-12_photo-001.md`)

```markdown
---
type: source
sous_type: photo
date_capture: 2026-05-12T14:28:42+02:00
auteur_capture: chris
fichier: "99_attachments/2026-001_MARTIN-vs-DURAND/filature-2026-05-12-cafe-flore-001.jpg"
sha256: a3f5b8c2e1...
gps: "48.8540, 2.3320"
appareil: "Sony RX100 VII"
zone: jaune
chaine_de_garde:
  - { date: 2026-05-12T14:28:42+02:00, action: capture, par: chris }
  - { date: 2026-05-12T19:15:00+02:00, action: import-vault, par: chris }
hash_verifie: true
exif_intact: true
---

# Photo 001 — 2026-05-12 14:28

Photographie horodatée de l'arrivée de [[durand-sophie]] en terrasse du
[[cafe-de-flore-paris-6]]. Cadrage large, identité reconnaissable.

Lien événement : [[2026-05-12_rdv-cafe-flore]]
```

### 4.5 Template Index global (`00_system/index.md`)

Fichier maintenu **exclusivement par l’agent**. Format strict :

```markdown
---
type: index
last_update: 2026-05-15T18:30:00+02:00
total_dossiers_actifs: 7
total_dossiers_archives: 23
total_personnes_entities: 184
total_pieces_sources: 1247
---

# Index du vault

## Dossiers actifs
- [[2026-001_MARTIN-vs-DURAND]] — adultère soupçonné, en cours, 3 sorties documentées
- [[2026-002_DUBOIS-due-diligence]] — DD pré-acquisition, OSINT principalement
- ... (max 50 lignes, format identique)

## Dossiers récemment clos (90 derniers jours)
- [[2026-A012_TREMBLAY]] — clos 2026-04-22, succès

## Méthodologies disponibles
- [[methodologie/filature-piedmobile]]
- [[methodologie/retrosurveillance]]
- ... (liste plate)

## Entités transversales fréquentes
*Personnes apparaissant dans 2+ dossiers — à surveiller pour conflits d'intérêts.*
- [[lefevre-paul]] (2 dossiers : 2026-001, 2025-A089)
```

L’agent garantit que **tout fichier du vault** est référencé soit dans cet index, soit dans un sous-index de dossier.

-----

## 5. Le fichier `AGENTS.md` (instructions canoniques)

> Ce fichier vit à `00_system/AGENTS.md`. Claude Code le lit à chaque opération sur le vault. C’est le contrat opérationnel de l’agent.

```markdown
# AGENTS.md — Second Cerveau Détective DHS

## Qui tu es
Tu es l'agent mainteneur du second cerveau de Digital Highway Solutions,
un cabinet de détective privé. Tu travailles dans un vault Obsidian
structuré selon la doctrine PARA adaptée aux enquêtes.

Tu n'es pas un assistant généraliste. Tu es un **bibliothécaire judiciaire**.
Chaque action que tu prends doit être traçable, sourcée, et défendable
devant un magistrat.

## Tes obligations absolues (non-négociables)

1. **JAMAIS** envoyer en clair vers un modèle cloud un fichier marqué
   `#zone-rouge`. Si la tâche nécessite du raisonnement cloud, refuse
   et explique. Demande à l'humain de déclasser ou de traiter localement.

2. **JAMAIS** modifier un fichier dans `99_attachments/`. Ces fichiers
   sont des originaux à valeur potentielle de preuve. Tu peux les LIRE
   pour extraction, jamais les éditer.

3. **JAMAIS** modifier un fichier dans `01_inbox/`. C'est du raw. Tu
   le lis, tu crées des fiches dérivées, tu ne touches pas à l'original.

4. **TOUJOURS** ajouter une ligne au `log.md` après toute opération
   d'écriture (création, modification, fusion, archivage), avec :
   timestamp ISO 8601, type d'action, fichiers touchés, raison.

5. **TOUJOURS** anonymiser avant un appel cloud sur fichier `#zone-jaune` :
   remplacer noms propres, dates de naissance, adresses, numéros par
   des tokens (PERSONNE_A, LIEU_A, DATE_A...). Conserver le mapping
   localement, dé-anonymiser la réponse.

6. **TOUJOURS** citer la source dans les synthèses : tout fait avancé
   doit pointer vers le fichier source via wikilink.

7. **JAMAIS** d'interprétation dans les fiches `evenements/` ou
   `sources/`. Faits bruts uniquement. Les interprétations vivent dans
   les sections « Synthèse en cours » des `_index.md` de dossier.

## Les opérations canoniques

### `/ingest` — ajouter une pièce au vault

Quand un fichier apparaît dans `01_inbox/` :

1. Identifie le type (PDF rapport, audio dictée, image, capture web, doc texte).
2. Si audio : transcris d'abord (Ollama Cloud whisper ou équivalent).
3. Si image : OCR + extraction EXIF (date, GPS si présent).
4. Identifie le dossier de rattachement (demande à l'humain si ambigu).
5. Calcule le SHA256 du fichier original, copie-le dans `99_attachments/<dossier>/`.
6. Crée la fiche `sources/<YYYY-MM-DD>_<type>-<NNN>.md` avec frontmatter
   complet (chaîne de garde, EXIF, hash).
7. Extrait les entités : personnes, lieux, sociétés, dates, événements.
8. Pour chaque entité :
   - Cherche si elle existe déjà (dans le dossier, puis dans `04_entities/`).
   - Si oui : ajoute une apparition, mets à jour les sections concernées.
   - Si non : crée une nouvelle fiche depuis le template approprié.
9. Si l'extraction révèle un événement nouveau : crée la fiche `evenements/`.
10. Mets à jour `_chronologie.md` du dossier.
11. Mets à jour `_index.md` du dossier (synthèse en cours, compteurs).
12. Mets à jour `00_system/index.md` (compteurs globaux).
13. Déplace le fichier original de `01_inbox/` vers une zone traitée
    (`01_inbox/_processed/`) avec datage.
14. Écris la ligne de log.

**Touche typiquement 5 à 15 fichiers en une passe.** C'est normal et attendu.

### `/query <question>`

1. Lis `00_system/index.md`.
2. Si la question mentionne un dossier explicite : lis son `_index.md`
   et son `_chronologie.md`.
3. Identifie 3 à 10 fiches potentiellement pertinentes via les wikilinks
   de l'index et la structure du vault. **Pas de grep aveugle.** Tu navigues
   comme un humain ouvrirait des onglets.
4. Lis ces fiches dans leur intégralité.
5. Si insuffisant : élargis à 5 fichiers supplémentaires par sauts de wikilink.
6. Synthétise la réponse, **avec citations wikilink obligatoires** pour
   chaque fait. Pas de fait sans source.
7. Si la question demande raisonnement complexe sur fichiers `#zone-jaune` :
   anonymise, appelle Claude API ou Ollama Cloud, dé-anonymise, recompose.
8. Si tu n'as pas trouvé : dis-le. **Ne fabule jamais.**

### `/lint`

Passe de maintenance hebdomadaire. À exécuter avec confirmation humaine.

- Détecte wikilinks cassés (cible n'existe pas) → propose création ou correction.
- Détecte fiches orphelines (aucun fichier ne pointe vers elles) → propose
  rattachement ou archivage.
- Détecte doublons d'entités (même personne, deux fiches) → propose fusion.
- Détecte contradictions factuelles entre fiches → soulève une alerte.
- Détecte fiches `evenements/` sans source → soulève une alerte.
- Détecte fiches `sources/` avec hash non vérifié ou EXIF corrompu → alerte.
- Génère un rapport `00_system/lint-YYYY-MM-DD.md`.

### `/cloturer-dossier <id>`

1. Vérifie qu'il y a un `pieces/rapport-final.md` validé.
2. Verrouille les fichiers du dossier (read-only via frontmatter `status: clos`).
3. Génère un export ZIP signé (signature détachée GPG avec ta clé cabinet)
   contenant tout le dossier + tous ses `99_attachments`.
4. Déplace le dossier vers `99_archives/<année>/`.
5. Met à jour les index.
6. Logue.

## Comportement face à l'ambiguïté

- **Demande, ne devine pas** sur tout ce qui touche à l'identification
  d'une personne, au rattachement à un dossier, ou à la classification
  de zone.
- **Devine et propose** sur tout ce qui est mise en forme, structure
  de fiche, génération de chronologie.

## Style d'écriture pour les fiches

- Phrases courtes, factuelles, voix active.
- Pas d'adjectifs subjectifs dans les fiches `evenements/` ou `sources/`.
  « Sourire prolongé » ✗ → « Sourire de 4 secondes » ✓.
- Heures au format 24h.
- Dates au format ISO 8601 dans le frontmatter, format français lisible
  dans le corps si besoin.
- Coordonnées GPS en décimal.
```

-----

## 6. La couche API (FastAPI ultra-légère)

### 6.1 Pourquoi une API si Claude Code lit directement les fichiers ?

Parce que **d’autres systèmes** voudront interroger le second cerveau :

- n8n (workflows d’automatisation type « envoie-moi le résumé du dossier X chaque vendredi »)
- Le futur portail client si tu en développes un
- Une app mobile de capture terrain
- D’autres agents (Cowork, etc.)

L’API expose **une façade stable** sur le vault, indépendante de qui lit/écrit dessous.

### 6.2 Endpoints

|Méthode|Route                |Description                                                                |Auth                   |
|-------|---------------------|---------------------------------------------------------------------------|-----------------------|
|`POST` |`/ingest`            |Upload binaire + métadonnées → dépose dans `01_inbox/` et déclenche l’agent|mTLS                   |
|`POST` |`/query`             |Question texte + dossier_id optionnel → résultat synthèse + sources        |mTLS                   |
|`POST` |`/anonymize`         |Texte → texte anonymisé + mapping (pour usage par agents externes)         |mTLS                   |
|`POST` |`/deanonymize`       |Texte anonymisé + mapping → texte clair                                    |mTLS                   |
|`GET`  |`/audit/{dossier_id}`|Journal complet des accès et modifications d’un dossier                    |mTLS + rôle admin      |
|`GET`  |`/health`            |Statut composants                                                          |Aucune (interne Docker)|

### 6.3 Anonymisation

Pipeline :

1. Premier passage **regex** : numéros de téléphone, emails, IBAN, plaques, dates ISO.
1. Deuxième passage **NER local** : un petit modèle FR/EN (spaCy `fr_core_news_md` ou un Llama 3.2 1B via Ollama Cloud lui-même, mode batch) extrait personnes, lieux, organisations.
1. Remplacement par tokens `PERSONNE_001`, `LIEU_001`, etc., avec table de mapping en mémoire.
1. Texte envoyé au modèle cloud cible.
1. Réponse reçue, table de mapping réappliquée en sens inverse.
1. Mapping détruit immédiatement après dé-anonymisation (pas de persistance).

**Important** : même Ollama Cloud, qui a une politique no-training/no-retention, ne voit jamais les vrais noms. C’est ceinture **et** bretelles.

-----

## 7. Sync Syncthing — config recommandée

### 7.1 Topologie

- **Serveur central** : container Syncthing sur le VPS, ID stable.
- **Clients** : Mac de Chris + laptops enquêteurs.
- **Mode** : sync bidirectionnelle, mais **versioning trash** activé côté VPS (10 jours) pour récupérer toute suppression accidentelle.

### 7.2 Fichier `.stignore` à placer à la racine du vault

```
# Conflits Syncthing (à ignorer pour éviter les boucles)
*.sync-conflict-*

# Workspace Obsidian (différent par device, pas à synchroniser)
.obsidian/workspace
.obsidian/workspace.json
.obsidian/workspace-mobile.json
.obsidian/workspaces.json

# Caches Obsidian
.obsidian/cache
.obsidian/plugins/*/data.json

# OS
.DS_Store
Thumbs.db
desktop.ini

# Éditeurs
*.swp
*.swo
*~
.~lock.*

# Trash
.trash/

# Caches d'agents (chacun le sien)
.claude/
.cursor/
```

Cette config est **critique** : sans elle, tu auras des dizaines de fichiers `.sync-conflict-*` parce qu’Obsidian réécrit constamment `workspace.json`.

### 7.3 Plugins Obsidian à utiliser sur le Mac

|Plugin            |Rôle                                                                                     |
|------------------|-----------------------------------------------------------------------------------------|
|**Dataview**      |Requêtes structurées sur frontmatter (« toutes les fiches personne du dossier 2026-001 »)|
|**Templater**     |Création de fiches par template avec variables                                           |
|**Obsidian Git**  |Backup local automatique en plus de Syncthing                                            |
|**Excalidraw**    |Schémas de relations entre personnes (idéal en filature)                                 |
|**Kanban**        |Tableau de tâches par dossier                                                            |
|**Periodic Notes**|Daily notes structurées par enquêteur                                                    |

L’agent **n’a pas besoin** que ces plugins tournent côté serveur. Côté Mac uniquement, pour visualisation et édition humaine.

-----

## 8. Backups & sécurité

### 8.1 Triple sauvegarde

1. **Git** : commit horaire automatique du vault. Repo privé sur ton Gitea ou GitHub privé. Permet retour en arrière sur n’importe quelle modification de l’agent.
1. **Restic** : snapshot quotidien chiffré (clé AES-256) vers stockage externe (Backblaze B2, Hetzner Storage Box, ou ton propre NAS).
1. **Syncthing versioning** : 10 jours de trash sur le VPS pour suppressions accidentelles côté client.

### 8.2 Chiffrement at-rest sur le VPS

Volume Docker `cerveau-vault` monté sur un **dossier LUKS** (`/data` chiffré). Clé déverrouillée au boot via un mot de passe que toi seul connais (ou via TPM si Hostinger expose ça — sinon démarrage manuel après redémarrage).

Coût : si le VPS reboot tout seul (mise à jour Hostinger), il faut que tu te connectes pour déverrouiller. Avantage : un opérateur Hostinger qui clone le disque ne lit rien.

### 8.3 Chiffrement at-rest sur les Macs des enquêteurs

FileVault activé, point. Obsidian ne chiffre pas le vault, mais FileVault si.

### 8.4 Politique de mots de passe & clés

- mTLS pour l’API : génère un certificat client par enquêteur, signé par une CA privée que tu héberges.
- Syncthing : IDs de device validés manuellement par toi (pas d’auto-discover).
- Clé GPG cabinet pour signature des exports de dossiers clôturés.

-----

## 9. Plan de sprints Claude Code

**Hypothèse** : tu codes seul avec Claude Code, en mode commits courts.

### Sprint 0 — Préparation (½ journée)

- [ ] Créer le repo `cerveau-detective-dhs` (privé)
- [ ] Initialiser la structure de dossiers du vault (vide mais avec README et AGENTS.md)
- [ ] Rédiger le `AGENTS.md` final (cf. section 5, à personnaliser nom du cabinet)
- [ ] Rédiger les 6 templates de fiches dans `00_system/templates/`

### Sprint 1 — Vault + Syncthing (1 journée)

- [ ] `docker-compose.yml` avec services `cerveau-syncthing` et `cerveau-vault` (volume)
- [ ] Config Traefik pour exposer Syncthing UI sur `sync.digitalhs.biz` (auth basic)
- [ ] Premier client Syncthing sur ton Mac, sync test du vault vide
- [ ] Vérification du `.stignore` (créer un workspace dans Obsidian, confirmer non-sync)
- [ ] Ouverture du vault dans Obsidian Mac, installation des 6 plugins

### Sprint 2 — Agent Claude Code basique (1 journée)

- [ ] Sur ton Mac, ouvrir Claude Code dans le vault
- [ ] Tester que Claude Code lit `AGENTS.md` au démarrage
- [ ] Implémenter `/ingest` en slash command custom :
  - prend un fichier de `01_inbox/`
  - extrait entités via Claude API (pas encore d’anonymisation, on est en zone-verte pour tester)
  - crée les fiches dérivées
  - met à jour les index
- [ ] Tester sur un faux dossier `2026-TEST_DEMO` avec 3 pièces fictives

### Sprint 3 — API FastAPI + anonymisation (1,5 journée)

- [ ] Container `cerveau-api` avec FastAPI
- [ ] Endpoint `/anonymize` (regex + spaCy FR)
- [ ] Endpoint `/deanonymize`
- [ ] Endpoint `/ingest` qui dépose dans `01_inbox/` (l’agent traite ensuite)
- [ ] Endpoint `/query` qui invoque l’agent Claude Code en CLI sur le VPS
- [ ] Auth mTLS via Traefik
- [ ] Tests bout-en-bout avec curl

### Sprint 4 — Slash commands avancées (1 journée)

- [ ] `/lint` : détection de wikilinks cassés, orphelines, doublons
- [ ] `/cloturer-dossier` : verrou + export ZIP signé GPG
- [ ] `/fusionner-entites` : merge de deux fiches personne (avec confirmation)
- [ ] `/synthese-dossier <id>` : génère ou rafraîchit la section « Synthèse en cours »

### Sprint 5 — Backups & sécurité (½ journée)

- [ ] Container `cerveau-git` : cron horaire `git add . && git commit -m "auto" && git push`
- [ ] Container `cerveau-backup` : restic vers Backblaze B2 quotidien
- [ ] Configuration LUKS sur `/data` du VPS
- [ ] Génération CA privée + premier certificat client mTLS
- [ ] Documentation runbook recovery (comment restaurer si VPS perdu)

### Sprint 6 — Premier dossier réel (½ journée)

- [ ] Choisir un dossier ancien ou un dossier en cours
- [ ] Ingérer toutes les pièces via `/ingest`
- [ ] Lire ce que l’agent a produit, corriger les templates si besoin
- [ ] Documenter dans `00_system/README.md` le retour d’expérience pour les futurs enquêteurs

**Total estimé : 6 jours-homme** pour un MVP fonctionnel.

-----

## 10. Métriques de réussite

À 30 jours d’usage :

- 100% des nouvelles pièces passent par `/ingest` (pas de fichiers hors système)
- Temps moyen d’ingestion d’une pièce < 3 minutes (vs 20-30 min en classement manuel)
- 0 fichier orphelin détecté par `/lint` hebdomadaire
- 100% des fiches `evenements/` ont au moins une `sources/` rattachée

À 90 jours :

- Capacité à répondre à « qui est apparu dans plus d’un dossier ? » en < 30 secondes
- Capacité à générer un rapport de synthèse de dossier complet en < 5 minutes
- 0 incident de fuite (aucune zone-rouge n’a quitté le VPS)

-----

## 11. Évolutions ultérieures (hors MVP)

- **Web Clipper Obsidian** configuré pour capturer LinkedIn, registres publics (BCE, Infogreffe, etc.) directement dans `01_inbox/` avec frontmatter pré-rempli.
- **Transcription audio temps réel** sur terrain (dictée pendant filature → transcript dans le vault avant le retour au bureau).
- **Vision** : analyse automatique des photos (reconnaissance de plaques, OCR de panneaux, géolocalisation par paysage).
- **qmd** ajouté quand le vault dépasse 5000 fiches, sans toucher à la structure.
- **Portail client lecture-seule** : un sous-ensemble du dossier accessible au client via URL signée temporaire (rapports finaux uniquement, jamais les sources brutes).
- **MCP server natif** du vault pour Claude Code et Cowork (en remplacement du shell direct).

-----

## 12. Risques identifiés & mitigations

|Risque                                         |Probabilité|Impact  |Mitigation                                                                                       |
|-----------------------------------------------|-----------|--------|-------------------------------------------------------------------------------------------------|
|L’agent hallucine une fiche personne           |Moyenne    |Élevé   |`/lint` détecte fiches sans source. Règle « pas de fait sans wikilink ».                         |
|Fuite via cloud malgré anonymisation           |Faible     |Critique|Double passage NER + audit log de chaque appel sortant.                                          |
|Sync conflict massif Syncthing                 |Moyenne    |Moyen   |`.stignore` strict + résolution manuelle hebdo.                                                  |
|Perte du VPS Hostinger                         |Faible     |Élevé   |Restic vers B2 + git remote. RTO < 4h.                                                           |
|Saturation contexte agent (vault > 5000 fiches)|Long terme |Moyen   |Ajout `qmd` planifié dès 3000 fiches.                                                            |
|Compromission d’un laptop enquêteur            |Moyenne    |Élevé   |FileVault + révocation cert mTLS + Syncthing device remove.                                      |
|Demande de saisie judiciaire                   |Faible     |Critique|Vault chiffré at-rest, clé non stockée sur le VPS, procédure légale à documenter avec ton avocat.|

-----

*Fin du cahier des charges. Version 1.0 — 2026-05-15. À itérer après MVP.*