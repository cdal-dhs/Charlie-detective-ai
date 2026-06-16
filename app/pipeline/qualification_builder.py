"""Construction déterministe d'un brouillon qualifiant prospect.

v1.22.7+ : les modèles LLM disponibles ne suivent pas de façon fiable une
instruction de liste numérotée. On construit donc le squelette du brouillon
par code (questions par cas + tarifs) et on délègue éventuellement au LLM
une passe de "polish" pour humaniser la prose.
"""

from __future__ import annotations

import re

from app.config import MailboxConfig, get_settings

# Sign-off courants en fin de mail ; la ligne suivante est souvent le nom du signataire.
_SIGN_OFFS = (
    "bien cordialement",
    "cordialement",
    "bien à vous",
    "bien à toi",
    "à bientôt",
    "à bientot",
    "sincères salutations",
    "meilleures salutations",
    "respectueusement",
    "salutations",
    "ciao",
    "kind regards",
    "best regards",
    "regards",
    "sincerely",
    "yours sincerely",
    "yours faithfully",
    "best",
    "met vriendelijke groeten",
    "vriendelijke groeten",
    "groeten",
    "met vriendelijke groet",
)

# Titres / mots qui indiquent qu'on n'a pas encore le nom propre.
_TITLE_WORDS = {
    "directeur",
    "directrice",
    "manager",
    "ceo",
    "fondateur",
    "fondatrice",
    "consultant",
    "consultante",
    "responsable",
    "chef",
    "opérations",
    "operations",
    "commercial",
    "commerciale",
    "administrateur",
    "administratrice",
    "gérant",
    "gérante",
    "dg",
    "hr",
    "marketing",
    "digitalhs",
    "detective",
    "belgique",
    "belgium",
    "contact",
    "service",
    "client",
}


def _extract_first_name(body: str) -> str | None:
    """Extraire le prénom du signataire à partir de la fin du body."""
    if not body:
        return None

    lines = [line.strip() for line in body.splitlines() if line.strip()]
    # On ne regarde que les 15 dernières lignes (signature).
    tail = lines[-15:] if len(lines) > 15 else lines

    # 1. Chercher juste après un sign-off.
    after_signoff = False
    for line in tail:
        lowered = line.lower().rstrip(",.;:-")
        if after_signoff:
            name = _clean_name_candidate(line)
            if name:
                return name
        if any(lowered.startswith(so) for so in _SIGN_OFFS):
            after_signoff = True
            continue

    # 2. Sinon, dernière ligne qui ressemble à un nom propre.
    for line in reversed(tail):
        name = _clean_name_candidate(line)
        if name:
            return name

    return None


def _clean_name_candidate(line: str) -> str | None:
    """Vérifie qu'une ligne ressemble à 'Prénom NOM' et retourne le prénom."""
    # Supprime les accolades / parenthèses typiques des signatures.
    line = line.strip("-*•▪")
    if not line:
        return None

    # Rejette si contient des chiffres, @, http, ou est trop long.
    if re.search(r"[0-9@/:\\]|http|www\.", line):
        return None

    words = line.split()
    if len(words) < 2:
        return None

    # Rejette les lignes qui ne sont que des titres.
    lowered_words = {w.lower().strip(".,;") for w in words}
    if lowered_words.issubset(_TITLE_WORDS):
        return None
    if any(w.lower().strip(".,;") in _TITLE_WORDS for w in words[:2]):
        return None

    # Le premier mot doit ressembler à un prénom : initiale majuscule, >= 2 lettres.
    first = words[0]
    if len(first) < 2 or not first[0].isupper():
        return None

    # Deuxième mot doit aussi commencer par une majuscule (nom).
    second = words[1].strip(".,;")
    if not second or not second[0].isupper():
        return None

    return first


_BASE_QUESTIONS = [
    "Vos nom et prénom complets",
    "Votre adresse complète (ou société + administrateur + TVA si professionnel)",
    "Votre GSM de contact direct",
    "Nom, prénom et adresse de départ connue de la personne concernée",
    "Photo récente de la personne concernée",
    "Véhicule de la personne concernée (marque, modèle, couleur) si connu",
]

