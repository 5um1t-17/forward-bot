"""Application logging setup."""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from bot.config import config


def setup_logging() -> None:
    os.makedirs(config.LOG_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # StreamHandler -> stdout is what Render's dashboard captures. The file
    # handler below is a convenience for local runs; on Render the container
    # filesystem is ephemeral and is wiped on every deploy, so the dashboard
    # stdout is the authoritative log source.
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        os.path.join(config.LOG_DIR, "bot.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # keep telethon noise down
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
