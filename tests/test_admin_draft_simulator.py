"""Tests du simulateur de brouillon super-admin."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.web.admin import require_admin
from app.web.app import make_app


@pytest.fixture
def admin_user():
    return {
        "id": 1,
        "email": "admin@example.com",
        "role": "super_admin",
        "name": "Admin",
    }


@pytest.fixture
def client(admin_user):
    app = make_app()
    app.dependency_overrides[require_admin] = lambda: admin_user
    return TestClient(app)


def test_draft_simulator_page_requires_admin(client):
    resp = client.get("/admin/draft-simulator")
    assert resp.status_code == 200
    assert "Simulateur de brouillon" in resp.text
    assert "Corps de l'email" in resp.text


def test_draft_simulator_run_requires_admin(client, monkeypatch):
    async def fake_generate(*args, **kwargs):
        from types import SimpleNamespace

        return SimpleNamespace(
            draft="Bonjour,\n\nBrouillon simulé.\n\nDaniel",
            language="fr",
            rag_pairs=(),
            model_used="fake",
            category="demande_client",
            vault_notes=(),
        )

    monkeypatch.setattr("app.web.admin.generate_draft", fake_generate)

    resp = client.post(
        "/admin/api/draft-simulator/run",
        data={
            "mailbox_id": "1",
            "category": "demande_client",
            "subject": "Test sujet",
            "body": "Bonjour,\n\nCordialement,\nPierre\n",
        },
    )
    assert resp.status_code == 200
    assert "Brouillon simulé" in resp.text
    assert "Boîte" in resp.text
    assert "Catégorie" in resp.text


def test_draft_simulator_page_rejects_anonymous():
    app = make_app()
    client = TestClient(app)
    resp = client.get("/admin/draft-simulator")
    assert resp.status_code == 403
