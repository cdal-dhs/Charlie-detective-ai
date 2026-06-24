"""Tests des fixes v1.25.22 — garde-fous anti-crash silencieux (#629).

Couvre :
- Reply-To prioritaire comme email client (forwarders WP). Bug B.
- Regex « nom » stricte : ne matche plus « ce nom » au milieu d'une phrase. Bug D.
- Sujet de brouillon lisible quand le sujet original est un template WP. Bug E.
- Réconcilieur Drafts : recherche du brouillon par header X-Detective-Mail-Id.
- Réconcilieur : propagation du reply_to depuis la DB vers l'IncomingMail.

Cas de référence : mail #629 (Christèle Kremp-Voinova, Reply-To ckremp@vo.lu,
forwarder mail@detectivebelgique.be, body pollué par le chrome marketing
wikipreneurs.be).
"""

from __future__ import annotations

import pytest

from app.delivery.imap_draft import _build_draft_body
from app.delivery.resend_notifier import IncomingMail
from app.pipeline.generator import GenerationResult
from app.pipeline.qualification_builder import (
    _extract_client_info,
    suggested_subject_for_draft,
)
from app.pipeline.subject_fixer import mask_forwarder_sender

# --- Bug B : Reply-To prioritaire comme email client -------------------------


def test_mask_forwarder_sender_uses_reply_to() -> None:
    """#629 — un forwarder WP avec Reply-To = vrai email client."""
    displayed = mask_forwarder_sender(
        "mail@detectivebelgique.be",
        body="Nom: KREMP-VOINOVA",
        reply_to="ckremp@vo.lu",
    )
    assert displayed == "ckremp@vo.lu"


def test_mask_forwarder_sender_no_reply_to_forwarder() -> None:
    """Forwarder WP sans Reply-To ni email dans le body -> NO_EMAIL_IN_THE_FORM."""
    displayed = mask_forwarder_sender(
        "wordpress@detectivebelgium.com", body="Bonjour", reply_to=""
    )
    assert displayed == "NO_EMAIL_IN_THE_FORM"


def test_mask_forwarder_sender_ignores_internal_reply_to() -> None:
    """Un Reply-To interne (no-reply / domaine Detective) n'est pas un client."""
    displayed = mask_forwarder_sender(
        "mail@detectivebelgique.be", body="x", reply_to="no-reply@detectivebelgique.be"
    )
    # reply_to interne rejeté -> on retombe sur le forwarder sans email body.
    assert displayed == "NO_EMAIL_IN_THE_FORM"


def test_extract_client_info_reply_to_prioritaire() -> None:
    """Le Reply-To écrase tout email glané dans le body (cas du scammeur)."""
    body = "Nom: KREMP-VOINOVA\nContact: scammeur@poisson.com"
    info = _extract_client_info(body, "mail@detectivebelgique.be", reply_to="ckremp@vo.lu")
    assert info["email"] == "ckremp@vo.lu"


def test_extract_client_info_sans_reply_to_body_gagne() -> None:
    """Sans Reply-To, l'email du body est conservé."""
    body = "Nom: DUPONT\nMon email: client@example.com"
    info = _extract_client_info(body, "x@y.com", reply_to="")
    assert info["email"] == "client@example.com"


# --- Bug D : regex « nom » stricte -------------------------------------------


def test_extract_nom_label_debut_ligne() -> None:
    """« Nom: » en début de ligne de formulaire WP est capturé."""
    body = "Nom: KREMP-VOINOVA\nPrénom: CHRISTELE"
    info = _extract_client_info(body, "mail@detectivebelgique.be")
    assert info["nom"] == "KREMP-VOINOVA"


def test_extract_nom_ne_matche_pas_ce_nom_au_milieu() -> None:
    """« ce nom » au milieu d'une phrase NE doit PAS être capturé (régression #629)."""
    body = (
        "Bonjour, je consulte votre site car je veux connaitre ce nom de domaine "
        "et aussi ce nom de société pour mon voisin."
    )
    info = _extract_client_info(body, "x@y.com")
    assert info.get("nom") is None


