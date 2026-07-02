"""Dérivation du fil de discussion pour le cockpit inbox — v1.29.0.

Contexte : l'inbox actuelle affiche 1 mail = 1 ligne, mais un client peut
envoyer un mail initial puis des replies ping-pong avec un sujet qui change
(ex: 'Dossier Dupont : 740' → 'Re: Dossier Dupont : 748' → 'ajout au dossier : 746').
La clé de dédup v1.28.3 = (sender_normalized, subject_normalized) rate ce cas
car les Re: sont strippés mais les reformulations de sujet cassent la clé.

Ce module fournit une clé de fil robuste :
- `extract_dossier_name()` : capte un nom de dossier humain ('Dossier Dupont').
- `derive_dossier_id_threading()` : hiérarchie name > ref > hash stable.
- `compute_thread_id()` : 'dossier::sender' (cross-boîte safe).
- `pick_thread_subject()` : sujet canonique = plus ancien + plus spécifique.

Pas de LLM, déterministe, < 5ms par mail. Réutilise `_slug` et `extract_dossier_ref`
de `app.cerveau_dossier` pour ne pas dupliquer la logique métier.
"""

from __future__ import annotations

import hashlib
import re

from app.cerveau_dossier import _slug, extract_dossier_ref
from app.pipeline.dedup import normalize_sender, normalize_subject

# Regex "Dossier <NOM>" étendu (reprises de charlie.py:744, enrichies accents + composé)
# Accepte : "Dossier Dupont", "dossier dupont", "Dossier Dupré", "Dossier de la Rue", etc.
# Le groupe capturant global est "name_full" (group 1) qui inclut prépositions
# optionnelles (de, du, de la, d') + 1-5 mots capitalisés. Filtre anti-ref
# appliqué en aval via _is_name_with_lowercase (un vrai nom a des minuscules).
_DOSSIER_NAME_RE = re.compile(
    r"[Dd][Oo][Ss]{2}[Ii][Ee][Rr]\s+"
    r"((?:(?:d[eu']|d'|de\s+la)\s+)?"  # préposition optionnelle
    r"(?:[A-ZÉÈÀÂÊÎÔÛÄËÏÖÜÇ][a-zA-Zéèàâêîôûäëïöüç\-']{1,40}"
    r"(?:\s+[A-ZÉÈÀÂÊÎÔÛÄËÏÖÜÇ][a-zA-Zéèàâêîôûäëïöüç\-']{1,40}){0,4}))",
    re.UNICODE,
)


def _is_name_with_lowercase(text: str) -> bool:
    """Vérifie qu'un texte ressemble à un nom (contient au moins une minuscule).

    Exclut les refs uppercase type "ABC123" qui sont gérées par `extract_dossier_ref`.
    """
    return any(c.islower() for c in text)

# Préfixes à stripper pour le sujet canonique (cosmétique — la version canonique
# est le sujet du mail le plus ancien, mais on normalise Re:/Fwd: pour l'affichage)
_RE_PREFIX_RE = re.compile(r"^\s*(re|fwd?|aw|tr|sv)\s*:\s*", re.IGNORECASE)


def _strip_reply_prefix(subject: str) -> str:
    """Strippe les préfixes Re:/Fwd: multi-niveaux (pour pick_thread_subject)."""
    if not subject:
        return ""
    s = subject.strip()
    prev = None
    while s and s != prev:
        prev = s
        s = _RE_PREFIX_RE.sub("", s).strip()
    return s


def extract_dossier_name(subject: str, body: str) -> str | None:
    """Extrait un nom de dossier humain du sujet ou du 1er paragraphe du body.

    Returns:
        Slug ASCII (lowercase, underscores) ou None si pas de match.
    """
    # 1. D'abord dans le sujet (toujours plus fiable)
    if subject:
        m = _DOSSIER_NAME_RE.search(subject)
        if m:
            name = m.group(1).strip()
            # Filtre anti-ref : un vrai nom de personne a des minuscules.
            if _is_name_with_lowercase(name):
                slug = _slug(name)
                if slug:
                    return slug

    # 2. Fallback : 1er paragraphe du body (1000 chars max — évite footers marketing)
    if body:
        head = body[:1000]
        m = _DOSSIER_NAME_RE.search(head)
        if m:
            name = m.group(1).strip()
            if _is_name_with_lowercase(name):
                slug = _slug(name)
                if slug:
                    return slug

    return None


