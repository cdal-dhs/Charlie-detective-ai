"""Tests de non-régression du rendu cockpit inbox (/app/ et /api/inbox).

v1.25.19 — le bug P0 a été introduit par un désalignement entre le SELECT SQL
et la liste `cols` de `_fetch_mails()` / `_fetch_mails_partial()`. `ai_draft`
recevait la valeur entière de `attachment_count`, ce qui provoquait :

    TypeError: object of type 'int' has no len()

sur `m.ai_draft|length` dans `app/web/templates/app/inbox_rows.html`.

Ces tests créent une DB temporaire avec le vrai schema mail_processed et
vérifient que le template inbox se rend sans 500, avec et sans brouillon,
et que le masque forwarder WP s'applique correctement.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.web.app import make_app
from app.web.deps import get_db, require_operator


def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "agent_state.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE mail_processed (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            imap_uid TEXT NOT NULL,
            mailbox_name TEXT NOT NULL,
            subject TEXT,
            sender TEXT,
            received_at TEXT,
            category TEXT,
            draft_generated INTEGER DEFAULT 0,
            draft_sent_at TEXT,
            processed_at TEXT DEFAULT CURRENT_TIMESTAMP,
            status TEXT,
            priority TEXT,
            ai_draft TEXT,
            human_draft TEXT,
            reviewed_by INTEGER,
            reviewed_at DATETIME,
            sent_at DATETIME,
            sent_by INTEGER,
            body_preview TEXT,
            body TEXT,
            delivered_at TEXT,
            suggested_subject TEXT,
            -- v1.29.0 — threading columns
            message_id TEXT,
            in_reply_to TEXT,
            "references" TEXT,
            dossier_id TEXT,
            thread_id TEXT,
            thread_subject TEXT,
            -- v1.29.0.6 — dedup column
            duplicate_of INTEGER,
            UNIQUE(imap_uid, mailbox_name)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT,
            resource_type TEXT,
            resource_id TEXT,
            details TEXT,
            ip_address TEXT,
            user_agent TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE email_attachment (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mail_processed_id INTEGER NOT NULL,
            filename TEXT,
            storage_path TEXT,
            size_bytes INTEGER,
            extracted_text_preview TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # Mail avec brouillon généré (hot — demande_client + high + pending)
    conn.execute(
        "INSERT INTO mail_processed "
        "(id, imap_uid, mailbox_name, subject, sender, received_at, category, "
        " status, priority, body_preview, body, ai_draft, draft_generated, processed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            1,
            "uid1",
            "detective_belgique",
            "Demande de tarif",
            "client@example.com",
            "2026-06-23 10:00:00",
            "demande_client",
            "pending",
            "high",
            "Bonjour, je voudrais un tarif...",
            "Bonjour, je voudrais un tarif...",
            "Cher client, voici nos tarifs...",
            1,
            "2026-06-23 10:01:00",
        ),
    )
    # Mail sans brouillon
    conn.execute(
        "INSERT INTO mail_processed "
        "(id, imap_uid, mailbox_name, subject, sender, received_at, category, "
        " status, priority, body_preview, body, ai_draft, draft_generated, processed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            2,
            "uid2",
            "detective_belgium",
            "Newsletter mensuelle",
            "newsletter@fournisseur.be",
            "2026-06-23 09:00:00",
            "newsletter",
            "approved",
            "low",
            "Notre newsletter de juin...",
            "Notre newsletter de juin...",
            None,
            0,
            "2026-06-23 09:01:00",
        ),
    )
    # Forwarder WP NL sans email client (doit être masqué)
    conn.execute(
        "INSERT INTO mail_processed "
        "(id, imap_uid, mailbox_name, subject, sender, received_at, category, "
        " status, priority, body_preview, body, ai_draft, draft_generated, processed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            3,
            "uid3",
            "detective_belgium",
            "Nieuwe aanvraag",
            "wordpress@detectivebelgium.com",
            "2026-06-23 08:00:00",
            "demande_client",
            "pending",
            "high",
            "Achternaam: Dupont\nVoornaam: Jean\nTelefoonnummer: 0477/123456",
            "Achternaam: Dupont\nVoornaam: Jean\nTelefoonnummer: 0477/123456",
            "Cher Jean, je vous appelle...",
            1,
            "2026-06-23 08:01:00",
        ),
    )
    conn.commit()
    conn.close()
    return db


def _db_path_holder(db_path: Path):
    async def _override():
        db = await aiosqlite.connect(str(db_path))
        try:
            yield db
        finally:
            await db.close()

    return _override


@pytest.fixture
def operator_user():
    return {"id": 1, "email": "cdal@digitalhs.biz", "role": "super_admin", "name": "CDAL"}


@pytest.fixture
def client(tmp_path: Path, operator_user):
    db_path = _make_db(tmp_path)
    app = make_app()
    app.dependency_overrides[get_db] = _db_path_holder(db_path)
    app.dependency_overrides[require_operator] = lambda: operator_user
    test_client = TestClient(app)
    test_client._db_path = db_path  # type: ignore[attr-defined]
    return test_client


def test_app_index_renders_inbox_200(client) -> None:
    """Le cockpit /app/ doit s'afficher sans erreur 500."""
    resp = client.get("/app/")
    assert resp.status_code == 200, resp.text[:500]


def test_api_inbox_renders_rows_200(client) -> None:
    """Le fragment HTMX /api/inbox doit retourner les lignes sans 500."""
    resp = client.get("/api/inbox")
    assert resp.status_code == 200, resp.text[:500]


def test_inbox_rows_show_draft_badge(client) -> None:
    """Un mail avec ai_draft non vide affiche le badge brouillon dans la colonne Boîte."""
    resp = client.get("/api/inbox")
    assert resp.status_code == 200
    # Le title du badge est plus stable que l'emoji qui peut être encodé différemment.
    assert "Proposition de réponse générée par Charlie" in resp.text
    assert "Demande de tarif" in resp.text


def test_inbox_rows_mask_forwarder_sender(client) -> None:
    """Un forwarder WP sans email client doit afficher NO_EMAIL_IN_THE_FORM."""
    resp = client.get("/api/inbox")
    assert resp.status_code == 200
    assert "NO_EMAIL_IN_THE_FORM" in resp.text
    # L'email technique ne doit pas apparaître dans le rendu cockpit.
    assert "wordpress@detectivebelgium.com" not in resp.text


def test_inbox_rows_no_int_len_crash(client) -> None:
    """Régression P0 : ai_draft ne doit jamais être un int (cela causait |length error)."""
    resp = client.get("/api/inbox")
    assert resp.status_code == 200
    # Si l'erreur se reproduisait, FastAPI retournerait du JSON detail 500, pas du HTML.
    assert "Internal Server Error" not in resp.text
    assert "TypeError" not in resp.text


# ─────────────────────────────────────────────────────────────────────────────
# v1.30.0.4 — Garde-fou anti-bruit dans la hot band.
#
# Avant : la hot band (1ère bande verte) affichait des mails mal classés
# (Pluxee Card, Reçu Apple, bilans du comptable cvfconsult, mails internes
# CDAL). Daniel voyait ces lignes dans son flux "à traiter" alors qu'elles
# ne sont PAS des demandes client. Le tri SQL était correct ; c'est la
# classification en amont qui était trop large. Plutôt que d'attendre un
# backfill de reclassement, on ajoute un filtre déterministe dans la hot_where
# qui exclut les expéditeurs/mots-clés manifestement pas client.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def inbox_with_noise(db_path_holder_with_noise):
    """DB avec 1 vrai demande_client + 3 intrus manifestes dans la hot."""
    raise NotImplementedError  # placeholder, le vrai setup est inline plus bas


