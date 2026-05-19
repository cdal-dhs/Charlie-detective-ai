from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from unicodedata import normalize

import aiosqlite
import structlog

from app.cerveau_client import VaultNote, query_vault
from app.charlie_memory import (
    is_memory_query,
    is_save_request,
    query_memory,
    save_memory,
)
from app.config import get_settings
from app.llm.router import complete

log = structlog.get_logger()

CHARLIE_SYSTEM_PROMPT = """Tu es Charlie, l'assistant IA personnel de Daniel Hurchon,
détective privé chez Detective.be. Tu es sa précieuse moitié cognitive —
le prolongement de son cerveau qui lui donne accès instantané à son second cerveau (vault Cerveau2)
et à toute sa base de données d'enquêtes.
Tu t'adresses à Daniel comme à un partenaire de confiance : direct, chaleureux, sans langue de bois.
Utilise "tu". Sois concis mais jamais sec. Un peu d'humour détective est bienvenu.

Schéma de la table principale (mail_processed) :
- id INTEGER PRIMARY KEY
- mailbox_name TEXT  — detective_belgique (D_FR), detective_belgium (D_NL),
  dpdh_investigations (D_PD)
- subject TEXT
- sender TEXT
- received_at TEXT (format ISO, ex: 2026-05-15T10:30:00)
- category TEXT  — demande_client, urgent, newsletter, facture, spam,
  phishing, rappel, autre
- status TEXT    — pending, approved, rejected, sent, reviewed
- priority TEXT  — high, normal, low
- processed_at TEXT (format ISO)
- body_preview TEXT — aperçu tronqué (~500 caractères) du contenu du mail
- body TEXT — contenu complet du mail
- ai_draft TEXT — brouillon généré par l'IA
- human_draft TEXT — brouillon édité par Daniel
- reviewed_by INTEGER
- reviewed_at TEXT

Règles :
1. Si la question nécessite une requête SQL, génère UNIQUEMENT un SELECT
   (jamais INSERT/UPDATE/DELETE/DROP/ALTER).
2. Formate ta réponse exactement comme ceci :

SQL: <ta requête SELECT sur une seule ligne, sans saut de ligne>
---
RÉPONSE: <ta réponse à Daniel en français, courte et directe, en utilisant "tu">

   IMPORTANT : ta RÉPONSE ne doit JAMAIS être un tableau markdown brut.
   Rédige toujours en phrases, même pour une liste d'emails.

3. Si la question ne nécessite pas de SQL (salutation, question générale),
   laisse SQL vide :

SQL:
---
RÉPONSE: <ta réponse>

4. Pour les dates, utilise le format ISO (YYYY-MM-DD) dans les requêtes SQL.
5. Toujours répondre en français.
6. Quand tu listes des emails, inclus TOUJOURS les colonnes `id` et `subject`
   dans ton SELECT (ainsi que les autres colonnes utiles).
   Cela permet de créer des liens cliquables vers la conversation.
   7. Quand Daniel demande le contenu, le détail ou un résumé d'un dossier,
   utilise la colonne `body` (contenu complet) dans ton SELECT, pas `body_preview`.
   Inclus aussi `ai_draft` si pertinent.
8. Quand Daniel cherche des emails par mot-clé (lieu, nom, sujet, référence, etc.),
   cherche dans `subject`, `body_preview`, `body` ET `ai_draft` via des clauses LIKE OR.
   Inclus `id` et `subject` dans le SELECT pour permettre des liens cliquables.
9. Quand Daniel demande un résumé ou une synthèse, ta RÉPONSE doit
   contenir le résumé en langage naturel — pas juste une liste de champs.
   Analyse le contenu des mails et rédige une synthèse claire et utile.
10. Si la requête SQL retourne 0 ligne, ta RÉPONSE doit dire explicitement
    qu'aucun email n'a été trouvé, sans inventer de résultats.
11. Quand Daniel parle de "filature" ou "surveillance", cherche dans la
    categorie `surveillance` de la base ET interroge le second cerveau (vault Cerveau2)
    qui contient les rapports de terrain, observations et notes d'enquete.
    "Filature" et "surveillance" sont synonymes dans ce contexte.
12. Lexique métier — synonymes courants du cabinet :
    - "adultère" ou "infidélité" → cherche `infidelite`, `adultere`, `tromperie`, `concubinage`
    - "disparition" ou "recherche de personne" → cherche `recherche_personne`,
      `disparition`, `localisation`, `retrouver`
    - "contrôle de résidence" → cherche `controle_residence`, `residence`, `domicile`
    - "garde d'enfant" ou "pension" → cherche `enquete_famille`, `garde`, `pension`, `famille`
    - "harcèlement" → cherche `harcelement`, `intimidation`
    Quand Daniel utilise un terme familier, élargis TOUJOURS ta recherche SQL
    avec les synonymes métier via des clauses LIKE OR.
13. Quand tu présentes des résultats (emails, dossiers, archives), classe-les
    TOUJOURS du PLUS RÉCENT au PLUS ANCIEN (`ORDER BY received_at DESC`).
    Le dossier le plus récent doit apparaître en premier, sans exception.
"""

