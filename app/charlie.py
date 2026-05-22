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

from app.cerveau_client import VaultNote, query_vault
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
- received_at TEXT (format ISO, ex: 2026-05-15T10:30:00)
- category TEXT  — demande_client, urgent, newsletter, facture, spam,
  phishing, rappel, autre
- status TEXT    — pending, approved, rejected, sent, reviewed
- priority TEXT  — high, normal, low
- processed_at TEXT (format ISO)
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

4. Pour les dates, utilise le format ISO (YYYY-MM-DD) dans les requêtes SQL.
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
   - Inclus TOUJOURS la condition sur l'année si Daniel la précise :
     `received_at >= '2026-01-01' AND received_at < '2027-01-01'`
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
    TOUJOURS du PLUS RÉCENT au PLUS ANCIEN (`ORDER BY received_at DESC`).
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


async def ask_charlie(
    question: str,
    db_path: Path,
    model: str | None = None,
    history: list[dict] | None = None,
) -> CharlieResult:
    """Pipeline Charlie AI V1.14.0 — Orchestrateur Intelligence.

    classify → execute → formulate
    """
    settings = get_settings()
    model = model or settings.llm_model_default

    # ── Phase 1 : Classification ──
    qtype, entities = _classify_question(question)
    dossier_id = entities.get("dossier_id")
    year = entities.get("year")
    log.info("charlie.classify", qtype=qtype, dossier_id=dossier_id, year=year, question=question[:60])

    # ── Phase 2 : Exécution spécialisée ──
    facts = await _execute_plan(qtype, entities, question, db_path, model, settings)

    # ── Phase 3 : Formulation ──
    response = await _formulate_response(qtype, facts, question, model, settings)

    # Auto-save des faits clés
    await _auto_save_fact(db_path, question, response, dossier_id)

    return CharlieResult(
        response_text=response,
        sql=facts.get("sql", ""),
        rows=facts.get("rows"),
        sql_safe=True,
        sql_error=facts.get("sql_error"),
        vault_notes=facts.get("vault_notes", []),
    )


async def _generate_count_sql(
    question: str,
    model: str,
    settings,
    dossier_id: str | None,
) -> str:
    """Génère un SELECT COUNT(*) pour une question de comptage."""
    system = CHARLIE_SYSTEM_PROMPT + (
        "\n\nINSTRUCTION SPÉCIALE : cette question demande un COMPTAGE. "
        "Génère UNIQUEMENT `SELECT COUNT(*) as total FROM mail_processed WHERE ...`. "
        "N'inclus JAMAIS d'autres colonnes."
    )
    if dossier_id:
        system += (
            f"\n\nNote : Daniel demande le dossier '{dossier_id}'. "
            "Inclus ce terme dans les clauses LIKE sur subject, body, body_preview, ai_draft, sender."
        )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": _enrichir_question(question)},
    ]
    try:
        raw = await complete(model=model, messages=messages, max_tokens=300, temperature=0.1)
    except Exception:
        return ""
    sql, _ = parse_charlie_response(raw)
    return sql


async def _generate_list_sql(
    question: str,
    model: str,
    settings,
    dossier_id: str | None,
) -> str:
    """Génère un SELECT pour une question de liste."""
    system = CHARLIE_SYSTEM_PROMPT + (
        "\n\nINSTRUCTION SPÉCIALE : cette question demande une LISTE. "
        "Génère un SELECT avec id, subject, received_at, category. "
        "Classe par `ORDER BY received_at DESC`."
    )
    if dossier_id:
        system += (
            f"\n\nNote : Daniel demande le dossier '{dossier_id}'. "
            "Inclus ce terme dans les clauses LIKE sur subject, body, body_preview, ai_draft, sender."
        )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": _enrichir_question(question)},
    ]
    try:
        raw = await complete(model=model, messages=messages, max_tokens=400, temperature=0.1)
    except Exception:
        return ""
    sql, _ = parse_charlie_response(raw)
    return sql


async def _generate_summary_sql(
    question: str,
    model: str,
    settings,
    dossier_id: str | None,
) -> str:
    """Génère un SELECT pour une question de synthèse."""
    system = CHARLIE_SYSTEM_PROMPT
    if dossier_id:
        system += (
            f"\n\nNote : Daniel demande le dossier '{dossier_id}'. "
            "Inclus ce terme dans les clauses LIKE sur subject, body, body_preview, ai_draft, sender."
        )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": _enrichir_question(question)},
    ]
    try:
        raw = await complete(model=model, messages=messages, max_tokens=500, temperature=0.1)
    except Exception:
        return ""
    sql, _ = parse_charlie_response(raw)
    return sql


