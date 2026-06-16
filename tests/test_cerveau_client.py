import httpx
import pytest
import respx

from app.cerveau_client import query_vault

BASE = "https://cerveau2-det.digitalhs.biz"
SECRET = "test-secret"


# ── dégradation silencieuse ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_base_url_returns_empty():
    notes, answer = await query_vault("test", base_url="", api_secret=SECRET)
    assert notes == []
    assert answer is None


@pytest.mark.asyncio
async def test_no_secret_returns_empty():
    notes, answer = await query_vault("test", base_url=BASE, api_secret="")
    assert notes == []
    assert answer is None


@pytest.mark.asyncio
async def test_network_error_returns_empty():
    with respx.mock:
        respx.post(f"{BASE}/query").mock(side_effect=httpx.ConnectError("down"))
        notes, answer = await query_vault("test", base_url=BASE, api_secret=SECRET)
    assert notes == []
    assert answer is None


@pytest.mark.asyncio
async def test_http_500_returns_empty():
    with respx.mock:
        respx.post(f"{BASE}/query").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )
        notes, answer = await query_vault("test", base_url=BASE, api_secret=SECRET)
    assert notes == []


# ── réponses nominales ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ok_response_returns_notes():
    payload = {
        "status": "ok",
        "question": "surveillance Liège",
        "total_found": 2,
        "context": [
            {"path": "02_dossiers/import_detectivebelgique/foo.md", "content": "Corps A"},
            {"path": "02_dossiers/import_detectivebelgique/bar.md", "content": "Corps B"},
        ],
        "answer": "Voici ce que j'ai trouvé…",
    }
    with respx.mock:
        respx.post(f"{BASE}/query").mock(return_value=httpx.Response(200, json=payload))
        notes, answer = await query_vault(
            "surveillance Liège", base_url=BASE, api_secret=SECRET, context_only=False
        )
    assert len(notes) == 2
    assert notes[0].path == "02_dossiers/import_detectivebelgique/foo.md"
    assert notes[0].content == "Corps A"
    assert answer == "Voici ce que j'ai trouvé…"


@pytest.mark.asyncio
async def test_context_only_returns_notes():
    """Sans clé LLM, status=context_only mais les notes doivent quand même être retournées."""
    payload = {
        "status": "context_only",
        "question": "test",
        "total_found": 1,
        "context": [{"path": "01_inbox/note.md", "content": "Contenu"}],
        "answer": None,
    }
    with respx.mock:
        respx.post(f"{BASE}/query").mock(return_value=httpx.Response(200, json=payload))
        notes, answer = await query_vault("test", base_url=BASE, api_secret=SECRET)
    assert len(notes) == 1
    assert answer is None


@pytest.mark.asyncio
async def test_zone_rouge_returns_empty():
    payload = {
        "status": "zone_rouge",
        "question": "test",
        "total_found": 1,
        "context": [{"path": "rouge.md", "content": "Sensible"}],
        "answer": None,
    }
    with respx.mock:
        respx.post(f"{BASE}/query").mock(return_value=httpx.Response(200, json=payload))
        notes, answer = await query_vault("test", base_url=BASE, api_secret=SECRET)
    assert notes == []
    assert answer is None


@pytest.mark.asyncio
async def test_empty_context_returns_empty_list():
    payload = {"status": "ok", "question": "rien", "total_found": 0, "context": [], "answer": None}
    with respx.mock:
        respx.post(f"{BASE}/query").mock(return_value=httpx.Response(200, json=payload))
        notes, answer = await query_vault("rien", base_url=BASE, api_secret=SECRET)
    assert notes == []
    assert answer is None


# ── paramètres de la requête ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auth_header_sent():
    with respx.mock:
        route = respx.post(f"{BASE}/query").mock(
            return_value=httpx.Response(200, json={"status": "ok", "context": [], "total_found": 0})
        )
        notes, answer = await query_vault("test", base_url=BASE, api_secret=SECRET)
    assert route.called
    assert notes == []
    assert answer is None
    req = route.calls[0].request
    assert req.headers["Authorization"] == f"Bearer {SECRET}"


@pytest.mark.asyncio
async def test_limit_and_dossier_sent():
    with respx.mock:
        route = respx.post(f"{BASE}/query").mock(
            return_value=httpx.Response(200, json={"status": "ok", "context": [], "total_found": 0})
        )
        notes, answer = await query_vault(
            "test", base_url=BASE, api_secret=SECRET, dossier_id="2024-001_test", limit=5
        )
    import json

    assert notes == []
    assert answer is None
    body = json.loads(route.calls[0].request.content)
    assert body["limit"] == 5
    assert body["dossier_id"] == "2024-001_test"
