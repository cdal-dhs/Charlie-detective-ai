from dataclasses import dataclass, field
from pathlib import Path

import structlog

from app.cerveau_client import VaultNote, query_vault
from app.config import MailboxConfig, get_settings
from app.llm.router import complete
from app.pipeline.language import Language
from app.pipeline.rag import RetrievedPair, retrieve
from app.settings_store import get_llm_models

log = structlog.get_logger()

PERSONALITY_PATH = Path(__file__).parent.parent / "prompts" / "personality_daniel.txt"


@dataclass
class GenerationResult:
    draft: str
    language: Language
    rag_pairs: list[RetrievedPair]
    model_used: str
    category: str
    vault_notes: list[VaultNote] = field(default_factory=list)


def _load_personality() -> str:
    return PERSONALITY_PATH.read_text(encoding="utf-8")


def _load_soul_for_brand(brand: str) -> str:
    """Extrait du SOUL.md la section correspondant à la marque demandée."""
    soul_path = get_settings().data_dir / "SOUL.md"
    if not soul_path.exists():
        return ""
    text = soul_path.read_text(encoding="utf-8")
    header = f"## {brand}"
    idx = text.find(header)
    if idx == -1:
        return text  # fallback : tout le fichier
    start = idx
    end = text.find("\n## ", start + len(header))
    if end == -1:
        end = len(text)
    return text[start:end].strip()


def _format_rag_context(pairs: list[RetrievedPair]) -> str:
    blocks = []
    for i, p in enumerate(pairs, 1):
        blocks.append(
            f"Cas #{i} (similarité {p.similarity:.2f}, langue {p.metadata.get('lang')}):\n"
            f"--- Mail entrant ---\n{p.incoming}\n"
            f"--- Réponse de Daniel ---\n{p.response}\n"
        )
    return "\n".join(blocks)


def _format_vault_context(notes: list[VaultNote]) -> str:
    if not notes:
        return ""
    blocks = ["=== Correspondances historiques du vault ==="]
    for note in notes:
        blocks.append(f"[{note.path}]\n{note.content[:600]}")
    return "\n\n".join(blocks)


def _build_messages(
    incoming_subject: str,
    incoming_body: str,
    sender: str,
    mailbox: MailboxConfig,
    language: Language,
    pairs: list[RetrievedPair],
    vault_notes: list[VaultNote],
) -> list[dict]:
    soul_section = _load_soul_for_brand(mailbox.brand)
    system = (
        _load_personality()
        + f"\n\nMarque/boîte source : {mailbox.brand}"
        + f"\nLangue de réponse OBLIGATOIRE : {language}"
        + (f"\n\n{soul_section}" if soul_section else "")
    )
    vault_section = _format_vault_context(vault_notes)
    user = (
        f"{_format_rag_context(pairs)}\n"
        + (f"\n{vault_section}\n\n" if vault_section else "")
        + f"--- NOUVEAU MAIL À TRAITER ---\n"
        f"De : {sender}\n"
        f"Sujet : {incoming_subject}\n"
        f"Corps :\n{incoming_body}\n\n"
        f"Génère UN brouillon de réponse en {language}, signé au nom de {mailbox.brand}, "
        f"dans le style de Daniel illustré par les cas ci-dessus. "
        f"Renvoie UNIQUEMENT le corps du message, sans préambule, sans 'Sujet:', sans markdown."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


async def generate_draft(
    incoming_subject: str,
    incoming_body: str,
    sender: str,
    mailbox: MailboxConfig,
    language: Language,
    category: str = "",
) -> GenerationResult:
    settings = get_settings()
    pairs = retrieve(
        mailbox.db_path, f"{incoming_subject}\n{incoming_body}", top_k=settings.rag_top_k
    )
    log.info("generator.retrieved", rag=len(pairs))

    vault_notes, _vault_answer = await query_vault(
        question=f"{incoming_subject}\n{incoming_body[:500]}",
        base_url=settings.cerveau2_base_url,
        api_secret=settings.cerveau2_api_secret,
        limit=settings.cerveau2_limit,
        context_only=True,
    )
    log.info("generator.vault", notes=len(vault_notes))

    messages = _build_messages(
        incoming_subject, incoming_body, sender, mailbox, language, pairs, vault_notes
    )
    llm_default, _llm_fallback = get_llm_models()
    draft = await complete(
        model=llm_default,
        messages=messages,
        max_tokens=1500,
        temperature=0.4,
    )
    log.info("generator.draft", length=len(draft), preview=draft[:200])
    return GenerationResult(
        draft=draft.strip(),
        language=language,
        rag_pairs=pairs,
        model_used=llm_default,
        category=category,
        vault_notes=vault_notes,
    )
