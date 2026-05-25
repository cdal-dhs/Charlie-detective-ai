from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from unicodedata import normalize

import aiosqlite
import structlog

from app.cerveau_client import VaultNote, query_dossiers, query_vault
from app.charlie_memory import (
    is_correction,
    is_memory_query,
    is_save_request,
    query_corrections,
    query_memory,
    save_memory,
)
from app._version import VERSION
from app.config import get_settings
from app.llm.router import complete

log = structlog.get_logger()

CHARLIE_SYSTEM_PROMPT = f"""Tu es Charlie, l'assistant IA personnel de Daniel Hurchon,
détective privé chez Detective.be. Version actuelle : {VERSION}.
Tu es sa précieuse moitié cognitive —
le prolongement de son cerveau qui lui donne accès instantané à son second cerveau (vault Cerveau2)
et à toute sa base de données d'enquêtes.
Tu t'adresses à Daniel comme à un partenaire de confiance : direct, chaleureux, sans langue de bois.
Utilise "tu". Sois concis mais jamais sec. Un peu d'humour détective est bienvenu.

Schéma de la table principale (mail_processed) :
- id INTEGER PRIMARY KEY
- mailbox_name TEXT  — detective_belgique (D_FR), detective_belgium (D_NL),
  dpdh_investigations (D_PD)
- subject TEXT
- sender TEXT
- received_at TEXT (format RFC 2822, ex: "Wed, 20 May 2026 17:20:29 +0800" — NE PAS utiliser pour >= ou <= !)
- category TEXT  — demande_client, urgent, newsletter, facture, spam,
  phishing, rappel, autre
- status TEXT    — pending, approved, rejected, sent, reviewed
- priority TEXT  — high, normal, low
- processed_at TEXT (format ISO YYYY-MM-DD HH:MM:SS — UTILISER POUR LES FILTRES DE DATE)
- body_preview TEXT — aperçu tronqué (~500 caractères) du contenu du mail
- body TEXT — contenu complet du mail
- ai_draft TEXT — brouillon généré par l'IA
- human_draft TEXT — brouillon édité par Daniel
- reviewed_by INTEGER
- reviewed_at TEXT

Règles :
1. Si la question nécessite une requête SQL, génère UNIQUEMENT un SELECT
   (jamais INSERT/UPDATE/DELETE/DROP/ALTER).
2. Formate ta réponse exactement comme ceci :

SQL: <ta requête SELECT sur une seule ligne, sans saut de ligne>
---
RÉPONSE: <ta réponse à Daniel en français, courte et directe, en utilisant "tu">

   IMPORTANT : ta RÉPONSE ne doit JAMAIS être un tableau markdown brut.
   Rédige toujours en phrases, même pour une liste d'emails.

3. Si la question ne nécessite pas de SQL (salutation, question générale),
   laisse SQL vide :

SQL:
---
RÉPONSE: <ta réponse>

4. Pour les dates, utilise TOUJOURS `processed_at` (format ISO YYYY-MM-DD) pour les filtres >=, <= ou BETWEEN.
   N'utilise JAMAIS `received_at` pour des comparaisons de date (format RFC 2822, incompatible).
   Si Daniel demande une date précise (ex: "depuis le 20 mai 2026"), génère : processed_at >= '2026-05-20'
   Si Daniel demande un mois (ex: "en mai 2026"), génère : processed_at >= '2026-05-01' AND processed_at < '2026-06-01'
5. Toujours répondre en français.
6. **Quand Daniel demande ta version actuelle** (ex: "quelle version", "version de Charlie"),
   réponds directement : "Je suis Charlie AI version {VERSION}." — pas besoin de SQL ni de vault.
7. Quand tu listes des emails, inclus TOUJOURS les colonnes `id` et `subject`
   dans ton SELECT (ainsi que les autres colonnes utiles).
   Cela permet de créer des liens cliquables vers la conversation.
   7. Quand Daniel demande le contenu, le détail ou un résumé d'un dossier,
   utilise la colonne `body` (contenu complet) dans ton SELECT, pas `body_preview`.
   Inclus aussi `ai_draft` si pertinent.
8. **DEUX modes de recherche — ne les confonds pas :**
   - **Mode A : recherche par TYPE d'enquête** (filature, adultère, disparition,
     garde d'enfant, contrôle de résidence, harcèlement, etc.) :
     Utilise UNIQUEMENT `category = 'xxx'` exacte. Ne mets JAMAIS de LIKE OR
     sur `subject`, `body` ou `ai_draft` dans ce mode — cela attrape des emails
     non pertinents (factures, newsletters, spam).
   - **Mode B : recherche par mot-clé spécifique** (nom de client, référence de
     dossier, lieu, adresse, etc.) :
     Tu peux utiliser des LIKE OR sur `subject`, `body_preview`, `body` et `ai_draft`.
     Si le mot-clé est un nom de DOSSIER (ex: ADF), cherche AUSSI dans `sender`
     pour attraper les emails du domaine associé (ex: `@groupeadf.com`).
   Inclus `id` et `subject` dans le SELECT pour permettre des liens cliquables.
9. **Questions de comptage (combien, nombre, total)** :
   - Utilise `SELECT COUNT(*) as total FROM mail_processed WHERE ...`
   - Inclus TOUJOURS la condition de date si Daniel la précise, avec `processed_at` (ISO) :
     Exemple : `processed_at >= '2026-05-20'` ou `processed_at >= '2026-01-01' AND processed_at < '2027-01-01'`
   - Ta réponse doit être un nombre brut et clair, pas une liste d'emails.
10. Quand Daniel demande un résumé ou une synthèse, ta RÉPONSE doit
    contenir le résumé en langage naturel — pas juste une liste de champs.
    Analyse le contenu des mails et rédige une synthèse claire et utile.
11. Si la requête SQL retourne 0 ligne, ta RÉPONSE doit dire explicitement
    qu'aucun email n'a été trouvé, sans inventer de résultats.
12. Quand Daniel parle de "filature" ou "surveillance", cherche dans la
    categorie `surveillance` de la base ET interroge le second cerveau (vault Cerveau2)
    qui contient les rapports de terrain, observations et notes d'enquete.
    "Filature" et "surveillance" sont synonymes dans ce contexte.
13. Lexique métier — synonymes courants du cabinet :
    - "adultère" ou "infidélité" → cherche `infidelite`, `adultere`, `tromperie`, `concubinage`
    - "disparition" ou "recherche de personne" → cherche `recherche_personne`,
      `disparition`, `localisation`, `retrouver`
    - "contrôle de résidence" → cherche `controle_residence`, `residence`, `domicile`
    - "garde d'enfant" ou "pension" → cherche `enquete_famille`, `garde`, `pension`, `famille`
    - "harcèlement" → cherche `harcelement`, `intimidation`
    Quand Daniel utilise un terme familier pour un TYPE d'enquête, tu dois
    chercher par `category` exacte correspondante (Mode A). Les synonymes
    ne servent que pour toi, pas pour générer des LIKE OR dans le SQL.
14. Quand tu présentes des résultats (emails, dossiers, archives), classe-les
    TOUJOURS du PLUS RÉCENT au PLUS ANCIEN (`ORDER BY processed_at DESC`).
    Le dossier le plus récent doit apparaître en premier, sans exception.
"""

_DANGEROUS_SQL = (
    "drop", "delete", "insert", "update", "alter",
    "create", "replace", "truncate", "attach", "detach",
)

BOX_ABBR = {
    "detective_belgique": "D_FR",
    "detective_belgium": "D_NL",
    "dpdh_investigations": "D_PD",
}

