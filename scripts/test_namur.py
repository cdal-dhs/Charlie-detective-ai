import asyncio, sys, json
sys.path.insert(0, '/app')
from app.charlie import ask_charlie
from app.config import get_settings

async def main():
    settings = get_settings()
    result = await ask_charlie('donne-moi mes dossiers qui se deroulent a Namur ou province de namur', db_path=settings.db_agent_state)
    print('SQL:', result.sql)
    print('ROWS:', len(result.rows) if result.rows else 0)
    print('VAULT:', len(result.vault_notes))
    print('---RESPONSE---')
    print(result.response_text)
    if result.rows:
        print('---SAMPLE ROWS---')
        for r in result.rows[:3]:
            print(json.dumps({k:v for k,v in r.items() if k in ('id','subject','sender','category')}, ensure_ascii=False))

asyncio.run(main())