_DANGEROUS_SQL = (
    "drop", "delete", "insert", "update", "alter",
    "create", "replace", "truncate", "attach", "detach",
)

BOX_ABBR = {
    "detective_belgique": "D_FR",
    "detective_belgium": "D_NL",
    "dpdh_investigations": "D_PD",
}

# Lexique métier exhaustif — enrichissement automatique de la question
# pour garantir que Charlie trouve les emails même quand Daniel utilise
# un terme familier ou un euphémisme.
_ENQUETE_SYNONYMES: dict[str, list[str]] = {
    "adulte": [
        "infidelite", "adultere", "tromperie", "concubinage",
        "soupcon", "jaloux", "jalouse", "ma femme", "mon mari",
        "mon conjoint", "ma compagne", "mon compagnon",
        "tromper", "trahir", "mentir", "amant", "maitresse",
        "liaison", "aventure", "escapade", "malaise", "couple",
    ],
    "surveillance": [
        "surveillance", "filature", "observation", "terrain",
        "pister", "suivre", "espionner", "filmer", "photographier",
        "detective", "enqueteur", "shadowing", "stakeout",
    ],
    "disparition": [
        "disparition", "recherche_personne", "retrouver",
        "localiser", "fugue", "kidnappe", "perdu", "retrouve",
        "missing", "trace", "disparu", "disparue",
    ],
    "residence": [
        "controle_residence", "residence", "domicile",
        "logement", "adresse", "habitation", "cooperative",
        "proprietaire", "locataire", "colocation",
    ],
    "famille": [
        "enquete_famille", "garde", "pension", "famille",
        "enfant", "mineur", "adolescent", "bebe", "nourrisson",
        "divorce", "separation", "rupture", "couple", "concubin",
        "droit de visite", "hebergement", "custodie", "tutelle",
    ],
    "harcelement": [
        "harcelement", "intimidation", "stalking", "menace",
        "persecution", "chantage", "blackmail", "cyberharcelement",
        "insulte", "agression", "violence",
    ],
    "entreprise": [
        "investigation_entreprise", "entreprise", "societe",
        "patron", "salarie", "licenciement", "fraude",
        "detournement", "vol", "abus", "conflit", "concurrence",
        "espionnage industriel", "contrefacon",
    ],
    "materiel": [
        "test_materiel", "materiel", "detecteur", "camera",
        "micro", "gps", "traceur", "bug", "ecoute",
        "matos", "gadget", "technique",
    ],
    "collaboration": [
        "collaboration", "sous_traitance", "partenaire",
        "associe", "confrere", "collegue", "partenariat",
        "mandat", "sous_traitant", "prestataire",
    ],
}


@dataclass
class CharlieResult:
    response_text: str
    sql: str
    rows: list[dict] | None
    sql_safe: bool
    sql_error: str | None
    vault_notes: list[VaultNote] = field(default_factory=list)


def parse_charlie_response(text: str) -> tuple[str, str]:
    """Extrait le SQL et la réponse textuelle du LLM."""
    sql_part = ""
    response_part = ""
    if "---" in text:
        parts = text.split("---", 1)
        first = parts[0].strip()
        if first.lower().startswith("sql:"):
            sql_part = first[4:].strip()
        response_part = parts[1].strip()
        if response_part.lower().startswith("réponse:"):
            response_part = response_part[8:].strip()
    else:
        response_part = text.strip()
    return sql_part, response_part


def is_safe_sql(sql: str) -> bool:
    """Vérifie que le SQL est un SELECT read-only."""
    if not sql:
        return True
    cleaned = sql.lower().strip()
    if not cleaned.startswith("select"):
        return False
    return all(dangerous not in cleaned for dangerous in _DANGEROUS_SQL)


