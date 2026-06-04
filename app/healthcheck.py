from __future__ import annotations

from datetime import UTC, datetime

import structlog

log = structlog.get_logger()


class HealthState:
    def __init__(self) -> None:
        self.last_cycle: dict[str, datetime] = {}
        self.imap_connected: dict[str, bool] = {}
        # Compteur d'erreurs consécutives du poller par boîte (v1.21.3).
        # Reset à chaque cycle où ≥ 1 mail est traité avec succès.
        self.consecutive_errors: dict[str, int] = {}

    def mark_cycle(self, mailbox: str) -> None:
        self.last_cycle[mailbox] = datetime.now(UTC)

    def mark_imap(self, mailbox: str, connected: bool) -> None:
        self.imap_connected[mailbox] = connected

    def mark_error(self, mailbox: str) -> int:
        """Incrémente le compteur d'erreurs consécutives. Retourne la nouvelle valeur."""
        self.consecutive_errors[mailbox] = self.consecutive_errors.get(mailbox, 0) + 1
        return self.consecutive_errors[mailbox]

    def reset_errors(self, mailbox: str) -> None:
        """Reset le compteur d'erreurs (cycle OK : au moins 1 mail traité)."""
        if self.consecutive_errors.get(mailbox, 0) > 0:
            log.info("health.errors_reset", mailbox=mailbox)
        self.consecutive_errors[mailbox] = 0

    def error_snapshot(self) -> dict:
        return dict(self.consecutive_errors)

    def snapshot(self) -> dict:
        now = datetime.now(UTC)
        return {
            "imap": self.imap_connected,
            "last_cycle_seconds_ago": {
                m: (now - t).total_seconds() for m, t in self.last_cycle.items()
            },
            "consecutive_errors": dict(self.consecutive_errors),
        }


health = HealthState()
