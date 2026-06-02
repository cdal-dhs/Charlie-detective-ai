# Note technique — Recherche factuelle via Cerveau2 (Chat AI Agent)

> **Contexte** : Charlie AI (Detective.be) utilise Cerveau2-Det comme source primaire pour les recherches factuelles (numéros de téléphone, factures, dossiers clients). Cette note résume les patterns et les pièges rencontrés en prod, pour réutilisation dans le projet **Second Cerveau Pro**.

---

## 1. Le piège principal : dense search = implicit AND

Cerveau2 fait un **dense retrieval** (embedding cosine similarity). Quand on envoie une phrase complète comme :

```
"retrouve moi le dossier avec ce téléphone : 0488/411192"
```

Le modèle d'embedding calcule un vecteur moyen de toute la phrase. Les documents qui matchent **tous les concepts** (retrouve + dossier + téléphone + numéro) sont favorisés. Un document qui contient juste le numéro `0488/411192` mais pas les autres mots aura un score faible.

**→ Résultat** : Cerveau2 ne trouve pas le document, même s'il est dans le vault.

---

## 2. Solution : question allégée (keywords purs)

Pour les recherches factuelles, on n'envoie **pas** la phrase complète à Cerveau2. On extrait les identifiants précis et on les envoie seuls.

### 2.1 Règle du score ≥30

Dans `_extract_keywords()`, les identifiants numériques (téléphone, IBAN, numéro de dossier…) reçoivent un score de **30** :

```python
for raw_num in re.findall(r"[\d\s./-]{6,}", question):
    digits_only = re.sub(r"\D", "", raw_num)
    if len(digits_only) >= 6:
        keywords.append((30, digits_only))
```

Le score 30 est supérieur aux mots alphabétiques (max ~24 pour un nom propre de 14 lettres). Donc le numéro est toujours le **premier** keyword.

### 2.2 Logique de construction `vault_question`

```python
kws = [kw for _, kw in _extract_keywords(question)[:5]]
if kws and kws[0].isdigit():
    vault_question = kws[0]        # ex: "0488411192"
else:
    vault_question = " ".join(kws + yrs)
```

**Règle d'or** :
- Si le meilleur keyword est un **numéro** → envoyer **que** ce numéro.
- Si c'est un **nom propre** → envoyer les 5 premiers keywords + années.
- **Jamais** envoyer les verbes d'action ("retrouve", "donne", "cherche").

---

## 3. Le piège du faux négatif LLM

Même quand le retrieval Cerveau2 remonte le bon document, le **LLM de synthèse** (la couche `answer` dans la réponse JSON) peut écrire :

> "Je ne trouve pas ce numéro dans les documents."

…alors que le `context` contient bien le document avec le numéro.

### 3.1 Pourquoi ça arrive

Le LLM de synthèse est entraîné sur des patterns génériques. Parfois il hallucine une négation ou il ne reconnaît pas le numéro dans le contexte (ex: format avec `/` dans le document, format sans `/` dans la question).

### 3.2 Détection et correction

1. **Liste de patterns négatifs** (`_bad_vault`) :
   ```python
   _bad_vault = (
       "je ne trouve pas", "pas trouvé", "aucune information",
       "pas d'information", "aucune donnée", "aucun résultat",
       "n'apparaît pas", "n'apparait pas", "ne figure pas",
       "aucune mention", "n'apparaît", "ne figure",
   )
   ```

2. **Vérification faux négatif** : si Cerveau2 dit "pas trouvé", mais que le numéro recherché est **présent dans `vault_answer`** (la chaîne brute retournée par Cerveau2), alors c'est un faux négatif.

   ```python
   if vault_is_bad:
       for raw_num in re.findall(r"[\d\s./-]{6,}", question):
           digits_only = re.sub(r"\D", "", raw_num)
           if len(digits_only) >= 6 and digits_only in vault_answer:
               vault_is_bad = False   # Faux négatif détecté
               break
   ```

3. **Court-circuit** : si Cerveau2 contient encore des négations malgré les probants en base, on ignore sa réponse et on affiche une réponse propre basée sur les emails trouvés en base :
   ```python
   if any(p in vault_answer.lower() for p in _bad_vault):
       full_answer = "Voici ce que j'ai trouvé :\n\n" + "\n".join(probant_lines)
   ```

---

## 4. Recherche en base SQL comme preuve

Cerveau2 est la **source primaire**, mais les emails en base (`mail_processed` + archives `boite1/2/3.sqlite`) sont les **preuves**.

### 4.1 Normalisation des numéros dans SQL

Les numéros dans les emails peuvent avoir des formats variés :
- `0488/411192`
- `0488.411.192`
- `0488 411 192`
- `0488411192`

La recherche SQL doit normaliser les colonnes **et** la recherche :

```sql
replace(replace(replace(body, '/', ''), '.', ''), ' ', '') LIKE '%0488411192%'
```

### 4.2 Tri chronologique fiable

`ORDER BY received_at DESC` sur du texte RFC 2822 (`"Fri, 9 Jan 2026..."`) fait un **tri lexicographique** (`W` > `F`), pas chronologique.

**→ Fix** : `ORDER BY id DESC LIMIT 20` (id auto-incrément = proxy chronologique correct). Puis tri en Python avec `parsedate_to_datetime` si besoin.

### 4.3 Déduplication des probants

Un même email peut exister dans `mail_processed` (non anonymisé) ET dans `boite2.sqlite` (anonymisé). La déduplication doit ignorer `sender` car il diffère entre les deux sources.

```python
dedup_key = (r.get("subject", "").strip().lower(), r.get("received_at", "").strip())
```

---

## 5. Pipeline complet (récap)

```
Question utilisateur
    │
    ▼
_extract_keywords() → score 30 pour les numéros
    │
    ▼
vault_question = numéro seul (si identifiant numérique)
    │
    ▼
Cerveau2 /query → notes + answer (LLM)
    │
    ▼
Si answer contient une négation → vérifier faux négatif
    │
    ▼
SQL sur mail_processed + archives (normalisation numéro)
    │
    ▼
Déduplication probants + formatage réponse propre
    │
    ▼
Résultat : "Voici ce que j'ai trouvé : - Besoin urgent  (Fri, 9 Jan)"
```

---

## 6. Paramètres recommandés pour Second Cerveau Pro

| Paramètre | Valeur | Pourquoi |
|---|---|---|
| `limit` (factual) | 10–15 | Besoin de contexte, pas juste le top-1 |
| `timeout` | 30s | Le LLM de synthèse Cerveau2 met du temps |
| `context_only` | `False` | On veut la réponse LLM pour détecter les faux négatifs |
| `threshold` | 0.65 | Dense search : ne pas être trop strict |
| `top_k` | 10 | Marge pour le reranking |

---

*Document rédigé après résolution du bug « numéro 0488/411192 non trouvé » — Detective.be v1.20.10.*
