from __future__ import annotations

from datetime import UTC, datetime

import structlog

log = structlog.get_logger()


class HealthState:
    def __init__(self) -> None:
        self.last_cycle: dict[str, datetime] = {}
        self.imap_connected: dict[str, bool] = {}

    def mark_cycle(self, mailbox: str) -> None:
        self.last_cycle[mailbox] = datetime.now(UTC)

    def mark_imap(self, mailbox: str, connected: bool) -> None:
        self.imap_connected[mailbox] = connected

    def snapshot(self) -> dict:
        now = datetime.now(UTC)
        return {
            "imap": self.imap_connected,
            "last_cycle_seconds_ago": {
                m: (now - t).total_seconds() for m, t in self.last_cycle.items()
            },
        }


health = HealthState()
