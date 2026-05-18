from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from unicodedata import normalize

import aiosqlite
import structlog

from app.cerveau_client import VaultNote, query_vault
from app.config import get_settings
from app.llm.router import complete

log = structlog.get_logger()

CHARLIE_SYSTEM_PROMPT = """Tu es Charlie, l'assistant IA de Detective.be.
Tu aides l'opérateur à interroger la base de données des emails traités.

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
- human_draft TEXT — brouillon édité par l'opérateur
- reviewed_by INTEGER
- reviewed_at TEXT

Règles :
1. Si la question nécessite une requête SQL, génère UNIQUEMENT un SELECT
   (jamais INSERT/UPDATE/DELETE/DROP/ALTER).
2. Formate ta réponse exactement comme ceci :

SQL: <ta requête SELECT sur une seule ligne, sans saut de ligne>
---
RÉPONSE: <ta réponse conversationnelle en français, courte et directe>

3. Si la question ne nécessite pas de SQL (salutation, question générale),
   laisse SQL vide :

SQL:
---
RÉPONSE: <ta réponse>

4. Pour les dates, utilise le format ISO (YYYY-MM-DD) dans les requêtes SQL.
5. Toujours répondre en français.
6. Quand tu listes des emails, inclus TOUJOURS les colonnes `id` et `subject`
   dans ton SELECT (ainsi que les autres colonnes utiles).
   Cela permet de créer des liens cliquables vers la conversation.
7. Quand l'utilisateur demande le contenu, le détail ou un résumé d'un dossier,
   utilise la colonne `body` (contenu complet) dans ton SELECT, pas `body_preview`.
   Inclus aussi `ai_draft` si pertinent.
8. Quand l'utilisateur demande un résumé ou une synthèse, ta RÉPONSE doit
   contenir le résumé en langage naturel — pas juste une liste de champs.
   Analyse le contenu des mails et rédige une synthèse claire et utile.
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


_SUMMARY_PROMPT = """Tu es Charlie, l'assistant IA de Detective.be.
Tu viens d'exécuter une requête SQL pour l'opérateur et voici les résultats.

Question de l'opérateur : {question}

Résultats SQL ({count} lignes) :
{rows}

Rédige une réponse en français, concise et utile :
- Si l'opérateur demande un résumé ou une synthèse, analyse le contenu des mails
  et rédige une synthèse claire.
- Si l'opérateur demande un détail, présente l'information de façon lisible.
- Si les résultats sont une simple liste, présente-les proprement.
- Toujours mentionner les ID et sujets pour permettre les liens cliquables.
- Si aucun résultat, dis-le simplement.
"""


async def ask_charlie(question: str, db_path: Path, model: str | None = None) -> CharlieResult:
    """Pipeline Charlie AI complet : question → LLM → SQL → validation → exécution → synthèse."""
    settings = get_settings()
    model = model or settings.llm_model_default

    messages = [
        {"role": "system", "content": CHARLIE_SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]

    try:
        raw = await complete(model=model, messages=messages, max_tokens=800, temperature=0.1)
    except Exception as e:
        log.warning("charlie.llm_failed", error=str(e))
        return CharlieResult(
            response_text="Charlie est momentanément indisponible. Réessaie dans un instant.",
            sql="", rows=None, sql_safe=True, sql_error=None,
        )

    sql, response_text = parse_charlie_response(raw)
    result = CharlieResult(
        response_text=response_text, sql=sql, rows=None, sql_safe=True, sql_error=None,
    )

    if sql:
        if not is_safe_sql(sql):
            result.sql_safe = False
            return result

        try:
            rows = await run_sql(db_path, sql)
            result.rows = rows
        except Exception as e:
            log.warning("charlie.sql_failed", sql=sql, error=str(e))
            result.sql_error = str(e)
            return result

        if rows and _needs_summary(question):
            summary = await _summarize_results(question, rows, model, settings)
            if summary:
                result.response_text = summary

    if _is_vault_relevant(question, sql):
        result.vault_notes = await query_vault(
            question=question,
            base_url=settings.cerveau2_base_url,
            api_secret=settings.cerveau2_api_secret,
            limit=settings.cerveau2_limit,
        )

    return result


_NEEDS_SUMMARY_KEYWORDS = (
    "resume", "synthese", "synthetiser",
    "analyser", "analyse", "detail",
    "contenu", "explique", "expliquer", "que dit",
    "donne-moi le contenu", "de quoi parle",
)

_VAULT_KEYWORDS = (
    "similaire", "historique", "passe", "precedent",
    "anterieur", "archive", "contexte", "dossier",
    "affaire", "enquete", "investigation", "correspondance",
)


def _normalize(text: str) -> str:
    return normalize("NFKD", text.lower()).encode("ascii", "ignore").decode("ascii")


def _needs_summary(question: str) -> bool:
    q = _normalize(question)
    return any(kw in q for kw in _NEEDS_SUMMARY_KEYWORDS)


def _is_vault_relevant(question: str, sql: str) -> bool:
    if not sql:  # question conversationnelle → vault toujours utile
        return True
    q = _normalize(question)
    return any(kw in q for kw in _VAULT_KEYWORDS)


async def _summarize_results(
    question: str, rows: list[dict], model: str, settings,
) -> str | None:
    """Appelle le LLM une seconde fois pour synthétiser les résultats SQL."""
    import json

    rows_text = json.dumps(rows[:20], ensure_ascii=False, default=str)
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
