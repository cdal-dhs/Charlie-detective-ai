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

from app.cerveau_client import (
    VaultNote,
    get_vault_note,
    query_corrections_vault,
    query_dossiers,
    query_vault,
)
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
   7b. Si Daniel demande des emails en attente, en pending ou urgent,
   inclus aussi `status` et `priority` dans ton SELECT pour que je
   puisse indiquer clairement le statut de chaque email.
   7c. Quand Daniel demande le contenu, le détail ou un résumé d'un dossier,
   utilise la colonne `body` (contenu complet) dans ton SELECT, pas `body_preview`.
   Inclus aussi `ai_draft` si pertinent.
8. **CORRECTIONS UTILISATEUR (RÈGLE ABSOLUE)** :
   - Si le contexte contient une section "CORRECTIONS CERVEAU2" ou "CORRECTIONS LOCALES",
     tu DOIS utiliser EXCLUSIVEMENT la `corrected_response` pour répondre.
   - Ignore complètement l'`original_response` et toute autre source contradictoire.
   - La correction prime sur TOUT : vault, SQL, mémoire, et ton propre raisonnement.
   - Ne JAMAIS dire "je n'ai pas trouvé" si une correction est présente.
9. **DEUX modes de recherche — ne les confonds pas :**
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
10. **Questions de comptage (combien, nombre, total)** :
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
    hide_rows: bool = False  # Quand True, le template web ne montre pas le tableau SQL brut
    archive_rows: list[dict] = field(default_factory=list)  # Emails trouvés dans les DB historiques boite1/2/3


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
    """Cherche dans les 3 DB historiques par mot-clé (subject, body_preview, body_full, sender).

    Utilisé quand un dossier spécifique est mentionné (ex: ADF) ou quand un
    mot-clé est extrait de la question (ex: "Lampaert") pour trouver tous les
    emails liés, même ceux antérieurs au cutoff de mail_processed.

    body_full est inclus car certains emails ont un body_preview vide mais
    contiennent le mot-clé dans le corps complet (ex: réponses avec citations).
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
                    "SELECT id, subject, sender, date, body_preview, body_full, category "
                    "FROM emails WHERE (subject LIKE ? OR body_preview LIKE ? OR body_full LIKE ? OR sender LIKE ?) "
                )
                params: list = [like, like, like, like]
                if year:
                    sql += "AND date LIKE ? "
                    params.append(f"%{year}%")
                sql += "ORDER BY date DESC LIMIT ?"
                params.append(limit)
                cursor = await db.execute(sql, tuple(params))
                rows = await cursor.fetchall()
                for row in rows:
                    preview = row[4] or ""
                    body_full = row[5] or ""
                    # Les body_preview des DB historiques sont souvent incomplets ou tronqués.
                    # On utilise systématiquement le body_full (jusqu'à 3000 chars) pour donner
                    # au LLM le contenu réel de l'email, pas juste un extrait partiel.
                    if body_full:
                        full_clean = body_full.strip()
                        preview = full_clean[:3000]
                        if len(full_clean) > 3000:
                            preview += "\n\n[… tronqué à 3000 caractères]"
                    results.append({
                        "id": row[0],
                        "subject": row[1],
                        "sender": row[2],
                        "received_at": row[3],
                        "body_preview": preview,
                        "category": row[6],
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
    r"[Dd][Oo][Ss]{2}[Ii][Ee][Rr]\s*[Nn]°\s*([A-Za-z0-9_-]+)"
)
_DOSSIER_NAME_RE = re.compile(
    r"[Dd][Oo][Ss]{2}[Ii][Ee][Rr]\s+([A-Z][a-zA-Z]+)"
)
_HASH_DOSSIER_RE = re.compile(r"#([A-Z][A-Z0-9]{2,})")
_CODE_RE = re.compile(r"\b([A-Z]{3,6})\b")
_YEAR_RE = re.compile(r"\b(20\d{2})\b")


def _extract_dossier_id(question: str) -> str | None:
    # Pattern 1 : "dossier N°123" ou "dossier N° ABC-123"
    m = _DOSSIER_RE.search(question)
    if m:
        return m.group(1)
    # Pattern 2 : "dossier Lampaert" — nom propre après "dossier"
    m = _DOSSIER_NAME_RE.search(question)
    if m:
        name = m.group(1)
        # Exclure les mots communs qui ne sont pas des noms de dossier
        if name.lower() not in ("client", "general", "generale", "monsieur", "madame", "mademoiselle", "monsieur", "madame", "cliente"):
            return name
    # Pattern 3 : hashtag #ADF
    m = _HASH_DOSSIER_RE.search(question)
    if m:
        return m.group(1)
    # Pattern 4 : codes ALL-CAPS isolés (ex: ADF, DPDH)
    for m in _CODE_RE.finditer(question):
        code = m.group(1)
        if code not in ("SQL", "OK", "HTTP", "API", "URL", "HTML", "XML", "JSON", "HTTPS", "SMTP", "IMAP", "PDF", "CSV", "JPEG", "PNG"):
            return code
    return None


def _extract_year(question: str) -> str | None:
    m = _YEAR_RE.search(question)
    return m.group(1) if m else None


def _extract_years(question: str) -> list[str]:
    """Extrait TOUTES les années 20xx mentionnées dans la question.

    Retourne une liste triée. Ex: '2025 et 2026' → ['2025', '2026'].
    """
    years = sorted({m.group(1) for m in _YEAR_RE.finditer(question)})
    return years


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


def _build_status_sql(question: str, dossier_id: str | None) -> str | None:
    """Génère SQL de liste pour les questions de statut email (pending, urgent, etc.).

    Retourne None si la question ne concerne pas un statut de demande client.
    """
    q = _normalize(question)

    # Racines courtes = fuzzy / tolérance aux fautes de frappe
    is_pending = any(kw in q for kw in ("pending", "attente", "a repondre", "a traiter", "non traite"))
    is_demande = any(kw in q for kw in ("demand", "client", "clients", "requete"))
    is_urgent = any(kw in q for kw in ("urgent", "urgente", "prioritaire", "important"))

    # "en attente" / "à traiter" / "pending" suffisent même sans "demande" explicite
    if not is_demande and not is_pending and not any(kw in q for kw in ("email", "mail", "message")):
        return None

    where_clauses: list[str] = []
    if is_demande:
        where_clauses.append("category = 'demande_client'")
    if is_pending:
        where_clauses.append("status = 'pending'")
    if is_urgent:
        where_clauses.append("priority = 'high'")
    if dossier_id:
        where_clauses.append(f"(subject LIKE '%{dossier_id}%' OR body LIKE '%{dossier_id}%')")

    # Par défaut "demandes clients" sans filtre statut → ne pas tout retourner
    if is_demande and not is_pending and not is_urgent and not dossier_id:
        where_clauses.append("status = 'pending'")

    if not where_clauses:
        return None

    date_filter = _extract_date_filter(question)
    if date_filter:
        where_clauses.append(date_filter)

    where = " AND ".join(f"({c})" for c in where_clauses)
    return f"SELECT id, subject, received_at, category, status, priority FROM mail_processed WHERE {where} ORDER BY processed_at DESC LIMIT 20"


def _extract_keywords(question: str) -> list[tuple[int, str]]:
    """Extrait et score les mots-clés pertinents d'une question.

    Retourne une liste triée par score décroissant (score, mot_original).
    Pénalise les verbes d'action génériques et booste les noms concrets
    (lieux, types de documents, objets de recherche).
    """
    STOP_WORDS = {
        "moi", "vous", "dossier", "client", "resume", "resumer",
        "question", "reponse", "donne", "donner", "aussi",
        "partie", "partir", "faire", "etre", "avoir", "aller",
        "comme", "alors", "apres", "avant", "encore", "toujours",
        "jamais", "toutes", "toute", "tous", "tout", "plusieurs",
        "quelques", "beaucoup", "souvent", "parfois", "maintenant",
        "aujourd", "hier", "demain", "matin", "soir", "jour",
        "semaine", "mois", "annee", "temps", "heure", "minute",
        "proposition", "propose", "proposer", "offre", "offert",
        "offrir", "financier", "financiere", "finance", "finances",
        "budget", "prix", "cout", "couts", "montant", "euro",
        "euros", "paiement", "payer", "paye", "versement", "provision",
        "honoraires", "tarif", "tarifs", "forfait", "forfaits", "total",
        "somme", "sommes", "argent", "gratuit", "gratuite",
        "avec", "principaux", "principales", "important", "importants",
        "details", "detail", "information", "informations",
    }
    ACTION_WORDS = {
        "retrouve", "trouve", "donne", "donner", "montre", "montrer",
        "cherche", "chercher", "liste", "lister", "affiche", "afficher",
        "envoie", "envoyer", "rapporte", "rapport", "dis", "dire",
        "trouves", "donnes", "montres", "cherches", "listes", "afficher",
        "trouver", "donner", "montrer", "chercher", "lister", "afficher",
        "envoyer", "dire", "demande", "demander", "demandes", "demandent",
        "envoies", "envoyes", "envoyez", "regarde", "regarder", "regardes",
        "presente", "presenter", "presentes", "presentez",
        "retrouver", "retrouves", "retrouvez", "retrouvent",
        "trouves", "trouvez", "trouvent", "recherche", "rechercher",
        "recherches", "recherchez", "recherchent",
    }
    SEMANTIC_BOOST = {
        "hotel", "hotels", "facture", "factures", "devis", "contrat",
        "rapport", "reservation", "vol", "avion", "train", "taxi",
        "restaurant", "parking", "essence", "carburant", "peage", "toll",
        "autoroute", "document", "photo", "video", "preuve", "temoin",
        "adresse", "telephone", "email", "mail", "message", "sujet",
        "client", "enquete", "investigation", "surveillance", "adulte",
        "infidelite", "disparition", "recherche", "personne", "garde",
        "enfant", "famille", "residence", "domicile", "entreprise",
        "fraude", "materiel", "collaboration", "harcelement",
    }
    keywords: list[tuple[int, str]] = []
    for word in re.findall(r"[A-Za-zÀ-Ÿà-ÿ]{4,}", question):
        w = word.strip().lower()
        if len(w) < 4:
            continue
        w_norm = normalize("NFD", w).encode("ascii", "ignore").decode("ascii")
        if w_norm in STOP_WORDS:
            continue
        score = len(w)
        if word[0].isupper():
            score += 10
        if w_norm in ACTION_WORDS:
            score -= 15
        if w_norm in SEMANTIC_BOOST:
            score += 15
        if score > 0:
            keywords.append((score, word))
    keywords.sort(key=lambda x: x[0], reverse=True)
    return keywords


def _build_keyword_sql(question: str) -> str | None:
    """Génère un SQL de recherche par mot-clé pour les questions factuelles
    sur un dossier/client spécifique (ex: 'résume le dossier Lampaert').

    Retourne None si aucun mot-clé significatif n'est trouvé.
    Privilégie les noms propres (majuscule initiale) et normalise les accents.
    """
    keywords = _extract_keywords(question)
    if not keywords:
        return None

    likes = []
    for _, kw in keywords[:5]:
        kw_safe = kw.replace("'", "''")
        likes.append(f"subject LIKE '%{kw_safe}%'")
        likes.append(f"body LIKE '%{kw_safe}%'")
        likes.append(f"body_preview LIKE '%{kw_safe}%'")

    where = " OR ".join(likes)

    # Restriction par catégorie quand la question mentionne un type d'email connu
    # AND (pas OR) : on veut les emails qui matchent les mots-clés ET la catégorie
    q_norm = _normalize(question)
    category_clause = ""
    if any(kw in q_norm for kw in ("facture", "factures", "invoice")):
        category_clause = " AND category = 'facture'"
    elif any(kw in q_norm for kw in ("newsletter", "digest", "bulletin")):
        category_clause = " AND category = 'newsletter'"
    elif any(kw in q_norm for kw in ("rappel", "reminder")):
        category_clause = " AND category = 'rappel'"

    years = _extract_years(question)
    date_clause = ""
    if len(years) == 1:
        y = years[0]
        date_clause = f" AND (processed_at >= '{y}-01-01' AND processed_at < '{int(y) + 1}-01-01')"
    elif len(years) > 1:
        min_y, max_y = years[0], years[-1]
        date_clause = f" AND (processed_at >= '{min_y}-01-01' AND processed_at < '{int(max_y) + 1}-01-01')"
    return f"SELECT id, subject, sender, received_at, category, status, priority, body_preview, substr(body, 1, 3000) as body FROM mail_processed WHERE ({where}{category_clause}){date_clause} ORDER BY received_at DESC LIMIT 20"


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


def _extract_frontmatter(text: str) -> dict:
    """Parse le frontmatter YAML d'un fichier Markdown."""
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    fm_text = parts[1].strip()
    fm: dict = {}
    for line in fm_text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if v.startswith("[") and v.endswith("]"):
                try:
                    import json

                    fm[k] = json.loads(v.replace("'", '"'))
                except Exception:
                    fm[k] = [x.strip().strip('"').strip("'") for x in v[1:-1].split(",")]
            else:
                fm[k] = v
    return fm