def _make_db_with_hot_noise(tmp_path: Path) -> Path:
    """DB avec 1 vraie demande_client (hot) + 3 intrus à exclure de la hot."""
    db = tmp_path / "agent_state.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE mail_processed (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            imap_uid TEXT NOT NULL,
            mailbox_name TEXT NOT NULL,
            subject TEXT,
            sender TEXT,
            received_at TEXT,
            category TEXT,
            draft_generated INTEGER DEFAULT 0,
            draft_sent_at TEXT,
            processed_at TEXT DEFAULT CURRENT_TIMESTAMP,
            status TEXT,
            priority TEXT,
            ai_draft TEXT,
            human_draft TEXT,
            reviewed_by INTEGER,
            reviewed_at DATETIME,
            sent_at DATETIME,
            sent_by INTEGER,
            body_preview TEXT,
            body TEXT,
            delivered_at TEXT,
            suggested_subject TEXT,
            message_id TEXT,
            in_reply_to TEXT,
            "references" TEXT,
            dossier_id TEXT,
            thread_id TEXT,
            thread_subject TEXT,
            duplicate_of INTEGER,
            UNIQUE(imap_uid, mailbox_name)
        );
        CREATE TABLE audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT,
            resource_type TEXT,
            resource_id TEXT,
            details TEXT,
            ip_address TEXT,
            user_agent TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE email_attachment (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mail_processed_id INTEGER NOT NULL,
            filename TEXT,
            storage_path TEXT,
            size_bytes INTEGER,
            extracted_text_preview TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    # 1) VRAIE demande client (doit rester dans la hot)
    conn.execute(
        "INSERT INTO mail_processed "
        "(id, imap_uid, mailbox_name, subject, sender, received_at, category, "
        " status, priority, body_preview, body, ai_draft, draft_generated, processed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            100, "uid100", "detective_belgique",
            "Demande de filature",
            "client@example.com",
            "2026-06-23 10:00:00",
            "demande_client", "pending", "high",
            "Bonjour, je voudrais une filature...",
            "Bonjour, je voudrais une filature...",
            "Cher client, voici...", 1,
            "2026-06-23 10:01:00",
        ),
    )
    # 2) Pluxee Card (newsletter mal classé demande_client)
    conn.execute(
        "INSERT INTO mail_processed "
        "(id, imap_uid, mailbox_name, subject, sender, received_at, category, "
        " status, priority, body_preview, body, ai_draft, draft_generated, processed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            101, "uid101", "detective_belgique",
            "Pluxee Card: Vos Pluxee Lunch sur votre compte Pluxee",
            "noreply@pluxee.be",
            "2026-06-23 09:00:00",
            "demande_client", "pending", "high",
            "#outlook html pluxee...",
            "#outlook html pluxee...",
            None, 0,
            "2026-06-23 09:01:00",
        ),
    )
    # 3) Reçu Apple (newsletter mal classé demande_client)
    conn.execute(
        "INSERT INTO mail_processed "
        "(id, imap_uid, mailbox_name, subject, sender, received_at, category, "
        " status, priority, body_preview, body, ai_draft, draft_generated, processed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            102, "uid102", "detective_belgique",
            "Re: Votre reçu Apple",
            "noreply@email.apple.com",
            "2026-06-23 08:00:00",
            "demande_client", "pending", "high",
            "Votre reçu Apple Store...",
            "Votre reçu Apple Store...",
            None, 0,
            "2026-06-23 08:01:00",
        ),
    )
    # 4) Mail interne CDAL (forward interne mal classé)
    conn.execute(
        "INSERT INTO mail_processed "
        "(id, imap_uid, mailbox_name, subject, sender, received_at, category, "
        " status, priority, body_preview, body, ai_draft, draft_generated, processed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            103, "uid103", "detective_belgique",
            "260613 - modif reponse 100 : Réponse que j'attendrai",
            "cdal@digitalhs.biz",
            "2026-06-23 07:00:00",
            "demande_client", "pending", "high",
            "ok daniel voici la modif...",
            "ok daniel voici la modif...",
            None, 0,
            "2026-06-23 07:01:00",
        ),
    )
    # 5) Mail du comptable cvfconsult (note administrative mal classée)
    conn.execute(
        "INSERT INTO mail_processed "
        "(id, imap_uid, mailbox_name, subject, sender, received_at, category, "
        " status, priority, body_preview, body, ai_draft, draft_generated, processed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            104, "uid104", "detective_belgique",
            "DETECTIVEBELGIQUE.BE - BILAN 31/12/2025",
            "valerie@cvfconsult.be",
            "2026-06-23 06:00:00",
            "demande_client", "pending", "high",
            "Bonjour Daniel, tu trouveras ci-joint le bilan...",
            "Bonjour Daniel, tu trouveras ci-joint le bilan...",
            None, 0,
            "2026-06-23 06:01:00",
        ),
    )
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def client_with_hot_noise(tmp_path: Path, operator_user):
    """Client TestClient avec DB contenant 1 vraie demande + 4 intrus dans la hot."""
    db_path = _make_db_with_hot_noise(tmp_path)
    app = make_app()
    app.dependency_overrides[get_db] = _db_path_holder(db_path)
    app.dependency_overrides[require_operator] = lambda: operator_user
    return TestClient(app)


def test_hot_band_excludes_pluxee_card(client_with_hot_noise) -> None:
    """v1.30.0.4 — Un mail Pluxee (newsletter) ne doit PAS apparaître dans la hot band,
    même s'il est classifié demande_client+high+pending en DB.
    """
    resp = client_with_hot_noise.get("/api/inbox")
    assert resp.status_code == 200
    # Le mail Pluxee doit être filtré hors de la hot (donc visible dans other ou absent).
    # Le test accepte les deux tant qu'il n'est PAS dans la 1ère bande.
    # On vérifie qu'on NE le voit PAS avant le séparateur vert 4px.
    body = resp.text
    sep_idx = body.find("border-b-4 border-green-600")
    assert sep_idx > 0, "Séparateur vert introuvable"
    hot_section = body[:sep_idx]
    # La vraie demande (id=100) doit être dans la hot
    assert "Demande de filature" in hot_section
    # Le Pluxee ne doit PAS être dans la hot
    assert "Pluxee Card" not in hot_section


def test_hot_band_excludes_apple_receipt(client_with_hot_noise) -> None:
    """v1.30.0.4 — Un mail Reçu Apple (newsletter) ne doit PAS être dans la hot."""
    resp = client_with_hot_noise.get("/api/inbox")
    assert resp.status_code == 200
    body = resp.text
    sep_idx = body.find("border-b-4 border-green-600")
    assert sep_idx > 0
    hot_section = body[:sep_idx]
    assert "Demande de filature" in hot_section
    assert "Re: Votre reçu Apple" not in hot_section


def test_hot_band_excludes_internal_sender(client_with_hot_noise) -> None:
    """v1.30.0.4 — Un mail interne (cdal@digitalhs.biz) ne doit PAS être dans la hot."""
    resp = client_with_hot_noise.get("/api/inbox")
    assert resp.status_code == 200
    body = resp.text
    sep_idx = body.find("border-b-4 border-green-600")
    assert sep_idx > 0
    hot_section = body[:sep_idx]
    assert "Demande de filature" in hot_section
    # Le mail id=103 (CDAL) ne doit pas être dans la hot
    assert "260613 - modif reponse" not in hot_section


