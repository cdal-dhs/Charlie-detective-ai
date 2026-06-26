#!/usr/bin/env python3
"""Extraction du SOUL.md — guide de style Daniel par marque.

Interroge Cerveau2 pour les emails sortants (direction="out"),
analyse le style d'écriture de Daniel par marque avec le LLM,
et génère un SOUL.md enrichi pour le générateur de brouillons.
"""

from __future__ import annotations

import asyncio
import random
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx
import structlog

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import get_settings
from app.llm.router import complete

log = structlog.get_logger()

SOUL_PATH = get_settings().data_dir / "SOUL.md"

# Mapping marque interne → marque Cerveau2
MARQUE_MAP = {
    "detective_belgique": "detectivebelgique",
    "detective_belgium": "detectivebelgium",
    "dpdh_investigations": "dpdhu",
    "detectives_belgique": "detectivesbelgique",
}

MARQUE_BRAND = {
    "detective_belgique": "Detective Belgique",
    "detective_belgium": "Detective Belgium",
    "dpdh_investigations": "DPDH Investigations",
    "detectives_belgique": "Detectives Belgique",
}


@dataclass
class EmailSample:
    objet: str
    body: str
    langue: str
    date: str


async def _fetch_outgoing(
    base_url: str, api_secret: str, marque: str, limit: int = 20
) -> list[dict]:
    """Récupère les emails sortants d'une marque depuis Cerveau2."""
    url = f"{base_url.rstrip('/')}/query"
    payload = {
        "question": f"emails sortants direction out marque {marque}",
        "limit": limit,
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {api_secret}"},
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("context", [])
    except Exception as e:
        log.warning("soul.fetch_failed", marque=marque, error=str(e))
        return []


def _parse_email(item: dict) -> EmailSample | None:
    """Parse une note Cerveau2 en EmailSample."""
    content = item.get("content", "")
    if not content:
        return None

    # Les notes Cerveau2 sont structurées en markdown frontmatter
    lines = content.split("\n")
    metadata: dict[str, str] = {}
    body_lines: list[str] = []
    in_frontmatter = False
    frontmatter_done = False

    for line in lines:
        if line.strip() == "---" and not frontmatter_done:
            if not in_frontmatter:
                in_frontmatter = True
            else:
                frontmatter_done = True
            continue
        if in_frontmatter and not frontmatter_done:
            if ":" in line:
                k, v = line.split(":", 1)
                metadata[k.strip().lower()] = v.strip()
        elif frontmatter_done:
            body_lines.append(line)

    return EmailSample(
        objet=metadata.get("objet", ""),
        body="\n".join(body_lines).strip(),
        langue=metadata.get("langue", "fr"),
        date=metadata.get("date", ""),
    )


def _sample_balanced(emails: list[EmailSample], n: int = 30) -> list[EmailSample]:
    """Échantillonne en équilibrant les langues."""
    by_lang: dict[str, list[EmailSample]] = {}
    for e in emails:
        lang = e.langue if e.langue in ("fr", "nl", "en") else "fr"
        by_lang.setdefault(lang, []).append(e)

    per_lang = n // len(by_lang) if by_lang else n
    samples: list[EmailSample] = []
    for lang, items in by_lang.items():
        samples.extend(random.sample(items, min(per_lang, len(items))))

    # Compléter si manquant
    if len(samples) < n:
        rest = [e for e in emails if e not in samples]
        samples.extend(random.sample(rest, min(n - len(samples), len(rest))))

    return samples


def _sanitize_for_prompt(samples: list[EmailSample]) -> str:
    """Formate les échantillons pour le prompt LLM sans fuiter de données sensibles."""
    blocks = []
    for i, s in enumerate(samples, 1):
        # Tronquer le body pour ne garder que l'essentiel
        body = s.body[:800]
        blocks.append(f"Email #{i} [{s.langue}]\nSujet: {s.objet[:100]}\n---\n{body}\n")
    return "\n".join(blocks)


_STYLE_ANALYSIS_PROMPT = """Tu es un linguiste expert en analyse de style d'écriture professionnelle.
Tu dois analyser ces {count} emails envoyés par Daniel Hurchon, détective privé, à ses clients.

{samples}

RÈGLES ABSOLUES :
1. NE JAMAIS citer de noms de clients, d'adresses, de numéros de dossier réels.
2. NE JAMAIS reproduire un email complet.
3. Extraire UNIQUEMENT les patterns de style, les formules récurrentes, le ton.

Pour chaque section ci-dessous, donne 3-4 observations concrètes avec exemples génériques :

## 1. Ton et registre
- Formel/décontracté ? Vouvoiement/tutoiement ?
- Distance professionnelle vs proximité humaine ?
- Humour ? Sérieux absolu ?

## 2. Structure typique
- Comment commence-t-il ses emails ? (formules d'ouverture)
- Comment termine-t-il ? (formules de cloture, signature)
- Longueur moyenne ? (court/moyen/long)
- Paragraphes courts ou texte dense ?

## 3. Formules récurrentes
- Phrases qu'il réutilise souvent
- Transitions favorites
- Façon de dire "non" ou "pas possible"
- Façon de proposer un rendez-vous
- Façon de rassurer un client anxieux

## 4. Spécificités par type de demande
- Adultère/infidélité : ton particulier ?
- Surveillance/filature : ton particulier ?
- Disparition/recherche : ton particulier ?
- Devis/prix : comment aborde-t-il la question ?

## 5. Signature et marque
- Comment signe-t-il ?
- Mention de la marque ?
- Mention du téléphone ?
- CTA (call-to-action) final ?

## 6. Règles ABSOLUES de Daniel
- Ce qu'il ne dira JAMAIS
- Ce qu'il refuse de faire par email
- Ce qu'il renvoie systématiquement vers un appel

Réponds UNIQUEMENT en français, sous forme de liste structurée."""


async def _analyze_style(samples: list[EmailSample], marque: str) -> str:
    """Appelle le LLM pour analyser le style des échantillons."""
    settings = get_settings()
    prompt = _STYLE_ANALYSIS_PROMPT.format(
        count=len(samples),
        samples=_sanitize_for_prompt(samples),
    )
    messages = [
        {
            "role": "system",
            "content": f"Tu analyses le style d'écriture de Daniel Hurchon pour {MARQUE_BRAND.get(marque, marque)}.",
        },
        {"role": "user", "content": prompt},
    ]
    try:
        result = await complete(
            model=settings.llm_model_default,
            messages=messages,
            max_tokens=2000,
            temperature=0.3,
        )
        return result.strip()
    except Exception as e:
        log.warning("soul.analysis_failed", marque=marque, error=str(e))
        return f"## Analyse indisponible pour {marque}\n\nErreur LLM: {str(e)[:100]}"


def _build_soul(brand_analyses: dict[str, str]) -> str:
    """Assemble le SOUL.md final."""
    lines = [
        "# SOUL.md — Guide de style Daniel Hurchon (par marque)",
        "",
        "> Généré automatiquement depuis Cerveau2 (emails sortants Daniel).",
        "> À mettre à jour périodiquement quand le corpus s'enrichit.",
        "",
        "## Instructions générales",
        "",
        "Tu es Daniel Hurchon, détective privé belge. Tu réponds aux emails de tes clients",
        "dans le style décrit ci-dessous, adapté à la marque concernée.",
        "",
        "- Jamais de promesse légale ou contractuelle par email.",
        "- Toujours une porte de sortie vers un appel ou un rendez-vous.",
        "- Vouvoiement par défaut (sauf indication contraire dans l'historique client).",
        "- Concis, direct, humain.",
        "",
    ]

    for marque, analysis in brand_analyses.items():
        brand_name = MARQUE_BRAND.get(marque, marque)
        lines.extend(
            [
                "---",
                "",
                f"## {brand_name}",
                "",
                analysis,
                "",
            ]
        )

    lines.append("---")
    lines.append("")
    lines.append("## Lexique métier (à utiliser quand pertinent)")
    lines.append("")
    lines.append("- filature = surveillance discrète")
    lines.append("- enquête = investigation")
    lines.append("- mandant = client qui donne le mandat")
    lines.append("- constat = rapport d'observation")
    lines.append("- garde alternée / droit de visite = enquête famille")
    lines.append("")

    return "\n".join(lines)


async def main():
    settings = get_settings()
    if not settings.cerveau2_base_url or not settings.cerveau2_api_secret:
        log.error("soul.no_config")
        print("ERREUR : Cerveau2 non configuré")
        return

    brand_analyses: dict[str, str] = {}

    for marque_key, marque_val in MARQUE_MAP.items():
        log.info("soul.fetching", marque=marque_key)
        raw_items = await _fetch_outgoing(
            settings.cerveau2_base_url,
            settings.cerveau2_api_secret,
            marque_val,
            limit=20,
        )

        if not raw_items:
            log.warning("soul.no_data", marque=marque_key)
            brand_analyses[marque_key] = (
                f"Pas assez de données pour {MARQUE_BRAND.get(marque_key, marque_key)}."
            )
            continue

        emails = [e for e in (_parse_email(item) for item in raw_items) if e and e.body]
        log.info("soul.parsed", marque=marque_key, total=len(emails))

        if len(emails) < 10:
            brand_analyses[marque_key] = (
                f"Corpus insuffisant ({len(emails)} emails) pour {MARQUE_BRAND.get(marque_key, marque_key)}."
            )
            continue

        samples = _sample_balanced(emails, n=min(15, len(emails)))
        log.info("soul.sampling", marque=marque_key, samples=len(samples))

        analysis = await _analyze_style(samples, marque_key)
        brand_analyses[marque_key] = analysis
        log.info("soul.analyzed", marque=marque_key, analysis_len=len(analysis))

    # Écriture SOUL.md
    soul_content = _build_soul(brand_analyses)
    SOUL_PATH.parent.mkdir(parents=True, exist_ok=True)
    SOUL_PATH.write_text(soul_content, encoding="utf-8")

    log.info("soul.written", path=str(SOUL_PATH), size=len(soul_content))
    print(f"✅ SOUL.md généré : {SOUL_PATH}")
    print(f"   Taille : {len(soul_content)} caractères")
    print(f"   Marques analysées : {list(brand_analyses.keys())}")


if __name__ == "__main__":
    asyncio.run(main())
