"""Entity resolution: links, usernames, ids and forwarded messages."""
from __future__ import annotations

import asyncio
import logging
import re

from telethon import TelegramClient
from telethon.errors import UsernameInvalidError, ChatIdInvalidError

log = logging.getLogger("bot.resolve")

# https://t.me/username / https://t.me/username/123 / t.me/c/<channel_id>/<msg_id>
# https://t.me/joinchat/<hash> / https://t.me/+<id>
TME_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/"
    r"(?:"
    r"joinchat/(?P<joinchat_slug>[A-Za-z0-9_+-]{1,64})"
    r"|\+(?P<plus_slug>[A-Za-z0-9_+-]{1,64})"
    r"|(?P<slug>[A-Za-z0-9_+-]{1,64})"
    r")"
    r"(?:/(?P<msg>\d{1,15}))?"
)
# private-channel share link: t.me/c/<channel_id>/<msg_id>
# also supports the private restricted-group form: t.me/c/<channel_id>/<msg_id>
C_LINK_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/c/(?P<cid>\d{1,15})(?:/(?P<msg>\d{1,15}))?"
)
USERNAME_RE = re.compile(r"^@?([A-Za-z0-9_]{3,64})$")
ID_RE = re.compile(r"^-?\d{4,15}$")


class Resolved:
    def __init__(self, entity, title: str, chat_id: int, msg_id: int | None = None,
                 input_type: str = "") -> None:
        self.entity = entity
        self.title = title
        self.chat_id = chat_id
        self.msg_id = msg_id
        self.input_type = input_type


def parse_input(text: str) -> dict:
    """Parse a user-provided reference without hitting the API.

    Returns one of:
        {"kind": "message_link", "slug": ..., "msg_id": ...}
        {"kind": "username", "username": ...}
        {"kind": "channel_id", "id": int}
        {"kind": "unknown"}
    """
    text = text.strip()
    low = text.lower()
    if low.startswith("tg://"):
        m = re.search(r"telegram=(?:resolve\?domain=)?([^&/]+)", low)
        if m:
            return {"kind": "username", "username": m.group(1)}
        return {"kind": "unknown"}

    c = C_LINK_RE.search(low)
    if c:
        cid = int(c.group("cid"))
        msg = int(c.group("msg")) if c.group("msg") else None
        return {"kind": "message_link", "slug": "c", "cid": cid, "msg_id": msg}

    m = TME_RE.search(low)
    if m:
        msg_id = int(m.group("msg")) if m.group("msg") else None
        if m.group("joinchat_slug") is not None:
            return {"kind": "message_link", "identifier": f"joinchat/{m.group('joinchat_slug')}", "msg_id": msg_id}
        if m.group("plus_slug") is not None:
            return {"kind": "message_link", "identifier": f"+{m.group('plus_slug')}", "msg_id": msg_id}
        slug = m.group("slug")
        return {"kind": "message_link", "slug": slug, "msg_id": msg_id}

    if ID_RE.match(text):
        try:
            return {"kind": "channel_id", "id": int(text)}
        except ValueError:
            return {"kind": "unknown"}

    m = USERNAME_RE.match(text)
    if m:
        return {"kind": "username", "username": m.group(1)}

    return {"kind": "unknown"}


async def resolve(client: TelegramClient, text: str) -> Resolved | None:
    parsed = parse_input(text)
    try:
        if parsed["kind"] == "username":
            entity = await client.get_entity(parsed["username"])
            return _to_resolved(entity, "username")
        if parsed["kind"] == "channel_id":
            entity = await client.get_entity(parsed["id"])
            return _to_resolved(entity, "id")
        if parsed["kind"] == "message_link":
            if parsed.get("slug") == "c":
                entity = await _resolve_private_channel(client, parsed["cid"])
                if entity is None:
                    return None
                resolved = _to_resolved(entity, "message_link")
                resolved.msg_id = parsed.get("msg_id")
                return resolved
            identifier = parsed.get("identifier") or parsed.get("slug")
            entity = await client.get_entity(identifier)
            resolved = _to_resolved(entity, "message_link")
            resolved.msg_id = parsed.get("msg_id")
            return resolved
    except (UsernameInvalidError, ChatIdInvalidError, ValueError) as exc:
        log.warning("resolve failed for %r: %s", text, exc)
        return None
    except Exception as exc:  # FloodWait, ChannelPrivate, etc.
        log.warning("resolve error for %r: %s", text, exc)
        return None
    return None