async def _execute_plan(
    qtype: str,
    entities: dict,
    question: str,
    db_path: Path,
    model: str,
    settings,
) -> dict:
    """Exécute la recherche spécialisée selon le type de question.

    Retourne un dict avec les faits structurés :
    - "count"   → {total: int, sql: str, rows: list, archives_count: int}
    - "list"    → {rows: list, sql: str, archive_rows: list}
    - "identity"→ {answer: str, source: str, vault_notes: list}
    - "summary" → {rows: list, vault_notes: list, memory_notes: list, correction_notes: list, sql: str}
    - "general" → {answer: str}
    """
    dossier_id = entities.get("dossier_id")
    year = entities.get("year")
    facts: dict = {}

    if qtype == "general":
        facts["answer"] = _general_response(question)
        return facts

    if qtype == "count":
        sql = await _generate_count_sql(question, model, settings, dossier_id)
        facts["sql"] = sql
        sql_rows: list[dict] = []
        if sql and is_safe_sql(sql):
            try:
                sql_rows = await run_sql(db_path, sql)
            except Exception as e:
                log.warning("charlie.count_sql_failed", sql=sql, error=str(e))
                facts["sql_error"] = str(e)
        sql_count = 0
        if sql_rows and len(sql_rows) == 1 and len(sql_rows[0]) == 1:
            sql_count = int(next(iter(sql_rows[0].values())) or 0)
        facts["rows"] = sql_rows

        archive_count = 0
        if dossier_id:
            histo = await _search_historical_by_keyword(db_path, dossier_id, year=year, limit=1000)
            archive_count = len(histo)
        else:
            q_norm = _normalize(question)
            matched_cat = None
            for type_key, cat in _ENQUETE_TO_CATEGORY.items():
                if type_key in q_norm:
                    matched_cat = cat
                    break
            if matched_cat:
                histo = await _search_historical_by_category(db_path, matched_cat, year=year, limit=1000)
                archive_count = len(histo)

        total = sql_count + archive_count
        if sql_count == 0 and archive_count > 0:
            total = archive_count
        facts["total"] = total
        facts["sql_count"] = sql_count
        facts["archives_count"] = archive_count
        return facts

    if qtype == "identity":
        corrections = await query_corrections(db_path, dossier_id=dossier_id, limit=5)
        if corrections:
            facts["answer"] = corrections[0].response.strip()
            facts["source"] = "correction"
            facts["vault_notes"] = []
            log.info("charlie.identity_correction_bypass", dossier_id=dossier_id)
            return facts

        vault_notes = await query_vault(
            question=question,
            base_url=settings.cerveau2_base_url,
            api_secret=settings.cerveau2_api_secret,
            dossier_id=dossier_id,
            limit=settings.cerveau2_limit,
        )
        facts["vault_notes"] = vault_notes
        if vault_notes:
            direct = _extract_identity_answer(vault_notes, question)
            if direct:
                facts["answer"] = direct
                facts["source"] = "vault_extract"
                return facts

        memory_notes = await query_memory(db_path, question=question, dossier_id=dossier_id, limit=3)
        if memory_notes:
            facts["answer"] = memory_notes[0].response
            facts["source"] = "memory"
            return facts

        facts["answer"] = ""
        facts["source"] = "none"
        return facts

    if qtype == "list":
        sql = await _generate_list_sql(question, model, settings, dossier_id)
        facts["sql"] = sql
        rows: list[dict] = []
        if sql and is_safe_sql(sql):
            try:
                rows = await run_sql(db_path, sql)
            except Exception as e:
                log.warning("charlie.list_sql_failed", sql=sql, error=str(e))
                facts["sql_error"] = str(e)
        facts["rows"] = rows

        archive_rows: list[dict] = []
        if dossier_id:
            archive_rows = await _search_historical_by_keyword(db_path, dossier_id, year=year, limit=20)
        else:
            q_norm = _normalize(question)
            matched_cat = None
            for type_key, cat in _ENQUETE_TO_CATEGORY.items():
                if type_key in q_norm:
                    matched_cat = cat
                    break
            if matched_cat:
                archive_rows = await _search_historical_by_category(db_path, matched_cat, year=year, limit=20)
        facts["archive_rows"] = archive_rows
        return facts

    # Fallback "summary"
    sql = await _generate_summary_sql(question, model, settings, dossier_id)
    facts["sql"] = sql

    async def _sql_task() -> list[dict]:
        if not sql or not is_safe_sql(sql):
            return []
        try:
            return await run_sql(db_path, sql)
        except Exception as e:
            log.warning("charlie.summary_sql_failed", sql=sql, error=str(e))
            facts["sql_error"] = str(e)
            return []

    async def _vault_task() -> list:
        return await query_vault(
            question=question,
            base_url=settings.cerveau2_base_url,
            api_secret=settings.cerveau2_api_secret,
            dossier_id=dossier_id,
            limit=settings.cerveau2_limit,
        )

    async def _memory_task() -> list:
        return await query_memory(db_path, question=question, dossier_id=dossier_id, limit=3)

    async def _correction_task() -> list:
        return await query_corrections(db_path, dossier_id=dossier_id, limit=3)

    rows, vault_notes, memory_notes, correction_notes = await asyncio.gather(
        _sql_task(), _vault_task(), _memory_task(), _correction_task(),
    )
    facts["rows"] = rows
    facts["vault_notes"] = vault_notes
    facts["memory_notes"] = memory_notes
    facts["correction_notes"] = correction_notes
    return facts