def test_extract_nom_mon_nom_est_naturel() -> None:
    """« mon nom est » (formulation naturelle) reste capturé."""
    body = "Bonjour, mon nom est Bassem Sophie."
    info = _extract_client_info(body, "x@y.com")
    assert info["nom_complet"] is not None
    assert "Bassem" in info["nom_complet"]


# --- Bug E : sujet de brouillon lisible --------------------------------------


def test_suggested_subject_template_wp_avec_nom() -> None:
    """Sujet template WP + body avec Nom/Prénom -> sujet lisible représentatif."""
    body = "Nom: KREMP-VOINOVA\nPrénom: CHRISTELE\nVotre profil ?: Particulier"
    subj = suggested_subject_for_draft(
        "Nouveau Message De Détective privé Belgique - Prenons contact",
        body,
        "mail@detectivebelgique.be",
        "non_determine",
    )
    assert subj is not None
    assert "Kremp" in subj  # nom du client présent
    assert "Demande" in subj  # libellé du cas


def test_suggested_subject_sujet_normal_renvoie_none() -> None:
    """Un sujet déjà pertinent (non template WP) -> None (on garde l'original)."""
    subj = suggested_subject_for_draft(
        "Demande de filature - suspicion infidélité",
        "Nom: Dupont",
        "client@example.com",
        "infidelite_filature",
    )
    assert subj is None


# --- Bug C : réconcilieur Drafts (recherche par header marker) ---------------


class _FakeSearchResult:
    def __init__(self, ok: bool, lines: list[bytes]) -> None:
        self.result = "OK" if ok else "NO"
        self.lines = lines


# aioimaplib ajoute toujours une ligne de status "Search completed (...)" à la
# fin de resp.lines. On la simule pour régresser le bug P0 v1.25.23 (l'ancien code
# confondait cette ligne non-vide avec un vrai match -> "present" systématique).
_STATUS_LINE = b"Search completed (0.003 + 0.000 secs)."


class _FakeImapClient:
    """Faux client IMAP pour tester _draft_present sans vraie connexion."""

    def __init__(self, header_lines: list[bytes], body_lines: list[bytes]) -> None:
        self._header_lines = header_lines
        self._body_lines = body_lines
        self.search_calls: list[str] = []

    async def select(self, folder: str) -> _FakeSearchResult:
        return _FakeSearchResult(ok=True, lines=[])

    async def search(self, criteria: str) -> _FakeSearchResult:
        self.search_calls.append(criteria)
        if "HEADER X-Detective-Mail-Id" in criteria:
            return _FakeSearchResult(ok=True, lines=[*self._header_lines, _STATUS_LINE])
        if 'BODY "EMAIL #' in criteria:
            return _FakeSearchResult(ok=True, lines=[*self._body_lines, _STATUS_LINE])
        return _FakeSearchResult(ok=True, lines=[_STATUS_LINE])


@pytest.mark.asyncio
async def test_draft_present_par_header_marker() -> None:
    """Le brouillon est trouvé via le header X-Detective-Mail-Id (v1.25.22+)."""
    from app.workers.drafts_reconciler import _draft_present

    client = _FakeImapClient(header_lines=[b"1"], body_lines=[])
    found = await _draft_present(client, "Drafts", 629)
    assert found is True
    assert any("HEADER X-Detective-Mail-Id 629" in c for c in client.search_calls)


@pytest.mark.asyncio
async def test_draft_absent_retourne_false() -> None:
    """Aucun match header ni body -> False (brouillon manquant, à re-livrer).

    Régression bug P0 v1.25.23 : la ligne de status 'Search completed' seule
    (présente dans toute réponse aioimaplib) ne doit PAS être confondue avec un
    match. Avant le fix, _draft_present retournait True ici (faux positif) ->
    le réconcilieur ne détectait jamais les manquants.
    """
    from app.workers.drafts_reconciler import _draft_present

    client = _FakeImapClient(header_lines=[], body_lines=[])
    found = await _draft_present(client, "Drafts", 999)
    assert found is False