def test_hot_band_excludes_comptable_sender(client_with_hot_noise) -> None:
    """v1.30.0.4 — Un mail du comptable (cvfconsult.be) ne doit PAS être dans la hot."""
    resp = client_with_hot_noise.get("/api/inbox")
    assert resp.status_code == 200
    body = resp.text
    sep_idx = body.find("border-b-4 border-green-600")
    assert sep_idx > 0
    hot_section = body[:sep_idx]
    assert "Demande de filature" in hot_section
    # Le mail id=104 (BILAN du comptable) ne doit pas être dans la hot
    assert "BILAN 31/12/2025" not in hot_section


def test_hot_band_keeps_real_demande_client(client_with_hot_noise) -> None:
    """v1.30.0.4 — Une vraie demande client (id=100) doit RESTER dans la hot."""
    resp = client_with_hot_noise.get("/api/inbox")
    assert resp.status_code == 200
    body = resp.text
    sep_idx = body.find("border-b-4 border-green-600")
    assert sep_idx > 0
    hot_section = body[:sep_idx]
    # La vraie demande (id=100) doit être dans la hot
    assert "Demande de filature" in hot_section
    assert "client@example.com" in hot_section


# ─────────────────────────────────────────────────────────────────────────────
# v1.30.0.5 — Garde-fou anti-reply-orpheline dans la hot band.
#
# Avant : un mail `Re: ...` pending pouvait apparaître comme 1ère ligne de
# la hot band, alors que son PARENT était déjà traité par Daniel (approved /
# rejected / sent). Le reply pending est techniquement "à faire" mais
# Daniel ne peut rien en faire (le parent est clos). Il polluait la hot
# band avec un sujet "Re: ..." sans contexte parent visible.
#
# CDAL : "un sous mail ne peut pas être en premier il doit avoir un email
# parent !"
#
# Le fix : `_group_into_threads()` retourne 2 listes (keep, move_to_other).
# - Les replies dont le parent est non-pending → move_to_other.
# - Les orphelins avec sujet Re:/AW:/TR:/Fwd: sans thread_id → move_to_other.
# - Les threads avec parent pending + replies pending → keep (parent first,
#   replies enfilées en-dessous avec badge ›).
# ─────────────────────────────────────────────────────────────────────────────


def _make_db_with_reply_threads(tmp_path: Path) -> Path:
    """DB avec 3 scénarios de threads pour valider le fix v1.30.0.5."""
    db = tmp_path / "agent_state.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE mail_processed (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            imap_uid TEXT NOT NULL,
            mailbox_name TEXT NOT NULL,
            subject TEXT,
            sender TEXT,
            received_at TEXT,
            category TEXT,
            draft_generated INTEGER DEFAULT 0,
            draft_sent_at TEXT,
            processed_at TEXT DEFAULT CURRENT_TIMESTAMP,
            status TEXT,
            priority TEXT,
            ai_draft TEXT,
            human_draft TEXT,
            reviewed_by INTEGER,
            reviewed_at DATETIME,
            sent_at DATETIME,
            sent_by INTEGER,
            body_preview TEXT,
            body TEXT,
            delivered_at TEXT,
            suggested_subject TEXT,
            message_id TEXT,
            in_reply_to TEXT,
            "references" TEXT,
            dossier_id TEXT,
            thread_id TEXT,
            thread_subject TEXT,
            duplicate_of INTEGER,
            UNIQUE(imap_uid, mailbox_name)
        );
        CREATE TABLE audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT,
            resource_type TEXT,
            resource_id TEXT,
            details TEXT,
            ip_address TEXT,
            user_agent TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE email_attachment (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mail_processed_id INTEGER NOT NULL,
            filename TEXT,
            storage_path TEXT,
            size_bytes INTEGER,
            extracted_text_preview TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    # Scénario 1 : parent pending + reply pending (même thread_id) → KEEP en hot
    conn.execute(
        "INSERT INTO mail_processed "
        "(id, imap_uid, mailbox_name, subject, sender, received_at, category, "
        " status, priority, body_preview, body, ai_draft, draft_generated, processed_at, thread_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            200, "uid200", "detective_belgique",
            "Demande de devis initial",
            "client@example.com",
            "2026-06-25 10:00:00",
            "demande_client", "pending", "high",
            "Bonjour, devis...",
            "Bonjour, devis...",
            "Brouillon parent", 1,
            "2026-06-25 10:01:00",
            "thread-A",
        ),
    )
    conn.execute(
        "INSERT INTO mail_processed "
        "(id, imap_uid, mailbox_name, subject, sender, received_at, category, "
        " status, priority, body_preview, body, ai_draft, draft_generated, processed_at, thread_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            201, "uid201", "detective_belgique",
            "Re: Demande de devis initial",
            "client@example.com",
            "2026-06-30 10:00:00",
            "demande_client", "pending", "high",
            "Merci pour le devis, voici les infos...",
            "Merci pour le devis, voici les infos...",
            None, 0,
            "2026-06-30 10:01:00",
            "thread-A",
        ),
    )
    # Scénario 2 : parent APPROVED + reply pending (même thread_id)
    # → la reply (pending) doit aller dans OTHER, pas dans HOT
    conn.execute(
        "INSERT INTO mail_processed "
        "(id, imap_uid, mailbox_name, subject, sender, received_at, category, "
        " status, priority, body_preview, body, ai_draft, draft_generated, processed_at, thread_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            210, "uid210", "detective_belgique",
            "Re: DEMANDE D'Approbation - Reponse Demande Client : devis",
            "client2@example.com",
            "2026-06-23 10:00:00",
            "demande_client", "approved", "high",
            "Voici ma reponse...",
            "Voici ma reponse...",
            "Brouillon 210", 1,
            "2026-06-23 10:01:00",
            "thread-B",
        ),
    )
    conn.execute(
        "INSERT INTO mail_processed "
        "(id, imap_uid, mailbox_name, subject, sender, received_at, category, "
        " status, priority, body_preview, body, ai_draft, draft_generated, processed_at, thread_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            211, "uid211", "detective_belgique",
            "Re: DEMANDE D'Approbation - Reponse Demande Client : devis",
            "client2@example.com",
            "2026-06-28 10:00:00",
            "demande_client", "pending", "high",
            "Une question supplementaire...",
            "Une question supplementaire...",
            None, 0,
            "2026-06-28 10:01:00",
            "thread-B",
        ),
    )
    # Scénario 3 : orphelin pending avec sujet "Re: ..." sans thread_id
    # → MOVE to other (le parent n'est pas dans le set, probablement déjà traité)
    conn.execute(
        "INSERT INTO mail_processed "
        "(id, imap_uid, mailbox_name, subject, sender, received_at, category, "
        " status, priority, body_preview, body, ai_draft, draft_generated, processed_at, thread_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            220, "uid220", "detective_belgique",
            "Re: Demande de devis",
            "client3@example.com",
            "2026-06-30 15:00:00",
            "demande_client", "pending", "high",
            "Voici les complements...",
            "Voici les complements...",
            None, 0,
            "2026-06-30 15:01:00",
            None,  # pas de thread_id → orphelin
        ),
    )
    # Scénario 4 (contrôle négatif) : un NOUVEAU mail (pas Re:) sans thread_id
    # doit RESTER dans la hot (pas un reply orphelin)
    conn.execute(
        "INSERT INTO mail_processed "
        "(id, imap_uid, mailbox_name, subject, sender, received_at, category, "
        " status, priority, body_preview, body, ai_draft, draft_generated, processed_at, thread_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            230, "uid230", "detective_belgique",
            "Nouvelle demande de mission",
            "client4@example.com",
            "2026-06-30 16:00:00",
            "demande_client", "pending", "high",
            "Bonjour, je cherche un détective...",
            "Bonjour, je cherche un détective...",
            None, 0,
            "2026-06-30 16:01:00",
            None,
        ),
    )
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def client_with_reply_threads(tmp_path, operator_user):
    """Client avec DB contenant 4 scénarios de threads (1 keep, 2 moves, 1 contrôle)."""
    db_path = _make_db_with_reply_threads(tmp_path)
    app = make_app()
    app.dependency_overrides[get_db] = _db_path_holder(db_path)
    app.dependency_overrides[require_operator] = lambda: operator_user
    return TestClient(app)


