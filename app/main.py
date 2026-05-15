import asyncio
import signal

import structlog

from app.config import get_settings
from app.delivery.slack_bot import init_slack_bot
from app.logging_config import cleanup_old_logs, setup_logging
from app.web.app import run_web_server
from app.workers.imap_poller import poll_mailbox


async def main() -> None:
    settings = get_settings()
    setup_logging(log_level=settings.log_level, log_dir=settings.log_dir)
    cleanup_old_logs(settings.log_dir, keep_days=7)

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

    await stop_event.wait()
    log.info("agent.stop_requested")

    for task in [*poller_tasks, web_task]:
        task.cancel()
    await asyncio.gather(*poller_tasks, web_task, return_exceptions=True)
    log.info("agent.stopped")


if __name__ == "__main__":
    asyncio.run(main())