@pytest.mark.asyncio
async def test_draft_present_ignores_status_line_seule() -> None:
    """Une réponse ne contenant QUE la ligne de status -> False (régression P0)."""
    from app.workers.drafts_reconciler import _draft_present

    # header_lines et body_lines vides -> réponse = [status_line] uniquement
    client = _FakeImapClient(header_lines=[], body_lines=[])
    found = await _draft_present(client, "Drafts", 1234)
    assert found is False


def test_has_search_match_faux_positif_status() -> None:
    """La ligne 'Search completed' seule ne doit pas compter comme un match."""
    from app.workers.drafts_reconciler import _has_search_match

    assert _has_search_match([_STATUS_LINE]) is False
    assert _has_search_match([]) is False
    assert _has_search_match([b"1", _STATUS_LINE]) is True  # UID 1 = vrai match
    assert _has_search_match([b"Search completed", b"42"]) is True  # 42 = match


@pytest.mark.asyncio
async def test_draft_present_fallback_body_legacy() -> None:
    """Brouillon legacy (sans header marker) retrouvé via le body 'EMAIL #<id>'."""
    from app.workers.drafts_reconciler import _draft_present

    client = _FakeImapClient(header_lines=[], body_lines=[b"42"])
    found = await _draft_present(client, "Drafts", 42)
    assert found is True


@pytest.mark.asyncio
async def test_fetch_candidates_exclut_delivered(tmp_path) -> None:
    """Anti-doublon : seuls les brouillons JAMAIS livrés (delivered_at NULL) sont
    candidats à la re-livraison. Les brouillons déjà livrés puis envoyés par
    Daniel (delivered_at set) ne doivent PAS être re-livrés (sinon doublons massifs).
    """
    import aiosqlite

    from app.workers.drafts_reconciler import _fetch_candidates

    db = tmp_path / "state.db"
    async with aiosqlite.connect(db) as conn:
        await conn.execute(
            """CREATE TABLE mail_processed (
                id INTEGER PRIMARY KEY, imap_uid TEXT, mailbox_name TEXT,
                subject TEXT, sender TEXT, received_at TEXT, category TEXT,
                draft_generated INTEGER, body_preview TEXT, body TEXT,
                ai_draft TEXT, status TEXT, priority TEXT, reply_to TEXT,
                delivered_at TEXT, processed_at TEXT
            )"""
        )
        # mail 100 : jamais livré (delivered_at NULL) -> candidat (crash silencieux)
        # mail 101 : livré puis envoyé par Daniel (delivered_at set) -> exclus
        # mail 102 : livré (delivered_at set) -> exclus
        await conn.executemany(
            "INSERT INTO mail_processed (id, imap_uid, mailbox_name, category, "
            "draft_generated, ai_draft, body, delivered_at, processed_at) "
            "VALUES (?,?,?,?,1,?,?,?,?)",
            [
                (100, "u100", "detective_belgique", "demande_client", "draft100",
                 "body100", None, "2026-06-24T00:00:00Z"),
                (101, "u101", "detective_belgique", "demande_client", "draft101",
                 "body101", "2026-06-23T18:43:00Z", "2026-06-23T18:00:00Z"),
                (102, "u102", "detective_belgique", "demande_client", "draft102",
                 "body102", "2026-06-22T10:00:00Z", "2026-06-22T09:00:00Z"),
            ],
        )
        await conn.commit()
    cands = await _fetch_candidates(db)
    ids = sorted(c["id"] for c in cands)
    assert ids == [100], f"seul le mail jamais livré doit etre candidat, got {ids}"