async def _resolve_links(
    notes: list[VaultNote],
    base_url: str,
    api_secret: str,
    max_links: int = 5,
) -> list[VaultNote]:
    """Résout les liens [[slug]] trouvés dans les notes pour enrichir le contexte.

    Scanne le contenu et le frontmatter des notes, déduplique les slugs,
    récupère les notes liées depuis Cerveau2 et les retourne.
    """
    if not notes or not base_url or not api_secret:
        return []

    link_pattern = re.compile(r"\[\[([^\]\n]+?)\]\]")
    seen_slugs: set[str] = set()
    slugs_to_fetch: list[str] = []
    existing_paths = {n.path for n in notes}

    def _slug_to_paths(slug: str) -> list[str]:
        slug = slug.strip().rstrip("/")
        if "/" in slug and slug.endswith(".md"):
            return [slug]
        if "/" in slug:
            return [f"{slug}.md", slug]
        return [
            f"04_entities/societes/{slug}.md",
            f"04_entities/personnes/{slug}.md",
            f"04_entities/lieux/{slug}.md",
            f"02_dossiers/{slug}/_index.md",
            f"03_doctrine/{slug}.md",
            f"{slug}.md",
        ]

    for note in notes:
        for match in link_pattern.finditer(note.content):
            slug = match.group(1).strip()
            if slug and slug not in seen_slugs:
                seen_slugs.add(slug)
                for cp in _slug_to_paths(slug):
                    if cp not in existing_paths:
                        slugs_to_fetch.append(cp)
                        break

        fm = _extract_frontmatter(note.content)
        for key in (
            "employeur", "adresse_principale", "related", "dossier", "lieu", "personne", "entities",
            "epouse", "mari", "conjoint", "compagne", "compagnon",
            "fille", "fils", "enfant", "pere", "mere", "parent",
            "soeur", "frere", "cousin", "cousine", "oncle", "tante",
        ):
            val = fm.get(key, "")
            if isinstance(val, str):
                for match in link_pattern.finditer(val):
                    slug = match.group(1).strip()
                    if slug and slug not in seen_slugs:
                        seen_slugs.add(slug)
                        for cp in _slug_to_paths(slug):
                            if cp not in existing_paths:
                                slugs_to_fetch.append(cp)
                                break
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, str):
                        for match in link_pattern.finditer(item):
                            slug = match.group(1).strip()
                            if slug and slug not in seen_slugs:
                                seen_slugs.add(slug)
                                for cp in _slug_to_paths(slug):
                                    if cp not in existing_paths:
                                        slugs_to_fetch.append(cp)
                                        break

    if not slugs_to_fetch:
        return []

    unique_slugs = list(dict.fromkeys(slugs_to_fetch))[:max_links]
    coros = [get_vault_note(p, base_url, api_secret) for p in unique_slugs]
    linked = await asyncio.gather(*coros, return_exceptions=True)

    results: list[VaultNote] = []
    for note_or_err in linked:
        if isinstance(note_or_err, VaultNote):
            results.append(note_or_err)

    log.info("charlie.links_resolved", original=len(notes), linked=len(results), slugs=unique_slugs)
    return results