# Lexique métier exhaustif — enrichissement automatique de la question
# pour garantir que Charlie trouve les emails même quand Daniel utilise
# un terme familier ou un euphémisme.
_ENQUETE_SYNONYMES: dict[str, list[str]] = {
    "adulte": [
        "infidelite", "adultere", "tromperie", "concubinage",
        "soupcon", "jaloux", "jalouse", "ma femme", "mon mari",
        "mon conjoint", "ma compagne", "mon compagnon",
        "tromper", "trahir", "mentir", "amant", "maitresse",
        "liaison", "aventure", "escapade", "malaise", "couple",
    ],
    "surveillance": [
        "surveillance", "filature", "observation", "terrain",
        "pister", "suivre", "espionner", "filmer", "photographier",
        "detective", "enqueteur", "shadowing", "stakeout",
    ],
    "disparition": [
        "disparition", "recherche_personne", "retrouver",
        "localiser", "fugue", "kidnappe", "perdu", "retrouve",
        "missing", "trace", "disparu", "disparue",
    ],
    "residence": [
        "controle_residence", "residence", "domicile",
        "logement", "adresse", "habitation", "cooperative",
        "proprietaire", "locataire", "colocation",
    ],
    "famille": [
        "enquete_famille", "garde", "pension", "famille",
        "enfant", "mineur", "adolescent", "bebe", "nourrisson",
        "divorce", "separation", "rupture", "couple", "concubin",
        "droit de visite", "hebergement", "custodie", "tutelle",
    ],
    "harcelement": [
        "harcelement", "intimidation", "stalking", "menace",
        "persecution", "chantage", "blackmail", "cyberharcelement",
        "insulte", "agression", "violence",
    ],
    "entreprise": [
        "investigation_entreprise", "entreprise", "societe",
        "patron", "salarie", "licenciement", "fraude",
        "detournement", "vol", "abus", "conflit", "concurrence",
        "espionnage industriel", "contrefacon",
    ],
    "materiel": [
        "test_materiel", "materiel", "detecteur", "camera",
        "micro", "gps", "traceur", "bug", "ecoute",
        "matos", "gadget", "technique",
    ],
    "collaboration": [
        "collaboration", "sous_traitance", "partenaire",
        "associe", "confrere", "collegue", "partenariat",
        "mandat", "sous_traitant", "prestataire",
    ],
}


@dataclass
class CharlieResult:
    response_text: str
    sql: str
    rows: list[dict] | None
    sql_safe: bool
    sql_error: str | None
    vault_notes: list[VaultNote] = field(default_factory=list)


def parse_charlie_response(text: str) -> tuple[str, str]:
    """Extrait le SQL et la réponse textuelle du LLM."""
    sql_part = ""
    response_part = ""
    if "---" in text:
        parts = text.split("---", 1)
        first = parts[0].strip()
        if first.lower().startswith("sql:"):
            sql_part = first[4:].strip()
        response_part = parts[1].strip()
        if response_part.lower().startswith("réponse:"):
            response_part = response_part[8:].strip()
    else:
        response_part = text.strip()
    return sql_part, response_part


def is_safe_sql(sql: str) -> bool:
    """Vérifie que le SQL est un SELECT read-only."""
    if not sql:
        return True
    cleaned = sql.lower().strip()
    if not cleaned.startswith("select"):
        return False
    return all(dangerous not in cleaned for dangerous in _DANGEROUS_SQL)


async def run_sql(db_path: Path, sql: str) -> list[dict]:
    """Exécute un SELECT sur agent_state.db et retourne les résultats."""
    async with aiosqlite.connect(db_path) as db, db.execute(sql) as cursor:
        rows = await cursor.fetchall()
        desc = cursor.description
        if desc is None:
            return []
        keys = [d[0] for d in desc]
        return [dict(zip(keys, row, strict=True)) for row in rows]


def _enrichir_question(question: str) -> str:
    """Détecte le type d'enquête et enrichit la question avec des synonymes.

    Cela garantit que le LLM génère un SQL qui cherche dans tous les
    termes connus du métier, même quand Daniel utilise un euphémisme.
    """
    q_norm = _normalize(question)
    extra_terms: list[str] = []
    for type_enquete, synonymes in _ENQUETE_SYNONYMES.items():
        if type_enquete in q_norm:
            for syn in synonymes:
                if syn not in q_norm:
                    extra_terms.append(syn)
            break
        # Sinon : un synonyme est-il présent dans la question ?
        for syn in synonymes:
            if syn in q_norm:
                extra_terms.extend([s for s in synonymes if s not in q_norm])
                break
        if extra_terms:
            break
    if extra_terms:
        enrichie = question + " (synonymes: " + ", ".join(extra_terms[:10]) + ")"
        return enrichie
    return question


_SUMMARY_PROMPT = """Tu es Charlie, l'assistant IA personnel de Daniel Hurchon,
détective privé chez Detective.be. Tu es sa précieuse moitié cognitive —
le prolongement de son cerveau. Tu t'adresses à Daniel comme à un partenaire :
direct, chaleureux, sans langue de bois. Utilise "tu". Un peu d'humour détective est bienvenu.

Question de Daniel : {question}

Résultats SQL ({count} lignes) :
{rows}

RÈGLES ABSOLUES :
1. **NE JAMAIS afficher les champs techniques bruts** (id, sender, body_preview, source_db, etc.).
   Daniel ne veut PAS voir de dump de base de données.
2. **NE JAMAIS afficher les expéditeurs réels** ou le contenu brut des emails.
3. Si les résultats sont une simple liste, présente-les proprement sous forme de liste à puces.
4. **Liens cliquables** : quand tu cites un email spécifique, formate son sujet
   comme un lien markdown vers l'inbox : `[Sujet de l'email](/inbox?q=mot-clef)`.
   Utilise un mot-clef unique du sujet (ex: référence dossier, nom client).
5. Si aucun résultat, dis-le simplement avec une touche d'humour.
"""

_SUMMARY_PROMPT_VAULT = """Tu es Charlie, l'assistant IA personnel de Daniel Hurchon,
détective privé chez Detective.be. Tu es sa précieuse moitié cognitive —
le prolongement de son cerveau qui lui donne accès à son second cerveau (vault Cerveau2).
Tu t'adresses à Daniel comme à un partenaire : direct, chaleureux, sans langue de bois.
Utilise "tu". Un peu d'humour détective est bienvenu.

Tu viens d'exécuter une requête SQL ET consulté le "second cerveau" (vault Cerveau2).

Question de Daniel : {question}

Résultats SQL ({count} lignes) :
{rows}

Notes du second cerveau ({vault_count}) :
{vault_notes}

Souvenirs de Charlie ({memory_count}) :
{memory_notes}

CORRECTIONS UTILISATEUR ({correction_count}) — PRIORITÉ ABSOLUE :
{correction_notes}

RÈGLES ABSOLUES :
1. **CORRECTIONS UTILISATEUR PRIMENT SUR TOUT** — Si des corrections sont
   fournies ci-dessus, elles écrasent le vault ET la mémoire. Daniel a déjà
   corrigé cette information. Utilise-la directement sans discuter.
2. **NE JAMAIS afficher les champs techniques bruts** (id, sender, body_preview, source_db, type, direction, heure null, etc.).
   Daniel ne veut PAS voir de dump de base de données.
3. **NE JAMAIS afficher les expéditeurs réels** ou le contenu brut des emails.
4. **Ne liste pas brute** les champs techniques.
5. **Raconte l'histoire** : qui est le client, de quoi parle ce dossier,
   quelles sont les étapes clés, qui a écrit à qui et quand.
6. Synthétise les emails ET les notes du vault en un récit cohérent et fluide.
7. **Liens cliquables** : chaque fois que tu mentionnes un email spécifique,
   formate son sujet comme un lien markdown vers l'inbox :
   `[Sujet de l'email](/inbox?q=mot-clef)`.
   Utilise un mot-clef unique du sujet (ex: référence dossier AS445, nom client).
8. Si les notes du vault apportent un contexte historique, intègre-le naturellement.
9. **CRITIQUE** — Si SQL retourne 0 ligne mais les Notes du second cerveau
   contiennent une réponse, tu DOIS répondre en te basant UNIQUEMENT sur les
   notes du vault. Ne dis JAMAIS "aucun résultat" quand le vault a trouvé
   l'information. Le vault est ta source de vérité pour les faits non présents
   dans les emails SQL.
10. Si aucun résultat nulle part (SQL vide + vault vide + mémoire vide),
    dis-le simplement à Daniel avec une touche d'humour.
"""


