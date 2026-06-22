"""Rendu du brouillon enrichi avec traductions pour aide à la lecture Daniel.

Si langue détectée = FR : retourne draft_fr brut, aucun cadre.
Si langue détectée ≠ FR : ajoute en-tête (email d'origine + traduction FR),
puis brouillon FR, puis traduction du brouillon dans la langue source.
"""

import structlog

from app.pipeline.language import Language, language_label

log = structlog.get_logger()


def render_draft_with_translations(
    incoming_body: str,
    draft_fr: str,
    source_lang: Language,
    incoming_subject: str = "",
    translation_to_fr: str = "",
    translation_from_fr: str = "",
) -> str:
    """Compose le brouillon final.

    - Si source_lang == 'fr' : proposition FR suivie du message original.
    - Sinon : structure 4 blocs (origine + trad FR + proposition FR + trad
      proposition) puis le message original complet en dessous pour Daniel.
    """

    def _original_block() -> str:
        lines = [
            "",
            "────────────────────────────────────────",
            "=== MESSAGE ORIGINAL DU CLIENT ===",
        ]
        if incoming_subject:
            lines.append(f"Sujet : {incoming_subject}")
        lines.append(incoming_body.strip())
        return "\n".join(lines)

    if source_lang == "fr":
        return (
            f"================================================\n"
            f"✉️ PROPOSITION DE RÉPONSE (en Français)\n"
            f"================================================\n"
            f"{draft_fr.strip()}"
            f"{_original_block()}"
        )

    if not translation_to_fr and not translation_from_fr:
        # Traductions ont toutes échoué (garde-fou) : on rend juste le draft FR
        # + le message original en dessous + une note sur la langue source.
        label = language_label(source_lang)
        return (
            f"=== ⚠️ Mail entrant en {label} (traductions indisponibles) ===\n\n"
            f"{draft_fr}"
            f"{_original_block()}"
        )

    label = language_label(source_lang)
    blocks: list[str] = []

    # Bloc 1 : email d'origine
    blocks.append(
        f"================================================\n"
        f"📩 EMAIL D'ORIGINE ({label})\n"
        f"================================================"
    )
    if incoming_subject:
        blocks.append(f"Sujet : {incoming_subject}")
    blocks.append(incoming_body.strip())

    # Bloc 2 : traduction FR
    if translation_to_fr:
        blocks.append(
            f"\n================================================\n"
            f"🇫🇷 TRADUCTION FR (pour lecture Daniel)\n"
            f"================================================\n"
            f"{translation_to_fr.strip()}"
        )

    # Bloc 3 : proposition FR
    blocks.append(
        f"\n================================================\n"
        f"✉️ PROPOSITION DE RÉPONSE (en Français)\n"
        f"================================================\n"
        f"{draft_fr.strip()}"
    )

    # Bloc 4 : traduction de la proposition
    if translation_from_fr:
        blocks.append(
            f"\n================================================\n"
            f"🌍 TRADUCTION DE LA PROPOSITION ({label} — pour le client)\n"
            f"================================================\n"
            f"{translation_from_fr.strip()}"
        )

    # Message original en dessous de tout (même langue étrangère)
    blocks.append(_original_block())

    return "\n".join(blocks)