async def run_sql(db_path: Path, sql: str) -> list[dict]:
    """Exécute un SELECT sur agent_state.db et retourne les résultats."""
    async with aiosqlite.connect(db_path) as db, db.execute(sql) as cursor:
        rows = await cursor.fetchall()
        desc = cursor.description
        if desc is None:
            return []
        keys = [d[0] for d in desc]
        return [dict(zip(keys, row, strict=True)) for row in rows]


def _enrichir_question(question: str) -> str:
    """Détecte le type d'enquête et enrichit la question avec des synonymes.

    Cela garantit que le LLM génère un SQL qui cherche dans tous les
    termes connus du métier, même quand Daniel utilise un euphémisme.
    """
    q_norm = _normalize(question)
    extra_terms: list[str] = []
    for type_enquete, synonymes in _ENQUETE_SYNONYMES.items():
        if type_enquete in q_norm:
            for syn in synonymes:
                if syn not in q_norm:
                    extra_terms.append(syn)
            break
        # Sinon : un synonyme est-il présent dans la question ?
        for syn in synonymes:
            if syn in q_norm:
                extra_terms.extend([s for s in synonymes if s not in q_norm])
                break
        if extra_terms:
            break
    if extra_terms:
        enrichie = question + " (synonymes: " + ", ".join(extra_terms[:10]) + ")"
        return enrichie
    return question


_SUMMARY_PROMPT = """Tu es Charlie, l'assistant IA personnel de Daniel Hurchon,
détective privé chez Detective.be. Tu es sa précieuse moitié cognitive —
le prolongement de son cerveau. Tu t'adresses à Daniel comme à un partenaire :
direct, chaleureux, sans langue de bois. Utilise "tu". Un peu d'humour détective est bienvenu.

Question de Daniel : {question}

Résultats SQL ({count} lignes) :
{rows}

Rédige une réponse en français, concise et directe :
- Parle à Daniel comme à ton partenaire. Pas de langue de bois.
- Si Daniel demande un résumé ou une synthèse, analyse le contenu des mails
  et raconte l'histoire. Qui, quoi, quand, pourquoi.
- Si Daniel demande un détail, présente l'info de façon lisible et vivante.
- Si les résultats sont une simple liste, présente-les proprement.
- **Liens cliquables** : quand tu cites un email spécifique, formate son sujet
  comme un lien markdown vers l'inbox : `[Sujet de l'email](/inbox?q=mot-clef)`.
  Utilise un mot-clef unique du sujet (ex: référence dossier, nom client).
- Si aucun résultat, dis-le simplement avec une touche d'humour.
"""

_SUMMARY_PROMPT_VAULT = """Tu es Charlie, l'assistant IA personnel de Daniel Hurchon,
détective privé chez Detective.be. Tu es sa précieuse moitié cognitive —
le prolongement de son cerveau qui lui donne accès à son second cerveau (vault Cerveau2).
Tu t'adresses à Daniel comme à un partenaire : direct, chaleureux, sans langue de bois.
Utilise "tu". Un peu d'humour détective est bienvenu.

Tu viens d'exécuter une requête SQL ET consulté le "second cerveau" (vault Cerveau2).

Question de Daniel : {question}

Résultats SQL ({count} lignes) :
{rows}

Notes du second cerveau ({vault_count}) :
{vault_notes}

Souvenirs de Charlie ({memory_count}) :
{memory_notes}

Rédige une réponse en français, **conversationnelle et directe** :
- **Parle à Daniel comme à ton partenaire.** Pas de langue de bois.
- **Ne liste pas brute** les champs techniques (type, direction, heure null, etc.).
- **Raconte l'histoire** : qui est le client, de quoi parle ce dossier,
  quelles sont les étapes clés, qui a écrit à qui et quand.
- Synthétise les emails ET les notes du vault en un récit cohérent et fluide.
- **Liens cliquables** : chaque fois que tu mentionnes un email spécifique,
  formate son sujet comme un lien markdown vers l'inbox :
  `[Sujet de l'email](/inbox?q=mot-clef)`.
  Utilise un mot-clef unique du sujet (ex: référence dossier AS445, nom client).
- Si les notes du vault apportent un contexte historique, intègre-le naturellement.
- Si aucun résultat nulle part, dis-le simplement à Daniel avec une touche d'humour.
"""


