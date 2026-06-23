import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from app.cerveau_client import VaultNote, query_vault
from app.config import MailboxConfig, get_settings
from app.llm.router import complete
from app.pipeline.case_classifier import classify_case
from app.pipeline.language import Language, language_label
from app.pipeline.objective_check import assess_objective_clarity, extract_free_message
from app.pipeline.qualification_builder import (
    build_followup_ack_draft,
    build_qualification_draft,
    suggested_subject_for_draft,
)
from app.pipeline.rag import RetrievedPair, retrieve
from app.settings_store import get_llm_models

log = structlog.get_logger()

PERSONALITY_PATH = Path(__file__).parent.parent / "prompts" / "personality_daniel.txt"
QUALIFICATION_PATH = Path(__file__).parent.parent / "prompts" / "prospect_qualification.md"


_CASE_LABELS = {
    "incapacite_travail": "Ouvrier en incapacité de travail",
    "infidelite_filature": "Surveillance / suspicion d'infidélité",
    "recherche_personne": "Recherche de personne / adresse",
    "securite_passé_violences": "Passé de violences / sécurité",
    "contre_espionnage_micros": "Détection micros-caméras / installation",
    "non_determine": "Cas non déterminé",
}


@dataclass
class GenerationResult:
    draft: str  # texte final affiché (enrichi avec traductions si ≠ FR)
    raw_draft: str  # proposition FR brute, sans enrichissement
    language: Language
    rag_pairs: list[RetrievedPair]
    model_used: str
    category: str
    vault_notes: list[VaultNote] = field(default_factory=list)
    # v1.25.1 : sujet de brouillon lisible quand le sujet original est un template
    # WP absurde (formulaire relayé par forwarder). None = garder le sujet original.
    suggested_subject: str | None = None


def _is_multilingual_draft_complete(draft: str) -> tuple[bool, str]:
    """Vérifie la présence des 4 blocs attendus pour un mail non-FR."""
    required = {
        "orig_nl": "📩 EMAIL D'ORIGINE",
        "tr_fr": "🇫🇷 TRADUCTION FR",
        "prop_fr": "✉️ PROPOSITION DE RÉPONSE (en Français)",
        "prop_tr": "🌍 TRADUCTION DE LA PROPOSITION",
    }
    for key, marker in required.items():
        if marker not in draft:
            return False, key
    # Vérifie que la traduction de la proposition n'est pas vide/tronquée.
    prop_tr_block = draft.split("🌍 TRADUCTION DE LA PROPOSITION")[1]
    prop_tr_content = re.split(r"\n[═─]+", prop_tr_block)[0].strip()
    if len(prop_tr_content) < 50:
        return False, "prop_tr_empty"
    return True, ""


async def _validate_and_fix_translation(
    final_draft: str,
    incoming_body: str,
    raw_draft: str,
    language: Language,
    incoming_subject: str,
) -> str:
    """Valide le draft multilingue ; retente si incomplet."""
    ok, reason = _is_multilingual_draft_complete(final_draft)
    if ok:
        return final_draft

    log.warning("generator.draft_incomplete", language=language, reason=reason)
    if reason == "prop_tr_empty":
        # Retraduit la proposition avec un prompt plus strict.
        from app.pipeline.translator import translate_from_fr

        fixed_tr = await translate_from_fr(raw_draft, language)
        if fixed_tr and len(fixed_tr) >= 50:
            fixed_draft = final_draft.split("🌍 TRADUCTION DE LA PROPOSITION")[0]
            fixed_draft += (
                f"\n================================================\n"
                f"🌍 TRADUCTION DE LA PROPOSITION ({language_label(language)} — pour le client)\n"
                f"================================================\n"
                f"{fixed_tr.strip()}"
            )
            ok2, reason2 = _is_multilingual_draft_complete(fixed_draft)
            if ok2:
                log.info("generator.draft_fixed", language=language)
                return fixed_draft
            log.error("generator.draft_fix_failed", language=language, reason=reason2)
    return final_draft


def _load_personality() -> str:
    return PERSONALITY_PATH.read_text(encoding="utf-8")


def _load_qualification_guide() -> str:
    """Charge la directive de qualification prospect (v1.22.7)."""
    if not QUALIFICATION_PATH.exists():
        return ""
    return QUALIFICATION_PATH.read_text(encoding="utf-8")