async def resolve_forwarded(client: TelegramClient, message) -> Resolved | None:
    """Resolve the source chat of a message the user forwarded to the bot."""
    fwd = message.forward
    if fwd is None:
        return None
    peer = fwd.from_id or fwd.chat_id
    if peer is None:
        return None
    try:
        entity = await client.get_entity(peer)
        return _to_resolved(entity, "forwarded")
    except Exception as exc:
        log.warning("resolve_forwarded failed: %s", exc)
        return None


def _tme_c_channel_id(n: int) -> list[int]:
    """Convert the number in a t.me/c/<n>/<msg> link to possible negative chat ids."""
    if n > 1000000000000:
        return [-n, int(f"-100{n}")]
    return [int(f"-100{n}"), -n]


async def _resolve_private_channel(client: TelegramClient, cid: int):
    """Try multiple chat ID formats to resolve a private channel from t.me/c/ link."""
    errors = []
    for chat_id in _tme_c_channel_id(cid):
        try:
            return await client.get_entity(chat_id)
        except (ChatIdInvalidError, ValueError, TypeError) as exc:
            errors.append(f"{chat_id}: {exc}")
            continue
    log.warning("Failed to resolve private channel cid=%s with all formats: %s", cid, "; ".join(errors))
    return None


def _to_resolved(entity, input_type: str) -> Resolved:
    title = getattr(entity, "title", None) or getattr(entity, "username", None) or str(entity.id)
    return Resolved(entity=entity, title=title, chat_id=entity.id, input_type=input_type)


async def fetch_sendable_dialogs(client: TelegramClient, limit: int = 100) -> list[dict]:
    """Group/channel dialogs the account can (likely) post to.

    Uses a single bounded ``get_dialogs`` call (one network round trip, at most
    ``limit`` dialogs) wrapped in a hard timeout, so the wizard can never hang
    here even on accounts with thousands of chats or on slow networks. A bad
    dialog is skipped instead of aborting the whole scan.
    """
    from bot.config import config
    from telethon.tl.types import Channel, Chat

    dialogs: list[dict] = []

    async def _collect() -> None:
        try:
            dl = await client.get_dialogs(limit=limit)
            for dialog in dl:
                try:
                    entity = dialog.entity
                    if not isinstance(entity, (Channel, Chat)):
                        continue
                    if isinstance(entity, Channel):
                        if entity.broadcast:
                            if not (getattr(entity, "creator", False) or getattr(entity, "admin_rights", None) is not None):
                                continue
                    elif getattr(entity, "read_only", False):
                        if not (getattr(entity, "creator", False) or getattr(entity, "admin_rights", None) is not None):
                            continue
                    title = dialog.name or str(entity.id)
                except Exception as exc:  # noqa: BLE001 - skip one bad dialog
                    log.debug("skipping dialog in fetch_sendable_dialogs: %s", exc)
                    continue
                dialogs.append({"id": entity.id, "title": title})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("fetch_sendable_dialogs collection error: %s", exc)

    try:
        await asyncio.wait_for(_collect(), timeout=config.FETCH_DIALOGS_TIMEOUT)
    except asyncio.TimeoutError:
        log.warning("fetch_sendable_dialogs timed out after %ss", config.FETCH_DIALOGS_TIMEOUT)
    except Exception as exc:
        log.warning("fetch_sendable_dialogs failed: %s", exc)

    seen: set[int] = set()
    unique = []
    for d in dialogs:
        if d["id"] not in seen:
            seen.add(d["id"])
            unique.append(d)
    unique.sort(key=lambda d: d["title"].lower())
    return unique
