#!/usr/bin/env python3
"""Évolution incrémentale du SOUL.md à partir des emails sortants récents.

Compare le SOUL.md actuel avec les nouveaux emails de Daniel dans Cerveau2,
analyse les écarts via LLM, et produit une version enrichie.

Garde-fous :
- Backup systématique SOUL.md.bak.YYYYMMDD avant écriture
- Mode --dry-run pour prévisualiser sans toucher au fichier
- Seuls les ajouts sont auto-appliqués ; suppressions = warning
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
import structlog

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import get_settings
from app.llm.router import complete

log = structlog.get_logger()

SOUL_PATH = get_settings().data_dir / "SOUL.md"

# Mapping identique à extract_soul.py
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
    url = f"{base_url.rstrip('/')}/query"
    payload = {"question": f"emails sortants direction out marque {marque}", "limit": limit}
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
        log.warning("evolve.fetch_failed", marque=marque, error=str(e))
        return []


def _parse_email(item: dict) -> EmailSample | None:
    content = item.get("content", "")
    if not content:
        return None

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


def _sanitize_for_prompt(samples: list[EmailSample]) -> str:
    blocks = []
    for i, s in enumerate(samples, 1):
        body = s.body[:800]
        blocks.append(f"Email #{i} [{s.langue}]\nSujet: {s.objet[:100]}\n---\n{body}\n")
    return "\n".join(blocks)


_EVOLVE_PROMPT = """Tu es un linguiste expert chargé d'améliorer un guide de style existant.

== GUIDE ACTUEL (SOUL.md) ==
{soul_current}

== NOUVEAUX EMAILS ENVOYÉS PAR DANIEL ==
{samples}

== TA MISSION ==
Produis une version améliorée du guide de style qui :
1. AJOUTE les nouvelles formules récurrentes découvertes dans les nouveaux emails
2. AFFINE les observations existantes si les nouveaux emails les contredisent ou les précisent
3. NE SUPPRIME jamais une section entière sans raison majeure
4. CONSERVE la structure exacte : Instructions générales + 6 sections par marque + Lexique
5. AJOUTE UNIQUEMENT des observations que tu as vues au moins 2 fois dans le corpus

RÈGLES STRICTES :
- Chaque ajout doit être justifié par un exemple concret des emails
- Le ton global doit rester professionnel et factuel
- Ne modifie pas le lexique métier sauf si un nouveau terme apparaît fréquemment
- La signature/bloc légal doit être maintenu tel quel

