"""Construction déterministe d'un brouillon qualifiant prospect.

v1.22.7+ : les modèles LLM disponibles ne suivent pas de façon fiable une
instruction de liste numérotée. On construit donc le squelette du brouillon
par code (questions par cas + tarifs) et on délègue éventuellement au LLM
une passe de "polish" pour humaniser la prose.

v1.22.14 : le builder devient "intelligent" pour TOUS les cas de figure :
- détection des informations client déjà fournies dans le mail (nom, adresse,
  GSM, profil, heure de contact) ;
- détection des informations spécifiques au cas (cible, horaires, habitudes,
  véhicule, adresse de départ, certificat, etc.) ;
- affichage d'un résumé des éléments reçus ;
- suppression des questions déjà répondues ;
- closing adapté si le dossier est déjà complet.
"""

from __future__ import annotations

import re

from app.config import MailboxConfig, get_settings

# Labels d'info client fréquents dans les formulaires web.
# _INFO_STOP : arrêt au prochain champ client ou début d'adresse (rue/avenue...).
_INFO_STOP = r"(?=\n|nom|prénom|téléphone|email|gsm|adresse|profil|heure|rue|avenue|boulevard|$)"
# _INFO_STOP_NO_HEURE : idem sans heure.
_INFO_STOP_NO_HEURE = r"(?=\n|nom|prénom|téléphone|email|gsm|adresse|profil|rue|avenue|boulevard|$)"
# _INFO_STOP_ADDRESS : pour l'adresse, on ne s'arrête pas sur les mots d'adresse.
_INFO_STOP_ADDRESS = r"(?=\n|nom|prénom|téléphone|email|gsm|adresse|profil|heure|$)"
# _INFO_SEP accepte ':', '=', '-', '?' ou un simple espace (ex. "gsm 0491502786").
# _INFO_SEP_STRICT exige un séparateur explicite pour les labels ambigus (ex. "adresse").
_INFO_SEP = r"\s*[:\-=?]?\s*"
_INFO_SEP_STRICT = r"\s*[:\-=?]\s*"
# Split utilisé pour nettoyer une valeur brute capturée. On évite de couper sur
# '/' (adresses) et sur '\n' (valeurs multilignes) ; on garde les labels connus.
_INFO_FIELD_SPLIT = re.compile(
    r"\s*(?:^|\n)\s*(?:Nom|Prénom|Téléphone|Email|GSM|Adresse|Profil|Heure)\s*[:\-=?]?\s*"
)
_CLIENT_INFO_LABELS = {
    # "mon nom est" sans séparateur explicite + label Nom complet.
    # Le label "nom" seul exige une frontière de mot (\b) pour éviter d'accrocher
    # "nom" au milieu de "economic", "phenomenon", etc.
    "nom": re.compile(
        rf"(?:mon\s+nom\s+(?:est|saisit|c'est)|nom\s+complet|\bnom\b){_INFO_SEP}(.+?){_INFO_STOP}",
        re.IGNORECASE | re.DOTALL,
    ),
    "prenom": re.compile(rf"\bpr[ée]nom\b{_INFO_SEP}(.+?){_INFO_STOP}", re.IGNORECASE | re.DOTALL),
    "telephone": re.compile(
        rf"(?:\bt[ée]l[ée]phone\b|\bgsm\b|\bportable\b){_INFO_SEP}([\d\s./+\-]{{6,}})", re.IGNORECASE
    ),
    "email": re.compile(rf"(?:\be[-\s]?mail\b|\bcourriel\b){_INFO_SEP}([^\s]+@[^\s]+)", re.IGNORECASE),
    "adresse": re.compile(rf"\badresse\b{_INFO_SEP_STRICT}(.+?){_INFO_STOP_ADDRESS}", re.IGNORECASE | re.DOTALL),
    "heure_contact": re.compile(
        # "Heure de contact" (label explicite) ou "créneau/horaire:" avec séparateur strict
        # pour éviter de capturer les horaires de la cible dans le body libre.
        rf"(?:\bheure\s*de\s*contact\b|\bcréneau\b|\bhoraire\b){_INFO_SEP_STRICT}(.+?){_INFO_STOP_NO_HEURE}",
        re.IGNORECASE | re.DOTALL,
    ),
    "profil": re.compile(
        # "Profil" / "Votre profil" / "statut:" — exige un séparateur pour éviter
        # d'accrocher des mots comme "type" dans "type de dossier".
        rf"(?:\b(?:votre\s+)?profil\b|\bstatut\b){_INFO_SEP_STRICT}(.+?){_INFO_STOP_NO_HEURE}",
        re.IGNORECASE | re.DOTALL,
    ),
}

# Extraction d'un nom complet explicite (ex. "mon nom est Bassem Sophie").
# PAS de re.IGNORECASE : on exige que chaque mot du nom commence par une majuscule,
# ce qui élimine les faux positifs du type "je suis avec un avocat...".
_NOM_COMPLET_PATTERN = re.compile(
    r"(?:[Mm]on\s+nom\s+(?:est|saisit|c'est)|[Jj]e\s+suis)\s*[:\-=?\s]*"
    r"([A-ZÀ-Ÿ][a-zà-ÿ]+(?:[ \t]+[A-ZÀ-Ÿ][a-zà-ÿ]+){1,4})"
)

