import logging
import sys
from datetime import datetime
from pathlib import Path

import structlog


def setup_logging(log_level: str = "INFO", log_dir: Path | None = None) -> None:
    """Configure structlog : console (JSON/coloré) + fichier journalier lisible."""
    level = getattr(logging, log_level.upper(), logging.INFO)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    # Console : JSON compact (Docker) ou coloré (dev)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
            if sys.stdout.isatty()
            else structlog.processors.JSONRenderer(),
            foreign_pre_chain=shared_processors,
        )
    )

    handlers: list[logging.Handler] = [console_handler]

    # Fichier : format lisible pour tail/grep
    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        log_file = log_dir / f"agent-{today}.log"
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processor=structlog.dev.ConsoleRenderer(colors=False),
                foreign_pre_chain=shared_processors,
            )
        )
        handlers.append(file_handler)

    logging.basicConfig(
        level=level,
        handlers=handlers,
        force=True,
    )

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def cleanup_old_logs(log_dir: Path, keep_days: int = 7) -> None:
    """Supprime les fichiers de log plus anciens que keep_days."""
    if not log_dir.exists():
        return
    import time

    cutoff = time.time() - keep_days * 86400
    for f in log_dir.glob("agent-*.log"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
