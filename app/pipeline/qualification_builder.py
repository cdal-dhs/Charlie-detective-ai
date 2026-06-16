"""Construction déterministe d'un brouillon qualifiant prospect.

v1.22.7+ : les modèles LLM disponibles ne suivent pas de façon fiable une
instruction de liste numérotée. On construit donc le squelette du brouillon
par code (questions par cas + tarifs) et on délègue éventuellement au LLM
une passe de "polish" pour humaniser la prose.
"""

from __future__ import annotations

import re

from app.config import MailboxConfig, get_settings

# Labels d'info client fréquents dans les formulaires web.
# Le séparateur accepte ':', '=', '-' ou '?' (ex. "Votre profil ? Particulier").
_INFO_STOP = r"(?=\n|nom|prénom|téléphone|email|gsm|adresse|profil|heure|$)"
_INFO_STOP_NO_HEURE = r"(?=\n|nom|prénom|téléphone|email|gsm|adresse|profil|$)"
_INFO_SEP = r"\s*[:\-=?]\s*"
_INFO_FIELD_SPLIT = re.compile(
    r"\s*(?:/|\n|Nom|Prénom|Téléphone|Email|GSM|Adresse|Profil|Heure)"
)
_CLIENT_INFO_LABELS = {
    "nom": re.compile(rf"nom{_INFO_SEP}(.+?){_INFO_STOP}", re.IGNORECASE | re.DOTALL),
    "prenom": re.compile(rf"pr[ée]nom{_INFO_SEP}(.+?){_INFO_STOP}", re.IGNORECASE | re.DOTALL),
    "telephone": re.compile(
        rf"(?:t[ée]l[ée]phone|gsm|portable){_INFO_SEP}([\d\s./+\-]{{6,}})", re.IGNORECASE
    ),
    "email": re.compile(rf"(?:e[-\s]?mail|courriel){_INFO_SEP}([^\s]+@[^\s]+)", re.IGNORECASE),
    "adresse": re.compile(rf"adresse{_INFO_SEP}(.+?){_INFO_STOP}", re.IGNORECASE | re.DOTALL),
    "heure_contact": re.compile(
        rf"(?:heure\s*de\s*contact|horaire|créneau){_INFO_SEP}(.+?){_INFO_STOP_NO_HEURE}",
        re.IGNORECASE | re.DOTALL,
    ),
    "profil": re.compile(
        rf"(?:profil|type|statut){_INFO_SEP}(.+?){_INFO_STOP_NO_HEURE}",
        re.IGNORECASE | re.DOTALL,
    ),
}

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
    "recuperation_dette": [
        (
            "Avez-vous une reconnaissance de dette signée ou tout document prouvant "
            "la créance (contrat, convention, échanges de courriels/messages, "
            "preuves de virements) ?"
        ),
        "Identité complète de la personne concernée (nom, prénom, date de naissance si connue)",
        "Dernière adresse connue de la personne",
        "Numéros de téléphone et adresse e-mail de la personne",
        "Employeur ou activité professionnelle de la personne",
        "Biens éventuels de la personne (véhicules, société, biens immobiliers, etc.)",
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
    "recuperation_dette": "une récupération de dette ou de créance",
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
    if case == "recuperation_dette":
        return (
            "Nous accusons bonne réception de votre demande concernant une personne de "
            "votre entourage qui vous doit une somme importante d'argent."
        )
    if case == "securite_passé_violences":
        return "Je comprends que vous souhaitez obtenir des éléments sur le passé d'une personne."
    if case == "contre_espionnage_micros":
        return (
            "Je comprends que vous souhaitez faire contrôler un lieu "
            "ou installer un dispositif de surveillance."
        )
    return "Je comprends que vous souhaitez nos services pour une mission d'enquête."


def _extract_client_info(body: str, sender: str) -> dict[str, str | None]:
    """Extrait les informations client déjà fournies dans le body ou le sender."""
    info: dict[str, str | None] = {}
    for key, pattern in _CLIENT_INFO_LABELS.items():
        match = pattern.search(body)
        if match:
            value = match.group(1).strip()
            # Nettoie les séparateurs type " / " ou "," en fin de champ.
            value = _INFO_FIELD_SPLIT.split(value)[0].strip()
            value = value.lstrip(":-").strip()
            value = value.rstrip(";,.-:")
            info[key] = value or None
        else:
            info[key] = None

    # L'email expéditeur est une source fiable si le body n'en contient pas.
    if not info.get("email") and "@" in sender:
        email_match = re.search(r"[^\s<]+@[^\s>]+", sender)
        if email_match:
            info["email"] = email_match.group(0).strip("<>")

    # Normalise l'heure de contact (ajoute "h" si c'est juste un chiffre).
    heure = info.get("heure_contact")
    if heure and re.fullmatch(r"\d{1,2}", heure.strip()):
        info["heure_contact"] = f"{heure.strip()}h"

    return info


def build_qualification_draft(
    subject: str,
    body: str,
    sender: str,
    mailbox: MailboxConfig,
    case: str,
) -> str:
    """Génère un brouillon qualifiant structuré et déterministe."""
    settings = get_settings()
    client_info = _extract_client_info(body, sender)
    first_name = client_info.get("prenom") or _extract_first_name(body)
    need = _rephrase_need(subject, body, case)
    greeting = f"Bonjour {first_name}," if first_name else "Bonjour,"

    # Pour le cas dette, on reproduit la structure de Daniel (intro, question doc, liste
    # d'infos sur la cible, closing spécifique). Les autres cas conservent le template
    # standard avec les questions de base + spécifiques.
    if case == "recuperation_dette":
        questions = _CASE_QUESTIONS.get(case, [])
        lines = _build_dette_draft(greeting, first_name, questions, mailbox, client_info)
    else:
        questions = _BASE_QUESTIONS + _CASE_QUESTIONS.get(case, [])
        lines = [
            greeting,
            "",
            need,
            "",
            (
                "Afin de préparer votre dossier dans les meilleures conditions, et pouvoir "
                "vous donner une estimation de devis fiable, pourriez-vous me transmettre "
                "les éléments suivants :"
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
                "Dès réception de ces éléments, je reprendrai contact avec vous "
                "pour finaliser le devis et convenir d'un échange téléphonique "
                "sur ce nouveau dossier.",
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


def _format_received_info(client_info: dict[str, str | None]) -> list[str]:
    """Formate les informations client déjà connues pour le brouillon."""

    def _capitalize_name(value: str | None) -> str | None:
        if not value:
            return None
        return " ".join(part.capitalize() for part in value.strip().split())

    lines: list[str] = []
    nom = _capitalize_name(client_info.get("nom"))
    prenom = _capitalize_name(client_info.get("prenom"))
    if prenom or nom:
        full = " ".join(part for part in [prenom, nom] if part)
        lines.append(f"- Vos nom et prénom : {full}")
    if client_info.get("adresse"):
        lines.append(f"- Votre adresse : {client_info['adresse']}")
    if client_info.get("telephone"):
        lines.append(f"- Votre GSM : {client_info['telephone']}")
    if client_info.get("email"):
        lines.append(f"- Votre email : {client_info['email']}")
    if client_info.get("heure_contact"):
        lines.append(f"- Heure de contact souhaitée : {client_info['heure_contact']}")
    if client_info.get("profil"):
        lines.append(f"- Profil : {client_info['profil']}")
    return lines


def _build_dette_draft(
    greeting: str,
    first_name: str | None,
    questions: list[str],
    mailbox: MailboxConfig,
    client_info: dict[str, str | None],
) -> list[str]:
    """Brouillon spécifique pour récupération de dette, sur le modèle de Daniel."""
    received = _format_received_info(client_info)

    lines = [
        greeting,
        "",
        "Nous accusons bonne réception de votre demande concernant une personne de votre "
        "entourage qui vous doit une somme importante d'argent.",
        "",
    ]

    if received:
        lines.extend([
            "Voici les éléments que nous avons bien reçus de votre part :",
            "",
            *received,
            "",
        ])

    lines.extend([
        "Afin de pouvoir évaluer la situation et vous proposer une stratégie adaptée, "
        "pourriez-vous nous communiquer :",
        "",
        "Concernant la créance :",
        f"- {questions[0]};",
        "",
        "Concernant la personne concernée :",
    ])
    for q in questions[1:]:
        lines.append(f"- {q};")

    missing_client: list[str] = []
    if not client_info.get("adresse"):
        missing_client.append(
            "- Votre adresse complète "
            "(afin de pouvoir vous recontacter par courrier si nécessaire);"
        )

    if missing_client:
        lines.extend([
            "",
            "De votre côté, pour finaliser le dossier :",
        ])
        lines.extend(missing_client)

    lines.extend([
        "",
        "Sur base de ces éléments, nous pourrons analyser votre dossier et vous proposer "
        "une stratégie d'intervention adaptée, dans le respect du cadre légal applicable aux "
        "activités de détective privé en Belgique.",
        "",
        "Nous restons à votre disposition pour toute information complémentaire.",
        "",
        "Bien à vous,",
    ])

    if first_name:
        lines.extend([
            "",
            first_name,
        ])

    lines.extend([
        "",
        "Daniel Hurchon",
        f"{mailbox.brand}",
        "GSM 0471/31.81.20",
        "contact@detectivebelgique.be",
    ])
    return lines