_CASE_QUESTIONS: dict[str, list[str]] = {
    "incapacite_travail": [
        "Copie ou dates de validité du certificat d'incapacité de travail",
        "Horaire souhaité pour la mise en place du dispositif devant le domicile",
        "Indices sur un éventuel lieu de chantier ou type de travail suspecté",
    ],
    "infidelite_filature": [
        "Adresse précise de départ pour le début de la surveillance",
        "Créneau horaire souhaité (heure d'arrivée et estimation de fin)",
        "Habitudes de la cible (lieux fréquentés, horaires de bureau, restaurants, clubs)",
    ],
    "recherche_personne": [
        "Nom et prénom exacts (orthographe)",
        "Date de naissance exacte ou estimation de l'âge",
        "Région ou pays de recherche (Belgique, France, Luxembourg)",
    ],
    "securite_passé_violences": [
        "Anciens employeurs ou villes de résidence passées de la cible",
        "Adresse professionnelle éventuelle de la cible",
    ],
    "contre_espionnage_micros": [
        "Nombre exact de pièces à inspecter",
        "Présence d'un réseau Wi-Fi fonctionnel et prises électriques accessibles",
    ],
}

_CASE_LABELS = {
    "incapacite_travail": "une vérification d'incapacité de travail",
    "infidelite_filature": "une filature / surveillance",
    "recherche_personne": "une recherche de personne ou d'adresse",
    "securite_passé_violences": "une recherche sur le passé d'une personne",
    "contre_espionnage_micros": "une détection de micros ou installation de caméras",
    "non_determine": "une mission d'enquête",
}


def _rephrase_need(subject: str, body: str, case: str) -> str:
    """Reformule le besoin en 1 phrase personnalisée."""
    lowered = (subject + " " + body).lower()
    has_collaborator = "collaborateur" in lowered or "salarié" in lowered or "employé" in lowered
    has_company = "société" in lowered or "entreprise" in lowered or "company" in lowered

    if case == "infidelite_filature":
        if has_collaborator and has_company:
            return (
                "Je comprends que vous souhaitez mettre en place une surveillance afin "
                "d'obtenir des preuves concrètes sur les agissements d'un collaborateur."
            )
        if has_collaborator:
            return (
                "Je comprends que vous souhaitez mettre en place une surveillance afin "
                "d'obtenir des preuves concrètes sur les agissements d'une personne."
            )
        return (
            "Je comprends que vous souhaitez mettre en place une surveillance afin "
            "d'obtenir des éléments concrets sur une situation qui vous préoccupe."
        )
    if case == "incapacite_travail":
        return "Je comprends que vous souhaitez vérifier une situation d'incapacité de travail."
    if case == "recherche_personne":
        return "Je comprends que vous souhaitez localiser une personne ou obtenir une adresse."
    if case == "securite_passé_violences":
        return "Je comprends que vous souhaitez obtenir des éléments sur le passé d'une personne."
    if case == "contre_espionnage_micros":
        return (
            "Je comprends que vous souhaitez faire contrôler un lieu "
            "ou installer un dispositif de surveillance."
        )
    return "Je comprends que vous souhaitez nos services pour une mission d'enquête."


def build_qualification_draft(
    subject: str,
    body: str,
    sender: str,
    mailbox: MailboxConfig,
    case: str,
) -> str:
    """Génère un brouillon qualifiant structuré et déterministe."""
    settings = get_settings()
    first_name = _extract_first_name(body)
    need = _rephrase_need(subject, body, case)
    questions = _BASE_QUESTIONS + _CASE_QUESTIONS.get(case, [])

    greeting = f"Bonjour {first_name}," if first_name else "Bonjour,"
    lines = [
        greeting,
        "",
        need,
        "",
        (
            "Afin de préparer votre dossier dans les meilleures conditions, "
            "pourriez-vous me transmettre les éléments suivants :"
        ),
    ]
    for i, q in enumerate(questions, 1):
        lines.append(f"{i}. {q}.")

    lines.extend(
        [
            "",
            "Sur le plan tarifaire :",
            f"- Ouverture de dossier : {settings.dossier_opening_fee} € HTVA.",
            f"- Rapport final : {settings.report_fee} € HTVA.",
            f"- Heure de détective : {settings.hourly_rate_day} €/h HTVA "
            f"({settings.hourly_rate_night_weekend} €/h nuit/week-end).",
            "",
            "Pour toute filature ou surveillance mobile, nous déployons systématiquement "
            "deux détectives afin d'assurer l'efficacité et la discrétion.",
            "",
            "Dès réception de ces éléments, Daniel reprendra contact avec vous "
            "pour finaliser le devis et convenir d'un appel de clôture.",
            "",
            "Bien à vous,",
            "",
            "Daniel Hurchon",
            f"{mailbox.brand}",
            "GSM 0471/31.81.20",
            "contact@detectivebelgique.be",
        ]
    )
    return "\n".join(lines)