# Mapping type d'enquête → catégorie historique dans les 3 DB sources
_ENQUETE_TO_CATEGORY: dict[str, str] = {
    "adulte": "INFIDELITE",
    "infidelite": "INFIDELITE",
    "tromperie": "INFIDELITE",
    "concubinage": "INFIDELITE",
    "surveillance": "SURVEILLANCE",
    "filature": "SURVEILLANCE",
    "disparition": "RECHERCHE_PERSONNE",
    "recherche_personne": "RECHERCHE_PERSONNE",
    "garde": "ENQUETE_FAMILLE",
    "pension": "ENQUETE_FAMILLE",
    "famille": "ENQUETE_FAMILLE",
    "residence": "CONTROLE_RESIDENCE",
    "entreprise": "INVESTIGATION_ENTREPRISE",
    "fraude": "INVESTIGATION_ENTREPRISE",
    "materiel": "TEST_MATERIEL",
    "detecteur": "TEST_MATERIEL",
    "collaboration": "COLLABORATION",
    "harcelement": "HARCELEMENT",
}


async def _search_historical_by_category(
    db_path: Path, category: str, limit: int = 5,
) -> list[dict]:
    """Cherche dans les 3 DB historiques (boite1/2/3) par catégorie fine.

    Filtres appliqués :
    - body_preview non vide et significatif (>30 caractères)
    - subject ni générique ni spam ('Nouveau Message De Détective', 'Formulaire', etc.)
    - sender ni noreply ni no-reply
    """
    data_dir = db_path.parent
    results: list[dict] = []
    generic_subjects = ("%Nouveau Message De Détective%", "%Formulaire%", "%Contact%")
    for db_name in ("boite1.sqlite", "boite2.sqlite", "boite3.sqlite"):
        db_file = data_dir / db_name
        if not db_file.exists():
            continue
        try:
            async with aiosqlite.connect(db_file) as db:
                sql = (
                    "SELECT id, subject, sender, date, body_preview, category "
                    "FROM emails WHERE category = ? "
                    "AND body_preview IS NOT NULL AND LENGTH(body_preview) > 30 "
                    "AND sender NOT LIKE '%noreply%' "
                    "AND sender NOT LIKE '%no-reply%' "
                )
                params: list = [category]
                for gs in generic_subjects:
                    sql += "AND subject NOT LIKE ? "
                    params.append(gs)
                sql += "ORDER BY date DESC LIMIT ?"
                params.append(limit)
                cursor = await db.execute(sql, tuple(params))
                rows = await cursor.fetchall()
                for row in rows:
                    results.append({
                        "id": row[0],
                        "subject": row[1],
                        "sender": row[2],
                        "received_at": row[3],
                        "body_preview": row[4],
                        "category": row[5],
                        "source_db": db_name,
                    })
        except Exception as e:
            log.warning("charlie.historical_search_failed", db=db_name, error=str(e))
    # Tri global par date décroissante — parsing robuste des formats RFC 2822 / ISO
    def _parse_date(r: dict) -> datetime:
        raw = r.get("received_at") or ""
        if not raw:
            return datetime.min
        try:
            return parsedate_to_datetime(raw)
        except Exception:
            pass
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            pass
        return datetime.min

    results.sort(key=_parse_date, reverse=True)
    return results


