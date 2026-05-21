import sys

with open('/Users/cdal/DEV_APP_CLAUDE/DETECTIVE_BE/app/charlie.py', 'r') as f:
    content = f.read()

# Fix: add memory_notes initialization before Phase 3
old = '''    # --- Phase 3 : summary intelligent avec les deux sources ---
    has_sql_data = result.rows and len(result.rows) > 0
    has_vault_data = vault_notes and len(vault_notes) > 0
    # Garde : 0 resultats partout → reponse directe sans hallucination
    if sql and not has_sql_data and not has_vault_data:'''

new = '''    # --- Phase 2.5 : memoire Charlie (grand bibliothecaire) ---
    memory_notes = []
    if is_memory_query(question) or dossier_id:
        memory_notes = await query_memory(
            db_path=db_path,
            question=question,
            dossier_id=dossier_id,
            limit=3,
        )
        if memory_notes:
            log.info("charlie.memory_fetched", count=len(memory_notes), dossier_id=dossier_id)

    # --- Phase 3 : summary intelligent avec les deux sources ---
    has_sql_data = result.rows and len(result.rows) > 0
    has_vault_data = vault_notes and len(vault_notes) > 0
    has_memory_data = bool(memory_notes)
    # Garde : 0 resultats partout → reponse directe sans hallucination
    if sql and not has_sql_data and not has_vault_data and not has_memory_data:'''

content = content.replace(old, new)

# Fix Phase 3 condition and summary call
old2 = '''    # Si le vault a des donnees mais SQL est vide → forcer synthese conversationnelle
    force_summary = has_vault_data and not has_sql_data
    if sql and (has_sql_data or has_vault_data or memory_notes) and (_needs_summary(question) or force_summary):
        summary = await _summarize_results(
            question, result.rows or [], vault_notes, memory_notes, model, settings,
        )'''

new2 = '''    # Si le vault a des donnees mais SQL est vide → forcer synthese conversationnelle
    force_summary = has_vault_data and not has_sql_data
    if sql and (has_sql_data or has_vault_data or has_memory_data) and (_needs_summary(question) or force_summary):
        summary = await _summarize_results(
            question, result.rows or [], vault_notes, memory_notes, model, settings,
        )'''

content = content.replace(old2, new2)

# Fix long line
content = content.replace(
    '        result.response_text = f"C\'est note dans ma memoire, Daniel ! {result.response_text[:200]}..."',
    '        result.response_text = (\n            f"C\'est note dans ma memoire, Daniel ! "\n            f"{result.response_text[:200]}..."\n        )'
)

with open('/Users/cdal/DEV_APP_CLAUDE/DETECTIVE_BE/app/charlie.py', 'w') as f:
    f.write(content)
print('OK')