# Mapping type d'enquête → catégorie historique dans les 3 DB sources
_ENQUETE_TO_CATEGORY: dict[str, str] = {
    "adulte": "INFIDELITE",
    "infidelite": "INFIDELITE",
    "tromperie": "INFIDELITE",
    "concubinage": "INFIDELITE",
    "surveillance": "SURVEILLANCE",
    "filature": "SURVEILLANCE",
    "disparition": "RECHERCHE_PERSONNE",
    "recherche_personne": "RECHERCHE_PERSONNE",
    "garde": "ENQUETE_FAMILLE",
    "pension": "ENQUETE_FAMILLE",
    "famille": "ENQUETE_FAMILLE",
    "residence": "CONTROLE_RESIDENCE",
    "entreprise": "INVESTIGATION_ENTREPRISE",
    "fraude": "INVESTIGATION_ENTREPRISE",
    "materiel": "TEST_MATERIEL",
    "detecteur": "TEST_MATERIEL",
    "collaboration": "COLLABORATION",
    "harcelement": "HARCELEMENT",
}


async def _search_historical_by_keyword(
    db_path: Path, keyword: str, year: str | None = None, limit: int = 50,
) -> list[dict]:
    """Cherche dans les 3 DB historiques par mot-clé (subject, body_preview, sender).

    Utilisé quand un dossier spécifique est mentionné (ex: ADF) pour trouver
    tous les emails liés, même ceux antérieurs au cutoff de mail_processed.
    """
    data_dir = db_path.parent
    results: list[dict] = []
    like = f"%{keyword}%"
    for db_name in ("boite1.sqlite", "boite2.sqlite", "boite3.sqlite"):
        db_file = data_dir / db_name
        if not db_file.exists():
            continue
        try:
            async with aiosqlite.connect(db_file) as db:
                sql = (
                    "SELECT id, subject, sender, date, body_preview, category "
                    "FROM emails WHERE (subject LIKE ? OR body_preview LIKE ? OR sender LIKE ?) "
                )
                params: list = [like, like, like]
                if year:
                    sql += "AND date LIKE ? "
                    params.append(f"%{year}%")
                sql += "ORDER BY date DESC LIMIT ?"
                params.append(limit)
                cursor = await db.execute(sql, tuple(params))
                rows = await cursor.fetchall()
                for row in rows:
                    results.append({
                        "id": row[0],
                        "subject": row[1],
                        "sender": row[2],
                        "received_at": row[3],
                        "body_preview": row[4],
                        "category": row[5],
                        "source_db": db_name,
                    })
        except Exception as e:
            log.warning("charlie.historical_keyword_failed", db=db_name, keyword=keyword, error=str(e))
    # Tri global par date
    def _parse_date(r: dict) -> datetime:
        raw = r.get("received_at") or ""
        if not raw:
            return datetime.min
        try:
            dt = parsedate_to_datetime(raw)
            if dt.tzinfo is not None:
                return dt.replace(tzinfo=None)
            return dt
        except Exception:
            pass
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                return dt.replace(tzinfo=None)
            return dt
        except Exception:
            pass
        return datetime.min
    results.sort(key=_parse_date, reverse=True)
    return results


async def _search_historical_by_category(
    db_path: Path, category: str, year: str | None = None, limit: int = 5,
) -> list[dict]:
    """Cherche dans les 3 DB historiques (boite1/2/3) par catégorie fine.

    Filtres appliqués :
    - body_preview non vide et significatif (>30 caractères)
    - subject ni générique ni spam ('Nouveau Message De Détective', 'Formulaire', etc.)
    - sender ni noreply ni no-reply
    - Si `year` est fourni, filtre sur la date (format RFC 2822 ou ISO)
    """
    data_dir = db_path.parent
    results: list[dict] = []
    generic_subjects = ("%Nouveau Message De Détective%", "%Formulaire%", "%Contact%")
    for db_name in ("boite1.sqlite", "boite2.sqlite", "boite3.sqlite"):
        db_file = data_dir / db_name
        if not db_file.exists():
            continue
        try:
            async with aiosqlite.connect(db_file) as db:
                sql = (
                    "SELECT id, subject, sender, date, body_preview, category "
                    "FROM emails WHERE category = ? "
                    "AND body_preview IS NOT NULL AND LENGTH(body_preview) > 30 "
                    "AND sender NOT LIKE '%noreply%' "
                    "AND sender NOT LIKE '%no-reply%' "
                )
                params: list = [category]
                for gs in generic_subjects:
                    sql += "AND subject NOT LIKE ? "
                    params.append(gs)
                if year:
                    sql += "AND date LIKE ? "
                    params.append(f"%{year}%")
                sql += "ORDER BY date DESC LIMIT ?"
                params.append(limit)
                cursor = await db.execute(sql, tuple(params))
                rows = await cursor.fetchall()
                for row in rows:
                    results.append({
                        "id": row[0],
                        "subject": row[1],
                        "sender": row[2],
                        "received_at": row[3],
                        "body_preview": row[4],
                        "category": row[5],
                        "source_db": db_name,
                    })
        except Exception as e:
            log.warning("charlie.historical_search_failed", db=db_name, error=str(e))
    # Tri global par date décroissante — parsing robuste des formats RFC 2822 / ISO
    def _parse_date(r: dict) -> datetime:
        raw = r.get("received_at") or ""
        if not raw:
            return datetime.min
        try:
            dt = parsedate_to_datetime(raw)
            # Normaliser : toujours offset-naive pour comparaison homogène
            if dt.tzinfo is not None:
                return dt.replace(tzinfo=None)
            return dt
        except Exception:
            pass
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                return dt.replace(tzinfo=None)
            return dt
        except Exception:
            pass
        return datetime.min

    results.sort(key=_parse_date, reverse=True)
    return results


async def _search_historical_all(
    db_path: Path, year: str | None = None, limit: int = 50,
) -> list[dict]:
    """Cherche dans les 3 DB historiques tous les emails pertinents (exclut spam/newsletter/phishing)."""
    data_dir = db_path.parent
    results: list[dict] = []
    generic_subjects = ("%Nouveau Message De Détective%", "%Formulaire%", "%Contact%")
    exclude_cats = ("spam", "newsletter", "phishing")
    for db_name in ("boite1.sqlite", "boite2.sqlite", "boite3.sqlite"):
        db_file = data_dir / db_name
        if not db_file.exists():
            continue
        try:
            async with aiosqlite.connect(db_file) as db:
                placeholders = ", ".join(["?"] * len(exclude_cats))
                sql = (
                    f"SELECT id, subject, sender, date, body_preview, category "
                    f"FROM emails WHERE category NOT IN ({placeholders}) "
                    f"AND body_preview IS NOT NULL AND LENGTH(body_preview) > 30 "
                    f"AND sender NOT LIKE '%noreply%' "
                    f"AND sender NOT LIKE '%no-reply%' "
                )
                params: list = list(exclude_cats)
                for gs in generic_subjects:
                    sql += "AND subject NOT LIKE ? "
                    params.append(gs)
                if year:
                    sql += "AND date LIKE ? "
                    params.append(f"%{year}%")
                sql += "ORDER BY date DESC LIMIT ?"
                params.append(limit)
                cursor = await db.execute(sql, tuple(params))
                rows = await cursor.fetchall()
                for row in rows:
                    results.append({
                        "id": row[0],
                        "subject": row[1],
                        "sender": row[2],
                        "received_at": row[3],
                        "body_preview": row[4],
                        "category": row[5],
                        "source_db": db_name,
                    })
        except Exception as e:
            log.warning("charlie.historical_all_failed", db=db_name, error=str(e))
    def _parse_date(r: dict) -> datetime:
        raw = r.get("received_at") or ""
        if not raw:
            return datetime.min
        try:
            dt = parsedate_to_datetime(raw)
            if dt.tzinfo is not None:
                return dt.replace(tzinfo=None)
            return dt
        except Exception:
            pass
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                return dt.replace(tzinfo=None)
            return dt
        except Exception:
            pass
        return datetime.min
    results.sort(key=_parse_date, reverse=True)
    return results


