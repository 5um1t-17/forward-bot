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