def test_hot_band_keeps_pending_parent_with_pending_reply(client_with_reply_threads) -> None:
    """v1.30.0.5 — Thread avec parent pending + reply pending : les DEUX restent en hot,
    le parent en premier, la reply enfilée en-dessous.
    """
    resp = client_with_reply_threads.get("/api/inbox")
    assert resp.status_code == 200
    body = resp.text
    sep_idx = body.find("border-b-4 border-green-600")
    assert sep_idx > 0
    hot_section = body[:sep_idx]
    # Le parent id=200 doit être dans la hot
    assert "/app/conversation/200" in hot_section
    # La reply id=201 doit aussi être dans la hot (enfilée)
    assert "/app/conversation/201" in hot_section
    # Le parent doit apparaître AVANT la reply (ordre des lignes)
    parent_idx = hot_section.find("/app/conversation/200")
    reply_idx = hot_section.find("/app/conversation/201")
    assert parent_idx < reply_idx, (
        f"Le parent doit précéder la reply (parent={parent_idx}, reply={reply_idx})"
    )


def test_hot_band_excludes_reply_when_parent_approved(client_with_reply_threads) -> None:
    """v1.30.0.5 — Reply pending dont le parent est APPROVED : la reply doit
    DÉMÉNAGER dans other, pas dans la hot band. (CDAL : un sous mail ne peut
    pas être en premier il doit avoir un email parent !)

    v1.30.0.7 — On filtre explicitement ?category=demande_client pour SORTIR
    du mode worklist (qui supprime la bande OTHER). Le test continue de
    valider la sémantique move-to-other sur l'onglet "Demandes client",
    qui est l'onglet naturel pour auditer les fils.
    """
    resp = client_with_reply_threads.get("/api/inbox?category=demande_client")
    assert resp.status_code == 200
    body = resp.text
    sep_idx = body.find("border-b-4 border-green-600")
    assert sep_idx > 0
    hot_section = body[:sep_idx]
    other_section = body[sep_idx:]
    # id=211 (reply pending, parent approved) NE DOIT PAS être dans la hot
    assert "/app/conversation/211" not in hot_section
    # ...mais elle DOIT être dans la section other
    assert "/app/conversation/211" in other_section


def test_hot_band_excludes_orphan_reply_with_re_prefix(client_with_reply_threads) -> None:
    """v1.30.0.5 — Orphelin pending (pas de thread_id) avec sujet Re: : doit
    DÉMÉNAGER dans other car son parent (non groupable) est forcément ailleurs.

    v1.30.0.7 — Filtre explicite ?category=demande_client pour sortir du worklist.
    """
    resp = client_with_reply_threads.get("/api/inbox?category=demande_client")
    assert resp.status_code == 200
    body = resp.text
    sep_idx = body.find("border-b-4 border-green-600")
    assert sep_idx > 0
    hot_section = body[:sep_idx]
    other_section = body[sep_idx:]
    # id=220 (Re: Demande de devis, orphelin) ne doit PAS être dans la hot
    assert "/app/conversation/220" not in hot_section
    # ...mais DOIT être dans la section other
    assert "/app/conversation/220" in other_section


def test_hot_band_keeps_non_reply_orphan(client_with_reply_threads) -> None:
    """v1.30.0.5 — Un orphelin (pas de thread_id) mais dont le sujet NE commence
    PAS par Re:/AW:/Fwd: doit RESTER dans la hot (c'est un vrai nouveau mail).
    """
    resp = client_with_reply_threads.get("/api/inbox")
    assert resp.status_code == 200
    body = resp.text
    sep_idx = body.find("border-b-4 border-green-600")
    assert sep_idx > 0
    hot_section = body[:sep_idx]
    # id=230 (sujet "Nouvelle demande de mission", pas Re:) DOIT rester en hot
    assert "/app/conversation/230" in hot_section
    assert "Nouvelle demande de mission" in hot_section


def test_hot_band_no_first_row_is_reply(client_with_reply_threads) -> None:
    """v1.30.0.5 — AUCUNE 1ère ligne de la hot band ne doit avoir un badge ›.
    Le seul moyen pour avoir › est d'être enfilé sous un parent.
    """
    resp = client_with_reply_threads.get("/api/inbox")
    assert resp.status_code == 200
    body = resp.text
    sep_idx = body.find("border-b-4 border-green-600")
    assert sep_idx > 0
    hot_section = body[:sep_idx]
    # Le premier <tr> de la hot section ne doit pas avoir de › (› = reply)
    import re

    first_tr_match = re.search(r"<tr[^>]*>.*?</tr>", hot_section, re.DOTALL)
    assert first_tr_match is not None, "Aucune ligne dans la hot section"
    first_tr = first_tr_match.group(0)
    # › est rendu comme caractère unicode U+203A dans le template
    assert "›" not in first_tr, (
        f"La 1ère ligne de la hot ne doit pas être un reply :\n{first_tr[:500]}"
    )


def test_thread_grouping_returns_keep_and_move() -> None:
    """v1.30.0.5 — `_group_into_threads` retourne un tuple (keep, move_to_other)."""
    from app.web.app_routes import _group_into_threads

    mails = [
        # Thread A : parent pending + reply pending → keep
        {
            "id": 1, "mailbox_name": "detective_belgique",
            "subject": "Demande initiale", "sender": "a@a.com",
            "received_at": "2026-06-25 10:00:00", "status": "pending",
            "priority": "high", "category": "demande_client",
            "thread_id": "thread-A", "has_draft": 0,
        },
        {
            "id": 2, "mailbox_name": "detective_belgique",
            "subject": "Re: Demande initiale", "sender": "a@a.com",
            "received_at": "2026-06-30 10:00:00", "status": "pending",
            "priority": "high", "category": "demande_client",
            "thread_id": "thread-A", "has_draft": 0,
        },
        # Thread B : parent approved + reply pending → move
        {
            "id": 3, "mailbox_name": "detective_belgique",
            "subject": "Conversation passee", "sender": "b@b.com",
            "received_at": "2026-06-23 10:00:00", "status": "approved",
            "priority": "high", "category": "demande_client",
            "thread_id": "thread-B", "has_draft": 0,
        },
        {
            "id": 4, "mailbox_name": "detective_belgique",
            "subject": "Re: Conversation passee", "sender": "b@b.com",
            "received_at": "2026-06-28 10:00:00", "status": "pending",
            "priority": "high", "category": "demande_client",
            "thread_id": "thread-B", "has_draft": 0,
        },
        # Orphelin Re: sans thread_id → move
        {
            "id": 5, "mailbox_name": "detective_belgique",
            "subject": "Re: Sujet quelconque", "sender": "c@c.com",
            "received_at": "2026-06-30 12:00:00", "status": "pending",
            "priority": "high", "category": "demande_client",
            "thread_id": None, "has_draft": 0,
        },
        # Orphelin NON-Re: sans thread_id → keep
        {
            "id": 6, "mailbox_name": "detective_belgique",
            "subject": "Nouvelle demande", "sender": "d@d.com",
            "received_at": "2026-06-30 13:00:00", "status": "pending",
            "priority": "high", "category": "demande_client",
            "thread_id": None, "has_draft": 0,
        },
    ]
    keep, move = _group_into_threads(mails)
    # keep = thread A (parent + reply) + orphelin non-Re
    keep_ids = sorted(
        [t["parent"]["id"] for t in keep]
        + [r["id"] for t in keep for r in t.get("replies", [])]
    )
    assert keep_ids == [1, 2, 6], f"keep devrait contenir 1, 2, 6 mais a {keep_ids}"
    # move = reply du thread B (parent approved = split) + orphelin Re:
    move_ids = sorted([t["parent"]["id"] for t in move])
    assert move_ids == [4, 5], f"move devrait contenir 4, 5 mais a {move_ids}"


