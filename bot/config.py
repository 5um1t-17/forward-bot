import os
from dotenv import load_dotenv

load_dotenv()


def _int_list(raw: str) -> list[int]:
    return [int(x) for x in raw.split(",") if x.strip().isdigit()]


class Config:
    API_ID: int = int(os.getenv("API_ID", "0"))
    API_HASH: str = os.getenv("API_HASH", "")
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")

    MONGO_URI: str = os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017")
    MONGO_DB: str = os.getenv("MONGO_DB", "telegram_transfer_bot")

    SESSION_ENCRYPTION_KEY: str = os.getenv("SESSION_ENCRYPTION_KEY", "")
    SESSION_KEY_FILE: str = os.getenv("SESSION_KEY_FILE", "session.key")

    ADMIN_IDS: list[int] = _int_list(os.getenv("ADMIN_IDS", ""))

    LOG_DIR: str = os.getenv("LOG_DIR", "logs")
    SESSION_DIR: str = os.getenv("SESSION_DIR", "sessions")

    DEFAULT_THREADS: int = int(os.getenv("DEFAULT_THREADS", "4"))
    DEFAULT_FORWARD_DELAY: float = float(os.getenv("DEFAULT_FORWARD_DELAY", "0"))
    SCHEDULER_INTERVAL: int = int(os.getenv("SCHEDULER_INTERVAL", "60"))

    # Hard caps for safety / Telegram limits
    MAX_CUSTOM_RANGE: int = int(os.getenv("MAX_CUSTOM_RANGE", "20000"))
    MAX_THREADS: int = 10
    BATCH_SIZE: int = 100

    # Download & Re-upload speed: parallel transfers are multiplied past the
    # user's `threads` setting. Downloads and uploads use independent pools so
    # big files overlap as much as Telegram's limits allow.
    DOWNLOAD_MULT: int = int(os.getenv("DOWNLOAD_MULT", "3"))
    UPLOAD_MULT: int = int(os.getenv("UPLOAD_MULT", "3"))
    MAX_DL_THREADS: int = int(os.getenv("MAX_DL_THREADS", "12"))
    MAX_UP_THREADS: int = int(os.getenv("MAX_UP_THREADS", "12"))

    # Telethon fetches download parts one at a time (max 512 KiB per request),
    # so a single file is latency-bound (~part size / round-trip). These knobs
    # make each large file fetch several parts concurrently, which multiplies
    # the effective per-file download speed without changing the number of
    # simultaneous files. Set DOWNLOAD_PARTS to 1 to disable.
    DOWNLOAD_PARTS: int = int(os.getenv("DOWNLOAD_PARTS", "6"))
    DOWNLOAD_PARALLEL_MIN: int = int(os.getenv("DOWNLOAD_PARALLEL_MIN", str(1 * 1024 * 1024)))

    FETCH_DIALOGS_TIMEOUT: int = int(os.getenv("FETCH_DIALOGS_TIMEOUT", "20"))

    # Hard cap for connecting a user Telegram client (TCP + auth check). Keeps
    # a single slow/unreachable DC from hanging a callback forever.
    CLIENT_CONNECT_TIMEOUT: float = float(os.getenv("CLIENT_CONNECT_TIMEOUT", "25"))

    # How often the live progress message is refreshed (seconds).
    PROGRESS_REFRESH: float = float(os.getenv("PROGRESS_REFRESH", "2"))

    # FloodWait handling: a single wait is slept through up to MAX_FLOOD_SLEEP
    # seconds, but if the cumulative wait for one operation exceeds
    # MAX_FLOOD_WAIT the operation gives up (counted as failed) instead of
    # sleeping forever while Telegram keeps escalating the wait.
    MAX_FLOOD_SLEEP: float = 300
    MAX_FLOOD_WAIT: float = 600
    FLOOD_BUFFER: float = 0.5

    @property
    def configured(self) -> bool:
        return bool(self.API_ID and self.API_HASH and self.BOT_TOKEN)


config = Config()
