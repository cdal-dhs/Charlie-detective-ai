# Patterns réutilisables depuis Charlie v1.21.3 — Poller IMAP résilient

> **Contexte** : Le 2026-06-04, Charlie (instance Detective.be) a passé ~26h sans générer aucun brouillon parce que le poller IMAP crashait en boucle sur **3 bugs cumulés** et qu'aucune alerte n'existait pour prévenir CDAL. Hotfix complet en v1.21.3 (correctifs) + v1.21.4 (filtre date).
>
> **Pourquoi cette note** : CDAL développe en parallèle le produit générique **Second Cerveau Pro** (`/Users/cdal/DEV_APP_CLAUDE/SECONDCERVEAU-PRO/`) et son instance `CDAL2`. Tout client Second Cerveau Pro qui scrape des boîtes IMAP (Outlook, Gmail via IMAP, Infomaniak, OVH, etc.) **doit** intégrer ces patterns. Coût d'intégration ≈ 200 lignes Python + 1 alerte Resend.
>
> **Statut** : ⚠️ **Non encore intégré** dans SECONDCERVEAU-PRO/CDAL2. À backporter avant la première mise en prod d'un nouveau client.

---

## 1. Les 3 bugs à corriger (tous génériques Python `email` stdlib)

### 1.1 `_decode_header` crash sur charset `unknown-8bit`

**Symptôme** (production Charlie) :
```
LookupError: unknown encoding: unknown-8bit
  File "/app/app/workers/imap_poller.py", line 74, in _decode_header
    result.append(part.decode(charset or "utf-8", errors="replace"))
```

**Cause** : `email.header.decode_header()` retourne un charset "exotique" (souvent `unknown-8bit` sur des mails malformés) que `bytes.decode()` ne reconnaît pas. Le fallback `errors="replace"` ne couvre pas une chaîne invalide.

**Fix générique** (chaîne de fallback + try/except `HeaderParseError`) :
```python
from email.header import decode_header, HeaderParseError
from email.errors import HeaderParseError as _HPE


def _decode_header(value: str) -> str:
    """Décode un header MIME-encoded (RFC 2047) avec fallbacks robustes."""
    if not value:
        return ""
    try:
        parts = decode_header(value)
    except (_HPE, HeaderParseError, ValueError, TypeError):
        return str(value)  # str brute, dernier recours

    result: list[str] = []
    for part, charset in parts:
        if isinstance(part, bytes):
            # Chaîne de fallback : charset déclaré → utf-8 → latin-1 → replace
            for enc in (charset, "utf-8", "latin-1"):
                if not enc:
                    continue
                try:
                    result.append(part.decode(enc, errors="strict"))
                    break
                except (LookupError, UnicodeDecodeError):
                    continue
            else:
                result.append(part.decode("utf-8", errors="replace"))
        else:
            result.append(str(part))
    return "".join(result)
```

**Test** (couvre unknown-8bit, latin-1, garbage, vide) :
```python
assert _decode_header("=?unknown-8bit?Q?Bonjour?=")  # ne crash pas
assert "caf" in _decode_header("=?iso-8859-1?Q?caf=E9?=")
assert isinstance(_decode_header("\x00\x01\x02\x03\xff"), str)
assert _decode_header("") == ""
```

### 1.2 `_persist` crash sur `Header` objects

**Symptôme** (production Charlie) :
```
sqlite3.ProgrammingError: Error binding parameter 5: type 'Header' is not supported
  File "/app/app/workers/imap_poller.py", line 504, in _persist
    cursor = conn.execute(...)
```

**Cause** : `msg.get("Subject")` ou `msg.get("From")` peut retourner un `email.header.Header` (et non `str`) sur certains mails MIME. `sqlite3` ne sérialise pas ce type.

**Fix** : coercion `str()` **défensive** à l'entrée de toute fonction qui touche sqlite :
```python
def _persist(*, db_path, imap_uid, mailbox_name, subject, sender, received_at, ...):
    # Ceinture + bretelles : coercion str() AVANT sqlite
    subject = str(subject) if subject is not None else ""
    sender = str(sender) if sender is not None else ""
    received_at = str(received_at) if received_at is not None else ""
    body_preview = str(body_preview) if body_preview is not None else ""
    body = str(body) if body is not None else ""
    ai_draft = str(ai_draft) if ai_draft is not None else ""

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO mail_processed (imap_uid, mailbox_name, subject, sender, received_at, ...) "
            "VALUES (?, ?, ?, ?, ?, ...)",
            (imap_uid, mailbox_name, subject, sender, received_at, ...),
        )
        conn.commit()
    finally:
        conn.close()
```