_DOSSIER_RE = re.compile(
    r"(?i:dossier|affaire|projet|enquete|investigation)"
    r"[\s:]+([A-Z][a-zA-Z0-9]{2,})",
)
_HASH_DOSSIER_RE = re.compile(r"#([A-Z][A-Z0-9]{2,})")
_CODE_RE = re.compile(r"\b([A-Z]{3,6})\b")
_YEAR_RE = re.compile(r"\b(20\d{2})\b")


def _extract_dossier_id(question: str) -> str | None:
    m = _DOSSIER_RE.search(question)
    if m:
        return m.group(1)
    m = _HASH_DOSSIER_RE.search(question)
    if m:
        return m.group(1)
    # Codes dossier en majuscules isolés (ex: ADF, DPDH)
    for m in _CODE_RE.finditer(question):
        code = m.group(1)
        if code not in ("SQL", "OK", "HTTP", "API", "URL", "HTML", "XML", "JSON"):
            return code
    return None


def _extract_year(question: str) -> str | None:
    m = _YEAR_RE.search(question)
    return m.group(1) if m else None


_MONTH_FR = {
    "janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12,
    "jan": 1, "fev": 2, "avr": 4, "juil": 7, "aou": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _extract_date_filter(question: str) -> str | None:
    """Extrait une condition SQL de date depuis une question en langage naturel.

    Ex: 'depuis le 20 mai' → "processed_at >= '2026-05-20'"
        'en mai 2026' → "processed_at >= '2026-05-01' AND processed_at < '2026-06-01'"
    Retourne None si aucune date détectable.
    """
    from datetime import date as _d
    q = _normalize(question)
    today = _d.today()
    year_str = _extract_year(question) or str(today.year)
    year = int(year_str)

    # "depuis le D mois" ou "a partir du D mois"
    m = re.search(r'(?:depuis le?|a partir du?)\s+(\d{1,2})\s+([a-záàâéèêîïôùûü]+)', q)
    if m:
        day = int(m.group(1))
        month_name = _normalize(m.group(2))
        month = _MONTH_FR.get(month_name, 0)
        if month and 1 <= day <= 31:
            date_str = f"{year}-{month:02d}-{day:02d}"
            return f"processed_at >= '{date_str}'"

    # "en mois" ou "du mois de mois"
    m = re.search(r'(?:en|du mois de|ce mois)\s+([a-záàâéèêîïôùûü]+)', q)
    if m:
        month_name = _normalize(m.group(1))
        month = _MONTH_FR.get(month_name, 0)
        if month:
            next_month = month + 1 if month < 12 else 1
            next_year = year if month < 12 else year + 1
            return f"processed_at >= '{year}-{month:02d}-01' AND processed_at < '{next_year}-{next_month:02d}-01'"

    # "depuis le 2026-05-20" ou "depuis le 20/05/2026"
    m = re.search(r'depuis le?\s+(\d{4}-\d{2}-\d{2})', question)
    if m:
        return f"processed_at >= '{m.group(1)}'"

    # juste une année (ex: "en 2026", "depuis 2026")
    if year_str and re.search(r'\b' + year_str + r'\b', question):
        return f"processed_at >= '{year_str}-01-01' AND processed_at < '{year + 1}-01-01'"

    return None


def _build_count_sql(question: str, dossier_id: str | None) -> str | None:
    """Génère SQL de comptage sans LLM pour les questions simples (combien d'emails).

    Retourne None si la question est trop complexe pour être gérée programmatiquement.
    """
    q = _normalize(question)

    # Détecte 'combien d'emails reçus / envoyés'
    is_email_count = any(kw in q for kw in ("email", "mail", "message", "courriel"))
    if not is_email_count:
        return None

    date_filter = _extract_date_filter(question)
    where_clauses = []

    if dossier_id:
        where_clauses.append(f"(subject LIKE '%{dossier_id}%' OR body LIKE '%{dossier_id}%')")

    if date_filter:
        where_clauses.append(date_filter)

    # Emails envoyés seulement (mail_processed contient uniquement les emails reçus par le pipeline)
    if any(kw in q for kw in ("envoye", "sortant", "outgoing", "envoyes")):
        where_clauses.append("status = 'sent'")

    where = " AND ".join(f"({c})" for c in where_clauses) if where_clauses else "1=1"
    return f"SELECT COUNT(*) as total FROM mail_processed WHERE {where}"


def _normalize(text: str) -> str:
    return normalize("NFKD", text.lower()).encode("ascii", "ignore").decode("ascii")


def _general_response(question: str) -> str | None:
    """Réponse codée en dur pour les questions générales."""
    q = _normalize(question)
    if "version" in q or "quelle version" in q:
        return f"Je suis Charlie AI version {VERSION}."
    if any(kw in q for kw in ("salut", "bonjour", "coucou", "hey", "hello")):
        return "Salut Daniel ! Prêt à enquêter. Qu'est-ce que je peux faire pour toi ?"
    if any(kw in q for kw in ("ca va", "comment vas-tu", "comment ca va")):
        return "Ça va super, les neurones chauffent et les dossiers sont à jour. Et toi ?"
    if "merci" in q:
        return "Avec plaisir, Daniel ! C'est mon job."
    if "au revoir" in q or "bye" in q or "a plus" in q:
        return "À plus, Daniel ! N'hésite pas si tu as besoin de moi."
    if "tu es qui" in q or "qui es-tu" in q:
        return f"Je suis Charlie AI version {VERSION}, ton assistant détective personnel."
    return None


async def ask_charlie(
    question: str,
    db_path: Path,
    model: str | None = None,
    history: list[dict] | None = None,
) -> CharlieResult:
    """Pipeline Charlie AI V1.14.1 — Prompt unique avec contexte multi-sources."""
    settings = get_settings()
    model = model or settings.llm_model_chat or settings.llm_model_default

    # ── 1. Questions générales (pas de recherche) ──
    general_resp = _general_response(question)
    if general_resp:
        return CharlieResult(
            response_text=general_resp, sql="", rows=None,
            sql_safe=True, sql_error=None, vault_notes=[],
        )

    dossier_id = _extract_dossier_id(question)
    year = _extract_year(question)
    log.info("charlie.ask", question=question[:60], dossier_id=dossier_id, year=year)

    # Détection d'intention (avant les closures — late binding Python)
    q_norm = _normalize(question)
    is_list_request = any(kw in q_norm for kw in ("liste", "lister", "donne-moi", "donne moi", "quels", "quelles", "lesquels", "lesquelles", "montre-moi", "tous les", "toutes les"))
    is_count_request = any(kw in q_norm for kw in ("combien", "nombre", "total", "count", "combien de"))
    is_dossier_count = any(kw in q_norm for kw in ("nouveau dossier", "dossier ouvert", "dossier cree", "dossiers crees", "combien de dossier", "ouvert depuis", "crees depuis", "nouveau client", "nouveaux client", "dossiers client"))
    is_dossier_list = is_list_request and not dossier_id and any(kw in q_norm for kw in ("dossier", "enquete", "enquetes", "affaire", "affaires", "client"))
    is_identity_request = any(kw in q_norm for kw in ("qui est", "nom", "prenom", "contact", "personne", "sappelle", "epouse", "mari", "conjoint"))

    # ── 2. Génération SQL ──
    # Fallback programmatique pour les comptages simples (pas besoin de LLM)
    sql = _build_count_sql(question, dossier_id) if is_count_request else ""

    if not sql:
        try:
            from datetime import date as _date
            today_str = _date.today().isoformat()
            system = CHARLIE_SYSTEM_PROMPT + f"\n\nDate du jour : {today_str}. Si Daniel dit 'aujourd\\'hui', 'ce mois-ci', 'depuis le X mai' sans préciser l\\'année, utilise {today_str[:4]} comme année."
            if dossier_id:
                system += f"\nNote : Daniel demande le dossier '{dossier_id}'. Inclus ce terme dans les clauses LIKE."
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": _enrichir_question(question)},
            ]
            raw = await complete(model=model, messages=messages, max_tokens=500, temperature=0.1)
            if not raw or not raw.strip():
                log.warning("charlie.sql_gen_empty", model=model)
            else:
                sql, _ = parse_charlie_response(raw)
        except Exception as e:
            log.warning("charlie.sql_gen_failed", error=str(e))

    # ── 3. Recherches en parallèle ──
    async def _sql_task() -> list[dict]:
        if not sql or not is_safe_sql(sql):
            return []
        try:
            return await run_sql(db_path, sql)
        except Exception as e:
            log.warning("charlie.sql_exec_failed", sql=sql, error=str(e))
            return []

    vault_answer: str | None = None

    async def _vault_task() -> list:
        nonlocal vault_answer
        lim = 8 if (is_identity_request or is_list_request or is_dossier_list) else settings.cerveau2_limit
        notes, ans = await query_vault(
            question=question,
            base_url=settings.cerveau2_base_url,
            api_secret=settings.cerveau2_api_secret,
            dossier_id=dossier_id,
            limit=lim,
            context_only=False,
        )
        vault_answer = ans
        return notes

    async def _memory_task() -> list:
        return await query_memory(db_path, question=question, dossier_id=dossier_id, limit=3)

    async def _correction_task() -> list:
        if dossier_id:
            return await query_corrections(db_path, dossier_id=dossier_id, limit=3)
        return []

    async def _archive_task() -> list[dict]:
        lim = 500 if is_count_request else 50
        if dossier_id:
            return await _search_historical_by_keyword(db_path, dossier_id, year=year, limit=lim)
        if year:
            return await _search_historical_all(db_path, year=year, limit=lim)
        return []

    async def _dossiers_task() -> list[dict]:
        if not (is_dossier_count or is_dossier_list):
            return []
        since = f"{year}-01-01" if year else None
        return await query_dossiers(
            base_url=settings.cerveau2_base_url,
            api_secret=settings.cerveau2_api_secret,
            since=since,
        )

    rows, vault_notes, memory_notes, correction_notes, archive_rows, dossier_list = await asyncio.gather(
        _sql_task(), _vault_task(), _memory_task(), _correction_task(), _archive_task(), _dossiers_task(),
    )

    # ── 4. Construction du contexte ──
    context_parts: list[str] = []

    # Corrections (priorité absolue)
    if correction_notes:
        context_parts.append("CORRECTIONS UTILISATEUR (priorité absolue) :")
        for c in correction_notes[:3]:
            context_parts.append(f"- [{c.created_at}] Q: {c.question} | R: {c.response}")
        context_parts.append("")

    # Vault — source principale (SECOND CERVEAU)
    if vault_notes:
        context_parts.append(f"SECOND CERVEAU — notes Cerveau2-Det ({len(vault_notes)} note(s)) :")
        for note in vault_notes:
            fname = note.path.split("/")[-1].replace(".md", "")
            context_parts.append(f"[{fname}]\n{note.content[:2000]}")
            context_parts.append("")
        context_parts.append("")

    # Mémoire Charlie
    if memory_notes:
        context_parts.append("SOUVENIRS DE CHARLIE :")
        for mem in memory_notes[:3]:
            context_parts.append(f"- [{mem.created_at}] {mem.question}: {mem.response[:300]}")
        context_parts.append("")

    # SQL
    if rows:
        context_parts.append(f"EMAILS BASE COURANTE — SQL ({len(rows)} ligne(s)) :")
        context_parts.append(_sanitize_rows_for_prompt(rows))
        context_parts.append("")
    elif sql:
        context_parts.append("EMAILS BASE COURANTE : aucun email trouvé.")
        context_parts.append("")

    # Archives historiques — résumé par catégorie + détail des 50 premiers
    if archive_rows:
        from collections import Counter
        cat_counts = Counter(r.get("category") or "SANS_CAT" for r in archive_rows)
        context_parts.append(f"EMAILS ARCHIVES HISTORIQUES ({len(archive_rows)} email(s)) :")
        context_parts.append("Répartition par catégorie :")
        for cat, cnt in cat_counts.most_common():
            context_parts.append(f"  - {cat}: {cnt}")
        context_parts.append("Détail (50 premiers sujets) :")
        for r in archive_rows[:50]:
            subject = r.get("subject") or "Sans sujet"
            date = r.get("received_at") or r.get("date") or ""
            cat = r.get("category") or ""
            line = f"- {subject}"
            if date:
                line += f" ({date})"
            if cat:
                line += f" [{cat}]"
            context_parts.append(line)
        if len(archive_rows) > 50:
            context_parts.append(f"… et {len(archive_rows) - 50} autres.")
        context_parts.append("")

    context = "\n".join(context_parts)

    # ── 5. Réponses directes Python (pas de LLM) ──

    # Liste des dossiers (Cerveau2 registry ou fallback archives)
    if is_dossier_list:
        if dossier_list:
            total = len(dossier_list)
            lines = [f"J'ai **{total}** dossier{'s' if total != 1 else ''} client{'s' if total != 1 else ''} ouvert{'s' if total != 1 else ''}."]
            lines.append("")
            for d in dossier_list:
                date_str = d.get("created_at", "")[:10]
                ct = d.get("client_type", "?")
                marque = d.get("marque", "")
                line = f"- **{d['dossier_id']}** — ouvert le {date_str} ({ct})"
                if marque:
                    line += f" [{marque}]"
                lines.append(line)
            msg = "\n".join(lines)
            await _auto_save_fact(db_path, question, msg, dossier_id)
            return CharlieResult(
                response_text=msg,
                sql=sql, rows=rows, sql_safe=True, sql_error=None, vault_notes=vault_notes,
            )
        elif archive_rows:
            seen_ids: set[str] = set()
            dossier_ids_found: list[str] = []
            for r in archive_rows:
                did = r.get("dossier_id") or _extract_dossier_id(r.get("subject", "") or "")
                if did and did not in seen_ids:
                    seen_ids.add(did)
                    dossier_ids_found.append(did)
            if dossier_ids_found:
                lines = [f"J'ai identifié **{len(dossier_ids_found)}** dossier{'s' if len(dossier_ids_found) != 1 else ''} dans les archives historiques :"]
                lines.append("")
                for did in dossier_ids_found[:30]:
                    lines.append(f"- **{did}**")
                if len(dossier_ids_found) > 30:
                    lines.append(f"… et {len(dossier_ids_found) - 30} autres.")
                lines.append("")
                lines.append("_(Note : le registre Cerveau2 sera complet après les premières ingestions d'emails.)_")
                msg = "\n".join(lines)
                await _auto_save_fact(db_path, question, msg, dossier_id)
                return CharlieResult(
                    response_text=msg,
                    sql=sql, rows=rows, sql_safe=True, sql_error=None, vault_notes=vault_notes,
                )

    # Dossiers clients — comptage (Cerveau2 registry)
    if is_dossier_count and dossier_list is not None:
        total = len(dossier_list)
        label_time = f"depuis le 1er janvier {year}" if year else "en tout"
        msg = f"J'ai **{total}** dossier{'s' if total != 1 else ''} client{'s' if total != 1 else ''} ouverts {label_time}."
        if 0 < total <= 20:
            lines = [msg, ""]
            for d in dossier_list:
                date_str = d.get("created_at", "")[:10]
                ct = d.get("client_type", "?")
                lines.append(f"- **{d['dossier_id']}** — ouvert le {date_str} ({ct})")
            msg = "\n".join(lines)
        await _auto_save_fact(db_path, question, msg, dossier_id)
        return CharlieResult(
            response_text=msg,
            sql=sql,
            rows=rows,
            sql_safe=True,
            sql_error=None,
            vault_notes=vault_notes,
        )

    # Comptages emails → réponse directe Python (exacte, sans LLM, avec OU sans dossier précis)
    if is_count_request and (rows or archive_rows):
        if rows and len(rows) == 1 and len(rows[0]) == 1:
            sql_cnt = int(next(iter(rows[0].values())))
        else:
            sql_cnt = len(rows)
        arc_cnt = len(archive_rows)
        total = sql_cnt + arc_cnt
        label = f"le dossier **{dossier_id}**" if dossier_id else "cette période"
        msg = f"J'ai trouvé **{total}** email{'s' if total != 1 else ''} pour {label}"
        if year:
            msg += f" en {year}"
        msg += "."
        if sql_cnt > 0 and arc_cnt > 0:
            msg += f" ({sql_cnt} en base courante + {arc_cnt} dans les archives historiques)"
        elif arc_cnt > 0 and sql_cnt == 0:
            msg += " (tous dans les archives historiques)"

        await _auto_save_fact(db_path, question, msg, dossier_id)
        return CharlieResult(
            response_text=msg,
            sql=sql,
            rows=rows,
            sql_safe=True,
            sql_error=None,
            vault_notes=vault_notes,
        )

    # ── 6. Bypass LLM si Cerveau2 a déjà répondu de manière utile ──
    _BAD_VAULT = (
        "je ne trouve pas", "pas trouvé", "aucune information", "je ne trouve",
        "pas d'information", "aucune donnée", "aucun résultat",
        "ne trouve pas d'information", "pas explicitement identifié",
    )
    vault_has_bad = vault_answer and any(p in vault_answer.lower() for p in _BAD_VAULT)
    if vault_answer and not is_count_request and not vault_has_bad:
        # Cerveau2 a répondu en direct et de manière utile
        enriched = vault_answer.strip()
        if rows or archive_rows:
            enriched += "\n\n_(Sources complémentaires : "
            sources: list[str] = []
            if rows:
                sources.append(f"{len(rows)} email(s) base courante")
            if archive_rows:
                sources.append(f"{len(archive_rows)} archive(s)")
            enriched += " + ".join(sources) + ")_"
        log.info("charlie.vault_answer_used", question=question[:60], answer_len=len(enriched))
        await _auto_save_fact(db_path, question, enriched, dossier_id)
        return CharlieResult(
            response_text=enriched,
            sql=sql,
            rows=rows,
            sql_safe=True,
            sql_error=None,
            vault_notes=vault_notes,
        )
    if vault_has_bad:
        log.info("charlie.vault_answer_bad", question=question[:60], answer_preview=vault_answer[:120])

    # ── 7. Appel LLM final pour les questions spécifiques ──
    if is_list_request:
        format_rule = "7. Daniel demande une LISTE. Si le second cerveau a des notes sur ces dossiers, liste-les en priorité. Sinon, extrait les noms de dossiers identifiables depuis les emails. Ne liste pas les catégories — donne les NOMS (ex: ADF, Zaventem, ODM)."
    elif is_count_request:
        format_rule = "7. Daniel demande un COMPTAGE. Donne le nombre total clair et précis."
    elif is_identity_request:
        format_rule = "7. Daniel demande une IDENTITÉ. Cherche dans le second cerveau et réponds en une ou deux phrases maximum, directement."
    else:
        format_rule = "7. Réponds de manière fluide et directe, en une ou deux phrases maximum."

    # Si Cerveau2 a répondu mais c'est un comptage, on injecte sa réponse dans le contexte
    vault_context = context
    if vault_answer:
        vault_context = f"RÉPONSE DU SECOND CERVEAU (Cerveau2-Det) :\n{vault_answer.strip()}\n\n---\n\n{context}"

    final_prompt = f"""Tu es Charlie, l'assistant IA personnel de Daniel Hurchon, détective privé chez Detective.be. Version {VERSION}.
Tu t'adresses à Daniel comme à un partenaire : direct, chaleureux, sans langue de bois. Utilise "tu".

Question de Daniel : {question}

{vault_context}

RÈGLES :
1. Si des corrections utilisateur sont fournies, elles priment sur TOUT.
2. Le SECOND CERVEAU (Cerveau2-Det) est la SOURCE PRINCIPALE. Commence toujours par ses notes.
3. Les emails (base courante + archives) sont un complément pour appuyer ou enrichir la réponse.
4. Pour une identité (qui est, nom, contact, épouse, conjoint), cherche EN PRIORITÉ dans le second cerveau.
5. Pour un comptage, additionne les résultats SQL et les archives.
6. Si aucune source n'a de réponse, dis-le clairement en une phrase.
{format_rule}
8. N'invente jamais d'informations absentes des sources ci-dessus.

RÉPONSE À DANIEL :"""

    try:
        response = await complete(
            model=model,
            messages=[{"role": "user", "content": final_prompt}],
            max_tokens=600,
            temperature=0.2,
        )
        response = response.strip() if response else ""
    except Exception as e:
        log.warning("charlie.final_llm_failed", error=str(e))
        response = ""

    # Garde : réponse vide OU inutile alors qu'on a des données → réponse de secours
    _BAD = ("je n'ai pas trouvé", "aucun résultat", "aucune information", "je ne trouve pas", "pas d'information", "aucune donnée")
    if not response or (any(p in response.lower() for p in _BAD) and (rows or archive_rows)):
        response = ""
    if not response:
        if is_count_request and (rows or archive_rows):
            sql_cnt = int(next(iter(rows[0].values()))) if rows and len(rows) == 1 and len(rows[0]) == 1 else 0
            arc_cnt = len(archive_rows)
            total = sql_cnt + arc_cnt
            if sql_cnt == 0 and arc_cnt > 0:
                total = arc_cnt
            response = f"J'ai trouvé **{total}** email{'s' if total > 1 else ''} pour {dossier_id or 'cette recherche'} en {year or 'cette période'}."
            if arc_cnt > 0 and sql_cnt == 0:
                response += " (tous dans les archives historiques)"
        elif is_list_request and archive_rows:
            lines = [f"J'ai trouvé **{len(archive_rows)}** email{'s' if len(archive_rows) > 1 else ''} dans les archives pour {dossier_id or 'cette période'} :"]
            for r in archive_rows[:25]:
                subject = r.get("subject") or "Sans sujet"
                date = r.get("received_at") or r.get("date") or ""
                line = f"- {subject}"
                if date:
                    line += f" ({date})"
                lines.append(line)
            if len(archive_rows) > 25:
                lines.append(f"… et {len(archive_rows) - 25} autres.")
            response = "\n".join(lines)
        else:
            response = "Je n'ai pas trouvé d'informations."

    await _auto_save_fact(db_path, question, response, dossier_id)

    return CharlieResult(
        response_text=response,
        sql=sql,
        rows=rows,
        sql_safe=True,
        sql_error=None,
        vault_notes=vault_notes,
    )

