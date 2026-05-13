"""One-shot : génère app/prompts/personality_daniel.txt à partir d'un échantillon
représentatif de réponses de Daniel.

Usage : python -m scripts.extract_personality [--sample-size 50]

Le script :
  1. Échantillonne ~`sample_size` réponses depuis les 3 DB (couvrant FR/NL/EN, marques variées)
  2. Nettoie le texte cité pour ne garder que la prose de Daniel
  3. Demande au LLM principal d'analyser le ton, formules récurrentes, longueur, signature
  4. Sauvegarde le guide produit dans app/prompts/personality_daniel.txt

À ré-exécuter quand le corpus s'enrichit ou que le ton de Daniel évolue.
"""

import argparse
import asyncio
import random
import re
import sqlite3
from pathlib import Path

import structlog

from app.config import get_settings
from app.llm.router import complete
from app.pipeline.language import detect_language

log = structlog.get_logger()

OUTPUT_PATH = Path(__file__).parent.parent / "app" / "prompts" / "personality_daniel.txt"

ANALYSIS_PROMPT = """Tu es un expert en analyse stylistique. Voici {n} réponses écrites par Daniel Hurchon, détective privé belge, à des clients en français/néerlandais/anglais.

Produis un GUIDE DE STYLE concis et opérationnel (max 600 mots), destiné à être utilisé comme system prompt pour un LLM qui imitera Daniel. Le guide doit couvrir :

1. Ton général (formel/informel, chaleureux/distant, etc.)
2. Formules d'ouverture récurrentes (par langue)
3. Formules de clôture récurrentes (par langue)
4. Vocabulaire caractéristique et tics de langage
5. Longueur typique des réponses (courte/moyenne/longue)
6. Signature(s) utilisée(s) (par marque si différentes)
7. Sujets sensibles : comment Daniel les évite ou les dévie (ex : prix, engagements légaux)
8. Règles à respecter ABSOLUMENT (toujours / jamais)

Format de sortie : texte brut, prêt à être un system prompt. Pas de markdown.

--- ÉCHANTILLON DES RÉPONSES ---
{sample}
"""


def _clean_quoted_text(body: str) -> str:
    """Coupe le texte cité (lignes commençant par >)."""
    lines = body.splitlines()
    cleaned = []
    for line in lines:
        if line.strip().startswith(">"):
            break
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def _fetch_responses(db_path: Path, brand: str, default_lang: str, limit: int) -> list[tuple[str, str, str]]:
    """Retourne (response_clean, brand, lang) depuis sent_emails."""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "SELECT body FROM sent_emails WHERE body IS NOT NULL AND body != '' AND length(body) > 80 ORDER BY RANDOM() LIMIT ?",
            (limit,),
        )
        rows = cur.fetchall()
        results = []
        for (body,) in rows:
            clean = _clean_quoted_text(body)
            if len(clean) < 40:
                continue
            lang = detect_language(clean, default=default_lang)  # type: ignore[arg-type]
            results.append((clean, brand, lang))
        return results
    finally:
        conn.close()


def _sample_responses(sample_size: int) -> list[str]:
    settings = get_settings()
    all_responses: list[tuple[str, str, str]] = []

    for mbox in settings.mailboxes():
        if not mbox.db_path.exists():
            log.warning("extract.skip_missing", db=str(mbox.db_path))
            continue
        per_db = max(10, sample_size // 3 + 5)
        rows = _fetch_responses(mbox.db_path, mbox.brand, mbox.default_lang, per_db)
        all_responses.extend(rows)
        log.info("extract.fetched", db=str(mbox.db_path), brand=mbox.brand, n=len(rows))

    if not all_responses:
        raise RuntimeError("Aucune réponse trouvée dans les DB.")

    # Équilibrer FR/NL/EN
    by_lang: dict[str, list[tuple[str, str, str]]] = {"fr": [], "nl": [], "en": []}
    for resp, brand, lang in all_responses:
        by_lang.setdefault(lang, []).append((resp, brand, lang))

    per_lang = sample_size // 3
    selected: list[tuple[str, str, str]] = []
    for lang in ("fr", "nl", "en"):
        pool = by_lang.get(lang, [])
        selected.extend(random.sample(pool, min(per_lang, len(pool))))

    # Compléter si besoin
    remaining = sample_size - len(selected)
    if remaining > 0:
        leftover = [r for r in all_responses if r not in selected]
        selected.extend(random.sample(leftover, min(remaining, len(leftover))))

    random.shuffle(selected)

    # Formatter
    formatted = []
    for i, (resp, brand, lang) in enumerate(selected[:sample_size], 1):
        formatted.append(f"[#{i} | {brand} | {lang.upper()}]\n{resp}")

    return formatted


async def main(sample_size: int) -> None:
    samples = _sample_responses(sample_size)
    log.info("extract.sampled", n=len(samples))

    sample_text = "\n\n---\n\n".join(samples)
    prompt = ANALYSIS_PROMPT.format(n=len(samples), sample=sample_text)

    settings = get_settings()
    guide = await complete(
        model=settings.llm_model_default,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2500,
        temperature=0.2,
    )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(guide.strip() + "\n", encoding="utf-8")
    log.info("extract.written", path=str(OUTPUT_PATH), bytes=len(guide))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-size", type=int, default=50)
    args = parser.parse_args()
    asyncio.run(main(args.sample_size))