# Extraction d'adresse postale belge sans label explicite.
# Tolère des compléments entre le numéro et le code postal (ex. "(Bierset), Grace-Hollogne").
_ADRESSE_BE_PATTERN = re.compile(
    r"(?:rue|avenue|boulevard|chaussée|place|square|route|chemin|impasse|allée|quai|passage|drève|voie)\s+"
    r"[^\n]*?\s+\d{1,4}[^\n]{0,40}\s+\d{4}\s+"
    r"[A-ZÀ-Ÿ][a-zà-ÿ]+(?:[ \t'\-][a-zà-ÿA-ZÀ-Ÿ]+){0,4}",
    re.IGNORECASE,
)

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
    # Signatures anglaises / génériques à ignorer (ex. "The Google Ads Team").
    "the",
    "team",
    "best",
    "kind",
    "yours",
    "sincerely",
    "regards",
    "faithfully",
    "google",
    "ads",
}


def _strip_quoted_thread(body: str) -> str:
    """Supprime le thread cité (réponses en dessous de "... a écrit :" ou "> ...")."""
    if not body:
        return body
    # 1. Coupe au premier "Le ... a écrit :" même si Gmail casse l'adresse sur 2 lignes.
    cutoff = re.search(
        r"(?:^Le\s+.*(?:\n.*)?\s+a\s+écrit\s*:"
        r"|^On\s+.*\s+wrote:"
        r"|^\s*>\s*De\s*:"
        r"|^\s*De\s*:\s*.*\n\s*Date\s*:)",
        body,
        re.IGNORECASE | re.MULTILINE,
    )
    if cutoff:
        body = body[: cutoff.start()]
    # 2. Coupe aussi au premier bloc de lignes citées (> ...).
    quoted_start = re.search(r"\n\s*>\s+\S", body)
    if quoted_start:
        body = body[: quoted_start.start()]
    return body.strip()


def _extract_first_name(body: str) -> str | None:
    """Extraire le prénom du signataire à partir de la fin du body."""
    if not body:
        return None

    # On ne regarde que la partie non citée du mail.
    body = _strip_quoted_thread(body)
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


def _clean_value(value: str) -> str:
    """Nettoie une valeur extraite (séparateurs, retours à la ligne internes)."""
    value = _INFO_FIELD_SPLIT.split(value)[0].strip()
    value = value.lstrip(":-").strip()
    value = value.rstrip(";,.-:")
    # Collapse les retours à la ligne internes en espace.
    value = re.sub(r"\s+", " ", value)
    return value