**Idem** à l'acquisition :
```python
received_at = str(msg.get("Date", "") or "")
sender = str(msg.get("From", "") or "")
subject = str(msg.get("Subject", "") or "")
```

### 1.3 Retry éternel structurel sur crash

**Symptôme** : un mail reste en queue IMAP indéfiniment, rejoué toutes les 5 min pendant 26h, jamais traité.

**Cause** : le flag `AgentProcessed` (ou équivalent "traité") n'est posé qu'en cas de **succès complet** du pipeline. Tout crash en cours pipeline → le mail est vu comme "non traité" au cycle suivant → rejoué.

**Fix** : 2 flags distincts + try/except englobant :
- `AgentProcessed` = succès complet (pose le brouillon)
- `AgentAttempted` = traité mais en erreur (libère la queue, signale "à inspecter manuellement")

```python
async def _process_single_mail(client, uid, mailbox):
    try:
        # ... tout le pipeline ...
        # En cas de succès :
        await client.store(uid, "+FLAGS", f"({AGENT_FLAG})")  # AgentProcessed
        return "processed"
    except Exception as e:
        log.exception("poller.mail_crash", uid=uid, error=str(e))
        # 1. Télémétrie
        await asyncio.to_thread(
            _log_telemetry, settings.db_agent_state, "poller_mail_crash",
            mailbox.name, f"uid={uid} error={type(e).__name__}: {e}",
        )
        # 2. Pose du flag de libération (≠ succès)
        if not settings.dry_run:
            try:
                await client.store(uid, "+FLAGS", f"({AGENT_ATTEMPTED_FLAG})")
            except Exception:
                log.warning("poller.flag_attempted_failed", uid=uid)
        return "error"
```

**Note IMAP Infomaniak** : les flags doivent **sans** le préfixe `$`. Utiliser `AgentAttempted`, pas `$AgentAttempted`.

---

## 2. Visibilité — compteur d'erreurs + alerte Resend

### 2.1 Compteur `consecutive_errors` par boîte

À ajouter dans **tout** HealthState d'un service qui scrape des sources externes :
```python
from datetime import UTC, datetime

class HealthState:
    def __init__(self):
        self.consecutive_errors: dict[str, int] = {}

    def mark_error(self, mailbox: str) -> int:
        self.consecutive_errors[mailbox] = self.consecutive_errors.get(mailbox, 0) + 1
        return self.consecutive_errors[mailbox]

    def reset_errors(self, mailbox: str) -> None:
        if self.consecutive_errors.get(mailbox, 0) > 0:
            log.info("health.errors_reset", mailbox=mailbox)
        self.consecutive_errors[mailbox] = 0

    def error_snapshot(self) -> dict:
        return dict(self.consecutive_errors)
```