def _build_dossier_summary_from_emails(
    dossier_id: str | None, archive_rows: list[dict], rows: list[dict]
) -> str | None:
    """Extraction déterministe d'un résumé de dossier depuis les emails.

    Ne dépend PAS d'un LLM. Utilise des regex pour extraire :
    - nom/prénom du client
    - montants financiers (€, euro, euros, EUR)
    - dates importantes
    - type de demande

    Retourne un paragraphe fluide ou None si pas assez d'infos.
    """
    all_emails = (archive_rows or []) + (rows or [])
    if not all_emails:
        return None

    # Trier par date
    all_emails.sort(key=lambda r: r.get("received_at", r.get("date", "")), reverse=True)

    # --- Extraction nom client ---
    client_name: str | None = None
    for r in all_emails:
        text = r.get("body") or r.get("body_preview") or ""
        if not text:
            continue
        # Pattern formulaire NL : Achternaam: X Voornaam: Y
        m = re.search(r"Achternaam\s*[:=]\s*([A-Za-zÀ-Ÿ\-]+).*?Voornaam\s*[:=]\s*([A-Za-zÀ-Ÿ\-]+)", text, re.IGNORECASE | re.DOTALL)
        if m:
            client_name = f"{m.group(2)} {m.group(1)}"
            break
        # Pattern FR : Nom: X Prénom: Y
        m = re.search(r"Nom\s*[:=]\s*([A-Za-zÀ-Ÿ\-]+).*?Prénom\s*[:=]\s*([A-Za-zÀ-Ÿ\-]+)", text, re.IGNORECASE | re.DOTALL)
        if m:
            client_name = f"{m.group(2)} {m.group(1)}"
            break
        # Pattern simple : Name: / Naam:
        m = re.search(r"(?:Name|Naam)\s*[:=]\s*([A-Za-zÀ-Ÿ\-\s]{2,40})", text, re.IGNORECASE)
        if m:
            client_name = m.group(1).strip()
            break

    if not client_name and dossier_id:
        client_name = dossier_id

    # --- Extraction montants financiers ---
    amounts_found: list[str] = []
    seen_amounts: set[str] = set()
    for r in all_emails:
        text = r.get("body") or r.get("body_preview") or ""
        if not text:
            continue
        # Patterns : 200€, 200 euros, 200 EUR, €200, 1.234,56, 1234.56, 1 234,56
        for m in re.finditer(r"(?:€|EUR|euro?s?\s*)?\s*(\d{1,3}(?:[\s.]\d{3})*(?:,\d{2})?|\d+(?:,\d{2})?)\s*(?:€|EUR|euro?s?)?", text, re.IGNORECASE):
            raw = m.group(0).strip()
            # Filtrer les faux positifs (années, numéros de téléphone, IDs)
            num_str = m.group(1).replace(" ", "").replace(".", "").replace(",", ".")
            try:
                val = float(num_str)
            except ValueError:
                continue
            if val < 10 or val > 500000:
                continue
            # Éviter les doublons (valeur proche)
            key = f"{val:.2f}"
            if key not in seen_amounts:
                seen_amounts.add(key)
                amounts_found.append(raw)

    # --- Extraction dates ---
    dates_found: list[str] = []
    seen_dates: set[str] = set()
    for r in all_emails:
        text = r.get("body") or r.get("body_preview") or ""
        date = r.get("received_at") or r.get("date") or ""
        if date and date not in seen_dates:
            seen_dates.add(date)
            dates_found.append(date)
        # Chercher des dates dans le texte (ex: "7 février 2025", "07/02/2025")
        for m in re.finditer(r"\b(\d{1,2}\s+[a-zéûà]+\s+20\d{2})\b", text, re.IGNORECASE):
            d = m.group(1)
            if d not in seen_dates:
                seen_dates.add(d)
                dates_found.append(d)
        for m in re.finditer(r"\b(\d{1,2}[/-]\d{1,2}[/-]20\d{2})\b", text):
            d = m.group(1)
            if d not in seen_dates:
                seen_dates.add(d)
                dates_found.append(d)

    # --- Détection type de demande ---
    demand_type: str | None = None
    for r in all_emails:
        cat = (r.get("category") or "").lower()
        if cat in ("demande_client", "demande"):
            demand_type = "demande client"
            break
        if "infidel" in cat or "adultere" in cat:
            demand_type = "enquête d'infidélité"
            break
        if "surveillance" in cat or "filature" in cat:
            demand_type = "surveillance / filature"
            break
        if "recherche" in cat or "disparition" in cat:
            demand_type = "recherche de personne"
            break
        if "famille" in cat or "garde" in cat or "pension" in cat:
            demand_type = "enquête familiale"
            break
    if not demand_type:
        text_all = " ".join(r.get("body", "") or r.get("body_preview", "") or "" for r in all_emails).lower()
        if any(k in text_all for k in ("surveillance", "filature", "suivre", "observer")):
            demand_type = "surveillance / filature"
        elif any(k in text_all for k in ("infidel", "adultere", "tromperie", "ma femme", "mon mari", "conjoi")):
            demand_type = "enquête d'infidélité"
        elif any(k in text_all for k in ("disparu", "retrouver", "localiser", "fugue")):
            demand_type = "recherche de personne"
        elif any(k in text_all for k in ("garde", "pension", "enfant", "famille", "divorce")):
            demand_type = "enquête familiale"
        else:
            demand_type = "demande client"

    # --- Construction du résumé ---
    parts: list[str] = []
    parts.append(f"Voici le résumé du dossier **{dossier_id or 'non identifié'}** :")
    parts.append("")

    client_line = f"**Client** : {client_name}" if client_name else f"**Dossier** : {dossier_id}"
    parts.append(client_line)
    parts.append(f"**Type de demande** : {demand_type}")

    if dates_found:
        parts.append(f"**Dates clés** : {', '.join(dates_found[:3])}")

    if amounts_found:
        parts.append(f"**Montants mentionnés** : {', '.join(amounts_found[:6])}")
    else:
        parts.append("**Montants** : aucun montant financier détecté dans les emails.")

    parts.append("")
    # Résumé narratif basé sur les catégories et sujets
    subjects = [r.get("subject") for r in all_emails if r.get("subject")]
    if subjects:
        parts.append(f"**Emails trouvés** ({len(all_emails)} au total) : sujets liés à '{subjects[0][:60]}...'")

    return "\n".join(parts)


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
    years = _extract_years(question)
    # Pour les archives historiques : si plusieurs années, pas de filtre année
    # (la recherche par mot-clé trouve les emails de toutes les années)
    archive_year = None if len(years) > 1 else year
    log.info("charlie.ask", question=question[:60], dossier_id=dossier_id, year=year, years=years)

    # Détection d'intention (avant les closures — late binding Python)
    q_norm = _normalize(question)
    is_list_request = any(kw in q_norm for kw in ("liste", "lister", "donne-moi", "donne moi", "quels", "quelles", "lesquels", "lesquelles", "montre-moi", "tous les", "toutes les"))
    is_count_request = any(kw in q_norm for kw in ("combien", "nombre", "total", "count", "combien de"))
    is_dossier_count = any(kw in q_norm for kw in ("nouveau dossier", "dossier ouvert", "dossier cree", "dossiers crees", "combien de dossier", "ouvert depuis", "crees depuis", "nouveau client", "nouveaux client", "dossiers client"))
    is_dossier_list = is_list_request and not dossier_id and any(kw in q_norm for kw in ("dossier", "enquete", "enquetes", "affaire", "affaires", "client"))
    is_identity_request = any(kw in q_norm for kw in ("qui est", "nom", "prenom", "contact", "personne", "sappelle", "epouse", "mari", "conjoint"))
    is_dossier_summary = dossier_id is not None and any(kw in q_norm for kw in ("resume", "resumer", "resum", "synthese", "synthetiser", "info", "infos", "detail", "details", "situation", "etat"))

    # ── 2. Génération SQL ──
    # Fallback programmatique pour les comptages simples (pas besoin de LLM)
    sql = _build_count_sql(question, dossier_id) if is_count_request else ""
    if not sql:
        sql = _build_status_sql(question, dossier_id) or ""

    # Fallback recherche par mot-clé pour les questions factuelles spécifiques
    if not sql and not is_count_request and not is_dossier_count and not is_dossier_list:
        sql = _build_keyword_sql(question) or ""
        if sql:
            log.info("charlie.keyword_sql", question=question[:60], sql_preview=sql[:80])

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
        # Recherches factuelles (mot-clé, comptage, liste) = besoin de plus de sources
        is_factual_search = bool(sql) and not is_dossier_summary
        lim = 15 if (is_identity_request or is_list_request or is_dossier_list or is_factual_search) else settings.cerveau2_limit
        # Pour les questions identitaires, ne pas filtrer par dossier_id
        # car les fiches personnes/entités ne sont pas liées à un dossier
        vault_dossier_id = None if is_identity_request else dossier_id
        notes, ans = await query_vault(
            question=question,
            base_url=settings.cerveau2_base_url,
            api_secret=settings.cerveau2_api_secret,
            dossier_id=vault_dossier_id,
            limit=lim,
            context_only=False,
        )
        vault_answer = ans

        # --- FALLBACK DIRECT : pour les questions identitaires, si la recherche
        # sémantique ne remonte pas la fiche personne, on la demande directement
        # par son chemin. Cela contourne les problèmes d'indexation sqlite-vec.
        if is_identity_request and settings.cerveau2_base_url and settings.cerveau2_api_secret:
            # Extraire le nom cible (ex: "CDAL", "Christophe", "Sarah")
            q_words = [w for w in re.findall(r"[A-Za-zÀ-ÿ]+", question) if len(w) >= 3]
            candidate_slugs: set[str] = set()
            for w in q_words:
                w_lower = w.lower()
                # CDAL → christophe-dalla-valle (surnom connu)
                if w_lower == "cdal":
                    candidate_slugs.add("04_entities/personnes/christophe-dalla-valle.md")
                    candidate_slugs.add("04_entities/personnes/sarah-dalla-valle.md")
                # Christophe → christophe-dalla-valle
                elif w_lower == "christophe":
                    candidate_slugs.add("04_entities/personnes/christophe-dalla-valle.md")
                # Sarah → sarah-dalla-valle
                elif w_lower == "sarah":
                    candidate_slugs.add("04_entities/personnes/sarah-dalla-valle.md")
                # Daniel → daniel-hurchon
                elif w_lower == "daniel":
                    candidate_slugs.add("04_entities/personnes/daniel-hurchon.md")
                # DigitalHS → digitalhs-llc
                elif w_lower in ("digitalhs", "digital", "hs"):
                    candidate_slugs.add("04_entities/societes/digitalhs-llc.md")
                # Slug générique
                else:
                    slug = w_lower.replace(" ", "-")
                    candidate_slugs.add(f"04_entities/personnes/{slug}.md")
                    candidate_slugs.add(f"04_entities/societes/{slug}.md")

            existing_paths = {n.path for n in notes}
            coros = [
                get_vault_note(p, settings.cerveau2_base_url, settings.cerveau2_api_secret)
                for p in candidate_slugs if p not in existing_paths
            ]
            if coros:
                fetched = await asyncio.gather(*coros, return_exceptions=True)
                for item in fetched:
                    if isinstance(item, VaultNote):
                        notes.append(item)
                        log.info("charlie.vault_direct_fetch", path=item.path)

        return notes

    async def _memory_task() -> list:
        return await query_memory(db_path, question=question, dossier_id=dossier_id, limit=3)

    async def _correction_task() -> list:
        # Corrections locales — JAMAIS filtrées par dossier (globales)
        return await query_corrections(db_path, limit=3)

    async def _vault_correction_task() -> list[VaultNote]:
        # Pas de filtre dossier_id : les corrections sont globales
        return await query_corrections_vault(
            question=question,
            base_url=settings.cerveau2_base_url,
            api_secret=settings.cerveau2_api_secret,
            limit=3,
        )

    async def _archive_task() -> list[dict]:
        lim = 500 if is_count_request else 50
        if dossier_id:
            return await _search_historical_by_keyword(db_path, dossier_id, year=archive_year, limit=lim)
        keywords = _extract_keywords(question)
        if keywords:
            best = keywords[0][1]
            log.info("charlie.archive_keyword", best=best, all_keywords=[k[1] for k in keywords[:5]])
            return await _search_historical_by_keyword(db_path, best, year=archive_year, limit=lim)
        if archive_year:
            return await _search_historical_all(db_path, year=archive_year, limit=lim)
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

    (
        rows,
        vault_notes,
        memory_notes,
        correction_notes,
        vault_correction_notes,
        archive_rows,
        dossier_list,
    ) = await asyncio.gather(
        _sql_task(),
        _vault_task(),
        _memory_task(),
        _correction_task(),
        _vault_correction_task(),
        _archive_task(),
        _dossiers_task(),
    )

    # ── 3.5 Résolution des liens (nuage de liaison) ──
    linked_notes: list[VaultNote] = []
    if vault_notes:
        linked_notes = await _resolve_links(
            vault_notes,
            settings.cerveau2_base_url,
            settings.cerveau2_api_secret,
            max_links=5,
        )
    # Injecter les notes liées dans le pool principal pour que tous les bypass
    # (identité, entreprise, ville) y aient accès sans modification.
    if linked_notes:
        vault_notes = vault_notes + linked_notes

    # ── 3.6 COURT-CIRCUIT CORRECTIONS — si une correction Cerveau2 ou locale
    #    match la question, on retourne DIRECTEMENT la corrected_response sans LLM.
    #    C'est la règle absolue : la correction de Daniel prime sur tout.
    def _norm_q(q: str) -> str:
        return "".join(c for c in normalize("NFD", q.lower()) if c.isalnum())

    asked_norm = _norm_q(question)

    def _q_match(a: str, b: str) -> bool:
        """Match fuzzy entre deux questions normalisées."""
        a_norm = _norm_q(a)
        b_norm = _norm_q(b)
        if a_norm == b_norm:
            return True
        # Sous-chaîne si la question est assez longue (>= 15 chars)
        if len(a_norm) >= 15 and (a_norm in b_norm or b_norm in a_norm):
            return True
        # Similarité par mots : au moins 3 mots en commun ET ratio >= 0.7
        a_words = {w for w in a_norm.split() if len(w) >= 3}
        b_words = {w for w in b_norm.split() if len(w) >= 3}
        common = a_words & b_words
        if len(common) >= 3 and len(common) / max(len(a_words), len(b_words)) >= 0.7:
            return True
        return False

    # 1. Corrections locales (DB Charlie)
    for c in (correction_notes or []):
        if c.question and _q_match(c.question, question):
            log.info("charlie.correction_shortcut.local", question=question[:60])
            return CharlieResult(
                answer=c.response,
                sql="",
                rows=[],
                vault_notes=[],
                correction_notes=correction_notes,
            )

    # 2. Corrections Cerveau2
    for vc in (vault_correction_notes or []):
        vc_question = ""
        vc_corrected = ""
        for line in vc.content.splitlines():
            if line.strip().startswith("question:"):
                vc_question = line.split(":", 1)[1].strip().strip('"')
            if line.strip().startswith("corrected_response:"):
                vc_corrected = line.split(":", 1)[1].strip().strip('"')
        if vc_question and _q_match(vc_question, question) and vc_corrected:
            log.info("charlie.correction_shortcut.vault", question=question[:60], path=vc.path)
            return CharlieResult(
                answer=vc_corrected,
                sql="",
                rows=[],
                vault_notes=vault_notes,
                correction_notes=vault_correction_notes,
            )

    # ── 4. Construction du contexte ──
    context_parts: list[str] = []

    # Corrections (priorité absolue)
    if correction_notes:
        context_parts.append("CORRECTIONS UTILISATEUR LOCALES (priorité absolue — utiliser EXCLUSIVEMENT) :")
        for c in correction_notes[:3]:
            context_parts.append(f"- Q: {c.question}\n  RÉPONSE CORRECTE: {c.response}")
        context_parts.append("")

    if vault_correction_notes:
        context_parts.append("CORRECTIONS CERVEAU2 (priorité absolue — utiliser EXCLUSIVEMENT la corrected_response) :")
        for vc in vault_correction_notes[:3]:
            fname = vc.path.split("/")[-1].replace(".md", "")
            # Extraire corrected_response du frontmatter pour ne pas noyer le LLM
            corrected = ""
            question = ""
            for line in vc.content.splitlines():
                if line.strip().startswith("corrected_response:"):
                    corrected = line.split(":", 1)[1].strip().strip('"')
                if line.strip().startswith("question:"):
                    question = line.split(":", 1)[1].strip().strip('"')
            if corrected:
                context_parts.append(f"[{fname}] Question: {question}\nCORRECTED_RESPONSE: {corrected}")
            else:
                context_parts.append(f"[{fname}]\n{vc.content[:2000]}")
            context_parts.append("")
        context_parts.append("")

    # Vault — source principale (SECOND CERVEAU)
    if vault_notes:
        context_parts.append(f"SECOND CERVEAU — notes Cerveau2-Det ({len(vault_notes)} note(s)) :")
        for note in vault_notes:
            fname = note.path.split("/")[-1].replace(".md", "")
            context_parts.append(f"[{fname}]\n{note.content[:2000]}")
            context_parts.append("")
        context_parts.append("")

    # Nuage de liaison — documents liés par [[wikilinks]]
    if linked_notes:
        context_parts.append(f"NUAGE DE LIAISON — documents liés ({len(linked_notes)} lien(s)) :")
        for note in linked_notes:
            fname = note.path.split("/")[-1].replace(".md", "")
            context_parts.append(f"[LIEN] [{fname}]\n{note.content[:1500]}")
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
        if is_dossier_summary:
            # Pour les résumés de dossier, le LLM a besoin du CONTENU des emails,
            # pas d'un tableau de métadonnées. On injecte les body complets.
            context_parts.append(f"EMAILS BASE COURANTE ({len(rows)} email(s)) :")
            total_body = 0
            for r in rows[:10]:
                body = r.get("body") or r.get("body_preview") or ""
                if not body:
                    continue
                subject = r.get("subject") or "Sans sujet"
                date = r.get("received_at") or r.get("processed_at") or ""
                context_parts.append(f"--- Email : {subject} ({date}) ---")
                context_parts.append(body[:1500])
                total_body += len(body[:1500])
                if total_body > 6000:
                    context_parts.append("[… tronqué]")
                    break
            context_parts.append("")
        elif is_list_request:
            # Mode liste : tous les sujets visibles
            context_parts.append(f"EMAILS BASE COURANTE — SQL ({len(rows)} ligne(s)) :")
            context_parts.append(_sanitize_rows_for_prompt(rows))
            context_parts.append("")
        else:
            # Mode synthèse/recherche factuelle : RÉSUMÉ NARRATIF algorithmique
            # On n'envoie PAS une liste technique au LLM — il la recopierait.
            # On envoie un texte narratif qu'il doit synthétiser.
            from collections import Counter
            cat_counts = Counter(r.get("category") or "inconnu" for r in rows)
            top_cats = ", ".join(f"{k} ({v})" for k, v in cat_counts.most_common(3))
            recent_subjects = [r.get("subject", "Sans sujet") for r in rows[:3]]
            recent_text = " ; ".join(recent_subjects)
            date_range = ""
            if rows:
                dates = [r.get("received_at") or r.get("processed_at") or "" for r in rows]
                dates = [d for d in dates if d]
                if dates:
                    date_range = f"La période couverte va de {dates[-1][:10]} à {dates[0][:10]}."
            context_parts.append(f"RÉSUMÉ DES EMAILS TROUVÉS EN BASE COURANTE ({len(rows)} email(s)) :")
            context_parts.append(f"Catégories principales : {top_cats}. {date_range}")
            context_parts.append(f"Sujets les plus récents : {recent_text}.")
            context_parts.append("Tu dois SYNTHÉTISER ces informations en 1-2 phrases pour Daniel. Ne liste pas les sujets un par un.")
            context_parts.append("")
    elif sql:
        context_parts.append("EMAILS BASE COURANTE : aucun email trouvé.")
        context_parts.append("")

    # Archives historiques — contexte pour le LLM
    if archive_rows:
        if is_list_request:
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
        elif is_dossier_summary:
            # Résumé de dossier : body_preview des archives pour le LLM
            context_parts.append(f"EMAILS ARCHIVES HISTORIQUES ({len(archive_rows)} email(s)) :")
            context_parts.append("Contenu des emails pertinents :")
            total_preview = 0
            for r in archive_rows[:10]:
                preview = r.get("body_preview", "")
                if not preview:
                    continue
                subject = r.get("subject") or "Sans sujet"
                date = r.get("received_at") or r.get("date") or ""
                context_parts.append(f"--- Email : {subject} ({date}) ---")
                context_parts.append(preview[:1500])
                total_preview += len(preview[:1500])
                if total_preview > 8000:
                    context_parts.append("[… tronqué pour limiter le contexte]")
                    break
            context_parts.append("")
        else:
            # Recherche factuelle : RÉSUMÉ NARRATIF algorithmique des archives
            from collections import Counter
            cat_counts = Counter(r.get("category") or "inconnu" for r in archive_rows)
            top_cats = ", ".join(f"{k} ({v})" for k, v in cat_counts.most_common(3))
            recent_subjects = [r.get("subject", "Sans sujet") for r in archive_rows[:3]]
            recent_text = " ; ".join(recent_subjects)
            date_range = ""
            if archive_rows:
                dates = [r.get("received_at") or r.get("date") or "" for r in archive_rows]
                dates = [d for d in dates if d]
                if dates:
                    date_range = f"La période couverte va de {dates[-1][:10]} à {dates[0][:10]}."
            context_parts.append(f"RÉSUMÉ DES EMAILS TROUVÉS EN ARCHIVES ({len(archive_rows)} email(s)) :")
            context_parts.append(f"Catégories principales : {top_cats}. {date_range}")
            context_parts.append(f"Sujets les plus récents : {recent_text}.")
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

    # ── 5. Résumé de dossier — LLM CLAUDE avec prompt parfait ──
    if is_dossier_summary and (archive_rows or rows):
        # Utiliser le modèle chat (Kimi K2.6 sur Ollama Pro) pour les résumés
        summary_model = model

        # Assembler les contenus des emails (body complet, pas preview)
        email_parts: list[str] = []
        total_chars = 0
        all_emails = (archive_rows or []) + (rows or [])
        all_emails.sort(key=lambda r: r.get("received_at", r.get("date", "")), reverse=True)
        for r in all_emails[:10]:
            content = r.get("body") or r.get("body_preview") or ""
            if not content:
                continue
            subject = r.get("subject") or "Sans sujet"
            date = r.get("received_at") or r.get("date") or ""
            email_parts.append(f"SUJET: {subject} | DATE: {date}")
            email_parts.append(content[:2500])
            total_chars += len(content[:2500])
            if total_chars > 12000:
                email_parts.append("[tronqué pour limiter la taille]")
                break

        if email_parts:
            dossier_prompt = f"""Tu es Charlie, l'assistant IA personnel de Daniel Hurchon, détective privé chez Detective.be.

MISSION : Résumer le dossier "{dossier_id}" pour Daniel en UN SEUL PARAGRAPHE FLUIDE ET NARRATIF.

Ci-dessous, tu trouves les emails liés à ce dossier (contenus complets). Lis-les attentivement, comprends l'histoire, puis raconte-la à Daniel comme s'il te demandait "Qu'est-ce qui se passe avec ce dossier ?"

{chr(10).join(email_parts)}

CONSIGNES ABSOLUES :
- UN SEUL PARAGRAPHE continu. Pas de puces. Pas de tableaux. Pas de listes à puces.
- Mentionne : qui est le client, quelle est sa demande, quand ça se passe, et les montants financiers (offres, devis, honoraires, factures, acomptes, etc.) avec leur contexte.
- Si tu vois plusieurs emails, raconte la chronologie de l'échange.
- Sois direct, chaleureux, utilise "tu".
- Si aucun montant n'est mentionné, dis-le simplement.
- NE JAMAIS reproduire les métadonnées techniques (id, sender, statut, catégorie).
- NE JAMAIS dire "voici les informations" ou "selon les emails". Raconte l'histoire directement.

RÉPONSE :"""

            for attempt in (1, 2):
                try:
                    log.info("charlie.dossier_summary_llm_call", dossier_id=dossier_id, model=summary_model, attempt=attempt, prompt_len=len(dossier_prompt))
                    response = await complete(
                        model=summary_model,
                        messages=[{"role": "user", "content": dossier_prompt}],
                        max_tokens=1000,
                        temperature=0.3,
                    )
                    response = response.strip() if response else ""
                    log.info("charlie.dossier_summary_llm_response", dossier_id=dossier_id, attempt=attempt, response_len=len(response), response_preview=response[:300])
                    # Garde anti-vide seulement — on laisse le LLM décider du format
                    if response and len(response) > 50:
                        log.info("charlie.dossier_summary_ok", dossier_id=dossier_id, model=summary_model, attempt=attempt, len=len(response))
                        await _auto_save_fact(db_path, question, response, dossier_id)
                        return CharlieResult(
                            response_text=response,
                            sql=sql,
                            rows=rows,
                            sql_safe=True,
                            sql_error=None,
                            vault_notes=vault_notes,
                            hide_rows=True,  # ← Masque le tableau SQL dans le chat
                        )
                    log.warning("charlie.dossier_summary_too_short", attempt=attempt, preview=response[:300] if response else "(vide)")
                except Exception as e:
                    log.warning("charlie.dossier_summary_failed", attempt=attempt, error=str(e))

        # Dernier recours : message propre, jamais de tableau
        all_emails = (archive_rows or []) + (rows or [])
        all_emails.sort(key=lambda r: r.get("received_at", r.get("date", "")), reverse=True)
        lines = [f"J'ai trouvé **{len(all_emails)}** email{'s' if len(all_emails) > 1 else ''} liés au dossier **{dossier_id}**, mais je n'ai pas pu les résumer automatiquement. Voici les sujets :", ""]
        for r in all_emails[:8]:
            subject = r.get("subject") or "Sans sujet"
            date = r.get("received_at") or r.get("date") or ""
            line = f"- {subject}"
            if date:
                line += f" ({date})"
            lines.append(line)
        if len(all_emails) > 8:
            lines.append(f"… et {len(all_emails) - 8} autres.")
        fallback = "\n".join(lines)
        log.info("charlie.dossier_summary_fallback", dossier_id=dossier_id)
        await _auto_save_fact(db_path, question, fallback, dossier_id)
        return CharlieResult(
            response_text=fallback,
            sql=sql,
            rows=rows,
            sql_safe=True,
            sql_error=None,
            vault_notes=vault_notes,
            hide_rows=True,
        )

    # ── 6. Bypass LLM extraction directe depuis notes Cerveau2 (brutes, 0 ms, 100% déterministe) ──
    direct_answer: str | None = None
    if vault_notes:
        q_lower = question.lower()

        # 5a. Identité
        if _is_identity_query(question):
            direct_answer = _extract_identity_answer(vault_notes, question)
            if direct_answer:
                log.info("charlie.identity_direct_extract", question=question[:60], answer=direct_answer[:80])

        # 5b. Dossier par ville
        if not direct_answer and ("dossier" in q_lower or "enquête" in q_lower) and any(v in q_lower for v in ("bruxelles", "brussels", "brussel", "waterloo", "namur", "liège", "anvers", "gent", "gand")):
            ville_match = re.search(r"(?:à|a|sur|dans|en|pres de|proche de)\s+([A-Za-zÀ-Ÿ-]+)", question, re.IGNORECASE)
            if ville_match:
                direct_answer = _extract_dossier_par_ville(vault_notes, ville_match.group(1).strip())
                if direct_answer:
                    log.info("charlie.dossier_ville_direct", question=question[:60], answer=direct_answer[:80])

        # 5c. Entreprise / siège / localisation
        if not direct_answer and any(kw in _normalize(question) for kw in ("siege", "adresse", "localisation", "ou se trouve", "situe", "situer", "domicilie")):
            entreprise = _extract_entreprise_name(question)
            if entreprise:
                direct_answer = _extract_entreprise_info(vault_notes, entreprise)
                if direct_answer:
                    log.info("charlie.entreprise_info_direct", question=question[:60], answer=direct_answer[:80])

    if direct_answer:
        await _auto_save_fact(db_path, question, direct_answer, dossier_id)
        return CharlieResult(
            response_text=direct_answer,
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
        "tu n'as pas", "pas posé de question", "dernier message",
        "pas compris", "je ne comprends pas", "qu'est-ce que tu",
        "on fait quoi", "je t'écoute", "pas de question", "question précise",
        "pas de sujet", "pas de demande", "de quoi tu parles",
    )
    # Pertinence sémantique : la réponse du vault doit contenir AU MOINS un mot-clé
    # significatif de la question. Sinon c'est du garbage (ex: Lampaert quand on
    # demande des factures d'hôtel).
    def _vault_has_relevance(vault_ans: str, q: str) -> bool:
        if not vault_ans or len(vault_ans.strip()) < 30:
            return False
        q_words = {normalize("NFD", w.lower()).encode("ascii", "ignore").decode("ascii")
                   for w in re.findall(r"[A-Za-zÀ-Ÿà-ÿ]{4,}", q)}
        # Exclure les stop-words courants
        q_words -= {"moi", "vous", "dossier", "client", "question", "reponse",
                    "donne", "donner", "faire", "etre", "avoir", "aller", "comme",
                    "alors", "apres", "avant", "encore", "toujours", "jamais",
                    "toutes", "toute", "tous", "tout", "plusieurs", "quelques",
                    "beaucoup", "souvent", "parfois", "maintenant", "aujourd",
                    "hier", "demain", "matin", "soir", "jour", "semaine", "mois",
                    "annee", "temps", "heure", "minute", "avec", "depuis", "dans",
                    "pour", "sur", "sous", "entre", "contre", "vers", "chez",
                    "retrouve", "trouve", "cherche", "chercher", "liste", "lister",
                    "montre", "montrer", "donne", "donner"}
        ans_lower = normalize("NFD", vault_ans.lower()).encode("ascii", "ignore").decode("ascii")
        for w in q_words:
            if len(w) >= 4 and w in ans_lower:
                return True
        return False

    vault_has_bad = vault_answer and any(p in vault_answer.lower() for p in _BAD_VAULT)
    vault_is_relevant = vault_answer and _vault_has_relevance(vault_answer, question)
    if vault_answer and not is_count_request and not vault_has_bad and vault_is_relevant:
        # Cerveau2 a répondu en direct et de manière utile ET pertinente
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
            hide_rows=False,
            archive_rows=archive_rows,
        )
    if vault_has_bad or not vault_is_relevant:
        log.info("charlie.vault_answer_skipped", question=question[:60],
                 bad=vault_has_bad, relevant=vault_is_relevant,
                 preview=vault_answer[:120] if vault_answer else "(vide)")

    # ── 6.5 Guard anti-hallucination — si aucune source n'a de données ──
    if not vault_notes and not rows and not archive_rows and not vault_answer and not direct_answer:
        # Aucun contexte trouvé : le LLM inventerait obligatoirement.
        # On retourne une réponse honnête sans appeler le LLM.
        log.info("charlie.empty_guard_triggered", question=question[:60])
        msg = "Je n'ai trouvé aucune information sur ce sujet dans les sources disponibles."
        await _auto_save_fact(db_path, question, msg, dossier_id)
        return CharlieResult(
            response_text=msg,
            sql=sql,
            rows=rows,
            sql_safe=True,
            sql_error=None,
            vault_notes=vault_notes,
        )

    # ── 7. Appel LLM final pour les questions spécifiques ──
    if is_list_request:
        format_rule = "7. Daniel demande une LISTE. Si le second cerveau a des notes sur ces dossiers, liste-les en priorité. Sinon, extrait les noms de dossiers identifiables depuis les emails. Ne liste pas les catégories — donne les NOMS (ex: ADF, Zaventem, ODM)."
    elif is_count_request:
        format_rule = "7. Daniel demande un COMPTAGE. Donne le nombre total clair et précis."
    elif is_identity_request:
        format_rule = "7. Daniel demande une IDENTITÉ. Cherche dans le second cerveau et réponds en une ou deux phrases maximum, directement."
    elif is_dossier_summary:
        format_rule = "7. Daniel demande un RÉSUMÉ DE DOSSIER. Extrais les infos clés (client, demande, dates, montants) en un paragraphe clair et direct."
    elif bool(sql) and " LIKE " in sql:
        # Recherche factuelle par mot-clé (factures, hotel, etc.)
        format_rule = "RÉPOND EN 1-2 PHRASES FLUIDES. Résume pour Daniel ce que le Cerveau2 et les emails disent sur ce sujet. NE JAMAIS faire de liste à puces. NE JAMAIS recopier les sujets email un par un."
    else:
        format_rule = "7. Daniel demande une SYNTHÈSE ou une INFO. Réponds de manière fluide et directe, en une ou deux phrases maximum."

    # Si Cerveau2 a répondu mais c'est un comptage, on injecte sa réponse dans le contexte
    vault_context = context
    if vault_answer and not vault_has_bad and vault_is_relevant:
        vault_context = f"RÉPONSE DU SECOND CERVEAU (Cerveau2-Det) :\n{vault_answer.strip()}\n\n---\n\n{context}"
    elif vault_answer and (vault_has_bad or not vault_is_relevant):
        log.info("charlie.vault_context_purged", question=question[:60],
                 bad=vault_has_bad, relevant=vault_is_relevant,
                 preview=vault_answer[:120] if vault_answer else "(vide)")

    # ── Prompt LLM : court et ciblé pour les recherches factuelles, complet sinon ──
    is_factual = bool(sql) and " LIKE " in sql
    if is_factual:
        final_prompt = f"""Tu es Charlie, l'assistant de Daniel Hurchon (Detective.be). Version {VERSION}.

Question de Daniel : {question}

Voici ce que j'ai trouvé :
{vault_context}

Consigne absolue : réponds en 1-2 phrases fluides, directes, comme un partenaire qui fait un compte-rendu à Daniel. NE JAMAIS faire de liste à puces. NE JAMAIS recopier les sujets email un par un. SYNTHÉTISE. Utilise "tu".

RÉPONSE À DANIEL :"""
    else:
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
8. Le contexte ci-dessus contient des extraits d'emails et de notes. Tu dois les ANALYSER et SYNTHÉTISER en langage naturel fluide. NE RECOPIE JAMAIS les listes de sujets, les tableaux de métadonnées, ou les extraits techniques bruts. RACONTE ce que tu as trouvé, comme un partenaire qui fait un compte-rendu.
9. Si Daniel demande un résumé de dossier, extrais les infos clés (client, demande, dates, montants) en un paragraphe clair.
10. N'invente jamais d'informations absentes des sources ci-dessus.
11. ABSOLU : ta réponse ne doit contenir AUCUNE puce "- ", AUCUN tableau markdown "|", AUCUNE liste numérotée. Rédige uniquement en phrases continues.

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

    # Garde : réponse vide OU refus explicite alors qu'on a des données → réponse de secours
    # On ne court-circuite PAS le LLM pour "mauvais format" — le secours Python est encore pire.
    _BAD_RESPONSE = _BAD_VAULT + (
        "je n'ai pas trouvé", "aucun résultat", "aucune information",
        "je ne trouve pas", "pas d'information", "aucune donnée",
    )
    is_bad_response = any(p in response.lower() for p in _BAD_RESPONSE)
    if not response or (is_bad_response and (rows or archive_rows)):
        response = ""
    if not response:
        if is_dossier_summary and (rows or archive_rows):
            # Dernier recours pour un résumé de dossier : on ne fait JAMAIS un tableau brut.
            # On retourne un message propre avec les liens vers les emails trouvés.
            all_emails = (rows or []) + (archive_rows or [])
            all_emails.sort(key=lambda r: r.get("received_at", r.get("date", "")), reverse=True)
            lines = [f"J'ai trouvé **{len(all_emails)}** email{'s' if len(all_emails) > 1 else ''} liés au dossier **{dossier_id}**, mais je n'ai pas réussi à les synthétiser automatiquement. Voici les sujets :", ""]
            for r in all_emails[:10]:
                subject = r.get("subject") or "Sans sujet"
                date = r.get("received_at") or r.get("date") or ""
                line = f"- {subject}"
                if date:
                    line += f" ({date})"
                lines.append(line)
            if len(all_emails) > 10:
                lines.append(f"… et {len(all_emails) - 10} autres.")
            response = "\n".join(lines)
        elif is_count_request and (rows or archive_rows):
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
        elif rows:
            # Secours quand le LLM dit "pas trouvé" malgré des résultats SQL
            # JAMAIS de liste brute pour les recherches factuelles — résumé narratif algorithmique
            if bool(sql) and " LIKE " in sql:
                from collections import Counter
                cat_counts = Counter(r.get("category") or "inconnu" for r in rows)
                top_cats = ", ".join(f"{k} ({v})" for k, v in cat_counts.most_common(3))
                recent_subjects = [r.get("subject", "Sans sujet") for r in rows[:3]]
                recent_text = " ; ".join(recent_subjects)
                dates = [r.get("received_at") or r.get("processed_at") or "" for r in rows]
                dates = [d for d in dates if d]
                date_range = ""
                if dates:
                    date_range = f"La période couverte va de {dates[-1][:10]} à {dates[0][:10]}."
                if vault_answer and not vault_has_bad and vault_is_relevant:
                    vault_snippet = vault_answer.strip()[:300]
                    response = (
                        f"D'après le Cerveau2 : {vault_snippet}…\n\n"
                        f"J'ai aussi repéré **{len(rows)}** emails en base, principalement dans les catégories {top_cats}. "
                        f"{date_range} Les sujets récents portent sur : {recent_text}."
                    )
                else:
                    response = (
                        f"J'ai repéré **{len(rows)}** emails en base sur ce sujet, principalement dans les catégories {top_cats}. "
                        f"{date_range} Les sujets récents portent sur : {recent_text}."
                    )
            else:
                response = f"J'ai trouvé **{len(rows)}** élément{'s' if len(rows) > 1 else ''} en base. Tu veux que je te les détaille ?"
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
        archive_rows=archive_rows,
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