def _needs_summary(question: str) -> bool:
    q = _normalize(question)
    return any(kw in q for kw in _NEEDS_SUMMARY_KEYWORDS)


def _is_count_query(sql: str) -> bool:
    """Détecte les requêtes COUNT(*) — inutile d'interroger le vault pour un simple comptage."""
    return "count(" in sql.lower()


def _is_vault_relevant(question: str, sql: str) -> bool:
    if not sql:  # question conversationnelle → vault toujours utile
        return True
    q = _normalize(question)
    return any(kw in q for kw in _VAULT_KEYWORDS)


_IDENTITY_NAME_RE = re.compile(
    r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?"
)

_EMAIL_RE = re.compile(r"[\w\.-]+@[\w\.-]+\.\w+")


def _extract_emails_from_notes(notes: list) -> list[str]:
    """Extrait les adresses email uniques d'une liste de notes/mémoires."""
    emails: set[str] = set()
    for note in notes:
        text = f"{getattr(note, 'question', '')} {getattr(note, 'response', '')}"
        for m in _EMAIL_RE.finditer(text):
            emails.add(m.group(0).lower())
    return sorted(emails)


def _extract_identity_answer(vault_notes: list[VaultNote], question: str) -> str | None:
    """Extraction directe d'une réponse identitaire depuis le vault, sans LLM.

    Cherche des patterns comme 'épouse : Sarah', 'femme : Jean',
    'aidé par son épouse : Marie' dans le contenu des notes.
    """
    q_norm = _normalize(question)

    # Déterminer la relation cherchée
    relation: str | None = None
    target_person: str | None = None
    for kw, rel in (
        ("epouse", "épouse"), ("femme", "femme"), ("mari", "mari"),
        ("conjoint", "conjoint"), ("compagne", "compagne"), ("compagnon", "compagnon"),
        ("fille", "fille"), ("fils", "fils"), ("enfant", "enfant"),
        ("pere", "père"), ("mere", "mère"), ("parent", "parent"),
        ("soeur", "sœur"), ("frere", "frère"),
    ):
        if kw in q_norm:
            relation = rel
            break

    # Détecter la personne dont on parle (ex: "épouse de CDAL" → CDAL)
    # Pattern: "relation de/de/d' XXXX"
    m = re.search(r"(?:de|d')\s+([A-Z][a-zA-Z0-9]{1,})", question, re.IGNORECASE)
    if m:
        target_person = m.group(1)

    if not relation:
        return None

    # Concaténer tout le contenu des notes
    all_text = "\n".join(n.content for n in vault_notes)
    all_text_lower = all_text.lower()

    # Patterns de recherche ordonnés du plus spécifique au plus général
    patterns = [
        # "aidé par son épouse : Sarah"  →  capture Sarah
        rf"(?:par|avec)\s+(?:son|sa)\s+{relation}\s*[:\-–]\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
        # "son épouse Sarah"  →  capture Sarah
        rf"(?:son|sa)\s+{relation}\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
        # "épouse : Sarah"  →  capture Sarah
        rf"{relation}\s*[:\-–]\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
        # "Sarah, épouse de CDAL"  →  capture Sarah (avant la relation)
        rf"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?),?\s+(?:son|sa)?\s*{relation}",
        # "l'épouse de CDAL est Sarah"  →  capture Sarah (après 'est')
        rf"{relation}\s+(?:de|d')\s+{re.escape(target_person or '')}\s+(?:est|s'appelle|se nomme)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
        # "l'épouse de CDAL, Sarah"  →  capture Sarah (après virgule)
        rf"{relation}\s+(?:de|d')\s+{re.escape(target_person or '')},?\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
    ]

    for pat in patterns:
        try:
            m = re.search(pat, all_text, re.IGNORECASE)
            if m:
                name = m.group(1).strip()
                # Filtrer les faux positifs (mots communs)
                if name.lower() in ("lui", "elle", "moi", "toi", "nous", "vous", "personne", "rien", "tout", "tous"):
                    continue
                return f"La {relation} de {target_person or 'CDAL'} s'appelle **{name}**."
        except re.error:
            continue

    # Fallback : chercher un nom propre à proximité du mot relation
    # On tokenise et on cherche le mot relation, puis on regarde les tokens voisins
    tokens = re.findall(r"[A-Za-zÀ-ÿ]+", all_text)
    for i, tok in enumerate(tokens):
        tok_lower = tok.lower()
        # Match relation (ex: épouse, femme, mari)
        if tok_lower == relation or (relation == "conjoint" and tok_lower in ("conjoint", "conjointe")):
            # Chercher un nom propre majuscule dans une fenêtre de ±8 tokens
            for j in range(max(0, i - 8), min(len(tokens), i + 9)):
                if j == i:
                    continue
                candidate = tokens[j]
                if candidate[0].isupper() and len(candidate) > 2:
                    # Vérifier que ce n'est pas un mot commun majuscule
                    if candidate.lower() not in (
                        "detective", "belgique", "belgium", "investigations",
                        "digitalhs", "infomaniak", "gmail", "outlook", "yahoo",
                        "lundi", "mardi", "mercredi", "jeudi", "vendredi",
                        "samedi", "dimanche", "janvier", "fevrier", "mars",
                        "avril", "mai", "juin", "juillet", "aout", "septembre",
                        "octobre", "novembre", "decembre",
                        "monsieur", "madame", "mademoiselle", "docteur", "maitre",
                    ):
                        return f"La {relation} de {target_person or 'CDAL'} s'appelle **{candidate}**."

    return None