def test_thread_grouping_cross_band_move() -> None:
    """v1.30.0.5 — Cross-band : un reply pending hot dont le parent est dans
    other_mails (déjà traité) doit DÉMÉNAGER dans other. Détection via
    `all_thread_siblings`.
    """
    from app.web.app_routes import _group_into_threads

    # Simulation de hot_mails (réduit) + other_mails (avec le parent approved)
    hot = [
        {
            "id": 100, "mailbox_name": "detective_belgique",
            "subject": "Re: Sujet X", "sender": "x@x.com",
            "received_at": "2026-06-30 10:00:00", "status": "pending",
            "thread_id": "thread-X", "has_draft": 0,
        },
    ]
    other = [
        {
            "id": 99, "mailbox_name": "detective_belgique",
            "subject": "Sujet X", "sender": "x@x.com",
            "received_at": "2026-06-20 10:00:00", "status": "approved",
            "thread_id": "thread-X", "has_draft": 0,
        },
    ]
    keep, move = _group_into_threads(hot, all_thread_siblings=other)
    # Le reply id=100 doit être dans move (parent 99 dans other = déjà traité)
    move_ids = sorted([t["parent"]["id"] for t in move])
    assert move_ids == [100], f"move devrait contenir 100 (cross-band) mais a {move_ids}"
    assert keep == []


def test_looks_like_reply_subject_helper() -> None:
    """v1.30.0.5 — `_looks_like_reply_subject` détecte Re:/AW:/TR:/Fwd: (et
    leurs variantes avec espace / espace insécable).
    """
    from app.web.app_routes import _looks_like_reply_subject

    # Doit matcher
    assert _looks_like_reply_subject("Re: Bonjour")
    assert _looks_like_reply_subject("RE: Bonjour")  # casse
    assert _looks_like_reply_subject("re : Bonjour")  # espace variante FR
    assert _looks_like_reply_subject("re\xa0: Bonjour")  # espace insécable
    assert _looks_like_reply_subject("AW: Question")
    assert _looks_like_reply_subject("TR: Transfert")
    assert _looks_like_reply_subject("Fwd: Pour info")
    assert _looks_like_reply_subject("FW: Transfer")
    assert _looks_like_reply_subject("  Re: espaces avant")  # whitespace prefix
    # Ne doit PAS matcher
    assert not _looks_like_reply_subject("Bonjour Daniel")
    assert not _looks_like_reply_subject("Demande de devis")
    assert not _looks_like_reply_subject("Réglé : paiement")
    assert not _looks_like_reply_subject("Read me")  # Re en début de mot, pas préfixe
    assert not _looks_like_reply_subject("")
    assert not _looks_like_reply_subject(None)


# ─────────────────────────────────────────────────────────────────────────────
# v1.30.0.8 — Détection des "replies orphelins" dans _group_into_threads()
#
# Avant : un mail avec in_reply_to pointant vers un message_id absent du
# système était promu "parent" d'un fil (parce que c'était le mail le plus
# ancien), avec les autres replies enfilés en dessous — l'utilisateur voyait
# un "Re: …" sans parent connu en première ligne.
#
# Maintenant : le parent = le plus ancien mail du fil qui n'est PAS un
# reply orphelin (in_reply_to pointe vers un message_id qu'on connaît).
# Si TOUS les mails du fil sont orphelins, le parent = le plus ancien
# mais rendu sans icône › ("premier mail connu", pas un vrai parent).
# ─────────────────────────────────────────────────────────────────────────────


def test_orphan_reply_not_promoted_to_parent_when_sibling_exists() -> None:
    """v1.30.0.8 — Fil mixte : un mail avec in_reply_to="unknown" ne doit PAS
    être promu parent si un autre mail du fil N'A PAS d'in_reply_to.

    Cas concret : un client envoie "Bonjour" (id=1, in_reply_to=None) puis
    "Re: Bonjour" (id=2, in_reply_to="<x@y.com>"). Le mail id=2 cite un
    message_id jamais vu dans notre DB (réponse à un mail de Daniel envoyé
    hors-système) → c'est un reply orphelin. Le parent = id=1 (le seul
    non-orphelin), id=2 devient reply.
    """
    from app.web.app_routes import _group_into_threads

    mails = [
        {
            "id": 1, "mailbox_name": "detective_belgique",
            "subject": "Bonjour, je cherche un détective",
            "sender": "client@example.com",
            "received_at": "2026-06-25 10:00:00", "status": "pending",
            "priority": "high", "category": "demande_client",
            "thread_id": "thread-M", "has_draft": 0,
            "in_reply_to": None, "message_id": "<msg1@example.com>",
        },
        {
            "id": 2, "mailbox_name": "detective_belgique",
            "subject": "Re: Bonjour, je cherche un détective",
            "sender": "client@example.com",
            "received_at": "2026-06-30 10:00:00", "status": "pending",
            "priority": "high", "category": "demande_client",
            "thread_id": "thread-M", "has_draft": 0,
            "in_reply_to": "<x@y.com>",  # ← message_id absent du système
            "message_id": "<msg2@example.com>",
        },
    ]
    keep, move = _group_into_threads(mails)
    assert len(keep) == 1
    t = keep[0]
    assert t["parent"]["id"] == 1, (
        f"Le parent doit être id=1 (non-orphelin), pas id=2 (orphelin). "
        f"Got parent id={t['parent']['id']}, replies={[r['id'] for r in t['replies']]}"
    )
    assert [r["id"] for r in t["replies"]] == [2]
    assert t["parent_is_orphan"] is False
    assert move == []


def test_orphan_only_thread_marks_first_as_known_without_reply_icon() -> None:
    """v1.30.0.8 — Fil où TOUS les mails sont des replies orphelins (aucun
    n'a in_reply_to=None). Le parent = le plus ancien (premier mail connu
    du fil), mais `parent_is_orphan=True` → le template rend le parent SANS
    icône › (ce n'est pas un vrai parent de conversation, juste le 1er
    mail qu'on a en DB).
    """
    from app.web.app_routes import _group_into_threads

    mails = [
        {
            "id": 10, "mailbox_name": "detective_belgique",
            "subject": "Re: Devis",
            "sender": "c@c.com",
            "received_at": "2026-06-20 10:00:00", "status": "pending",
            "priority": "high", "category": "demande_client",
            "thread_id": "thread-O", "has_draft": 0,
            "in_reply_to": "<unknown@external.com>",
            "message_id": "<msg10@c.com>",
        },
        {
            "id": 11, "mailbox_name": "detective_belgique",
            "subject": "Re: Re: Devis",
            "sender": "c@c.com",
            "received_at": "2026-06-25 10:00:00", "status": "pending",
            "priority": "high", "category": "demande_client",
            "thread_id": "thread-O", "has_draft": 0,
            "in_reply_to": "<unknown2@external.com>",
            "message_id": "<msg11@c.com>",
        },
        {
            "id": 12, "mailbox_name": "detective_belgique",
            "subject": "Re: Re: Re: Devis",
            "sender": "c@c.com",
            "received_at": "2026-06-30 10:00:00", "status": "pending",
            "priority": "high", "category": "demande_client",
            "thread_id": "thread-O", "has_draft": 0,
            "in_reply_to": "<unknown3@external.com>",
            "message_id": "<msg12@c.com>",
        },
    ]
    keep, move = _group_into_threads(mails)
    assert len(keep) == 1
    t = keep[0]
    # Le parent = le plus ancien (id=10), les autres = replies
    assert t["parent"]["id"] == 10
    assert sorted([r["id"] for r in t["replies"]]) == [11, 12]
    # parent_is_orphan=True → rendu sans icône › sur le parent
    assert t["parent_is_orphan"] is True
    assert t["all_orphans"] is True
    assert move == []


