import asyncio, sys
sys.path.insert(0, '/app')
from app.charlie import ask_charlie
from app.config import get_settings

async def main():
    settings = get_settings()

    # Test 1 : enregistrer une mémoire
    print('=== TEST 1 : Enregistrer ===')
    r1 = await ask_charlie('retiens que le contact principal pour ADF est Sofie Latte', db_path=settings.db_agent_state)
    print('Response:', r1.response_text[:100])

    # Test 2 : se souvenir
    print('\n=== TEST 2 : Se souvenir ===')
    r2 = await ask_charlie('qui est le contact pour le dossier ADF', db_path=settings.db_agent_state)
    print('Response:', r2.response_text[:200])
    print('Memory notes:', len(r2.vault_notes))

asyncio.run(main())