**Reset automatique** : dans le `cycle_summary` du poller, si `cycle_stats` est non-vide (≥1 mail traité) → `reset_errors(mailbox.name)`. **Ne PAS reset sur cycle vide** (symptôme d'un autre bug en amont).

### 2.2 Helper d'alerte fire-and-forget

Pattern à recopier tel quel :
```python
async def _maybe_alert_poller_failure(mailbox, consecutive_errors, last_error, sample_uids):
    settings = get_settings()
    if consecutive_errors < settings.poller_alert_threshold:
        return  # pas d'alerte
    # Fire-and-forget : ne pas await pour ne pas figer le poller
    asyncio.create_task(
        alert_poller_persistent_failure(
            mailbox_name=mailbox.name,
            error_count=consecutive_errors,
            last_error=last_error,
            sample_uids=sample_uids,
        )
    )
```

### 2.3 Alerte Resend avec anti-spam 1h/boîte

```python
import html
from datetime import UTC, datetime
import httpx

RESEND_ENDPOINT = "https://api.resend.com/emails"
_POLLER_ALERT_COOLDOWN_SECONDS = 3600  # 1h
_last_poller_alert_sent: dict[str, datetime] = {}


async def alert_poller_persistent_failure(
    mailbox_name: str,
    error_count: int,
    last_error: str,
    sample_uids: list[str],
) -> None:
    """Email d'alerte quand le poller accumule des erreurs. Anti-spam 1h/boîte."""
    global _last_poller_alert_sent

    last_sent = _last_poller_alert_sent.get(mailbox_name)
    if last_sent and (datetime.now(UTC) - last_sent).total_seconds() < _POLLER_ALERT_COOLDOWN_SECONDS:
        log.info("alert.poller_throttled", mailbox=mailbox_name)
        return

    settings = get_settings()
    if not settings.resend_api_key:
        return

    uids_html = "".join(f"<li><code>{html.escape(uid)}</code></li>" for uid in sample_uids) or "<li>N/A</li>"
    payload = {
        "from": settings.resend_from,
        "to": [settings.admin_email],
        "subject": f"🚨 Poller en échec persistant ({error_count} erreurs)",
        "html": (
            "<html><body style='font-family:Arial,sans-serif;'>"
            f"<h2>🚨 Poller — échecs consécutifs</h2>"
            f"<p>Boîte <strong>{html.escape(mailbox_name)}</strong> : "
            f"<strong>{error_count} erreurs consécutives</strong> au-dessus du seuil.</p>"
            f"<p>Les mails sont flaggés <code>AgentAttempted</code> — pas de rejeu infini.</p>"
            f"<p><strong>Dernière erreur :</strong></p>"
            f"<pre style='background:#f5f5f5;padding:10px;'>{html.escape(last_error[:500])}</pre>"
            f"<ul>{uids_html}</ul>"
            "<p><strong>Action :</strong> vérifier les logs, retirer le flag <code>AgentAttempted</code> "
            "via IMAP/Thunderbird pour rejouer les mails.</p>"
            "</body></html>"
        ),
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                RESEND_ENDPOINT,
                headers={"Authorization": f"Bearer {settings.resend_api_key}"},
                json=payload,
            )
            r.raise_for_status()
        _last_poller_alert_sent[mailbox_name] = datetime.now(UTC)
    except Exception as e:
        log.error("alert.poller_failure_send_error", error=str(e))
```

### 2.4 Setting du seuil

```python
# Dans Settings (config.py)
poller_alert_threshold: int = 5  # Ajustable code uniquement
```

---

## 3. Tests à backporter

Les 19 tests de Charlie sont dans `tests/test_imap_poller_resilience.py` (5 sections) :
- 4 tests `_decode_header` (charsets exotiques)
- 3 tests `_persist` avec `Header` objects
- 3 tests `_process_single_mail` (try/except, télémétrie, pas d'`AgentAttempted` sur succès)
- 6 tests compteur d'erreurs + alerte (seuil, reset, anti-spam)
- 3 tests anti-spam 1h/boîte de l'alerte Resend

Pattern de mock pour pytest-asyncio :
```python
def _absorb_background_tasks(monkeypatch):
    """Absorbe tous les asyncio.create_task (Cerveau2 feed, etc.) en no-op."""
    def _silent_task(coro):
        coro.close()  # évite RuntimeWarning "coroutine was never awaited"
        return None
    monkeypatch.setattr("app.workers.imap_poller.asyncio.create_task", _silent_task)
```

---

## 4. Procédure de migration vers Second Cerveau Pro

Pour un nouveau client Second Cerveau Pro qui scrape IMAP :

1. **Copier `_decode_header` corrigé** dans `app/email/parser.py` (ou équivalent)
2. **Wrapper `_persist` / `_save` avec coercion `str()`** sur tous les champs texte
3. **Étendre le `HealthState`** avec `consecutive_errors` + `mark_error` + `reset_errors`
4. **Ajouter `alert_poller_persistent_failure`** + helper fire-and-forget
5. **Flag `AgentAttempted` dans la branche except** du pipeline
6. **Tests** : reprendre les 19 tests de `tests/test_imap_poller_resilience.py` (adapter les imports)
7. **Régler `poller_alert_threshold`** dans `config.py` selon SLA client (défaut 5)

**Coût estimé** : ~200 lignes Python + ~150 lignes tests + 1 alerte Resend. ROI énorme (26h d'incidents silencieux évités).

---

## 5. Note sur le périmètre Cerveau2 (serveur)

Aucun de ces patches ne touche le **serveur** Cerveau2 (`/Users/cdal/DEV_APP_CLAUDE/CERVEAU2-DEtective/`, v0.8.2) ni le produit `SECONDCERVEAU-PRO`. Le serveur Cerveau2 est un composant indépendant (FastAPI + sqlite-vec + E5-large) qui reçoit des requêtes HTTP du client Charlie. Les bugs étaient 100% côté parsing IMAP + sqlite3 local à Charlie.

Si un nouveau client Second Cerveau Pro rencontre les mêmes problèmes, c'est qu'il a son propre poller IMAP → il faut backporter ces patches dans son code client, pas dans Cerveau2.

---

## 6. Références

- **Code source** : `app/workers/imap_poller.py`, `app/alerts.py`, `app/healthcheck.py`, `app/config.py`
- **Tests** : `tests/test_imap_poller_resilience.py` (19 tests, ~600 lignes)
- **CHANGELOG** : `CHANGELOG.md` section `[1.21.3]` et `[1.21.4]`
- **HANDOVER** : `HANDOVER.md` section 9 (entries 17, 18) + section "Point de vigilance #11"