def _render_qualification_guide(
    case_type: str,
    case_confidence: str,
    case_reason: str,
) -> str:
    """Injecte les variables tarifaires et le cas détecté dans le guide."""
    guide = _load_qualification_guide()
    if not guide:
        return ""
    settings = get_settings()
    case_label = _CASE_LABELS.get(case_type, case_type)
    replacements = {
        "{{ dossier_opening_fee }}": str(settings.dossier_opening_fee),
        "{{ report_fee }}": str(settings.report_fee),
        "{{ hourly_rate_day }}": str(settings.hourly_rate_day),
        "{{ hourly_rate_night_weekend }}": str(settings.hourly_rate_night_weekend),
    }
    for placeholder, value in replacements.items():
        guide = guide.replace(placeholder, value)
    header = (
        f"=== CAS DE FIGURE DÉTECTÉ : {case_label} "
        f"(confiance {case_confidence}) ===\n{case_reason}\n"
    )
    return header + guide


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


def _load_daniel_fewshot(max_examples: int = 4) -> str:
    """Few-shot learning : récupère les N dernières réponses validées par Daniel.

    v1.22.0 : on injecte dans le system prompt 3-4 vraies réponses que Daniel
    a écrites (human_draft) ou approuvées (status=sent) à de vrais clients.
    Le LLM voit le VRAI Daniel, pas un style approximatif. Sélection : 30
    derniers jours, body > 200 chars, triés par date desc, les N plus récents.

    v1.22.4 : le filtre temporel est FAIT EN PYTHON (regex RFC 2822), pas en
    SQL avec `date(received_at) >= ?`. La colonne `received_at` est stockée
    en format `Sat, 13 Jun 2026 05:41:38 +0000` (RFC 2822) — la fonction
    SQLite `date()` ne sait pas la parser et retournait toujours 0 ligne
    (bug latent : le few-shot n'a JAMAIS fonctionné depuis v1.22.0).

    Args:
        max_examples: nombre max d'exemples (3-4 idéal — 1-2K tokens)

    Returns:
        Bloc formaté prêt à coller dans le system prompt, ou chaîne vide
        si aucun exemple trouvé (première install, table vide).
    """
    import re
    import sqlite3
    from datetime import UTC, datetime, timedelta

    settings = get_settings()
    db_path = settings.db_agent_state
    if not db_path.exists():
        return ""

    _RFC2822 = re.compile(r"[A-Za-z]{3},\s+(\d+)\s+(\w+)\s+(\d{4})\s+(\d{2}):(\d{2}):(\d{2})")
    _MONTHS = {
        "Jan": 1,
        "Feb": 2,
        "Mar": 3,
        "Apr": 4,
        "May": 5,
        "Jun": 6,
        "Jul": 7,
        "Aug": 8,
        "Sep": 9,
        "Oct": 10,
        "Nov": 11,
        "Dec": 12,
    }

    def _parse_received_at(text):
        if not text:
            return None
        m = _RFC2822.match(text)
        if not m:
            return None
        day, mon_s, year, hh, mm, ss = m.groups()
        try:
            return datetime(
                int(year),
                _MONTHS[mon_s],
                int(day),
                int(hh),
                int(mm),
                int(ss),
                tzinfo=UTC,
            )
        except (KeyError, ValueError):
            return None

    try:
        conn = sqlite3.connect(str(db_path))
        # Filtre SQL : body significatif + soit human_draft, soit status=sent.
        # Le tri met les corrections (human_draft) en tête, puis reçus desc.
        # On prend large (200) puis on filtre la fenêtre 30j en Python.
        candidates = conn.execute(
            """
            SELECT id, mailbox_name, subject, sender, body, human_draft, status, received_at
            FROM mail_processed
            WHERE length(coalesce(body, '')) > 200
              AND (
                length(coalesce(human_draft, '')) > 100
                OR status = 'sent'
              )
            ORDER BY
              CASE WHEN length(coalesce(human_draft, '')) > 100 THEN 0 ELSE 1 END,
              received_at DESC
            LIMIT 200
            """
        ).fetchall()
        conn.close()
    except Exception as exc:
        log.warning("generator.fewshot_load_failed", error=str(exc))
        return ""

    since = datetime.now(UTC) - timedelta(days=30)
    rows = []
    for row in candidates:
        received_dt = _parse_received_at(row[7])
        if received_dt and received_dt >= since:
            rows.append(row)
        if len(rows) >= max_examples:
            break

    if not rows:
        return ""

    blocks = ["=== EXEMPLES DE VRAIES RÉPONSES DE DANIEL (few-shot) ==="]
    blocks.append(
        "Ces exemples montrent comment Daniel écrit VRAIMENT à ses clients. "
        "Imite ce style, ce ton, cette structure, ce niveau de personnalisation."
    )
    for i, (mid, mbox, subj, sender, body, human, _status, _received_at) in enumerate(rows, 1):
        # Priorité : human_draft (correction) > rien d'autre (on n'utilise pas ai_draft
        # pour pas que le LLM s'auto-approuve)
        response = (human or "").strip()
        if not response:
            continue
        # Tronquer body pour pas bouffer le contexte (2000 chars max)
        body_short = (body or "")[:2000].strip()
        response_short = response[:1500].strip()
        kind = "CORRECTION" if human and len(human) > 100 else "ENVOI APPROUVÉ"
        blocks.append(
            f"\n--- EXEMPLE {i} (mail #{mid}, {kind}) ---\n"
            f"Boîte : {mbox}\n"
            f"Expéditeur : {sender or '?'}\n"
            f"Sujet : {subj or '?'}\n"
            f"--- Mail entrant (extrait) ---\n{body_short}\n"
            f"--- Réponse écrite par Daniel ---\n{response_short}\n"
        )
    return "\n".join(blocks)


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
    case_type: str = "non_determine",
    case_confidence: str = "low",
    case_reason: str = "",
) -> list[dict]:
    soul_section = _load_soul_for_brand(mailbox.brand)
    fewshot = _load_daniel_fewshot(max_examples=4)
    qualification = _render_qualification_guide(case_type, case_confidence, case_reason)
    # On génère TOUJOURS en français (langue de travail de Daniel).
    # Si le mail entrant est dans une autre langue, le module translator
    # ajoutera traductions + cadre multilingue autour du brouillon.
    system = (
        _load_personality()
        + f"\n\nMarque/boîte source : {mailbox.brand}"
        + "\nLangue de réponse OBLIGATOIRE : français (la langue de travail de Daniel)"
        + (f"\n\n{soul_section}" if soul_section else "")
        + (f"\n\n{fewshot}" if fewshot else "")
        + (f"\n\n{qualification}" if qualification else "")
    )
    vault_section = _format_vault_context(vault_notes)
    # La qualification est placée dans le user prompt pour être la consigne
    # la plus récente et la plus forte, juste avant la génération.
    user = (
        f"{_format_rag_context(pairs)}\n"
        + (f"\n{vault_section}\n\n" if vault_section else "")
        + "--- DIRECTIVE QUALIFICATION (à appliquer impérativement) ---\n"
        + qualification
        + "\n\n--- NOUVEAU MAIL À TRAITER ---\n"
        f"De : {sender}\n"
        f"Sujet : {incoming_subject}\n"
        f"Langue du mail entrant : {language}\n"
        f"Corps :\n{incoming_body}\n\n"
        f"Génère UN brouillon de réponse EN FRANÇAIS, signé au nom de {mailbox.brand}, "
        f"dans le style de Daniel. "
        "Tu es en mode COLLECTE D'INFORMATIONS : liste impérativement les "
        "questions manquantes sous forme numérotée AVANT tout rendez-vous. "
        "Renvoie UNIQUEMENT le corps du message, sans préambule, sans 'Sujet:', sans markdown."
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
    is_followup_response: bool = False,
) -> GenerationResult:
    settings = get_settings()
    pairs = await asyncio.to_thread(
        retrieve, mailbox.db_path, f"{incoming_subject}\n{incoming_body}", settings.rag_top_k
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

    # v1.22.7 : classification du cas de figure pour adapter la qualification
    case_type, case_confidence, case_reason = await classify_case(
        subject=incoming_subject,
        body=incoming_body,
        sender=sender,
    )
    log.info(
        "generator.case_classified",
        case=case_type,
        confidence=case_confidence,
    )

    llm_default, _llm_fallback = get_llm_models()
    draft_categories = {
        c.strip().lower() for c in settings.draft_categories.split(",") if c.strip()
    }
    suggested_subject: str | None = None
    if is_followup_response:
        # Réponse client à un échange récent : on n'envoie pas le brouillon
        # qualifiant standard, juste un accusé de réception professionnel.
        raw_draft = build_followup_ack_draft(
            incoming_subject, incoming_body, sender, mailbox, case_type
        )
        log.info(
            "generator.followup_ack_draft",
            case=case_type,
            length=len(raw_draft),
        )
    elif category.lower() in draft_categories:
        # Brouillon qualifiant déterministe : les LLM ne suivent pas de façon
        # fiable une consigne de liste numérotée, on construit donc le squelette
        # par code et on garde la main sur les questions/tarifs.
        # v1.25.6 — pour non_determine, on évalue en amont si le client a exprimé
        # un objectif final clair (heuristique + LLM gemma4 sur le message libre).
        # Sans objectif précis (#615 « faire une petite enquête »), on bascule sur
        # le brouillon flou qui demande l'objectif avant d'établir un devis.
        objective_clear = None
        if case_type == "non_determine":
            free_msg = extract_free_message(incoming_body)
            objective_clear = await assess_objective_clarity(free_msg)
            log.info(
                "generator.objective_check",
                case=case_type,
                objective_clear=objective_clear,
                free_len=len(free_msg),
            )
        raw_draft = build_qualification_draft(
            incoming_subject, incoming_body, sender, mailbox, case_type,
            objective_clear=objective_clear,
        )
        # v1.25.1 : sujet lisible si le sujet original est un template WP absurde.
        suggested_subject = suggested_subject_for_draft(
            incoming_subject, incoming_body, sender, case_type
        )
        log.info(
            "generator.qualification_draft",
            case=case_type,
            length=len(raw_draft),
            suggested_subject=bool(suggested_subject),
        )
    else:
        messages = _build_messages(
            incoming_subject,
            incoming_body,
            sender,
            mailbox,
            language,
            pairs,
            vault_notes,
            case_type=case_type,
            case_confidence=case_confidence,
            case_reason=case_reason,
        )
        draft = await complete(
            model=llm_default,
            messages=messages,
            max_tokens=2500,
            temperature=0.4,
        )
        raw_draft = draft.strip()
        log.info("generator.draft", length=len(raw_draft), preview=raw_draft[:200])

    # --- Enrichissement du rendu : message original en dessous de la proposition ---
    # Si langue ≠ FR : on ajoute aussi traductions FR + proposition traduite.
    # Si langue == FR : on garde proposition FR + message original du client.
    from app.pipeline.draft_renderer import render_draft_with_translations
    from app.pipeline.translator import translate_from_fr, translate_to_fr

    if language == "fr":
        translation_to_fr = ""
        translation_from_fr = ""
    else:
        # Lancer les 2 traductions en parallèle
        translation_to_fr_task = translate_to_fr(incoming_body, language)
        translation_from_fr_task = translate_from_fr(raw_draft, language)
        translation_to_fr, translation_from_fr = await asyncio.gather(
            translation_to_fr_task, translation_from_fr_task
        )

    final_draft = render_draft_with_translations(
        incoming_body=incoming_body,
        draft_fr=raw_draft,
        source_lang=language,
        incoming_subject=incoming_subject,
        translation_to_fr=translation_to_fr,
        translation_from_fr=translation_from_fr,
    )
    log.info(
        "generator.draft_enriched",
        language=language,
        final_length=len(final_draft),
    )

    # v1.25.14 : garde-fou qualité 100% pour les mails non-FR.
    # On s'assure que les 4 blocs sont présents ; si la traduction de la
    # proposition est tronquée/vide, on retente avec un prompt plus strict.
    if language != "fr":
        final_draft = await _validate_and_fix_translation(
            final_draft,
            incoming_body,
            raw_draft,
            language,
            incoming_subject,
        )

    return GenerationResult(
        draft=final_draft,
        raw_draft=raw_draft,
        language=language,
        rag_pairs=pairs,
        model_used=llm_default,
        category=category,
        vault_notes=vault_notes,
        suggested_subject=suggested_subject,
    )


# Alias exporté pour compatibilité tests
__all__ = ["GenerationResult", "generate_draft"]