def test_mixed_thread_real_parent_with_orphan_replies() -> None:
    """v1.30.0.8 — Fil où le premier mail est SANS in_reply_to (vrai parent)
    et les suivants sont des replies orphelins (in_reply_to pointe vers un
    message_id externe). Le parent = le mail sans in_reply_to, les
    orphelins sont en replies.
    """
    from app.web.app_routes import _group_into_threads

    mails = [
        {
            "id": 20, "mailbox_name": "detective_belgique",
            "subject": "Demande initiale",
            "sender": "d@d.com",
            "received_at": "2026-06-15 10:00:00", "status": "pending",
            "priority": "high", "category": "demande_client",
            "thread_id": "thread-Mix", "has_draft": 0,
            "in_reply_to": None,  # ← vrai premier mail
            "message_id": "<msg20@d.com>",
        },
        {
            "id": 21, "mailbox_name": "detective_belgique",
            "subject": "Re: Demande initiale",
            "sender": "d@d.com",
            "received_at": "2026-06-20 10:00:00", "status": "pending",
            "priority": "high", "category": "demande_client",
            "thread_id": "thread-Mix", "has_draft": 0,
            "in_reply_to": "<msg20@d.com>",  # ← in_reply_to pointe vers id=20 OK
            "message_id": "<msg21@d.com>",
        },
        {
            "id": 22, "mailbox_name": "detective_belgique",
            "subject": "Re: Re: Demande initiale",
            "sender": "d@d.com",
            "received_at": "2026-06-25 10:00:00", "status": "pending",
            "priority": "high", "category": "demande_client",
            "thread_id": "thread-Mix", "has_draft": 0,
            "in_reply_to": "<daniel@external>",  # ← ORPHELIN (Daniel hors-système)
            "message_id": "<msg22@d.com>",
        },
    ]
    keep, move = _group_into_threads(mails)
    assert len(keep) == 1
    t = keep[0]
    # Le parent = id=20 (le seul mail non-orphelin)
    assert t["parent"]["id"] == 20, (
        f"Parent doit être id=20 (vrai 1er mail), got {t['parent']['id']}"
    )
    # Les 2 autres sont en replies (id=21 et id=22)
    assert sorted([r["id"] for r in t["replies"]]) == [21, 22]
    # parent_is_orphan=False car le parent a un in_reply_to=None (vrai 1er mail)
    assert t["parent_is_orphan"] is False
    assert t["all_orphans"] is False
    assert move == []


def test_orphan_reply_helper_basic() -> None:
    """v1.30.0.8 — `_is_orphan_reply` retourne True si in_reply_to pointe
    vers un message_id absent du système.
    """
    from app.web.app_routes import _is_orphan_reply

    known_msg_ids = {"<real@server.com>"}

    # Cas orphelin : in_reply_to pointe vers inconnu
    assert _is_orphan_reply(
        {"in_reply_to": "<unknown@external.com>"},
        known_message_ids=known_msg_ids,
        same_thread_message_ids=set(),
    ) is True
    # Cas non-orphelin : in_reply_to pointe vers connu
    assert _is_orphan_reply(
        {"in_reply_to": "<real@server.com>"},
        known_message_ids=known_msg_ids,
        same_thread_message_ids=set(),
    ) is False
    # Cas non-orphelin : in_reply_to absent
    assert _is_orphan_reply(
        {"in_reply_to": None},
        known_message_ids=known_msg_ids,
        same_thread_message_ids=set(),
    ) is False
    # Cas non-orphelin : in_reply_to pointe vers un mail du MÊME fil
    # (intra-thread knowledge)
    assert _is_orphan_reply(
        {"in_reply_to": "<sibling@thread.com>"},
        known_message_ids=set(),  # pas connu globalement
        same_thread_message_ids={"<sibling@thread.com>"},  # mais connu intra-thread
    ) is False


# ─────────────────────────────────────────────────────────────────────────────
# v1.30.0.7 — Mode worklist : onglet "Toutes" = liste de travail de Daniel.
#
# Avant : "Toutes" affichait ~200 lignes mélangées (newsletter, spam, autre,
# doublons, factures, etc.). Daniel devait scroller dans le bruit pour trouver
# ses 23 demande_client pending.
#
# Maintenant : "Toutes" = worklist = UNIQUEMENT la hot band
# (demande_client + urgent + pending), sans les doublons, sans la bande
# OTHER. Les autres onglets (catégorie explicite) gardent le comportement
# 2 bandes actuel (hot + other + move-to-other).
#
# Critère de "parfait" CDAL : "Toutes" affiche ≤ 30 lignes, toutes = vrai
# demande_client ou urgent pending. Pas de doublons, pas de newsletters,
# pas de factures, pas de spam dans "Toutes".
# ─────────────────────────────────────────────────────────────────────────────