def _extract_dossier_par_ville(vault_notes: list[VaultNote], ville: str) -> str | None:
    """Extraction directe d'un dossier par ville depuis le vault Cerveau2, sans LLM.

    Parse le YAML frontmatter et le corps markdown pour trouver un dossier
    associé à la ville cherchée. Retourne un message formaté ou None.
    """
    import json
    ville_norm = ville.lower().strip()
    # Variantes de la ville
    variants = {ville_norm}
    if ville_norm == "bruxelles":
        variants |= {"brussels", "brussel", "bxl", "brux"}
    elif ville_norm == "brussels":
        variants |= {"bruxelles", "brussel", "bxl", "brux"}
    elif ville_norm == "brussel":
        variants |= {"bruxelles", "brussels", "bxl", "brux"}

    matches: list[tuple[str, str]] = []
    for note in vault_notes:
        content = note.content
        dossier_name: str | None = None

        # 1. Cherche dans le frontmatter YAML : dossier: "[[NOM/_index]]"
        m = re.search(r'dossier:\s*"?\[\[([^\]]+)/_index\]\]"?', content)
        if m:
            dossier_name = m.group(1)

        # 2. Cherche dans le corps markdown : **Dossier** : NOM
        if not dossier_name:
            m = re.search(r'\*\*Dossier\*\*\s*:\s*(\S+)', content)
            if m:
                dossier_name = m.group(1)

        if not dossier_name:
            continue

        # 3. Vérifie si la ville est mentionnée quelque part dans la note
        content_lower = content.lower()
        if any(v in content_lower for v in variants):
            matches.append((dossier_name, note.path))

    if not matches:
        return None

    # Dédoublonne et formate
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for d, p in matches:
        if d not in seen:
            seen.add(d)
            unique.append((d, p))

    if len(unique) == 1:
        return f"Le dossier **{unique[0][0]}** se déroule à **{ville.capitalize()}**."
    lines = [f"J'ai trouvé **{len(unique)}** dossiers se déroulant à **{ville.capitalize()}** :", ""]
    for d, p in unique:
        lines.append(f"- **{d}**")
    return "\n".join(lines)


