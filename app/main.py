import asyncio
import signal

import structlog

from app.config import get_settings
from app.web.app import run_web_server
from app.workers.imap_poller import poll_mailbox

log = structlog.get_logger()


async def main() -> None:
    settings = get_settings()
    log.info("agent.start", mailboxes=[m.name for m in settings.mailboxes()])

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
