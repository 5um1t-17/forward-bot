"""Process-level health flags, consumed by the web health endpoint.

``bot_alive`` flips to ``False`` whenever the main bot loop is not running
(e.g. while reconnecting or after a fatal failure) so :mod:`web` can return
HTTP 503 and let the platform / Render restart the process.
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_state = {"bot_alive": False}


def mark_bot_alive(alive: bool) -> None:
    with _lock:
        _state["bot_alive"] = bool(alive)


def bot_alive() -> bool:
    with _lock:
        return bool(_state.get("bot_alive"))
