"""Tests TDD v1.29.0 — Système de fil de discussion cockpit (groupement parent + replies).

Contexte : l'inbox cockpit actuelle affiche 1 mail = 1 ligne, même quand un client
envoie un mail initial puis des replies ping-pong avec sujet qui change
(ex: 'Dossier Dupont : 740' → 'Re: Dossier Dupont : 748' → 'ajout au dossier : 746').
Le cockpit doit afficher 1 fil (parent + replies) au lieu de N lignes non liées.

Fix v1.29.0 :
- `app/pipeline/threading.py` : module pur (extract_dossier_name, derive_dossier_id_threading,
  compute_thread_id, pick_thread_subject). Pas de LLM, regex déterministes + hash fallback.
- DB : 6 nouvelles colonnes (message_id, in_reply_to, references, dossier_id, thread_id,
  thread_subject) + index sur thread_id.
- Cockpit : refacto _fetch_threads() + macro mail_thread_row() + tabs view=threads|flat|duplicates.
"""

from __future__ import annotations

from app.pipeline.threading import (
    compute_thread_id,
    derive_dossier_id_threading,
    extract_dossier_name,
    pick_thread_subject,
)

# --- extract_dossier_name --------------------------------------------------


def test_extract_dossier_name_dupont_subject() -> None:
    """Cas CDAL : 'Dossier Dupont' → 'dupont' (slug)."""
    assert extract_dossier_name("Dossier Dupont : 740", "") == "dupont"


def test_extract_dossier_name_dupont_lowercase() -> None:
    """Case insensitive sur le préfixe uniquement : 'dossier Dupont' → 'dupont'.
    Le nom doit rester capitalisé (sinon on matche 'dossier de divorce' = faux positif)."""
    assert extract_dossier_name("dossier Dupont", "") == "dupont"
    assert extract_dossier_name("DOSSIER Dupont", "") == "dupont"


def test_extract_dossier_name_compound() -> None:
    """Nom composé : 'Dossier de la Rue' → 'de_la_rue'."""
    assert extract_dossier_name("Dossier de la Rue : ABC", "") == "de_la_rue"


def test_extract_dossier_name_in_body_only() -> None:
    """Si sujet vague, fallback body 1er paragraphe."""
    body = "Bonjour,\n\nDossier Marchal, voici le devis demandé.\n\nCordialement,"
    assert extract_dossier_name("Demande de devis", body) == "marchal"


def test_extract_dossier_name_returns_none_when_no_match() -> None:
    """Pas de mot 'Dossier' → None (pas de dédup forcée)."""
    assert extract_dossier_name("Demande de filature", "") is None
    assert extract_dossier_name("Re: Bonjour", "") is None


# --- derive_dossier_id_threading ------------------------------------------


def test_derive_dossier_id_priority_name() -> None:
    """Priorité 1 : nom 'Dossier Dupont' (regex name > ref > hash)."""
    dossier_id = derive_dossier_id_threading("Dossier Dupont", "", "x@y.com")
    assert dossier_id == "dupont"


def test_derive_dossier_id_priority_ref() -> None:
    """Priorité 2 : ref alphanum majuscule si pas de name."""
    dossier_id = derive_dossier_id_threading("DOSSIER ABC123 : truc", "", "x@y.com")
    assert dossier_id == "ABC123"


def test_derive_dossier_id_priority_hash() -> None:
    """Priorité 3 : hash stable 16 chars si ni name ni ref."""
    dossier_id = derive_dossier_id_threading("Demande de filature", "", "x@y.com")
    assert len(dossier_id) == 16
    # Stabilité
    assert derive_dossier_id_threading("Demande de filature", "", "x@y.com") == dossier_id


def test_derive_dossier_id_hash_changes_with_subject() -> None:
    """Hash fallback : change si sujet change (un client avec 2 fils distincts)."""
    a = derive_dossier_id_threading("Sujet A", "", "x@y.com")
    b = derive_dossier_id_threading("Sujet B", "", "x@y.com")
    assert a != b