def _extract_entreprise_name(question: str) -> str | None:
    """Extrait un nom d'entreprise potentiel depuis une question en français.

    Patterns cibles :
    - "siège de ADF Group" → ADF Group
    - "ADF Group" (majuscules consécutives ou nom suivi de 'sarl', 'sa', 'bvba')
    - "entreprise XXXX"
    """
    q_norm = _normalize(question)
    keywords = ("siege", "adresse", "localisation", "ou se trouve", "domiciliee", "domicilie", "entreprise", "situe", "situee", "situer", "ou se situe")
    if not any(kw in q_norm for kw in keywords):
        return None

    # Cherche "de/d' XXXX" dans la question originale (conserve les majuscules)
    m = re.search(
        r"(?:de|d')\s+([A-Z][A-Za-z0-9\s&\.\-]{2,}?)(?=\?|\.|,|$|\s+(?:sa|sarl|bvba|nv|sprl|asbl|scs|sca|scrl|à|a|en|dans|et|ou|qui|dont))",
        question, re.IGNORECASE,
    )
    if m:
        name = m.group(1).strip()
        if len(name) >= 2:
            return name

    # Fallback : acronyme en majuscules isolé (3+ lettres)
    for m in re.finditer(r"\b([A-Z]{3,})\b", question):
        code = m.group(1)
        if code not in ("SQL", "OK", "HTTP", "API", "URL", "HTML", "XML", "JSON", "DPDH", "AI", "VPS", "SMTP", "IMAP", "DNS", "CEO", "CFO", "COO", "SARL", "SA", "SCRL", "BVBA", "SPRL"):
            return code
    return None


