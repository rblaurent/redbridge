"""Rotating file log for the daemon."""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_FILE = Path(__file__).resolve().parent / "daemon.log"
_MAX_BYTES = 2 * 1024 * 1024
_BACKUP_COUNT = 3

_configured = False


def setup() -> logging.Logger:
    global _configured
    logger = logging.getLogger("redbridge")
    if _configured:
        return logger
    _configured = True

    logger.setLevel(logging.DEBUG)

    fh = RotatingFileHandler(
        _LOG_FILE, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("[%(name)s] %(message)s"))
    logger.addHandler(ch)

    return logger