_IDENTITY_KEYWORDS = (
    "qui", "personne", "nom", "prenom", "client", "contact", "sappelle",
    "epouse", "mari", "conjoint", "femme", "compagne", "compagnon",
    "fille", "fils", "enfant", "bebe", "pere", "mere", "parent",
    "soeur", "frere", "famille", "cousin", "cousine", "oncle", "tante",
)


def _is_identity_query(question: str) -> bool:
    q = _normalize(question)
    return any(kw in q for kw in _IDENTITY_KEYWORDS)


async def _auto_save_fact(
    db_path: Path, question: str, response: str, dossier_id: str | None,
) -> None:
    """Stocke automatiquement les faits identitaires dans la mémoire de Charlie."""
    if not response or len(response) < 10:
        return
    norm_resp = _normalize(response)
    # Skip réponses d'erreur / vide / temporaires
    skip_phrases = (
        "aucun email", "erreur sql", "charlie est momentanement",
        "reessaie dans un instant", "aucun dossier trouve",
        "reponse context_only", "reponse zone_rouge",
    )
    if any(p in norm_resp for p in skip_phrases):
        return
    lower_q = _normalize(question)
    is_identity = any(kw in lower_q for kw in _IDENTITY_KEYWORDS)
    # Un nom propre dans la réponse = potentiel fait durable
    has_proper_noun = bool(re.search(r"\b[A-Z][a-z]{2,}\b", response))
    if not is_identity and not has_proper_noun:
        return
    # Éviter les doublons exacts (question + réponse identique)
    try:
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "SELECT 1 FROM charlie_memory WHERE question = ? AND response = ?",
                (question, response),
            )
            if await cursor.fetchone():
                return
            # Ne pas ré-enregistrer une réponse qui a été corrigée par Daniel
            cursor = await db.execute(
                """
                SELECT 1 FROM charlie_memory
                WHERE response = ? AND feedback = 'bad'
                AND created_at >= datetime('now', '-30 days')
                """,
                (response,),
            )
            if await cursor.fetchone():
                log.info("charlie.auto_save_skipped_corrected", response=response[:60])
                return
    except Exception:
        pass
    await save_memory(
        db_path=db_path,
        question=question,
        response=response,
        dossier_id=dossier_id,
    )
    log.info("charlie.auto_save_fact", dossier_id=dossier_id, question=question[:60])