def test_rebuild_inputs_propage_reply_to() -> None:
    """_rebuild_inputs injecte reply_to depuis la DB dans l'IncomingMail."""
    from app.config import get_settings
    from app.workers.drafts_reconciler import _rebuild_inputs

    settings = get_settings()
    mailbox = settings.mailboxes()[0]
    mail = {
        "id": 629,
        "imap_uid": "UID-629",
        "mailbox_name": mailbox.name,
        "subject": "Nouveau Message",
        "sender": "mail@detectivebelgique.be",
        "received_at": "Mon, 23 Jun 2026 12:00:00 +0000",
        "ai_draft": "Bonjour, proposition...",
        "body": "Nom: KREMP-VOINOVA",
        "delivered_at": None,
        "reply_to": "ckremp@vo.lu",
    }
    incoming, gen = _rebuild_inputs(mail, mailbox)
    assert incoming.reply_to == "ckremp@vo.lu"
    assert gen.draft == "Bonjour, proposition..."


# --- _build_draft_body : bandeau affiche le reply_to -------------------------


def test_build_draft_body_affiche_reply_to() -> None:
    """Le bandeau du brouillon affiche le Reply-To (vrai client), pas le forwarder."""
    incoming = IncomingMail(
        sender="mail@detectivebelgique.be",
        subject="Nouveau Message",
        body="Nom: KREMP-VOINOVA",
        received_at="Mon, 23 Jun 2026",
        message_id="UID-629",
        reply_to="ckremp@vo.lu",
    )
    gen = GenerationResult(
        draft="Bonjour Christèle, proposition...",
        raw_draft="Bonjour Christèle, proposition...",
        language="fr",
        rag_pairs=[],
        model_used="test",
        category="demande_client",
    )
    body_text = _build_draft_body(incoming, gen, mail_id=629, base_url="https://detective.digitalhs.biz")
    assert "EMAIL #629" in body_text
    assert "ckremp@vo.lu" in body_text  # reply_to affiché, pas le forwarder
    assert "X-Detective-Mail-Id" not in body_text  # marker header, pas dans le body texte


# --- v1.25.24 : mask_forwarder_sender — ne plus jamais afficher le forwarder ---


def test_mask_forwarder_newsletter_no_reply_to() -> None:
    """newsletter@wikipreneurs.be (le vrai sender #629) sans Reply-To -> NO_EMAIL."""
    got = mask_forwarder_sender("newsletter@wikipreneurs.be", body="Bonjour", reply_to="")
    assert got == "NO_EMAIL_IN_THE_FORM"


def test_mask_forwarder_wordpress_no_reply_to() -> None:
    """wordpress@detectivebelgium.com sans Reply-To ni email body -> NO_EMAIL."""
    got = mask_forwarder_sender("wordpress@detectivebelgium.com", body="x", reply_to="")
    assert got == "NO_EMAIL_IN_THE_FORM"


def test_mask_forwarder_noreply_external() -> None:
    """noreply@domaine-tiers sans Reply-To -> NO_EMAIL (robot, pas un client)."""
    got = mask_forwarder_sender("noreply@shop.com", body="cmd", reply_to="")
    assert got == "NO_EMAIL_IN_THE_FORM"


def test_mask_forwarder_with_body_email_returns_body_email() -> None:
    """Forwarder sans Reply-To MAIS email client dans le body -> email du body."""
    body = "Nom: Dupont\nMon email: client.reel@gmail.com"
    got = mask_forwarder_sender("wordpress@detectivebelgium.com", body=body, reply_to="")
    assert got == "client.reel@gmail.com"


def test_mask_forwarder_direct_client_unchanged() -> None:
    """Un mail direct d'un humain (pas un robot) reste inchangé."""
    got = mask_forwarder_sender("jean.dupont@gmail.com", body="Bonjour", reply_to="")
    assert got == "jean.dupont@gmail.com"


def test_mask_forwarder_internal_reply_to_rejected() -> None:
    """Un Reply-To interne Detective n'est pas un client -> retombe sur NO_EMAIL."""
    assert mask_forwarder_sender(
        "mail@detectivebelgique.be", body="x", reply_to="no-reply@detectivebelgique.be"
    ) == "NO_EMAIL_IN_THE_FORM"