def test_derive_dossier_id_hash_same_subject_same_sender() -> None:
    """Hash fallback : même sujet + même sender = même dossier_id (dedupe OK)."""
    a = derive_dossier_id_threading("Re: Bonjour", "", "x@y.com")
    b = derive_dossier_id_threading("Re: Re: Bonjour", "", "x@y.com")
    assert a == b  # normalize_subject strippe les Re: → même clé


# --- compute_thread_id ----------------------------------------------------


def test_compute_thread_id_format() -> None:
    """Thread ID = 'dossier::sender' (cross-boîte safe via sender_normalized)."""
    tid = compute_thread_id("dupont", "X@Y.com")
    assert tid == "dupont::x@y.com"


def test_compute_thread_id_different_sender() -> None:
    """2 senders distincts = 2 threads distincts même dossier."""
    a = compute_thread_id("dupont", "alice@y.com")
    b = compute_thread_id("dupont", "bob@y.com")
    assert a != b


def test_compute_thread_id_cross_boite_same_thread() -> None:
    """Cross-boîte (D_FR + D_DS) : même sender = même thread."""
    a = compute_thread_id("dupont", "Client@Example.com")
    b = compute_thread_id("dupont", "Client@Example.com")
    assert a == b


def test_compute_thread_id_empty_dossier_uses_adhoc() -> None:
    """Pas de dossier_id (sujet vague) → thread_id 'adhoc::...' (jamais None)."""
    tid = compute_thread_id("", "x@y.com")
    assert tid.startswith("adhoc::")
    assert "x@y.com" in tid


# --- pick_thread_subject ---------------------------------------------------


def test_pick_thread_subject_oldest_specific() -> None:
    """Cas CDAL 740/748/746 : sujet canonique = plus ancien (740 'Dossier Dupont').

    rows = [(subject, received_at_iso)]. Prend le plus ancien qui a le plus de mots
    significatifs (= le plus spécifique = le parent du fil).
    """
    rows = [
        ("Dossier Dupont : 740", "2026-06-30T10:00:00"),
        ("Re: Dossier dupont : 748", "2026-06-30T11:00:00"),
        ("ajout au dossier : 746", "2026-06-30T12:00:00"),
    ]
    assert pick_thread_subject(rows) == "Dossier Dupont : 740"


def test_pick_thread_subject_only_replies() -> None:
    """Si tous les rows sont des Re:, retourne le plus ancien (le parent originel)."""
    rows = [
        ("Re: Bonjour", "2026-06-30T10:00:00"),
        ("Re: Re: Bonjour", "2026-06-30T11:00:00"),
    ]
    assert pick_thread_subject(rows) == "Re: Bonjour"


def test_pick_thread_subject_empty() -> None:
    """Empty rows → chaîne vide (defensif)."""
    assert pick_thread_subject([]) == ""


# --- Régression v1.28.3 dedup --------------------------------------------


def test_apple_dpdhu_still_dedupes_with_threading() -> None:
    """Régression v1.28.3 : 5 mails 'Re: Votre reçu Apple' de dpdhuinvestigations
    → 1 seul thread, hash fallback dédoublonne."""
    # Pas de 'Dossier' → hash fallback, mais normalize_subject strippe les Re:
    # donc les 5 mails ont le MÊME hash = même dossier_id = même thread.
    a = derive_dossier_id_threading("Re: Votre reçu Apple", "", "dpdhuinvestigations@gmail.com")
    b = derive_dossier_id_threading("Votre reçu Apple", "", "dpdhuinvestigations@gmail.com")
    assert a == b


def test_kirara_3_jours_4_threads() -> None:
    """Régression Kirara : 4 mails sur 3 jours avec sujets DIFFÉRENTS = 4 dossiers distincts.

    Kirara 'Re: Devis et convention' (3 jours) + 'Re: Re: Devis et convention' :
    → normalize_subject strippe → même clé pour ces 2-là.
    Mais 'Nouveau Message De Détective...' = sujet différent = autre fil.
    """
    a = derive_dossier_id_threading("Re: Devis et convention", "", "kirara.olivier@yahoo.fr")
    b = derive_dossier_id_threading(
        "Nouveau Message De Détective privé Belgique - Pren", "", "kirara.olivier@yahoo.fr"
    )
    assert a != b
