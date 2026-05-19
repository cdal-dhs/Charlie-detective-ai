import asyncio, sys, json
sys.path.insert(0, '/app')
from app.charlie import ask_charlie
from app.config import get_settings

async def main():
    settings = get_settings()
    result = await ask_charlie('qui est mon contact pour le dossier ADF', db_path=settings.db_agent_state)
    print('SQL:', result.sql)
    print('ROWS:', len(result.rows) if result.rows else 0)
    print('VAULT:', len(result.vault_notes))
    print('---RESPONSE---')
    print(result.response_text)

asyncio.run(main())