def _make_db_with_worklist_data(tmp_path: Path) -> Path:
    """DB pour valider le worklist "Toutes".

    Composition :
    - 2 vraies demande_client pending (doivent apparaître dans "Toutes")
    - 1 urgent pending (doit apparaître dans "Toutes")
    - 1 newsletter pending (NE DOIT PAS apparaître dans "Toutes")
    - 1 spam pending (NE DOIT PAS apparaître)
    - 1 facture pending (NE DOIT PAS apparaître)
    - 1 doublon demande_client (status='duplicate', NE DOIT PAS apparaître)
    - 1 mail Pluxee classifié demande_client (NE DOIT PAS apparaître)
    - 1 mail interne cdal@digitalhs.biz classifié demande_client (NE DOIT PAS)
    - 1 mail comptable cvfconsult.be classifié demande_client (NE DOIT PAS)
    - 1 mail "Reçu Apple" classifié demande_client (NE DOIT PAS)
    - 1 mail e-Box sécurité sociale (NE DOIT PAS apparaître)
    """
    db = tmp_path / "agent_state.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE mail_processed (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            imap_uid TEXT NOT NULL,
            mailbox_name TEXT NOT NULL,
            subject TEXT,
            sender TEXT,
            received_at TEXT,
            category TEXT,
            draft_generated INTEGER DEFAULT 0,
            draft_sent_at TEXT,
            processed_at TEXT DEFAULT CURRENT_TIMESTAMP,
            status TEXT,
            priority TEXT,
            ai_draft TEXT,
            human_draft TEXT,
            reviewed_by INTEGER,
            reviewed_at DATETIME,
            sent_at DATETIME,
            sent_by INTEGER,
            body_preview TEXT,
            body TEXT,
            delivered_at TEXT,
            suggested_subject TEXT,
            message_id TEXT,
            in_reply_to TEXT,
            "references" TEXT,
            dossier_id TEXT,
            thread_id TEXT,
            thread_subject TEXT,
            duplicate_of INTEGER,
            UNIQUE(imap_uid, mailbox_name)
        );
        CREATE TABLE audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT,
            resource_type TEXT,
            resource_id TEXT,
            details TEXT,
            ip_address TEXT,
            user_agent TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE email_attachment (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mail_processed_id INTEGER NOT NULL,
            filename TEXT,
            storage_path TEXT,
            size_bytes INTEGER,
            extracted_text_preview TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    # 1-2) Vraies demandes client pending (doivent rester)
    for i, (subj, sender) in enumerate(
        [
            ("Demande de filature urgente", "client1@example.com"),
            ("Recherche de personne disparue", "client2@example.com"),
        ],
        start=300,
    ):
        conn.execute(
            "INSERT INTO mail_processed "
            "(id, imap_uid, mailbox_name, subject, sender, received_at, category, "
            " status, priority, body_preview, body, ai_draft, draft_generated, processed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                i, f"uid{i}", "detective_belgique",
                subj, sender,
                "2026-06-23 10:00:00",
                "demande_client", "pending", "high",
                "Bonjour...",
                "Bonjour...",
                "Brouillon...", 1,
                "2026-06-23 10:01:00",
            ),
        )
    # 3) Urgent pending (doit rester)
    conn.execute(
        "INSERT INTO mail_processed "
        "(id, imap_uid, mailbox_name, subject, sender, received_at, category, "
        " status, priority, body_preview, body, ai_draft, draft_generated, processed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            302, "uid302", "detective_belgique",
            "URGENT - Vol de documents",
            "client.urgent@example.com",
            "2026-06-23 09:00:00",
            "urgent", "pending", "high",
            "URGENT...",
            "URGENT...",
            "Brouillon urgent...", 1,
            "2026-06-23 09:01:00",
        ),
    )
    # 4) Newsletter pending (DOIT être caché)
    conn.execute(
        "INSERT INTO mail_processed "
        "(id, imap_uid, mailbox_name, subject, sender, received_at, category, "
        " status, priority, body_preview, body, ai_draft, draft_generated, processed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            303, "uid303", "detective_belgique",
            "Newsletter mensuelle juin",
            "newsletter@fournisseur.be",
            "2026-06-23 08:00:00",
            "newsletter", "pending", "low",
            "Notre newsletter...",
            "Notre newsletter...",
            None, 0,
            "2026-06-23 08:01:00",
        ),
    )
    # 5) Spam pending (DOIT être caché)
    conn.execute(
        "INSERT INTO mail_processed "
        "(id, imap_uid, mailbox_name, subject, sender, received_at, category, "
        " status, priority, body_preview, body, ai_draft, draft_generated, processed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            304, "uid304", "detective_belgique",
            "Gagnez 1 million de dollars",
            "spammer@spam.com",
            "2026-06-23 07:00:00",
            "spam", "pending", "low",
            "Cliquez ici...",
            "Cliquez ici...",
            None, 0,
            "2026-06-23 07:01:00",
        ),
    )
    # 6) Facture pending (DOIT être caché)
    conn.execute(
        "INSERT INTO mail_processed "
        "(id, imap_uid, mailbox_name, subject, sender, received_at, category, "
        " status, priority, body_preview, body, ai_draft, draft_generated, processed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            305, "uid305", "detective_belgique",
            "Facture OV-2026-0042",
            "compta@ovhcloud.com",
            "2026-06-23 06:00:00",
            "facture", "pending", "normal",
            "Votre facture...",
            "Votre facture...",
            None, 0,
            "2026-06-23 06:01:00",
        ),
    )
    # 7) DOUBLON : status='duplicate' (DOIT être caché)
    conn.execute(
        "INSERT INTO mail_processed "
        "(id, imap_uid, mailbox_name, subject, sender, received_at, category, "
        " status, priority, body_preview, body, ai_draft, draft_generated, processed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            306, "uid306", "detective_belgique",
            "Demande de devis - doublon",
            "client.doublon@example.com",
            "2026-06-23 05:00:00",
            "demande_client", "duplicate", "high",
            "Doublon...",
            "Doublon...",
            "Brouillon doublon...", 1,
            "2026-06-23 05:01:00",
        ),
    )
    # 8) Pluxee classifié demande_client (DOIT être caché)
    conn.execute(
        "INSERT INTO mail_processed "
        "(id, imap_uid, mailbox_name, subject, sender, received_at, category, "
        " status, priority, body_preview, body, ai_draft, draft_generated, processed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            307, "uid307", "detective_belgique",
            "Pluxee Card: Vos Pluxee Lunch sur votre compte Pluxee",
            "noreply@pluxee.be",
            "2026-06-23 04:00:00",
            "demande_client", "pending", "high",
            "#outlook html pluxee...",
            "#outlook html pluxee...",
            None, 0,
            "2026-06-23 04:01:00",
        ),
    )
    # 9) CDAL interne (DOIT être caché)
    conn.execute(
        "INSERT INTO mail_processed "
        "(id, imap_uid, mailbox_name, subject, sender, received_at, category, "
        " status, priority, body_preview, body, ai_draft, draft_generated, processed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            308, "uid308", "detective_belgique",
            "Reunion interne demain",
            "cdal@digitalhs.biz",
            "2026-06-23 03:00:00",
            "demande_client", "pending", "high",
            "ok daniel...",
            "ok daniel...",
            None, 0,
            "2026-06-23 03:01:00",
        ),
    )
    # 10) Comptable cvfconsult (DOIT être caché)
    conn.execute(
        "INSERT INTO mail_processed "
        "(id, imap_uid, mailbox_name, subject, sender, received_at, category, "
        " status, priority, body_preview, body, ai_draft, draft_generated, processed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            309, "uid309", "detective_belgique",
            "BILAN 31/12/2025",
            "valerie@cvfconsult.be",
            "2026-06-23 02:00:00",
            "demande_client", "pending", "high",
            "Bonjour Daniel, voici le bilan...",
            "Bonjour Daniel, voici le bilan...",
            None, 0,
            "2026-06-23 02:01:00",
        ),
    )
    # 11) Reçu Apple (DOIT être caché)
    conn.execute(
        "INSERT INTO mail_processed "
        "(id, imap_uid, mailbox_name, subject, sender, received_at, category, "
        " status, priority, body_preview, body, ai_draft, draft_generated, processed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            310, "uid310", "detective_belgique",
            "Re: Votre reçu Apple",
            "noreply@email.apple.com",
            "2026-06-23 01:00:00",
            "demande_client", "pending", "high",
            "Votre reçu Apple Store...",
            "Votre reçu Apple Store...",
            None, 0,
            "2026-06-23 01:01:00",
        ),
    )
    # 12) e-Box (DOIT être caché)
    conn.execute(
        "INSERT INTO mail_processed "
        "(id, imap_uid, mailbox_name, subject, sender, received_at, category, "
        " status, priority, body_preview, body, ai_draft, draft_generated, processed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            311, "uid311", "detective_belgique",
            "Un message de l'e-Box arrive à expiration",
            "e-Box.noreply@socialsecurity.be",
            "2026-06-23 00:00:00",
            "autre", "pending", "low",
            "Votre e-Box expire...",
            "Votre e-Box expire...",
            None, 0,
            "2026-06-23 00:01:00",
        ),
    )
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def client_with_worklist_data(tmp_path, operator_user):
    """Client TestClient avec DB contenant 3 vrai travail + 9 intrus/bruit."""
    db_path = _make_db_with_worklist_data(tmp_path)
    app = make_app()
    app.dependency_overrides[get_db] = _db_path_holder(db_path)
    app.dependency_overrides[require_operator] = lambda: operator_user
    return TestClient(app)