def _sanitize_rows_for_prompt(rows: list[dict]) -> str:
    """Convertit les rows en texte anonymisé pour le prompt LLM.
    Ne garde que les champs publics (subject, received_at, category).
    Masque ABSOLUMENT : id, sender, body_preview, body, source_db.
    """
    if not rows:
        return "(aucun résultat)"
    # Détection COUNT(*) : une seule ligne avec une clé "total" ou "count(*)"
    first = rows[0]
    if len(rows) == 1 and len(first) == 1:
        key = next(iter(first.keys()))
        if key.lower() in ("total", "count(*)", "count"):
            return f"TOTAL : {first[key]}"
    lines: list[str] = []
    for r in rows[:20]:
        subject = r.get("subject") or "Sans sujet"
        received = r.get("received_at") or r.get("date") or ""
        cat = r.get("category") or ""
        line = f"- Sujet: {subject}"
        if cat:
            line += f" | Catégorie: {cat}"
        if received:
            line += f" | Date: {received}"
        lines.append(line)
    return "\n".join(lines)


def _format_historical_response(question: str, rows: list[dict]) -> str:
    """Formate une réponse directe pour les résultats historiques (archives).
    Ne passe PAS par le LLM — confidentialité + vitesse."""
    if not rows:
        return "Aucun dossier trouvé dans les archives."

    count = len(rows)
    # Détecter la catégorie pour le titre
    cat_display = ""
    if rows:
        cat = rows[0].get("category", "").lower().replace("_", " ")
        if cat:
            cat_display = f" {cat}"

    lines = [
        f"J'ai trouvé **{count} dossier{'s' if count > 1 else ''}{cat_display}** dans les archives :",
        "",
    ]
    for r in rows[:15]:
        subject = r.get("subject") or "Sans sujet"
        date = r.get("received_at") or r.get("date") or ""
        # Lien cliquable vers l'inbox avec un mot-clef unique
        q = subject.split()[0] if subject else ""
        link = f"[{subject}](/inbox?q={q})" if q else subject
        lines.append(f"- {link} ({date})")

    if count > 15:
        lines.append(f"\n… et {count - 15} autres.")

    return "\n".join(lines)