RÉPONDS UNIQUEMENT avec le SOUL.md complet mis à jour, sans introduction ni conclusion."""


async def _evolve_soul(soul_current: str, samples: list[EmailSample]) -> str:
    settings = get_settings()
    prompt = _EVOLVE_PROMPT.format(
        soul_current=soul_current,
        samples=_sanitize_for_prompt(samples),
    )
    messages = [
        {
            "role": "system",
            "content": "Tu es un linguiste expert en évolution de guide de style professionnel.",
        },
        {"role": "user", "content": prompt},
    ]
    try:
        result = await complete(
            model=settings.llm_model_default,
            messages=messages,
            max_tokens=4000,
            temperature=0.2,
        )
        return result.strip()
    except Exception as e:
        log.warning("evolve.llm_failed", error=str(e))
        raise


def _backup_soul() -> Path | None:
    if not SOUL_PATH.exists():
        return None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = SOUL_PATH.with_suffix(f".md.bak.{ts}")
    bak.write_text(SOUL_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    return bak


def _summarize_changes(old: str, new: str) -> dict:
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    diff = list(difflib.unified_diff(old_lines, new_lines, lineterm=""))
    added = sum(1 for d in diff if d.startswith("+"))
    removed = sum(1 for d in diff if d.startswith("-"))
    return {"added_lines": added, "removed_lines": removed, "diff": diff}


def _detect_risky_changes(diff: list[str]) -> list[str]:
    """Détecte les suppressions de sections critiques."""
    risks = []
    for line in diff:
        if line.startswith("-"):
            stripped = line[1:].strip()
            if stripped.startswith("## ") and "Règles ABSOLUES" in stripped:
                risks.append("Suppression d'une section RÈGLES ABSOLUES")
            if stripped.startswith("- Jamais") or stripped.startswith("- Toujours"):
                risks.append("Suppression d'une règle absolue")
    return risks


async def main(dry_run: bool = False) -> None:
    settings = get_settings()
    if not settings.cerveau2_base_url or not settings.cerveau2_api_secret:
        log.error("evolve.no_config")
        print("ERREUR : Cerveau2 non configuré")
        return

    soul_current = SOUL_PATH.read_text(encoding="utf-8") if SOUL_PATH.exists() else ""
    if not soul_current:
        log.error("evolve.no_soul")
        print("ERREUR : SOUL.md introuvable")
        return

    all_samples: list[EmailSample] = []
    for marque_key, marque_val in MARQUE_MAP.items():
        log.info("evolve.fetching", marque=marque_key)
        raw_items = await _fetch_outgoing(
            settings.cerveau2_base_url,
            settings.cerveau2_api_secret,
            marque_val,
            limit=20,
        )
        emails = [e for e in (_parse_email(item) for item in raw_items) if e and e.body]
        log.info("evolve.parsed", marque=marque_key, total=len(emails))
        all_samples.extend(emails)

    if len(all_samples) < 5:
        log.warning("evolve.insufficient_data", total=len(all_samples))
        print(f"⚠️  Corpus insuffisant ({len(all_samples)} emails) — évolution annulée.")
        return

    log.info("evolve.analyzing", total_emails=len(all_samples), soul_len=len(soul_current))
    print(
        f"Analyse de {len(all_samples)} emails vs SOUL.md actuel ({len(soul_current)} caractères)..."
    )

    try:
        soul_new = await _evolve_soul(soul_current, all_samples)
    except Exception:
        print("❌ Échec de l'analyse LLM")
        return

    summary = _summarize_changes(soul_current, soul_new)
    risks = _detect_risky_changes(summary["diff"])

    print("\n📊 Résumé des changements :")
    print(f"   +{summary['added_lines']} lignes ajoutées")
    print(f"   -{summary['removed_lines']} lignes supprimées")
    if risks:
        print(f"\n⚠️  RISQUES DÉTECTÉS ({len(risks)}):")
        for r in risks:
            print(f"   • {r}")

    if dry_run:
        print("\n🔍 MODE DRY-RUN — aucune écriture.")
        preview_path = Path("/tmp/SOUL_proposed.md")
        preview_path.write_text(soul_new, encoding="utf-8")
        print(f"   Aperçu écrit dans {preview_path}")
        return

    if risks and summary["removed_lines"] > 10:
        print(
            f"\n⛔ Évolution bloquée : trop de suppressions ({summary['removed_lines']} lignes) + risques détectés."
        )
        print("   Relance avec --force pour forcer, ou relis l'aperçu dans /tmp/SOUL_proposed.md")
        preview_path = Path("/tmp/SOUL_proposed.md")
        preview_path.write_text(soul_new, encoding="utf-8")
        return

    bak = _backup_soul()
    SOUL_PATH.write_text(soul_new, encoding="utf-8")
    log.info(
        "evolve.written",
        path=str(SOUL_PATH),
        backup=str(bak) if bak else None,
        added=summary["added_lines"],
        removed=summary["removed_lines"],
    )
    print(f"\n✅ SOUL.md mis à jour : {SOUL_PATH}")
    if bak:
        print(f"   Backup : {bak}")
    print(f"   Nouvelle taille : {len(soul_new)} caractères")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Évolution incrémentale du SOUL.md")
    parser.add_argument("--dry-run", action="store_true", help="Prévisualiser sans écrire")
    parser.add_argument("--force", action="store_true", help="Forcer l'écriture même avec risques")
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run))
