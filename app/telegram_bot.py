import asyncio
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from app.config import get_settings

log = structlog.get_logger()


@dataclass
class _ProcessedMail:
    mailbox: str
    sender: str
    subject: str
    category: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


class BotState:
    def __init__(self) -> None:
        self._last_cycle: dict[str, datetime] = {}
        self._queue: int = 0
        self._history: list[_ProcessedMail] = []
        self._lock = asyncio.Lock()

    async def mark_cycle(self, mailbox: str) -> None:
        async with self._lock:
            self._last_cycle[mailbox] = datetime.now(UTC)

    async def set_queue(self, count: int) -> None:
        async with self._lock:
            self._queue = count

    async def record(
        self, mailbox: str, sender: str, subject: str, category: str
    ) -> None:
        async with self._lock:
            self._history.append(
                _ProcessedMail(mailbox, sender, subject, category)
            )
            if len(self._history) > 500:
                self._history = self._history[-250:]

    async def daily_count(self) -> int:
        async with self._lock:
            today = datetime.now(UTC).date()
            return sum(1 for m in self._history if m.timestamp.date() == today)

    async def last_n(self, n: int) -> list[_ProcessedMail]:
        async with self._lock:
            return self._history[-n:]

    async def snapshot(self) -> dict:
        async with self._lock:
            now = datetime.now(UTC)
            return {
                "last_cycle_seconds_ago": {
                    m: (now - t).total_seconds()
                    for m, t in self._last_cycle.items()
                },
                "daily_count": await self.daily_count(),
                "queue": self._queue,
            }


bot_state = BotState()
_bot_app: Application | None = None


def _authorized_chat(update: Update) -> bool:
    settings = get_settings()
    chat_id = str(update.effective_chat.id) if update.effective_chat else ""
    return chat_id == settings.telegram_chat_id


def _sign(msg: str) -> str:
    return f"{msg}\n— Charlie"


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized_chat(update):
        log.warning(
            "telegram.unauthorized",
            command="start",
            chat_id=update.effective_chat.id,
        )
        return
    text = (
        "Bonjour. Je suis Charlie, l'agent IA de Detective.be.\n\n"
        "Voici ce que je peux faire pour vous :\n"
        "/start — cette aide\n"
        "/status — état de l'agent\n"
        "/resume [n] — résumé des n derniers mails traités\n"
        "/approve [id] — valider un brouillon\n"
        "/reject [id] [raison] — rejeter un brouillon\n"
        "/ask <question> — me poser une question"
    )
    await update.message.reply_text(_sign(text))


async def status_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not _authorized_chat(update):
        log.warning(
            "telegram.unauthorized",
            command="status",
            chat_id=update.effective_chat.id,
        )
        return
    snap = await bot_state.snapshot()
    cycles = snap["last_cycle_seconds_ago"]
    if not cycles:
        last_text = "Aucun cycle encore."
    else:
        last_text = f"Dernier cycle il y a {int(min(cycles.values()))}s."
    text = (
        f"Agent opérationnel.\n"
        f"{last_text}\n"
        f"Mails traités aujourd'hui : {snap['daily_count']}\n"
        f"Brouillons en attente : {snap['queue']}"
    )
    await update.message.reply_text(_sign(text))


async def resume_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not _authorized_chat(update):
        log.warning(
            "telegram.unauthorized",
            command="resume",
            chat_id=update.effective_chat.id,
        )
        return
    n = 5
    if context.args:
        try:
            n = int(context.args[0])
        except ValueError:
            await update.message.reply_text(
                _sign("L'argument doit être un nombre. Exemple : /resume 10")
            )
            return
    mails = await bot_state.last_n(n)
    if not mails:
        await update.message.reply_text(
            _sign("Aucun mail traité pour l'instant.")
        )
        return
    lines = []
    for m in reversed(mails):
        ts = m.timestamp.strftime("%H:%M")
        lines.append(
            f"[{ts}] {m.mailbox} | {m.category} | {m.sender} | {m.subject}"
        )
    header = f"Résumé des {len(mails)} derniers mails traités :\n"
    await update.message.reply_text(_sign(header + "\n".join(lines)))


async def approve_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not _authorized_chat(update):
        log.warning(
            "telegram.unauthorized",
            command="approve",
            chat_id=update.effective_chat.id,
        )
        return
    if not context.args:
        await update.message.reply_text(_sign("Usage : /approve <id>"))
        return
    draft_id = context.args[0]
    log.info("telegram.approve.stub", draft_id=draft_id)
    await update.message.reply_text(
        _sign(
            f"Brouillon {draft_id} marqué pour envoi. "
            "(Fonctionnalité complète en V2)"
        )
    )