def _extract_entreprise_info(vault_notes: list[VaultNote], entreprise: str) -> str | None:
    """Extraction directe d'informations sur une entreprise depuis le vault, sans LLM.

    Cherche dans le YAML frontmatter, le corps markdown et le texte brut des emails :
    - nom / entreprise / société
    - siège / ville / adresse / pays
    - emails, téléphones, contacts (fallback si pas d'adresse postale)
    Retourne un message formaté ou None.
    """
    ent_norm = entreprise.lower().strip()
    variants = {ent_norm}
    parts = ent_norm.split()
    if len(parts) > 1:
        variants.add(parts[0])

    DANIEL_SIGNATURE_MARKERS = ("detectivebelgique", "daniel hurchon", "0779.433.503", "chaussée bara")

    def _is_daniel_signature(text: str) -> bool:
        t = text.lower()
        return any(m in t for m in DANIEL_SIGNATURE_MARKERS)

    matches_loc: list[dict] = []
    contact_emails: set[str] = set()
    contact_phones: set[str] = set()
    contact_names: set[str] = set()

    for note in vault_notes:
        content = note.content
        content_lower = content.lower()

        found = any(v in content_lower for v in variants)
        if not found:
            continue

        info: dict = {"path": note.path, "nom": entreprise}

        # --- Extraction YAML frontmatter ---
        for field in ("nom", "entreprise", "societe", "société", "client", "raison_sociale"):
            m = re.search(rf"{field}\s*[:=]\s*\"?([^\"\n]+)\"?", content, re.IGNORECASE)
            if m:
                info["nom"] = m.group(1).strip()
                break

        for field in ("siege", "siège", "ville", "adresse", "pays", "country"):
            m = re.search(rf"{field}\s*[:=]\s*\"?([^\"\n]+)\"?", content, re.IGNORECASE)
            if m:
                info[field] = m.group(1).strip()

        # --- Extraction markdown inline ---
        m = re.search(r"\*\*Siège\*\*\s*[:=]\s*([^\n]+)", content, re.IGNORECASE)
        if m:
            info["siege"] = m.group(1).strip()
        m = re.search(r"\*\*Adresse\*\*\s*[:=]\s*([^\n]+)", content, re.IGNORECASE)
        if m:
            info["adresse"] = m.group(1).strip()
        m = re.search(r"\*\*Ville\*\*\s*[:=]\s*([^\n]+)", content, re.IGNORECASE)
        if m:
            info["ville"] = m.group(1).strip()

        # --- Extraction texte brut (emails, signatures, correspondances) ---
        # Emails du domaine ou mentionnant l'entreprise
        for email_match in re.finditer(r"[\w\.-]+@[\w\.-]+\.[a-z]{2,}", content):
            email = email_match.group(0)
            e_lower = email.lower()
            if any(v in e_lower for v in variants) or ent_norm.replace(" ", "") in e_lower.replace("-", "").replace(".", ""):
                contact_emails.add(email)
            # Si l'entreprise est ADF Group, tout @groupeadf.com est pertinent
            if "groupeadf" in e_lower and "adf" in ent_norm:
                contact_emails.add(email)

        # Téléphones belges / internationaux
        for phone_match in re.finditer(r"(?:\+32|0)(?:\s*\d){8,9}", content):
            raw = phone_match.group(0)
            digits = re.sub(r"\D", "", raw)
            if len(digits) >= 9 and not digits.endswith("433503"):  # exclure numéro Daniel
                contact_phones.add(raw.strip())

        # Noms de contact : lignes "M./Mme XXX" ou signatures
        for name_match in re.finditer(r"(?:M\.|Mme|Mr|Mrs|Dr)\s+([A-Z][a-zA-Z\-]+(?:\s+[A-Z][a-zA-Z\-]+)?)", content):
            contact_names.add(name_match.group(0).strip())

        # Adresse postale en texte brut — plusieurs patterns
        raw_address = None
        for pattern in (
            r"Siège\s*Social\s*[:=]\s*([^\n]+)",
            r"-?\s*Siège\s*[:=]\s*([^\n]+)",
            r"Adresse\s*[:=]\s*([^\n]+)",
            r"(?:situé|domicilié|domiciliée|résidant|réside)\s+(?:au|a|en|à)\s+([^\n]{3,60})",
        ):
            m = re.search(pattern, content, re.IGNORECASE)
            if m:
                candidate = m.group(1).strip()
                if not _is_daniel_signature(candidate):
                    raw_address = candidate
                    break

        if raw_address:
            info["siege"] = raw_address

        # Si on a au moins une info localisation
        if any(k in info for k in ("siege", "siège", "ville", "adresse", "pays", "country")):
            matches_loc.append(info)

    # --- Priorité 1 : adresse postale trouvée ---
    if matches_loc:
        seen: set[str] = set()
        unique: list[dict] = []
        for info in matches_loc:
            nom = info.get("nom", entreprise)
            if nom not in seen:
                seen.add(nom)
                unique.append(info)

        if len(unique) == 1:
            info = unique[0]
            parts_msg: list[str] = []
            siege = info.get("siege") or info.get("siège") or info.get("ville")
            if siege:
                parts_msg.append(f"son siège est à **{siege}**")
            if info.get("adresse"):
                parts_msg.append(f"adresse : {info['adresse']}")
            if info.get("pays") or info.get("country"):
                parts_msg.append(f"pays : {info.get('pays') or info.get('country')}")
            if parts_msg:
                return f"Pour **{info.get('nom', entreprise)}**, {', '.join(parts_msg)}."
            return f"J'ai trouvé **{info.get('nom', entreprise)}** dans le vault, mais sans détail de localisation."

        lines = [f"J'ai trouvé **{len(unique)}** entreprises correspondant à **{entreprise}** :", ""]
        for info in unique:
            nom = info.get("nom", entreprise)
            siege = info.get("siege") or info.get("siège") or info.get("ville")
            line = f"- **{nom}**"
            if siege:
                line += f" — siège à {siege}"
            lines.append(line)
        return "\n".join(lines)

    # --- Priorité 2 : contacts trouvés (emails, téléphones) sans adresse postale ---
    if contact_emails or contact_phones:
        parts_msg: list[str] = []
        if contact_emails:
            parts_msg.append(f"emails : {', '.join(sorted(contact_emails)[:3])}")
        if contact_phones:
            parts_msg.append(f"téléphones : {', '.join(sorted(contact_phones)[:2])}")
        if contact_names:
            parts_msg.append(f"contacts : {', '.join(sorted(contact_names)[:2])}")
        return (
            f"Je n'ai pas l'adresse postale du siège de **{entreprise}** dans nos données, "
            f"mais j'ai trouvé des coordonnées dans les correspondances : {', '.join(parts_msg)}."
        )

    return None


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

    # --- NOUVEAU : parser le frontmatter pour suivre les wikilinks relationnels ---
    relation_keys = {
        "épouse": {"epouse", "femme"}, "femme": {"epouse", "femme"},
        "mari": {"mari", "conjoint", "compagnon"},
        "conjoint": {"conjoint", "epouse", "mari", "compagne", "compagnon"},
        "compagne": {"compagne", "epouse", "conjoint"}, "compagnon": {"compagnon", "mari", "conjoint"},
        "fille": {"fille", "enfant"}, "fils": {"fils", "enfant"}, "enfant": {"enfant", "fille", "fils"},
        "père": {"pere", "parent"}, "mère": {"mere", "parent"}, "parent": {"parent", "pere", "mere"},
        "sœur": {"soeur"}, "frère": {"frere"},
    }
    expected_keys = relation_keys.get(relation, {relation.lower()})
    link_pattern = re.compile(r"\[\[([^\]\n]+?)\]\]")

    for note in vault_notes:
        fm = _extract_frontmatter(note.content)
        for key, val in fm.items():
            if key.lower() not in expected_keys:
                continue
            # Extraire le(s) slug(s) du wikilink
            slugs: list[str] = []
            if isinstance(val, str):
                for m in link_pattern.finditer(val):
                    slugs.append(m.group(1).strip())
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, str):
                        for m in link_pattern.finditer(item):
                            slugs.append(m.group(1).strip())
            for slug in slugs:
                # Chercher dans les notes (y compris liées) la fiche correspondante
                for linked in vault_notes:
                    if slug in linked.path or linked.path.endswith(f"{slug}.md"):
                        # Extraire prenom + nom du frontmatter
                        lfm = _extract_frontmatter(linked.content)
                        prenom = lfm.get("prenom", "")
                        nom = lfm.get("nom", "")
                        if prenom or nom:
                            full = f"{prenom} {nom}".strip()
                            return f"La {relation} de {target_person or 'CDAL'} est **{full}**."
                        # Fallback : titre H1
                        hm = re.search(r"^#\s+(.+)$", linked.content, re.MULTILINE)
                        if hm:
                            return f"La {relation} de {target_person or 'CDAL'} est **{hm.group(1).strip()}**."
                        # Fallback : nom de fichier
                        fname = linked.path.split("/")[-1].replace(".md", "").replace("-", " ").title()
                        return f"La {relation} de {target_person or 'CDAL'} est **{fname}**."

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
    Ne garde que les champs publics (subject, received_at, category, status, priority).
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
        status = r.get("status") or ""
        priority = r.get("priority") or ""
        line = f"- Sujet: {subject}"
        if cat:
            line += f" | Catégorie: {cat}"
        if status:
            line += f" | Statut: {status}"
        if priority:
            line += f" | Priorité: {priority}"
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

    # Bypass LLM pour les questions "dossier à VILLE" : extraction directe du vault
    q_lower = question.lower()
    if not has_sql and vault_notes and ("dossier" in q_lower or "enquête" in q_lower) and any(v in q_lower for v in ("bruxelles", "brussels", "brussel", "waterloo", "namur", "liège", "anvers", "gent", "gand")):
        ville_match = re.search(r"(?:à|a|sur|dans|en|pres de|proche de)\s+([A-Za-zÀ-Ÿ-]+)", question, re.IGNORECASE)
        if ville_match:
            ville = ville_match.group(1).strip()
            direct = _extract_dossier_par_ville(vault_notes, ville)
            if direct:
                log.info("charlie.dossier_ville_direct", question=question[:60], answer=direct[:80])
                return direct

    # Bypass LLM pour les questions "siège / adresse / localisation d'entreprise"
    if not has_sql and vault_notes and any(kw in q_lower for kw in ("siege", "adresse", "localisation", "ou se trouve", "où se trouve", "situe", "situer", "domicilie")):
        # Extraire le nom d'entreprise potentiel
        entreprise = _extract_entreprise_name(question)
        if entreprise:
            direct = _extract_entreprise_info(vault_notes, entreprise)
            if direct:
                log.info("charlie.entreprise_info_direct", question=question[:60], answer=direct[:80])
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