async def ask_charlie(
    question: str,
    db_path: Path,
    model: str | None = None,
    history: list[dict] | None = None,
) -> CharlieResult:
    """Pipeline Charlie AI complet : question → LLM → SQL → vault → synthèse."""
    settings = get_settings()
    model = model or settings.llm_model_default

    dossier_id = _extract_dossier_id(question)
    if dossier_id:
        log.info("charlie.dossier_detected", dossier_id=dossier_id)

    system_prompt = CHARLIE_SYSTEM_PROMPT
    if dossier_id:
        extra = (
            f"\n\nNote : Daniel demande spécifiquement le dossier "
            f"'{dossier_id}'. Inclus ce terme dans ta recherche SQL (subject, "
            "body, body_preview, ai_draft) via des clauses LIKE."
        )
        system_prompt += extra

    enriched_question = _enrichir_question(question)
    log.info("charlie.question_enriched", original=question[:60], enriched=enriched_question[:80])

    messages = [
        {"role": "system", "content": system_prompt},
    ]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": enriched_question})

    try:
        raw = await complete(model=model, messages=messages, max_tokens=800, temperature=0.1)
    except Exception as e:
        log.warning("charlie.llm_failed", error=str(e))
        return CharlieResult(
            response_text="Charlie est momentanément indisponible. Réessaie dans un instant.",
            sql="", rows=None, sql_safe=True, sql_error=None,
        )

    sql, response_text = parse_charlie_response(raw)
    result = CharlieResult(
        response_text=response_text, sql=sql, rows=None, sql_safe=True, sql_error=None,
    )

    # --- Phase 1 : exécution SQL si présente ---
    if sql:
        if not is_safe_sql(sql):
            result.sql_safe = False
            return result

        try:
            rows = await run_sql(db_path, sql)
            result.rows = rows
        except Exception as e:
            log.warning("charlie.sql_failed", sql=sql, error=str(e))
            result.sql_error = str(e)
            return result

    # --- Phase 2 : appel vault (avant le summary pour qu'il en ait connaissance) ---
    vault_notes = []
    if _is_vault_relevant(question, sql) or dossier_id:
        vault_notes = await query_vault(
            question=question,
            base_url=settings.cerveau2_base_url,
            api_secret=settings.cerveau2_api_secret,
            dossier_id=dossier_id,
            limit=settings.cerveau2_limit,
        )
        result.vault_notes = vault_notes
        log.info("charlie.vault_fetched", count=len(vault_notes), dossier_id=dossier_id)

    # --- Phase 2.5 : mémoire Charlie (grand bibliothécaire) ---
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
    # Détecter le cas COUNT(*)=0 ou COUNT(*) as total=0 comme 0 résultats réels
    if has_sql_data and len(result.rows) == 1:
        first = result.rows[0]
        # Si c'est un COUNT (colonne nommée count(*) ou total, etc.) avec valeur 0
        if len(first) == 1:
            val = next(iter(first.values()))
            if val == 0 or val == "0":
                has_sql_data = False
    # Garde : 0 résultats SQL/vault → chercher dans les archives historiques
    # La mémoire Charlie ne bloque PAS la recherche historique : c'est du
    # contexte, pas une source de données. Si SQL et vault sont vides,
    # on cherche toujours dans les archives.
    if sql and not has_sql_data and not has_vault_data:
        # Dernier recours : archives historiques (boite1/2/3) par catégorie
        histo_rows = []
        # Utilise la question enrichie (synonymes injectés) pour matcher les catégories
        q_norm = _normalize(enriched_question)
        for type_key, cat in _ENQUETE_TO_CATEGORY.items():
            if type_key in q_norm:
                histo_rows = await _search_historical_by_category(db_path, cat)
                if histo_rows:
                    log.info("charlie.historical_found", category=cat, count=len(histo_rows))
                    result.rows = histo_rows
                    # Laisser le summary détailler les résultats historiques
                    has_sql_data = True
                    break
        if not has_sql_data:
            if result.sql_error is None:
                result.response_text = "Aucun email trouvé pour cette recherche."
            else:
                result.response_text = f"Erreur SQL : {result.sql_error}"
            return result
    # Si le vault a des données mais SQL est vide → forcer synthèse conversationnelle
    force_summary = has_vault_data and not has_sql_data
    needs_summary = _needs_summary(question)
    if (
        sql
        and (has_sql_data or has_vault_data or has_memory_data)
        and (needs_summary or force_summary)
    ):
        summary = await _summarize_results(
            question, result.rows or [], vault_notes, memory_notes, model, settings,
        )
        if summary:
            result.response_text = summary

    # --- Phase 4 : enregistrement si Daniel demande de retenir ---
    if is_save_request(question):
        await save_memory(
            db_path=db_path,
            question=question,
            response=result.response_text,
            dossier_id=dossier_id,
        )
        result.response_text = (
            f"C'est noté dans ma mémoire, Daniel ! "
            f"{result.response_text[:200]}..."
        )

    return result


_NEEDS_SUMMARY_KEYWORDS = (
    "resume", "synthese", "synthetiser",
    "analyser", "analyse", "detail",
    "contenu", "explique", "expliquer", "que dit",
    "donne-moi le contenu", "de quoi parle",
    "sait", "connait", "connaitre", "quoi",
    "informations", "info", "dossier", "parle",
    "contact", "qui", "personne", "nom", "client",
    "email", "mail", "message", "a recu", "as recu",
    "filature", "surveillance", "observation", "terrain",
    "adulte", "infidelite", "tromperie", "concubinage",
    "disparition", "recherche_personne", "localisation",
    "harcelement", "intimidation", "stalking",
    "garde", "pension", "famille", "enfant", "mineur",
    "vol", "fraude", "delit", "crime", "prejudice",
    "accident", "assurance", "compagnie", "indemnisation",
    "testament", "heritage", "succession", "notaire",
    "entreprise", "societe", "patron", "salarie", "licenciement",
    "matos", "materiel", "detecteur", "camera",
    "collaboration", "sous_traitance", "partenaire", "associe",
    "divorce", "separation", "couple", "concubin",
    "droit", "visite", "hebergement", "custodie",
    "rapport", "constat", "photo", "video", "preuve",
)