async def reject_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not _authorized_chat(update):
        log.warning(
            "telegram.unauthorized",
            command="reject",
            chat_id=update.effective_chat.id,
        )
        return
    if not context.args:
        await update.message.reply_text(_sign("Usage : /reject <id> [raison]"))
        return
    draft_id = context.args[0]
    reason = " ".join(context.args[1:]) if len(context.args) > 1 else "non spécifiée"
    log.info("telegram.reject.stub", draft_id=draft_id, reason=reason)
    await update.message.reply_text(
        _sign(
            f"Brouillon {draft_id} rejeté. Raison enregistrée : {reason} "
            "(Fonctionnalité complète en V2)"
        )
    )


async def ask_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not _authorized_chat(update):
        log.warning(
            "telegram.unauthorized",
            command="ask",
            chat_id=update.effective_chat.id,
        )
        return
    log.info("telegram.ask.received", chat_id=update.effective_chat.id)
    await update.message.reply_text(
        _sign(
            "Je suis en train d'apprendre à répondre comme Daniel. "
            "Reviens vers moi dans quelques jours !"
        )
    )


async def inline_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not _authorized_chat(update):
        log.warning(
            "telegram.unauthorized",
            callback=update.callback_query.data,
            chat_id=update.effective_chat.id,
        )
        return
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data.startswith("approve:"):
        draft_id = data.split(":", 1)[1]
        log.info("telegram.inline.approve.stub", draft_id=draft_id)
        await query.edit_message_text(
            _sign(
                f"Brouillon {draft_id} approuvé. (Envoi effectif en V2)"
            )
        )
    elif data.startswith("reject:"):
        draft_id = data.split(":", 1)[1]
        log.info("telegram.inline.reject.stub", draft_id=draft_id)
        await query.edit_message_text(
            _sign(
                f"Brouillon {draft_id} rejeté. (Calibration en V2)"
            )
        )
    else:
        log.warning("telegram.inline.unknown", data=data)


async def notify_new_draft(
    draft_id: str, sender: str, subject: str, category: str
) -> None:
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        log.warning("telegram.notify.skipped", reason="missing_config")
        return
    if _bot_app is None:
        log.warning("telegram.notify.skipped", reason="bot_not_running")
        return
    text = (
        f"Nouveau brouillon généré\n\n"
        f"Expéditeur : {sender}\n"
        f"Sujet : {subject}\n"
        f"Catégorie : {category}"
    )
    keyboard = [
        [
            InlineKeyboardButton(
                "Approuver", callback_data=f"approve:{draft_id}"
            ),
            InlineKeyboardButton(
                "Rejeter", callback_data=f"reject:{draft_id}"
            ),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        await _bot_app.bot.send_message(
            chat_id=settings.telegram_chat_id,
            text=text,
            reply_markup=reply_markup,
        )
        log.info(
            "telegram.notify.sent",
            draft_id=draft_id,
            chat_id=settings.telegram_chat_id,
        )
    except Exception:
        log.exception("telegram.notify.failed", draft_id=draft_id)


def _bot_thread(stop_event: asyncio.Event) -> None:
    """Thread dédié au bot Telegram avec sa propre boucle asyncio."""
    settings = get_settings()
    if not settings.telegram_bot_token:
        log.warning("telegram.bot.skipped", reason="missing_token")
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _start() -> None:
        global _bot_app
        _bot_app = (
            Application.builder()
            .token(settings.telegram_bot_token)
            .build()
        )
        _bot_app.add_handler(CommandHandler("start", start_command))
        _bot_app.add_handler(CommandHandler("status", status_command))
        _bot_app.add_handler(CommandHandler("resume", resume_command))
        _bot_app.add_handler(CommandHandler("approve", approve_command))
        _bot_app.add_handler(CommandHandler("reject", reject_command))
        _bot_app.add_handler(CommandHandler("ask", ask_command))
        _bot_app.add_handler(CallbackQueryHandler(inline_callback))

        await _bot_app.initialize()
        await _bot_app.start()
        await _bot_app.updater.start_polling()
        log.info("telegram.bot.started", bot_name=settings.telegram_bot_name)

        try:
            while not stop_event.is_set():
                await asyncio.sleep(0.5)
        finally:
            await _bot_app.updater.stop()
            await _bot_app.stop()
            await _bot_app.shutdown()
            log.info("telegram.bot.stopped")

    loop.run_until_complete(_start())


async def run_bot(stop_event: asyncio.Event) -> None:
    """Lance le bot dans un thread séparé pour éviter les conflits asyncio."""
    thread = threading.Thread(
        target=_bot_thread,
        args=(stop_event,),
        name="telegram-bot",
        daemon=True,
    )
    thread.start()
    log.info("telegram.bot.thread_started")
    await stop_event.wait()