def _clean_snippet(value: str) -> str:
    """Nettoie un extrait de phrase sans couper au premier retour à la ligne."""
    value = value.replace("\n", " ").strip()
    value = value.lstrip(":-").strip()
    value = value.rstrip(";,.-:")
    # Supprime un éventuel label "Adresse :" resté accroché.
    value = re.sub(r"^Adresse\s*[\-:]\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value)
    return value


def _is_internal_email(email: str) -> bool:
    """True si l'email appartient au cabinet Detective ou est un no-reply/formulaire."""
    lowered = email.lower()
    if lowered.startswith("no-reply") or lowered.startswith("noreply"):
        return True
    internal_domains = {
        "detectivebelgique.be",
        "detectivebelgium.com",
        "dpdhuinvestigations.be",
        "digitalhs.biz",
    }
    domain = lowered.split("@")[-1] if "@" in lowered else ""
    if domain in internal_domains:
        return True
    try:
        settings = get_settings()
        for mb in settings.mailboxes():
            if mb.user and mb.user.lower() == lowered:
                return True
    except Exception:
        pass
    return False


def _extract_client_info(body: str, sender: str) -> dict[str, str | None]:
    """Extrait les informations client déjà fournies dans le body ou le sender."""
    info: dict[str, str | None] = {}
    clean_body = _strip_quoted_thread(body)

    for key, pattern in _CLIENT_INFO_LABELS.items():
        match = pattern.search(clean_body)
        if match:
            info[key] = _clean_value(match.group(1)) or None
        else:
            info[key] = None

    # Certains formulaires web envoient leurs champs dans le thread cité
    # (telephone, heure_contact, profil). On les cherche aussi dans le body
    # entier si absents du body propre, mais avec priorité au body propre.
    for key in ("telephone", "heure_contact", "profil"):
        if not info.get(key):
            match = _CLIENT_INFO_LABELS[key].search(body)
            if match:
                info[key] = _clean_value(match.group(1)) or None

    # Nom complet explicite (ex. "mon nom est Bassem Sophie").
    match = _NOM_COMPLET_PATTERN.search(clean_body)
    if match:
        info["nom_complet"] = _clean_value(match.group(1))

    # Si on a "Nom:" et "Prénom:" séparés (formulaire web), on les combine.
    if not info.get("nom_complet") and (info.get("nom") or info.get("prenom")):
        nom = info.get("nom") or ""
        prenom = info.get("prenom") or ""
        nom = nom.strip()
        prenom = prenom.strip()
        if nom and prenom and prenom.lower() not in nom.lower():
            info["nom_complet"] = f"{prenom} {nom}"
        elif nom and not prenom:
            info["nom_complet"] = nom
        elif prenom and not nom:
            info["nom_complet"] = prenom

    # Fallback prénom depuis une salutation du thread précédent ("Bonjour Sophie,").
    if not info.get("prenom"):
        salutation = re.search(
            r"(?:^|\n)\s*>?\s*Bonjour\s+([A-ZÀ-Ÿ][a-zà-ÿ]+)\s*[,.]",
            body,
            re.IGNORECASE,
        )
        if salutation:
            info["prenom"] = salutation.group(1)

    # Fallback prénom depuis une signature simple ("Bien à vous\nAnthony").
    if not info.get("prenom"):
        simple_sig = re.search(
            r"(?:Bien\s+à\s+vous|Cordialement|Bien\s+cordialement),?\s*\n\s*"
            r"([A-ZÀ-Ÿ][a-zà-ÿ]+)\s*$",
            clean_body,
            re.IGNORECASE | re.MULTILINE,
        )
        if simple_sig:
            info["prenom"] = simple_sig.group(1)

    # Fallback prénom depuis le nom complet (dernier mot = prénom le plus souvent).
    if not info.get("prenom") and info.get("nom_complet"):
        parts = info["nom_complet"].split()
        if len(parts) >= 2:
            # En l'absence de contexte, on prend le dernier mot comme prénom
            # (hypothèse "Nom Prénom" inverse fréquent dans les formulaires).
            info["prenom"] = parts[-1]

    # Fallback adresse si aucune adresse labellisée n'a été trouvée.
    if not info.get("adresse"):
        addr_match = _ADRESSE_BE_PATTERN.search(clean_body)
        if addr_match:
            info["adresse"] = _clean_value(addr_match.group(0))

    # L'email expéditeur est une source fiable si le body n'en contient pas,
    # mais on ignore les emails internes (boîtes Detective, no-reply, formulaires).
    if not info.get("email") and "@" in sender:
        email_match = re.search(r"[^\s<]+@[^\s>]+", sender)
        if email_match:
            candidate = email_match.group(0).strip("<>")
            if not _is_internal_email(candidate):
                info["email"] = candidate

    # Normalise l'heure de contact (ajoute "h" si c'est juste un chiffre).
    heure = info.get("heure_contact")
    if heure and re.fullmatch(r"\d{1,2}", heure.strip()):
        info["heure_contact"] = f"{heure.strip()}h"

    return info


# --- Extraction spécifique par cas de figure --------------------------------

# Nom/prénom de la cible : "Segers, Grégory", "Gregory Segers", etc.
# Utilise des espaces horizontaux uniquement pour ne pas traverser les sauts de ligne.
_NOM_CIBLE_PATTERN = re.compile(
    r"([A-ZÀ-Ÿ][a-zà-ÿ]+(?:[-' \t][A-ZÀ-Ÿ][a-zà-ÿ]+)?)[ \t]*,[ \t]*([A-ZÀ-Ÿ][a-zà-ÿ]+(?:[-' \t][A-ZÀ-Ÿ][a-zà-ÿ]+)?)",
)

# Véhicule : marque/modèle/couleur/plaque.
# S'arrête aux transitions logiques (travaille, et, pour, car, etc.) pour ne pas
# avaler les horaires/lieu de travail dans les textes mal ponctués.
_VEHICULE_PATTERN = re.compile(
    r"(?:son\s+véhicule\s+(?:est|était|c'est)|possédant|voiture|véhicule|auto|bmw|mercedes|audi|vw|volkswagen|renault|peugeot|toyota|ford|hyundai|citroën|volvo|porsche)\s+"
    r"(.{5,80}?)(?=\n|travaille|et\s+cette|pour\s+(?:le\s+)?prouver|car\s+|j'ai|je\s+voudrais|merci|cordialement|sais\s+pas|\.{2,}|\.|$)",
    re.IGNORECASE | re.DOTALL,
)

# Horaires / créneaux.
# Capture une indication temporelle optionnelle ("semaine du 18 juin", "le matin")
# suivie de travaille/horaire/créneau + l'heure, avec max 4 mots entre les deux.
_HORAIRE_PATTERN = re.compile(
    r"(?:du\s+\d{1,2}\s+\w+|semaine\s+du\s+\d{1,2}\s+\w+|le\s+\w+|ce\s+\w+|cette\s+\w+)?\s*"
    r"(?:travaille|horaire|créneau|travail)\s+"
    r"(?:\S+\s+){0,4}\d{1,2}\s*[hH]\s*(?:à|[-/])\s*\d{1,2}\s*[hH]",
    re.IGNORECASE,
)

# Habitudes de la cible : priorité aux indices forts (maîtresse, dort).
_HABITUDES_SPECIFIQUES = re.compile(
    r"(?:chez\s+(?:sa\s+)?maîtresse|dort\s+(?:là|la|chez)|retourne\s+.*?(?:maîtresse|domicile))",
    re.IGNORECASE,
)
_HABITUDES_GENERALES = re.compile(
    r"(?:dimanche|samedi|après\s+le\s+travail|lieux\s+fréquentés|restaurants|clubs|bars)",
    re.IGNORECASE,
)

# Photo : on considère fournie seulement si le client l'annonce clairement
# (pièce jointe, "je joins", "ci-joint"), pas s'il "demande" une photo.
_PHOTO_PATTERN = re.compile(
    r"(?:je\s+(?:joins|envoie|transmets)|ci-joint|pièce\s*jointe|fichier\s*attaché|"
    r"photo\s+(?:jointe|attachée|en\s+pièce\s+jointe))",
    re.IGNORECASE,
)

# Adresse de départ connue (adresse après nom d'entreprise ou "adresse").
# S'arrête aux transitions courantes (possédant, avec, et, car, etc.) pour éviter
# d'empiéter sur véhicule/horaires dans les textes mal ponctués.
_ADRESSE_DEPART_PATTERN = re.compile(
    r"(?:"
    r"adresse\s+(?:de\s+départ|connue|de|du\s+domicile)|"
    r"domicile\s+conjugal|"
    r"coordonnées\s+(?:de\s+)?(?:madame|mme|la\s+cible|l'épouse|la\s+femme)|"
    r"(?:elle|la\s+cible)\s+(?:habite|réside|demeure|vit)\s+(?:à|a|au|en)|"
    r"travaille\s+(?:à|a)"
    r")\s*[:\-=?]?\s*"
    r"(.{5,80}?)(?=\n|possédant|avec\s+(?:une|la|le)|travaille\s+(?:une\s+fois|le\s+matin|l'après)|et\s+|car\s+|j'ai|je\s+voudrais|merci|cordialement|sais\s+pas|\.{2,}|\.|$)",
    re.IGNORECASE | re.DOTALL,
)

# Recherche personne : date de naissance / âge.
_AGE_DOB_PATTERN = re.compile(
    r"(?:né\s+(?:le|en)|date\s+de\s+naissance|âge\s+(?:d'environ|de|enviro)|a\s+environ)\s*[:\-=?]?\s*"
    r"(.{3,60}?)(?=\n|merci|cordialement|\.|$)",
    re.IGNORECASE | re.DOTALL,
)

# Région / pays de recherche.
_REGION_PATTERN = re.compile(
    r"(?:région|pays|zone|recherche|localiser|en\s+Belgique|en\s+France|au\s+Luxembourg|en\s+Italie|en\s+Espagne)\s*[:\-=?]?\s*"
    r"(.{3,80}?)(?=\n|merci|cordialement|\.|$)",
    re.IGNORECASE | re.DOTALL,
)

# Incapacité : certificat / arrêt / dates.
_CERTIFICAT_INCAPACITE_PATTERN = re.compile(
    r"(?:certificat|arrêt\s+maladie|incapacité|incapacite|médecin|dates?\s+de\s+validité)\s*"
    r"(.{3,100}?)(?=\n|merci|cordialement|\.|$)",
    re.IGNORECASE | re.DOTALL,
)

# Incapacité : employeur / lieu de travail.
_EMPLOYEUR_PATTERN = re.compile(
    r"(?:"
    r"(?:employeur|entreprise|société|boîte|magasin|usine|grossiste|brico)\s*[:\-=?]?\s*|"
    r"travaille\s+(?:à|a|pour|chez)\s+"
    r")"
    r"(.{3,120}?)(?=\n\s*\n|du\s+\d|semaine|jour|merci|cordialement|sais\s+pas|\.|$)",
    re.IGNORECASE | re.DOTALL,
)

# Incapacité : lieu suspect (chez la maîtresse, domicile conjugal, adresse connue).
_LIEU_SUSPECT_PATTERN = re.compile(
    r"(?:"
    r"chez\s+(?:sa\s+)?maîtresse|maîtresse|"
    r"domicile\s+conjugal|"
    r"adresse\s+(?:connue|de\s+la\s+personne|du\s+domicile)|"
    r"lieu\s+suspect|lieu\s+de\s+rendez[\-]vous"
    r")\s*[:\-=?]?\s*"
    r"(.{5,120}?)(?=\n|merci|cordialement|sais\s+pas|\.|$)",
    re.IGNORECASE | re.DOTALL,
)

# Sécurité / passé de violences : anciens employeurs, villes passées.
_PASSE_VIOLENCES_PATTERN = re.compile(
    r"(?:ancien\s+employeur|ville\s+de\s+résidence|adresse\s+professionnelle|passé|antécédent|condamnation)\s*"
    r"(.{5,120}?)(?=\n|merci|cordialement|\.|$)",
    re.IGNORECASE | re.DOTALL,
)

# Contre-espionnage : pièces, Wi-Fi.
_MICROS_PATTERN = re.compile(
    r"(?:pièce|pièces|chambre|salon|bureau|Wi-Fi|wifi|prise|électrique|installation)\s*"
    r"(.{3,100}?)(?=\n|merci|cordialement|\.|$)",
    re.IGNORECASE | re.DOTALL,
)


def _body_without_signature(body: str) -> str:
    """Retourne le body sans la zone de signature (après un sign-off courant)."""
    if not body:
        return body
    lines = body.splitlines()
    for i, line in enumerate(lines):
        lowered = line.lower()
        for so in _SIGN_OFFS:
            idx = lowered.find(so)
            if idx >= 0:
                # Garde la partie de la ligne avant le sign-off.
                before = line[:idx].rstrip()
                lines[i] = before
                return "\n".join(lines[: i + 1]).strip()
    return body


def _clean_cible_name(value: str) -> str | None:
    """Normalise un nom de cible trouvé (ex. 'Segers,Grégory' -> 'Grégory Segers')."""
    value = value.strip(";,.:-")
    if not value or len(value) < 3:
        return None
    # Pattern Nom, Prénom.
    match = _NOM_CIBLE_PATTERN.match(value)
    if match:
        nom = match.group(1).strip()
        prenom = match.group(2).strip()
        return f"{prenom} {nom}"
    # Sinon on garde tel quel s'il y a 2 mots majuscules.
    words = value.split()
    if len(words) >= 2 and all(w[0].isupper() for w in words[:2] if w):
        return value
    return None


def _extract_case_info(body: str, case: str) -> dict[str, str | None]:
    """Extrait les informations spécifiques au cas de figure."""
    info: dict[str, str | None] = {}
    clean_body = _body_without_signature(_strip_quoted_thread(body))
    lowered = clean_body.lower()

    # --- Infidelité / filature / surveillance ---
    if case == "infidelite_filature":
        # Mots communs de lieux qu'on ne veut pas traiter comme un nom de cible.
        _LIEU_WORDS = {
            "cité", "verte", "selembao", "kinshasa", "liège", "bruxelles",
            "charleroi", "waterloo", "belgique", "france", "luxembourg",
            "rue", "avenue", "boulevard", "place", "square",
        }

        # Nom de la cible (recherche plus large que le label "Nom:").
        # 1. Pattern "Nom, Prénom" explicite, filtré pour éviter les noms de lieux.
        nom_match = _NOM_CIBLE_PATTERN.search(clean_body)
        if nom_match:
            candidate = f"{nom_match.group(1).strip()} {nom_match.group(2).strip()}"
            lowered_cand = {w.lower() for w in candidate.split()}
            if not lowered_cand & _LIEU_WORDS and len(candidate.split()) <= 5:
                info["nom_cible"] = nom_match.group(1).strip().strip(";,.:")
                info["prenom_cible"] = nom_match.group(2).strip().strip(";,.:")

        # 2. Relation explicite : "mon mari X Y", "ma femme X Y", "mon épouse X Y",
        #    "madame X Y", "ma conjointe X Y".
        if not info.get("prenom_cible"):
            relation_match = re.search(
                r"(?:"
                r"mon\s+(?:mari|époux|épouse|femme|conjoint|conjointe)|"
                r"ma\s+(?:femme|épouse|conjointe)|"
                r"(?:madame|mme)"
                r")\s*"
                r"([A-ZÀ-Ÿ][a-zà-ÿ]+(?:\s+[A-ZÀ-Ÿ][a-zà-ÿ]+){0,3})?",
                clean_body,
                re.IGNORECASE,
            )
            if relation_match:
                name_part = (relation_match.group(1) or "").strip()
                if name_part:
                    full = _clean_cible_name(name_part)
                    if full:
                        parts = full.split()
                        if len(parts) >= 2:
                            info["prenom_cible"] = parts[0]
                            info["nom_cible"] = " ".join(parts[1:])
                        else:
                            info["prenom_cible"] = full
                else:
                    # On sait au moins qu'il s'agit de la femme/épouse/madame.
                    info["relation_cible"] = "épouse / conjointe"

        # Véhicule.
        veh_match = _VEHICULE_PATTERN.search(clean_body)
        if veh_match:
            info["vehicule_cible"] = _clean_snippet(veh_match.group(1))
        # Mention explicite "pas de voiture / pas de véhicule".
        if re.search(r"(?:pas\s+de\s+(?:voiture|véhicule)|n'a\s+pas\s+de\s+(?:voiture|véhicule))", clean_body, re.IGNORECASE):
            info["vehicule_cible"] = info.get("vehicule_cible") or "aucun (transport en commun / taxi)"

        # Adresse de départ / lieu de travail / domicile de la cible.
        # 1. Labels explicites : "Coordonnées de madame", "Adresse de la cible", "Elle habite".
        addr_depart = _ADRESSE_DEPART_PATTERN.search(clean_body)
        if addr_depart:
            info["adresse_depart_cible"] = _clean_snippet(addr_depart.group(1))
        else:
            # 2. Cherche une adresse après "madame / épouse / femme / elle habite".
            relation_addr = re.search(
                r"(?:"
                r"coordonnées\s+(?:de\s+)?(?:madame|mme|la\s+cible|l'épouse|la\s+femme)|"
                r"(?:elle|la\s+cible)\s+(?:habite|réside|demeure|vit)\s+(?:à|a|au|en)|"
                r"domicile\s+(?:de|du|d'elle|conjugal)"
                r")\s*[:\-=?]?\s*"
                r"(.{5,200}?)(?=\n|j'ai|je\s+voudrais|merci|cordialement|sais\s+pas|\.{2,}|\.|$)",
                clean_body,
                re.IGNORECASE | re.DOTALL,
            )
            if relation_addr:
                info["adresse_depart_cible"] = _clean_snippet(relation_addr.group(1))
            else:
                # 3. Fallback : deuxième adresse postale trouvée (la première étant souvent celle du client).
                addresses = _ADRESSE_BE_PATTERN.findall(clean_body)
                if len(addresses) >= 2:
                    info["adresse_depart_cible"] = _clean_value(addresses[1])
                elif len(addresses) == 1 and not info.get("adresse_client_fallback"):
                    # Si le client n'a pas d'adresse, la seule adresse est probablement celle de la cible.
                    pass

        # Horaires / créneau.
        horaires = _HORAIRE_PATTERN.findall(clean_body)
        if horaires:
            info["horaires_cible"] = " ; ".join(_clean_snippet(h) for h in horaires)

        # Habitudes : priorité aux indices forts.
        habitudes_match = _HABITUDES_SPECIFIQUES.search(clean_body) or _HABITUDES_GENERALES.search(clean_body)
        if habitudes_match:
            # Extrait un court extrait autour du keyword, en s'alignant sur les mots.
            start = max(0, habitudes_match.start() - 25)
            while start > 0 and not clean_body[start - 1].isspace():
                start -= 1
            while start < habitudes_match.start() and clean_body[start].isspace():
                start += 1
            # S'arrête à la fin de la phrase suivant le keyword (max 200 car).
            end = min(len(clean_body), habitudes_match.end() + 200)
            # Cherche un point qui termine une phrase (lettre/chiffre suivi de '.').
            dot_pos = -1
            for i in range(habitudes_match.end(), end):
                if clean_body[i] == "." and i > 0 and clean_body[i - 1].isalnum():
                    dot_pos = i
                    break
            if dot_pos != -1:
                end = dot_pos + 1  # inclure le point final
            else:
                newline_pos = clean_body.find("\n", habitudes_match.end())
                if newline_pos != -1 and newline_pos < end:
                    end = newline_pos
            snippet = clean_body[start:end]
            info["habitudes_cible"] = _clean_snippet(snippet)

        # Photo fournie (pas juste "j'ai besoin d'une photo").
        if _PHOTO_PATTERN.search(clean_body):
            info["photo_cible"] = "fournie / mentionnée"

    # --- Recherche de personne / adresse ---
    elif case == "recherche_personne":
        nom_match = _NOM_CIBLE_PATTERN.search(clean_body)
        if nom_match:
            info["nom_recherche"] = nom_match.group(1).strip().strip(";,.:")
            info["prenom_recherche"] = nom_match.group(2).strip().strip(";,.:")
        else:
            relation_match = re.search(
                r"(?:recherche|rechercher|cherche|chercher|retrouver|localiser|disparu|personne)\s+"
                r"(?:"
                r"mon\s+(?:frère|soeur|mari|femme|père|mère|enfant|fils|fille|conjoint|cousin|cousine)|"
                r"ma\s+(?:soeur|fille|mère|femme|conjointe)|"
                r"nommée?"
                r")?\s*"
                r"([A-ZÀ-Ÿ][a-zà-ÿ]+(?:\s+[A-ZÀ-Ÿ][a-zà-ÿ]+)+)",
                clean_body,
                re.IGNORECASE,
            )
            if relation_match:
                full = _clean_cible_name(relation_match.group(1))
                if full:
                    parts = full.split()
                    if len(parts) >= 2:
                        info["prenom_recherche"] = parts[0]
                        info["nom_recherche"] = " ".join(parts[1:])

        dob_match = _AGE_DOB_PATTERN.search(clean_body)
        if dob_match:
            info["date_naissance"] = _clean_value(dob_match.group(1))

        region_match = _REGION_PATTERN.search(clean_body)
        if region_match:
            info["region_recherche"] = _clean_value(region_match.group(1))

    # --- Incapacité de travail ---
    elif case == "incapacite_travail":
        cert_match = _CERTIFICAT_INCAPACITE_PATTERN.search(clean_body)
        if cert_match:
            info["certificat_incapacite"] = _clean_value(cert_match.group(1))

        horaires = _HORAIRE_PATTERN.findall(clean_body)
        if horaires:
            info["horaire_surveillance"] = " ; ".join(_clean_value(h) for h in horaires)

        # Personne concernée : nom + adresse connue (2ème adresse postale = lieu de travail).
        nom_match = _NOM_CIBLE_PATTERN.search(clean_body)
        if nom_match:
            info["nom_cible"] = nom_match.group(1).strip().strip(";,.:")
            info["prenom_cible"] = nom_match.group(2).strip().strip(";,.:")
        addresses = _ADRESSE_BE_PATTERN.findall(clean_body)
        if len(addresses) >= 2:
            info["adresse_cible"] = _clean_value(addresses[1])

        # Lieu de travail / employeur suspecté : label explicite, puis 2ème adresse postale.
        employeur_match = _EMPLOYEUR_PATTERN.search(clean_body)
        if employeur_match:
            info["lieu_suspect"] = _clean_value(employeur_match.group(1))
        if len(addresses) >= 2 and not info.get("lieu_suspect"):
            info["lieu_suspect"] = _clean_value(addresses[1])

        # Lieu suspect alternatif (maîtresse, domicile conjugal).
        lieu_match = _LIEU_SUSPECT_PATTERN.search(clean_body)
        if lieu_match:
            info["lieu_suspect"] = info.get("lieu_suspect") or _clean_value(lieu_match.group(1))

    # --- Passé de violences / sécurité ---
    elif case == "securite_passé_violences":
        passe_match = _PASSE_VIOLENCES_PATTERN.search(clean_body)
        if passe_match:
            info["passe_violences"] = _clean_value(passe_match.group(1))

    # --- Contre-espionnage / micros ---
    elif case == "contre_espionnage_micros":
        micro_match = _MICROS_PATTERN.search(clean_body)
        if micro_match:
            info["micros_contexte"] = _clean_value(micro_match.group(1))

    return info


# --- Questions par cas avec mapping sur les clés d'info ----------------------

# Spécification : (texte_question, [clés à vérifier, au moins une non-vide = répondue])
_CASE_QUESTION_SPECS: dict[str, list[tuple[str, list[str]]]] = {
    "incapacite_travail": [
        ("Vos nom et prénom complets", ["nom", "prenom", "nom_complet"]),
        ("Votre adresse complète (ou société + administrateur + TVA si professionnel)", ["adresse"]),
        ("Votre GSM de contact direct", ["telephone"]),
        ("Nom, prénom et adresse connue de la personne concernée", ["nom_cible", "prenom_cible", "adresse_cible"]),
        ("Photo récente de la personne concernée", ["photo_cible"]),
        ("Véhicule de la personne concernée (marque, modèle, couleur) si connu", ["vehicule_cible"]),
        ("Copie ou dates de validité du certificat d'incapacité de travail", ["certificat_incapacite"]),
        ("Horaire souhaité pour la mise en place du dispositif devant le domicile", ["horaire_surveillance"]),
        ("Indices sur un éventuel lieu de chantier ou type de travail suspecté", ["lieu_suspect"]),
    ],
    "infidelite_filature": [
        ("Vos nom et prénom complets", ["nom", "prenom", "nom_complet"]),
        ("Votre adresse complète (ou société + administrateur + TVA si professionnel)", ["adresse"]),
        ("Votre GSM de contact direct", ["telephone"]),
        ("Nom, prénom et adresse de départ connue de la personne concernée", ["nom_cible", "prenom_cible", "adresse_depart_cible"]),
        ("Photo récente de la personne concernée", ["photo_cible"]),
        ("Véhicule de la personne concernée (marque, modèle, couleur) si connu", ["vehicule_cible"]),
        ("Adresse précise de départ pour le début de la surveillance", ["adresse_depart_cible"]),
        ("Créneau horaire souhaité (heure d'arrivée et estimation de fin)", ["horaires_cible"]),
        ("Habitudes de la cible (lieux fréquentés, horaires de bureau, restaurants, clubs)", ["habitudes_cible"]),
    ],
    "recherche_personne": [
        ("Vos nom et prénom complets", ["nom", "prenom", "nom_complet"]),
        ("Votre adresse complète (ou société + administrateur + TVA si professionnel)", ["adresse"]),
        ("Votre GSM de contact direct", ["telephone"]),
        ("Nom et prénom exacts (orthographe) de la personne recherchée", ["nom_recherche", "prenom_recherche", "nom_cible", "prenom_cible"]),
        ("Date de naissance exacte ou estimation de l'âge", ["date_naissance"]),
        ("Région ou pays de recherche (Belgique, France, Luxembourg)", ["region_recherche"]),
    ],
    "recuperation_dette": [
        # Gardé volontairement vide : le builder dédié gère sa propre logique.
    ],
    "securite_passé_violences": [
        ("Vos nom et prénom complets", ["nom", "prenom", "nom_complet"]),
        ("Votre adresse complète (ou société + administrateur + TVA si professionnel)", ["adresse"]),
        ("Votre GSM de contact direct", ["telephone"]),
        ("Nom, prénom et adresse connue de la cible", ["nom_cible", "prenom_cible", "adresse_cible"]),
        ("Anciens employeurs ou villes de résidence passées de la cible", ["passe_violences"]),
        ("Adresse professionnelle éventuelle de la cible", ["passe_violences"]),
    ],
    "contre_espionnage_micros": [
        ("Vos nom et prénom complets", ["nom", "prenom", "nom_complet"]),
        ("Votre adresse complète (ou société + administrateur + TVA si professionnel)", ["adresse"]),
        ("Votre GSM de contact direct", ["telephone"]),
        ("Nombre exact de pièces à inspecter", ["micros_contexte"]),
        ("Présence d'un réseau Wi-Fi fonctionnel et prises électriques accessibles", ["micros_contexte"]),
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


def _capitalize_name(value: str | None) -> str | None:
    if not value:
        return None
    return " ".join(part.capitalize() for part in value.strip().split())


def _format_received_info(
    client_info: dict[str, str | None],
    case_info: dict[str, str | None],
    case: str,
) -> list[str]:
    """Formate les informations déjà connues pour le brouillon (tous les cas)."""
    lines: list[str] = []

    # --- Infos client ---
    prenom = _capitalize_name(client_info.get("prenom"))
    nom = _capitalize_name(client_info.get("nom"))
    nom_complet = _capitalize_name(client_info.get("nom_complet"))

    full = nom_complet or " ".join(p for p in [prenom, nom] if p)
    if full:
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

    # --- Infos spécifiques au cas ---
    if case == "infidelite_filature":
        cible_parts = [p for p in [
            _capitalize_name(case_info.get("prenom_cible")),
            _capitalize_name(case_info.get("nom_cible")),
        ] if p]
        if cible_parts:
            lines.append(f"- Personne concernée : {' '.join(cible_parts)}")
        if case_info.get("adresse_depart_cible"):
            lines.append(f"- Adresse de départ / lieu de travail : {case_info['adresse_depart_cible']}")
        if case_info.get("horaires_cible"):
            lines.append(f"- Horaires / créneaux : {case_info['horaires_cible']}")
        if case_info.get("habitudes_cible"):
            lines.append(f"- Habitudes de la cible : {case_info['habitudes_cible']}")
        if case_info.get("vehicule_cible"):
            lines.append(f"- Véhicule : {case_info['vehicule_cible']}")

    elif case == "recherche_personne":
        cible_parts = [p for p in [
            _capitalize_name(case_info.get("prenom_recherche")),
            _capitalize_name(case_info.get("nom_recherche")),
        ] if p]
        if cible_parts:
            lines.append(f"- Personne recherchée : {' '.join(cible_parts)}")
        if case_info.get("date_naissance"):
            lines.append(f"- Date de naissance / âge : {case_info['date_naissance']}")
        if case_info.get("region_recherche"):
            lines.append(f"- Région / pays de recherche : {case_info['region_recherche']}")

    elif case == "incapacite_travail":
        cible_parts = [p for p in [
            _capitalize_name(case_info.get("prenom_cible")),
            _capitalize_name(case_info.get("nom_cible")),
        ] if p]
        if cible_parts:
            lines.append(f"- Personne concernée : {' '.join(cible_parts)}")
        if case_info.get("adresse_cible"):
            lines.append(f"- Adresse connue de la personne : {case_info['adresse_cible']}")
        if case_info.get("certificat_incapacite"):
            lines.append(f"- Certificat / arrêt : {case_info['certificat_incapacite']}")
        if case_info.get("horaire_surveillance"):
            lines.append(f"- Horaire souhaité : {case_info['horaire_surveillance']}")
        if case_info.get("lieu_suspect"):
            lines.append(f"- Lieu / employeur suspecté : {case_info['lieu_suspect']}")

    elif case == "securite_passé_violences":
        if case_info.get("passe_violences"):
            lines.append(f"- Éléments déjà fournis sur la cible : {case_info['passe_violences']}")

    elif case == "contre_espionnage_micros":
        if case_info.get("micros_contexte"):
            lines.append(f"- Contexte du lieu : {case_info['micros_contexte']}")

    return lines


def _question_is_answered(info: dict[str, str | None], keys: list[str]) -> bool:
    """Une question est considérée comme répondue si au moins une clé est présente."""
    return any(info.get(k) for k in keys)


def _filter_missing_questions(
    case: str,
    client_info: dict[str, str | None],
    case_info: dict[str, str | None],
) -> list[str]:
    """Retourne la liste des questions qui n'ont pas encore été répondues."""
    merged = {**client_info, **case_info}
    specs = _CASE_QUESTION_SPECS.get(case, [])
    missing: list[str] = []
    for question, keys in specs:
        if not _question_is_answered(merged, keys):
            missing.append(question)
    return missing


def _build_standard_draft(
    greeting: str,
    first_name: str | None,
    need: str,
    mailbox: MailboxConfig,
    case: str,
    client_info: dict[str, str | None],
    case_info: dict[str, str | None],
) -> list[str]:
    """Assemble le brouillon standard avec résumé des infos reçues + questions manquantes."""
    settings = get_settings()
    received = _format_received_info(client_info, case_info, case)
    missing = _filter_missing_questions(case, client_info, case_info)

    lines = [greeting, "", need, ""]

    if received:
        lines.extend([
            "Merci pour les éléments suivants :",
            "",
            *received,
            "",
        ])

    if missing:
        lines.extend([
            (
                "Afin de préparer votre dossier dans les meilleures conditions, et pouvoir "
                "vous donner une estimation de devis fiable, pourriez-vous me transmettre "
                "les éléments suivants :"
            ),
        ])
        for i, q in enumerate(missing, 1):
            lines.append(f"{i}. {q}.")
    else:
        lines.extend([
            "J'ai bien noté tous les éléments utiles à ce stade. "
            "Je vous recontacte très prochainement par téléphone pour finaliser le devis "
            "et convenir d'un échange sur ce dossier.",
        ])
        # Pas de bloc tarifaire si le dossier est déjà complet? On le garde quand même
        # pour la transparence, mais on l'insère avant le closing.
        lines.append("")

    # Tarifs (toujours présents, sauf si dossier déjà complet et qu'on veut alléger).
    # On les garde systématiquement car Daniel veut que le client sache.
    lines.extend([
        "Sur le plan tarifaire :",
        f"- Ouverture de dossier : {settings.dossier_opening_fee} € HTVA.",
        f"- Rapport final : {settings.report_fee} € HTVA.",
        f"- Heure de détective : {settings.hourly_rate_day} €/h HTVA "
        f"({settings.hourly_rate_night_weekend} €/h nuit/week-end).",
    ])

    # Mention 2 détectives pour les cas filature / surveillance mobile.
    if case == "infidelite_filature":
        lines.extend([
            "",
            "Pour toute filature ou surveillance mobile, nous déployons systématiquement "
            "deux détectives afin d'assurer l'efficacité et la discrétion.",
        ])

    if missing:
        lines.extend([
            "",
            "Dès réception de ces éléments, je reprendrai contact avec vous "
            "pour finaliser le devis et convenir d'un échange téléphonique "
            "sur ce nouveau dossier.",
        ])

    lines.extend([
        "",
        "Bien à vous,",
        "",
        "Daniel Hurchon",
        f"{mailbox.brand}",
        "GSM 0471/31.81.20",
        "contact@detectivebelgique.be",
    ])
    return lines


def build_qualification_draft(
    subject: str,
    body: str,
    sender: str,
    mailbox: MailboxConfig,
    case: str,
) -> str:
    """Génère un brouillon qualifiant structuré et déterministe."""
    client_info = _extract_client_info(body, sender)
    case_info = _extract_case_info(body, case)
    first_name = client_info.get("prenom") or _extract_first_name(body)
    need = _rephrase_need(subject, body, case)
    greeting = f"Bonjour {first_name}," if first_name else "Bonjour,"

    # Pour le cas dette, on conserve la structure spécifique de Daniel.
    if case == "recuperation_dette":
        questions = _CASE_QUESTIONS.get(case, [])
        lines = _build_dette_draft(greeting, first_name, questions, mailbox, client_info)
    else:
        lines = _build_standard_draft(
            greeting=greeting,
            first_name=first_name,
            need=need,
            mailbox=mailbox,
            case=case,
            client_info=client_info,
            case_info=case_info,
        )
    return "\n".join(lines)


# --- Brouillon récupération de dette (conservé tel quel) ---------------------

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


def _build_dette_draft(
    greeting: str,
    first_name: str | None,
    questions: list[str],
    mailbox: MailboxConfig,
    client_info: dict[str, str | None],
) -> list[str]:
    """Brouillon spécifique pour récupération de dette, sur le modèle de Daniel."""
    received = _format_received_info(client_info, {}, "recuperation_dette")

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
