"""Lecture synchrone des paramètres runtime depuis app_settings (DB).

Fournit un fallback DB → .env pour les paramètres "hot" que le superadmin
peut modifier en ligne sans redémarrer le serveur.
"""

import sqlite3
from pathlib import Path

from app.config import get_settings

_CACHED_DB_PATH: Path | None = None


def _db_path() -> Path:
    global _CACHED_DB_PATH
    if _CACHED_DB_PATH is None:
        _CACHED_DB_PATH = Path(get_settings().db_agent_state)
    return _CACHED_DB_PATH


def get_runtime_setting(key: str, default: str | None = None) -> str | None:
    """Lit une valeur dans app_settings (DB) en synchrone, avec fallback .env."""
    db_path = _db_path()
    if not db_path.exists():
        return default
    try:
        conn = sqlite3.connect(db_path, timeout=2.0)
        cur = conn.execute(
            "SELECT value FROM app_settings WHERE key = ? LIMIT 1",
            (key,),
        )
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    return default


def get_llm_models() -> tuple[str, str]:
    """Retourne (default_model, fallback_model) en lisant DB puis .env."""
    env = get_settings()
    default = get_runtime_setting("llm_model_default") or env.llm_model_default
    fallback = get_runtime_setting("llm_model_fallback") or env.llm_model_fallback
    return default, fallback


def get_llm_model_classifier() -> str:
    """Retourne le modèle de classification (DB puis .env)."""
    env = get_settings()
    return get_runtime_setting("llm_model_classifier") or env.llm_model_classifier


def get_llm_model_qualifier() -> str:
    """Retourne le modèle dédié à la qualification prospect (DB puis .env)."""
    env = get_settings()
    return get_runtime_setting("llm_model_qualifier") or getattr(
        env, "llm_model_qualifier", "openai/gemma4:31b"
    )