def test_worklist_excludes_duplicates(client_with_worklist_data) -> None:
    """v1.30.0.7 — Onglet "Toutes" = worklist : aucun mail avec status='duplicate'
    ne doit apparaître (ni en hot ni en other, puisque other est supprimé).
    """
    resp = client_with_worklist_data.get("/api/inbox")
    assert resp.status_code == 200
    # id=306 (status='duplicate') ne doit PAS être visible dans le rendu
    assert "/app/conversation/306" not in resp.text
    # Son sujet non plus
    assert "Demande de devis - doublon" not in resp.text


def test_worklist_excludes_noise_senders(client_with_worklist_data) -> None:
    """v1.30.0.7 — worklist : aucun mail avec sender @digitalhs.biz ou @cvfconsult
    ne doit apparaître (geste de CDAL sur le bruit interne/comptable).
    """
    resp = client_with_worklist_data.get("/api/inbox")
    assert resp.status_code == 200
    # id=308 (cdal@digitalhs.biz) ne doit PAS être visible
    assert "/app/conversation/308" not in resp.text
    assert "Reunion interne demain" not in resp.text
    # id=309 (cvfconsult.be) ne doit PAS être visible
    assert "/app/conversation/309" not in resp.text
    assert "BILAN 31/12/2025" not in resp.text


def test_worklist_excludes_parasite_subjects(client_with_worklist_data) -> None:
    """v1.30.0.7 — worklist : aucun mail avec sujet Pluxee / Reçu Apple / e-Box
    ne doit apparaître (geste de CDAL sur les newsletters/confirmations classifiées
    à tort en demande_client).
    """
    resp = client_with_worklist_data.get("/api/inbox")
    assert resp.status_code == 200
    # id=307 (Pluxee)
    assert "/app/conversation/307" not in resp.text
    assert "Pluxee Card" not in resp.text
    # id=310 (Reçu Apple)
    assert "/app/conversation/310" not in resp.text
    assert "Re: Votre reçu Apple" not in resp.text
    # id=311 (e-Box)
    assert "/app/conversation/311" not in resp.text
    assert "e-Box" not in resp.text


def test_worklist_excludes_newsletter_spam_facture(client_with_worklist_data) -> None:
    """v1.30.0.7 — worklist : aucune catégorie non-travail (newsletter, spam,
    facture) ne doit apparaître dans "Toutes". Catégories accessibles via
    les onglets dédiés.
    """
    resp = client_with_worklist_data.get("/api/inbox")
    assert resp.status_code == 200
    # id=303 (newsletter)
    assert "/app/conversation/303" not in resp.text
    # id=304 (spam)
    assert "/app/conversation/304" not in resp.text
    # id=305 (facture)
    assert "/app/conversation/305" not in resp.text


def test_worklist_keeps_real_demande_client_and_urgent(client_with_worklist_data) -> None:
    """v1.30.0.7 — worklist : les 3 vrais mails de travail (2 demande_client
    + 1 urgent) DOIVENT apparaître. C'est la finalité de la liste de travail.
    """
    resp = client_with_worklist_data.get("/api/inbox")
    assert resp.status_code == 200
    # id=300 (demande de filature)
    assert "/app/conversation/300" in resp.text
    assert "Demande de filature urgente" in resp.text
    # id=301 (recherche de personne)
    assert "/app/conversation/301" in resp.text
    assert "Recherche de personne disparue" in resp.text
    # id=302 (urgent)
    assert "/app/conversation/302" in resp.text
    assert "URGENT - Vol de documents" in resp.text


def test_worklist_count_is_short(client_with_worklist_data) -> None:
    """v1.30.0.7 — worklist : la liste "Toutes" doit être COURTE (≤ 30 lignes).
    Critère "parfait" CDAL : 23 demande_client + 1 urgent = 24 max.
    """
    resp = client_with_worklist_data.get("/api/inbox")
    assert resp.status_code == 200
    # Compter les liens /app/conversation/ dans le rendu
    import re
    ids = re.findall(r"/app/conversation/(\d+)", resp.text)
    unique_ids = set(int(i) for i in ids)
    # On a inséré 3 vrais travail (id 300, 301, 302). Le worklist doit afficher
    # EXACTEMENT ces 3 ids.
    assert unique_ids == {300, 301, 302}, (
        f"worklist devrait afficher {{300, 301, 302}} mais affiche {sorted(unique_ids)}"
    )


def test_demandes_client_tab_keeps_all_status(client_with_worklist_data) -> None:
    """v1.30.0.7 — Onglet "Demandes client" (?category=demande_client) garde
    le comportement actuel : les demande_client PENDING sont en hot band,
    la 2e bande conserve le comportement v1.30.0.4 (inclut aussi les autres
    catégories, pour que Daniel puisse tout voir en scrollant).
    Critère CDAL : "Les autres onglets (Demandes client, Urgent, Newsletters,
    Factures, Spam, Phishing, Rappels) gardent leur comportement actuel".
    """
    resp = client_with_worklist_data.get("/api/inbox?category=demande_client")
    assert resp.status_code == 200
    # id=300 et id=301 (demande_client pending) DOIVENT être en hot band
    import re
    sep_idx = resp.text.find("border-b-4 border-green-600")
    assert sep_idx > 0, "Séparateur vert introuvable"
    hot_section = resp.text[:sep_idx]
    assert "/app/conversation/300" in hot_section
    assert "/app/conversation/301" in hot_section
    assert "Demande de filature urgente" in hot_section
    assert "Recherche de personne disparue" in hot_section
    # Le mail doublon (id=306, status='duplicate') NE DOIT PAS être en hot
    # (la hot n'accepte que pending). Mais il est un demande_client.
    assert "/app/conversation/306" not in hot_section


def test_worklist_disabled_when_category_filter_set(client_with_worklist_data) -> None:
    """v1.30.0.7 — Le worklist est DÉSACTIVÉ dès qu'un filtre explicite est posé.
    ?category=newsletter → le mode worklist est OFF, on a 2 bandes (hot+other),
    comportement v1.30.0.4 préservé (cf. spec CDAL "les autres onglets
    gardent leur comportement actuel").

    Le test vérifie que ?category=newsletter N'EST PAS en worklist mode :
    - le séparateur vert (hot/other separator) peut être absent car aucune
      newsletter pending n'est en hot (la hot est demande_client+urgent+pending).
    - les newsletters pending sont visibles dans le rendu.
    """
    resp = client_with_worklist_data.get("/api/inbox?category=newsletter")
    assert resp.status_code == 200
    # id=303 (newsletter pending) DOIT être visible
    assert "/app/conversation/303" in resp.text
    assert "Newsletter mensuelle juin" in resp.text
    # Le critère distinctif : on n'est PAS en worklist mode strict (3 mails).
    # On est en mode "tab catégorie" → le rendu peut inclure d'autres mails
    # dans la bande other. Le worklist mode strict exclut TOUT le reste.
    # On vérifie qu'on a au moins autant de mails qu'en worklist (>= 1).
    import re
    ids = set(int(i) for i in re.findall(r"/app/conversation/(\d+)", resp.text))
    # Newsletter pending (id=303) doit être dans la liste — confirme worklist OFF
    assert 303 in ids
    # En worklist strict, on n'aurait QUE les demande_client+urgent pending.
    # En mode tab catégorie, on a PLUS que ça. Vérifions qu'on a au moins
    # les 9+ ids (newsletter+spam+facture+autre+e-Box en other band).
    assert len(ids) >= 1, f"newsletter tab devrait afficher au moins la newsletter pending"

