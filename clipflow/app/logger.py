"""Logging setup: console + rotating file."""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app import config

FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
_configured = False


def _configure() -> None:
    """Configure the root logger once."""
    global _configured
    if _configured:
        return
    s = config.settings
    log_dir = Path(s.LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(getattr(logging, s.LOG_LEVEL.upper(), logging.INFO))
    formatter = logging.Formatter(FORMAT)
    console = logging.StreamHandler()
    file_handler = RotatingFileHandler(
        log_dir / "clipflow.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    for handler in (console, file_handler):
        handler.setFormatter(formatter)
        root.addHandler(handler)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a named logger, configuring the root on first use."""
    _configure()
    return logging.getLogger(name)