def derive_dossier_id_threading(subject: str, body: str, sender: str) -> str:
    """Dérive un identifiant de dossier métier (slug court) à partir du mail.

    Hiérarchie de priorité :
    1. `extract_dossier_name()` → slug (ex: "dupont")
    2. `extract_dossier_ref()` → ref alphanum majuscule (ex: "ADF")
    3. Hash stable (sha1[:16]) de (sender_norm + subject_norm) → tag anonyme
    4. "" si sender vide

    Le hash en fallback garantit que `Re: truc` et `Re: Re: truc` d'un même
    sender vont dans le même fil (cas client qui reformule totalement).
    """
    # 1. Name
    name = extract_dossier_name(subject, body)
    if name:
        return name

    # 2. Ref (uppercase pour cohérence avec l'usage Cerveau2)
    ref = extract_dossier_ref(subject or "")
    if ref:
        return ref.upper()

    # 3. Hash stable (sender + subject normalisé → clé unique)
    sender_n = normalize_sender(sender or "")
    subject_n = normalize_subject(subject or "")
    if not sender_n or not subject_n:
        return ""
    key = f"{sender_n}|{subject_n}".encode("utf-8")
    return hashlib.sha1(key).hexdigest()[:16]


def compute_thread_id(dossier_id: str, sender: str, subject: str = "") -> str:
    """Calcule un identifiant de fil de discussion stable.

    - Si dossier_id non-vide : 'dossier_id::sender_normalized'
    - Sinon : 'adhoc::sender_normalized::hash12' (jamais None, jamais vide)

    Stabilité garantie : même (dossier_id, sender[, subject]) → même thread_id.

    v1.29.0.7 — FIX BUG P0 : le hash adhoc inclut MAINTENANT le subject
    normalisé (pas seulement le sender). AVANT : un client qui envoie 10
    mails SANS dossier explicite (sender "unknown" / dossier_id vide)
    se retrouvait avec thread_id='adhoc::unknown::50d8b4a9' pour TOUS,
    regroupant 207 mails dans un même fil fourre-tout → le cockpit
    affichait le 1er mail (id=63) comme parent + les 206 autres comme
    "replies" invisibles, mais avec LIMIT 1000 on ne voyait que les
    1000 derniers → reply fantôme sans parent visible.

    APRÈS : hash12 = sha1(sender_n|subject_n)[:12] → 2 mails même sender
    même subject normalisé (Re: truc et Re: truc) = même fil (légitime).
    2 mails même sender sujet DIFFÉRENT = fils DIFFÉRENTS (cas normal).
    """
    sender_n = normalize_sender(sender or "")
    if not sender_n:
        sender_n = "unknown"
    if dossier_id:
        return f"{dossier_id}::{sender_n}"
    # Adhoc : un client qui envoie 2 mails SANS dossier mais MÊME sujet
    # normalisé doit être groupé (Re: truc et Re: truc = même fil).
    # Mais 2 mails MÊME sender SUJET DIFFÉRENT = 2 fils séparés.
    subject_n = normalize_subject(subject or "")
    adhoc_key = f"{sender_n}|{subject_n}".encode("utf-8")
    adhoc_hash = hashlib.sha1(adhoc_key).hexdigest()[:12]
    return f"adhoc::{sender_n}::{adhoc_hash}"


def pick_thread_subject(rows: list[tuple[str, str]]) -> str:
    """Choisit le sujet canonique d'un fil (le + ancien, le + spécifique).

    Args:
        rows: Liste de (subject, received_at_iso) — tous les mails du même thread.

    Returns:
        Sujet du mail le plus ancien (parent du fil), ou '' si vide.
    """
    if not rows:
        return ""
    # Tri par received_at ASC (le plus ancien en premier)
    sorted_rows = sorted(rows, key=lambda r: r[1])
    # On prend le plus ancien — c'est le parent du fil, le sujet le plus
    # "original" (le client n'a pas encore reformulé).
    return sorted_rows[0][0] or ""