_VAULT_KEYWORDS = (
    "similaire", "historique", "passe", "precedent",
    "anterieur", "archive", "contexte", "dossier",
    "affaire", "enquete", "investigation", "correspondance",
    "filature", "surveillance", "observation", "terrain",
    "adulte", "infidelite", "tromperie", "concubinage",
    "disparition", "recherche_personne", "localisation",
    "harcelement", "intimidation", "stalking",
    "garde", "pension", "famille", "enfant", "mineur",
    "vol", "fraude", "delit", "crime", "prejudice",
    "accident", "assurance", "compagnie", "indemnisation",
    "testament", "heritage", "succession", "notaire",
    "entreprise", "societe", "patron", "salarie", "licenciement",
    "matos", "materiel", "detecteur", "camera",
    "collaboration", "sous_traitance", "partenaire", "associe",
    "divorce", "separation", "couple", "concubin",
    "droit", "visite", "hebergement", "custodie",
    "rapport", "constat", "photo", "video", "preuve",
)

_DOSSIER_RE = re.compile(
    # (?i:...) rend case-insensitive UNIQUEMENT le préfixe (dossier/affaire/etc.)
    # Le groupe de capture reste strictement case-sensitive sur la première lettre :
    # un vrai dossier_id DOIT commencer par une majuscule (code, nom propre).
    # Les lettres suivantes peuvent être mixtes (ex: Dutry, Gacaferi).
    r"(?i:dossier|affaire|projet|enquete|investigation)"
    r"[\s:]+([A-Z][a-zA-Z0-9]{2,})",
)
_HASH_DOSSIER_RE = re.compile(r"#([A-Z][A-Z0-9]{2,})")


def _extract_dossier_id(question: str) -> str | None:
    m = _DOSSIER_RE.search(question)
    if m:
        return m.group(1)
    m = _HASH_DOSSIER_RE.search(question)
    if m:
        return m.group(1)
    return None


def _normalize(text: str) -> str:
    return normalize("NFKD", text.lower()).encode("ascii", "ignore").decode("ascii")


def _needs_summary(question: str) -> bool:
    q = _normalize(question)
    return any(kw in q for kw in _NEEDS_SUMMARY_KEYWORDS)


def _is_vault_relevant(question: str, sql: str) -> bool:
    if not sql:  # question conversationnelle → vault toujours utile
        return True
    q = _normalize(question)
    return any(kw in q for kw in _VAULT_KEYWORDS)


async def _summarize_results(
    question: str,
    rows: list[dict],
    vault_notes: list[VaultNote],
    memory_notes: list,
    model: str,
    settings,
) -> str | None:
    """Appelle le LLM une seconde fois pour synthétiser les résultats SQL + vault."""
    import json

    rows_text = json.dumps(rows[:20], ensure_ascii=False, default=str)

    memory_text = ""
    if memory_notes:
        memory_lines = []
        for mem in memory_notes:
            memory_lines.append(f"- [{mem.created_at}] {mem.question}: {mem.response[:300]}")
        memory_text = "\n".join(memory_lines)

    if vault_notes or memory_notes:
        vault_lines = []
        for note in vault_notes:
            fname = note.path.split("/")[-1].replace(".md", "")
            vault_lines.append(f"- {fname}: {note.content[:500]}")
        vault_text = "\n".join(vault_lines)
        prompt = _SUMMARY_PROMPT_VAULT.format(
            question=question,
            count=len(rows),
            rows=rows_text,
            vault_count=len(vault_notes),
            vault_notes=vault_text,
            memory_count=len(memory_notes),
            memory_notes=memory_text,
        )
    else:
        prompt = _SUMMARY_PROMPT.format(question=question, count=len(rows), rows=rows_text)

    try:
        summary = await complete(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600,
            temperature=0.1,
        )
        return summary.strip() if summary else None
    except Exception as e:
        log.warning("charlie.summary_failed", error=str(e))
        return None