async def _formulate_response(
    qtype: str,
    facts: dict,
    question: str,
    model: str,
    settings,
) -> str:
    """Formule la réponse finale à partir des faits structurés."""

    if qtype == "general":
        return facts.get("answer", _general_response(question))

    if qtype == "count":
        total = facts.get("total", 0)
        sql_count = facts.get("sql_count", 0)
        archive_count = facts.get("archives_count", 0)
        if total == 0:
            return "Aucun email trouvé pour cette recherche."
        parts = [f"J'ai trouvé **{total}** email{"s" if total > 1 else ""}."]
        if sql_count > 0 and archive_count > 0:
            parts.append(f"({sql_count} en base courante + {archive_count} dans les archives)")
        elif archive_count > 0 and sql_count == 0:
            parts.append("(tous dans les archives historiques — la base courante est vide pour cette période)")
        return " ".join(parts)

    if qtype == "identity":
        answer = facts.get("answer", "")
        if answer:
            return answer
        vault_notes = facts.get("vault_notes", [])
        if vault_notes:
            summary = await _summarize_results(
                question, [], vault_notes, [], [], model, settings,
            )
            if summary:
                return summary
        return "Je ne trouve pas cette information."

    if qtype == "list":
        rows = facts.get("rows", []) or []
        archive_rows = facts.get("archive_rows", []) or []
        all_rows = rows + archive_rows
        if not all_rows:
            return "Aucun email trouvé pour cette recherche."
        all_rows.sort(
            key=lambda r: r.get("received_at") or r.get("date") or "",
            reverse=True,
        )
        lines = []
        count = len(all_rows)
        lines.append(f"J'ai trouvé **{count}** résultat{"s" if count > 1 else ""} :")
        lines.append("")
        for r in all_rows[:15]:
            subject = r.get("subject") or "Sans sujet"
            date = r.get("received_at") or r.get("date") or ""
            cat = r.get("category") or ""
            line = f"- **{subject}**"
            if date:
                line += f" ({date})"
            if cat:
                line += f" — {cat}"
            lines.append(line)
        if count > 15:
            lines.append(f"\n… et {count - 15} autres.")
        return "\n".join(lines)

    # qtype == "summary"
    rows = facts.get("rows", []) or []
    vault_notes = facts.get("vault_notes", [])
    memory_notes = facts.get("memory_notes", [])
    correction_notes = facts.get("correction_notes", [])

    if correction_notes and _is_identity_query(question):
        return correction_notes[0].response.strip()

    summary = await _summarize_results(
        question, rows, vault_notes, memory_notes, correction_notes, model, settings,
    )
    if summary:
        return summary

    if rows:
        return _sanitize_rows_for_prompt(rows)
    return "Je n'ai pas trouvé d'informations pour cette question."


def _general_response(question: str) -> str:
    """Réponse codée en dur pour les questions générales (pas de LLM)."""
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
    return "Je suis Charlie, ton assistant. Pose-moi une question sur les emails ou les dossiers."

_NEEDS_SUMMARY_KEYWORDS = (
    "resume", "synthese", "synthetiser",
    "analyser", "analyse", "detail",
    "contenu", "explique", "expliquer", "que dit",
    "donne-moi le contenu", "de quoi parle",
    "sait", "connait", "connaitre", "quoi",
    "informations", "info", "dossier", "parle",
    "contact", "qui", "personne", "nom", "client",
    "email", "mail", "message", "a recu", "as recu",
    "filature", "surveillance", "observation", "terrain",
    "adulte", "infidelite", "tromperie", "concubinage",
    "disparition", "recherche_personne", "localisation",
    "harcelement", "intimidation", "stalking",
    "garde", "pension", "famille", "enfant", "mineur",
    "vol", "fraude", "delit", "crime", "prejudice",
    "accident", "assurance", "compagnie", "indemnisation",
    "testament", "heritage", "succession", "notaire",
    "entreprise", "societe", "patron", "salarie", "licenciement",
    "matos", "materiel", "detecteur", "camera",
    "collaboration", "sous_traitance", "partenaire", "associe",
    "divorce", "separation", "couple", "concubin",
    "droit", "visite", "hebergement", "custodie",
    "rapport", "constat", "photo", "video", "preuve",
)