def test_extract_client_email_skips_detective_domain() -> None:
    from app.pipeline.subject_fixer import _extract_client_email_from_body

    assert _extract_client_email_from_body("Contact: privacy@detectivebelgium.com") == ""


def test_extract_client_email_finds_client() -> None:
    from app.pipeline.subject_fixer import _extract_client_email_from_body

    assert _extract_client_email_from_body("Email: client@vo.lu merci") == "client@vo.lu"


def test_extract_client_email_ignores_markdown_url_at() -> None:
    """Regression : le @ d'une URL markdown (youtube.com/@lab9be) ne doit PAS
    etre matche comme email (backfill #80 newsletter@lab9.be)."""
    from app.pipeline.subject_fixer import _extract_client_email_from_body

    body = "[YouTube](https://www.youtube.com/@lab9be) Suivez-nous"
    assert _extract_client_email_from_body(body) == ""


def test_extract_client_email_ignores_css_at_rule() -> None:
    """Regression : une regle CSS @media / @-ms-viewport ne doit PAS etre
    matchee comme email (backfill #94 noreply@communication.bpost.be)."""
    from app.pipeline.subject_fixer import _extract_client_email_from_body

    body = "@media screen and (max-width:600px){ @@-ms-viewport{ width:100% }}"
    assert _extract_client_email_from_body(body) == ""


def test_persist_masks_forwarder_sender(tmp_path) -> None:
    """_persist stocke NO_EMAIL_IN_THE_FORM pour un forwarder sans contact client."""
    import sqlite3

    from app.workers.imap_poller import _persist

    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE mail_processed (id INTEGER PRIMARY KEY, imap_uid TEXT, "
        "mailbox_name TEXT, subject TEXT, sender TEXT, received_at TEXT, "
        "category TEXT, draft_generated INTEGER, body_preview TEXT, body TEXT, "
        "ai_draft TEXT, status TEXT, priority TEXT, reply_to TEXT, "
        "delivered_at TEXT, processed_at TEXT)"
    )
    conn.commit()
    conn.close()
    mail_id = _persist(
        db_path=db,
        imap_uid="uid-mask-1",
        mailbox_name="detective_belgique",
        subject="Sujet",
        sender="wordpress@detectivebelgium.com",
        received_at="Mon, 1 Jan 2026 10:00:00 +0200",
        category="demande_client",
        draft_generated=0,
        body="Bonjour",
        reply_to="",
    )
    row = sqlite3.connect(db).execute(
        "SELECT sender FROM mail_processed WHERE id=?", (mail_id,)
    ).fetchone()
    assert row[0] == "NO_EMAIL_IN_THE_FORM"


def test_persist_keeps_reply_to_as_sender(tmp_path) -> None:
    """_persist stocke le Reply-To comme sender quand c'est le vrai client (#629)."""
    import sqlite3

    from app.workers.imap_poller import _persist

    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE mail_processed (id INTEGER PRIMARY KEY, imap_uid TEXT, "
        "mailbox_name TEXT, subject TEXT, sender TEXT, received_at TEXT, "
        "category TEXT, draft_generated INTEGER, body_preview TEXT, body TEXT, "
        "ai_draft TEXT, status TEXT, priority TEXT, reply_to TEXT, "
        "delivered_at TEXT, processed_at TEXT)"
    )
    conn.commit()
    conn.close()
    mail_id = _persist(
        db_path=db,
        imap_uid="uid-mask-2",
        mailbox_name="detective_belgique",
        subject="Sujet",
        sender="newsletter@wikipreneurs.be",
        received_at="Mon, 1 Jan 2026 10:00:00 +0200",
        category="demande_client",
        draft_generated=1,
        body="Nom: Kremp",
        reply_to="ckremp@vo.lu",
    )
    row = sqlite3.connect(db).execute(
        "SELECT sender FROM mail_processed WHERE id=?", (mail_id,)
    ).fetchone()
    assert row[0] == "ckremp@vo.lu"
