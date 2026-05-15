from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import aiosqlite
import structlog

from app.config import get_settings
from app.llm.router import complete

log = structlog.get_logger()

CHARLIE_SYSTEM_PROMPT = """Tu es Charlie, l'assistant IA de Detective.be. Tu aides l'opérateur à interroger la base de données des emails traités.  # noqa: E501

Schéma de la table principale (mail_processed) :
- id INTEGER PRIMARY KEY
- mailbox_name TEXT  — detective_belgique (D_FR), detective_belgium (D_NL), dpdh_investigations (D_PD)  # noqa: E501
- subject TEXT
- sender TEXT
- received_at TEXT (format ISO, ex: 2026-05-15T10:30:00)
- category TEXT  — demande_client, urgent, newsletter, facture, spam, phishing, rappel, autre
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
1. Si la question nécessite une requête SQL, génère UNIQUEMENT un SELECT (jamais INSERT/UPDATE/DELETE/DROP/ALTER).  # noqa: E501
2. Formate ta réponse exactement comme ceci :

SQL: <ta requête SELECT sur une seule ligne, sans saut de ligne>
---
RÉPONSE: <ta réponse conversationnelle en français, courte et directe>

3. Si la question ne nécessite pas de SQL (salutation, question générale), laisse SQL vide :

SQL:
---
RÉPONSE: <ta réponse>

4. Pour les dates, utilise le format ISO (YYYY-MM-DD) dans les requêtes SQL.
5. Toujours répondre en français.
6. Quand tu listes des emails, inclus TOUJOURS les colonnes `id` et `subject` dans ton SELECT (ainsi que les autres colonnes utiles). Cela permet de créer des liens cliquables vers la conversation.  # noqa: E501
7. Quand l'utilisateur demande le contenu ou le détail d'un mail, utilise la colonne `body` (contenu complet) dans ton SELECT, pas `body_preview`.  # noqa: E501
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
    for dangerous in _DANGEROUS_SQL:
        if dangerous in cleaned:
            return False
    return True


async def run_sql(db_path: Path, sql: str) -> list[dict]:
    """Exécute un SELECT sur agent_state.db et retourne les résultats."""
    async with aiosqlite.connect(db_path) as db, db.execute(sql) as cursor:
        rows = await cursor.fetchall()
        desc = cursor.description
        if desc is None:
            return []
        keys = [d[0] for d in desc]
        return [dict(zip(keys, row, strict=True)) for row in rows]


async def ask_charlie(question: str, db_path: Path, model: str | None = None) -> CharlieResult:
    """Pipeline Charlie AI complet : question → LLM → SQL → validation → exécution."""
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

    if not sql:
        return result

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
