import asyncio
import signal
import sys
from pathlib import Path

import structlog

from app.charlie_memory import init_memory_table
from app.config import get_settings
from app.delivery.slack_bot import init_slack_bot
from app.logging_config import cleanup_old_logs, setup_logging
from app.web.app import run_web_server
from app.web.db_migrate import migrate
from app.workers.disk_watcher import watch_disk
from app.workers.imap_poller import cleanup_old_attachments, poll_mailbox

SOUL_EVOLVE_INTERVAL_HOURS = 72  # 3 jours


async def main() -> None:
    settings = get_settings()
    setup_logging(log_level=settings.log_level, log_dir=settings.log_dir)
    cleanup_old_logs(settings.log_dir, keep_days=3)

    await migrate(settings.db_agent_state)
    await init_memory_table(settings.db_agent_state)

    # Copier SOUL.md vers data/ au boot si absent (permet persistance + édition)
    soul_src = Path(__file__).parent / "prompts" / "SOUL.md"
    soul_dst = settings.data_dir / "SOUL.md"
    if soul_src.exists() and not soul_dst.exists():
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        soul_dst.write_text(soul_src.read_text(encoding="utf-8"), encoding="utf-8")

    log = structlog.get_logger()
    log.info("agent.start", mailboxes=[m.name for m in settings.mailboxes()])

    init_slack_bot()

    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    poller_tasks = [
        asyncio.create_task(poll_mailbox(mb, stop_event), name=f"poller-{mb.name}")
        for mb in settings.mailboxes()
    ]
    web_task = asyncio.create_task(run_web_server(stop_event), name="web")
    soul_task = asyncio.create_task(run_soul_evolver(stop_event), name="soul-evolver")
    disk_task = asyncio.create_task(watch_disk(stop_event), name="disk-watcher")
    att_task = asyncio.create_task(run_attachment_cleanup(stop_event), name="attachment-cleanup")

    await stop_event.wait()
    log.info("agent.stop_requested")

    for task in [*poller_tasks, web_task, soul_task, disk_task, att_task]:
        task.cancel()
    await asyncio.gather(*poller_tasks, web_task, soul_task, disk_task, att_task, return_exceptions=True)
    log.info("agent.stopped")


async def run_soul_evolver(stop_event: asyncio.Event) -> None:
    """Tâche de fond : relance l'évolution du SOUL.md toutes les 72h."""
    log = structlog.get_logger()
    interval = SOUL_EVOLVE_INTERVAL_HOURS * 3600

    # Attendre 5 min au démarrage pour ne pas saturer le LLM au boot
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=300)
        return
    except asyncio.TimeoutError:
        pass

    while not stop_event.is_set():
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "scripts.evolve_soul",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            out = stdout.decode().strip()
            if proc.returncode == 0 and out:
                # Ne loguer que les 3 dernières lignes pour éviter le bruit
                tail = "\n".join(out.splitlines()[-3:])
                log.info("soul_evolver.ran", tail=tail)
            elif proc.returncode != 0:
                err = stderr.decode()[-200:]
                log.warning("soul_evolver.failed", error=err)
        except Exception as e:
            log.warning("soul_evolver.error", error=str(e))

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def run_attachment_cleanup(stop_event: asyncio.Event) -> None:
    """Purge les pièces jointes locales de plus de 30 jours (toutes les 24h)."""
    log = structlog.get_logger()
    settings = get_settings()
    interval = 24 * 3600  # 24h

    # Attendre 10 min au démarrage
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=600)
        return
    except asyncio.TimeoutError:
        pass

    while not stop_event.is_set():
        try:
            cleanup_old_attachments(
                settings.db_agent_state,
                settings.data_dir,
                retention_days=30,
            )
        except Exception as e:
            log.warning("attachment_cleanup.error", error=str(e))

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
