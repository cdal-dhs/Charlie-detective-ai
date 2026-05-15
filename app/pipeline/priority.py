"""Assigne une priorité (high / normal / low) à un email selon sa catégorie et son contenu.

Principe : une vraie demande client (formulaire, email direct, ton insistant) = HIGH.
"""

import re

HIGH_KEYWORDS = (
    "urgent", "immédiat", "immediate", "asap", "aujourd'hui", "aujourdhui",
    "rapidement", "vite", "dès que possible", "des que possible",
    "suspension", "suspendu", "compte bloqué", "compte bloque",
    "plainte", "tribunal", "huissier", "gendarmerie", "police",
    "assignation", "convocation", "audience", "déposition", "deposition",
    "subpoena", "citation", "comparution",
    "dernier délai", "dernier delai", "avant recours", "avant poursuite",
    "24h", "48h", "72h", "dans 1 jour", "dans 2 jours",
    "menace", "extorsion", "chantage", "kidnapping", "disparition",
)

FORM_KEYWORDS = (
    "formulaire", "form", "contact form", "nouveau contact",
    "demande de devis", "demande de consultation", "prise de contact",
)


def assign_priority(category: str, subject: str, body: str, sender: str) -> str:
    """Retourne 'high', 'normal' ou 'low'."""
    text = f"{subject} {body}".lower()
    sender_lower = sender.lower()

    # 1. Sécurité / urgence objective → HIGH
    if category in ("phishing", "urgent"):
        return "high"

    # 2. Demande client = toujours HIGH (business vital)
    if category == "demande_client":
        return "high"

    # 3. Rappel avec deadline proche → HIGH
    if category == "rappel":
        if any(kw in text for kw in HIGH_KEYWORDS):
            return "high"
        return "normal"

    # 4. Facture → normal
    if category == "facture":
        return "normal"

    # 5. Le reste (newsletter, spam, autre) → low
    return "low"
