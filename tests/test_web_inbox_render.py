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
    """
    resp = client_with_reply_threads.get("/api/inbox")
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
    """
    resp = client_with_reply_threads.get("/api/inbox")
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
