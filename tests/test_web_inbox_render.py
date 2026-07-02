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