_VAULT_KEYWORDS = (
    "similaire", "historique", "passe", "precedent",
    "anterieur", "archive", "contexte", "dossier",
    "affaire", "enquete", "investigation", "correspondance",
    "filature", "surveillance", "observation", "terrain",
    "adulte", "infidelite", "tromperie", "concubinage",
    "disparition", "recherche_personne", "localisation",
    "harcelement", "intimidation", "stalking",
    "garde", "pension", "famille", "enfant", "mineur",
    "vol", "fraude", "delit", "crime", "prejudice",
    "accident", "assurance", "compagnie", "indemnisation",
    "testament", "heritage", "succession", "notaire",
    "entreprise", "societe", "patron", "salarie", "licenciement",
    "matos", "materiel", "detecteur", "camera",
    "collaboration", "sous_traitance", "partenaire", "associe",
    "divorce", "separation", "couple", "concubin",
    "droit", "visite", "hebergement", "custodie",
    "rapport", "constat", "photo", "video", "preuve",
    # Identité / connaissance factuelle (docs Cerveau2)
    "qui", "personne", "nom", "prenom", "client",
    "epouse", "mari", "conjoint", "contact",
)

_DOSSIER_RE = re.compile(
    # (?i:...) rend case-insensitive UNIQUEMENT le préfixe (dossier/affaire/etc.)
    # Le groupe de capture reste strictement case-sensitive sur la première lettre :
    # un vrai dossier_id DOIT commencer par une majuscule (code, nom propre).
    # Les lettres suivantes peuvent être mixtes (ex: Dutry, Gacaferi).
    r"(?i:dossier|affaire|projet|enquete|investigation)"
    r"[\s:]+([A-Z][a-zA-Z0-9]{2,})",
)
_HASH_DOSSIER_RE = re.compile(r"#([A-Z][A-Z0-9]{2,})")


def _extract_dossier_id(question: str) -> str | None:
    m = _DOSSIER_RE.search(question)
    if m:
        return m.group(1)
    m = _HASH_DOSSIER_RE.search(question)
    if m:
        return m.group(1)
    return None


_YEAR_RE = re.compile(r"\b(20\d{2})\b")


def _extract_year(question: str) -> str | None:
    """Extrait une année (20xx) de la question."""
    m = _YEAR_RE.search(question)
    return m.group(1) if m else None


def _classify_question(question: str) -> tuple[str, dict]:
    """Classifie la question pour déterminer le type de recherche nécessaire.

    Retourne (type, entities) où type est l'un de :
    - "general" : salutation, version, question hors-sujet
    - "count" : comptage (combien, nombre, total)
    - "list" : demande de liste (liste, quels, quelles)
    - "identity" : question identitaire (qui est, nom, contact, personne)
    - "summary" : résumé, synthèse, analyse, contenu
    """
    q_norm = _normalize(question)
    entities: dict = {}

    # Extraire entités communes
    entities["dossier_id"] = _extract_dossier_id(question)
    entities["year"] = _extract_year(question)

    # ── Type GENERAL (pas de recherche nécessaire) ──
    general_keywords = (
        "salut", "bonjour", "coucou", "hey", "hello",
        "version", "quelle version", "tu es qui", "qui es-tu",
        "ca va", "comment vas-tu", "merci", "au revoir",
    )
    if any(kw in q_norm for kw in general_keywords):
        return "general", entities

    # ── Type COUNT (comptage) ──
    count_keywords = (
        "combien", "nombre", "total", "count", "combien d'",
        "combien de", "combien demail", "combien de mail",
    )
    if any(kw in q_norm for kw in count_keywords):
        return "count", entities

    # ── Type IDENTITY (identité, contact, personne) ──
    identity_keywords = (
        "qui est", "qui est-ce", "nom", "prenom", "contact",
        "personne", "client", "sappelle", "epouse", "mari",
        "conjoint", "femme", "compagne", "compagnon",
        "fille", "fils", "enfant", "bebe", "pere", "mere",
        "soeur", "frere", "famille", "oncle", "tante",
    )
    if any(kw in q_norm for kw in identity_keywords):
        return "identity", entities

    # ── Type LIST (liste, énumération) ──
    list_keywords = (
        "liste", "lister", "donne-moi", "donne moi",
        "quels", "quelles", "lesquels", "lesquelles",
        "montre-moi", "affiche", "tous les", "toutes les",
    )
    if any(kw in q_norm for kw in list_keywords):
        return "list", entities

    # ── Fallback SUMMARY ──
    return "summary", entities


def _normalize(text: str) -> str:
    return normalize("NFKD", text.lower()).encode("ascii", "ignore").decode("ascii")


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