_SUMMARY_PROMPT_VAULT_ONLY = """Tu es Charlie, l'assistant IA personnel de Daniel Hurchon,
détective privé chez Detective.be. Tu es sa précieuse moitié cognitive.
Tu t'adresses à Daniel comme à un partenaire : direct, chaleureux, sans langue de bois.
Utilise "tu".

**INSTRUCTION CRITIQUE : la réponse à la question de Daniel se trouve DANS les notes ci-dessous.**
Tu dois lire attentivement ces notes et extraire l'information demandée.
Tu ne dois PAS dire "je ne trouve rien" ou "aucune trace" — l'information est forcément dans les notes.

Question de Daniel : {question}

Notes du second cerveau ({vault_count}) :
{vault_notes}

Souvenirs de Charlie ({memory_count}) :
{memory_notes}

CORRECTIONS UTILISATEUR ({correction_count}) — PRIORITÉ ABSOLUE :
{correction_notes}

RÈGLES ABSOLUES :
1. **CORRECTIONS UTILISATEUR PRIMENT SUR TOUT** — Si des corrections sont
   fournies ci-dessus, elles écrasent le vault ET la mémoire. Daniel a déjà
   corrigé cette information. Utilise-la directement sans discuter.
2. **Lis les notes du second cerveau et extrait la réponse.**
   Si la question demande un nom, un prénom, une identité — cherche ce nom
   DANS les notes. La réponse y est.
3. **Ne dis JAMAIS "je ne trouve rien"** quand des notes sont fournies.
4. Réponds de manière fluide et directe, en une ou deux phrases.
5. Si les notes contiennent un nom propre, utilise-le dans ta réponse.
"""


async def _summarize_results(
    question: str,
    rows: list[dict],
    vault_notes: list[VaultNote],
    memory_notes: list,
    correction_notes: list,
    model: str,
    settings,
) -> str | None:
    """Appelle le LLM une seconde fois pour synthétiser les résultats SQL + vault.

    ATTENTION : les rows sont pré-filtrés et anonymisés avant d'être envoyés au LLM.
    Jamais de données brutes (sender, body_preview, id, source_db) dans le prompt.
    """
    rows_text = _sanitize_rows_for_prompt(rows)
    has_sql = rows and len(rows) > 0

    memory_text = ""
    if memory_notes:
        memory_lines = []
        for mem in memory_notes:
            memory_lines.append(f"- [{mem.created_at}] {mem.question}: {mem.response[:300]}")
        memory_text = "\n".join(memory_lines)

    correction_text = ""
    if correction_notes:
        correction_lines = []
        for c in correction_notes:
            correction_lines.append(f"- [{c.created_at}] {c.question}: {c.response[:500]}")
        correction_text = "\n".join(correction_lines)

    # Bypass si corrections existent ET question identitaire — réponse directe
    # sans passer par le LLM. Pour les questions analytiques (comptage,
    # statistiques, etc.), la correction reste un contexte PRIORITAIRE
    # dans le prompt mais laisse le LLM synthétiser avec les résultats SQL.
    if correction_notes and _is_identity_query(question):
        log.info("charlie.correction_bypass", question=question[:60], corrections=len(correction_notes))
        latest = correction_notes[0]
        return latest.response.strip()

    vault_lines = []
    for note in vault_notes:
        fname = note.path.split("/")[-1].replace(".md", "")
        # Augmenter à 2000 car pour que le LLM voit le contenu réel
        # au-delà du frontmatter YAML qui prend ~300-400 caractères
        vault_lines.append(f"- {fname}: {note.content[:2000]}")
    vault_text = "\n".join(vault_lines)

    # Bypass LLM pour les questions identitaires : extraction directe du vault
    if not has_sql and vault_notes and _is_identity_query(question):
        direct = _extract_identity_answer(vault_notes, question)
        if direct:
            log.info("charlie.identity_direct_extract", question=question[:60], answer=direct[:80])
            return direct

    # Prompt spécifique si SQL vide mais vault a trouvé la réponse
    if not has_sql and vault_notes:
        prompt = _SUMMARY_PROMPT_VAULT_ONLY.format(
            question=question,
            vault_count=len(vault_notes),
            vault_notes=vault_text,
            memory_count=len(memory_notes),
            memory_notes=memory_text,
            correction_count=len(correction_notes),
            correction_notes=correction_text,
        )
    elif vault_notes or memory_notes:
        prompt = _SUMMARY_PROMPT_VAULT.format(
            question=question,
            count=len(rows),
            rows=rows_text,
            vault_count=len(vault_notes),
            vault_notes=vault_text,
            memory_count=len(memory_notes),
            memory_notes=memory_text,
            correction_count=len(correction_notes),
            correction_notes=correction_text,
        )
    else:
        prompt = _SUMMARY_PROMPT.format(question=question, count=len(rows), rows=rows_text)

    try:
        summary = await complete(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600,
            temperature=0.1,
        )
        return summary.strip() if summary else None
    except Exception as e:
        log.warning("charlie.summary_failed", error=str(e))
        return None
