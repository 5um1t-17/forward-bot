"""Entity resolution: links, usernames, ids and forwarded messages."""
from __future__ import annotations

import logging
import re

from telethon import TelegramClient
from telethon.errors import UsernameInvalidError, ChatIdInvalidError

log = logging.getLogger("bot.resolve")

# https://t.me/username / https://t.me/username/123 / t.me/c/<channel_id>/<msg_id>
TME_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/"
    r"(?:joinchat/)?(?P<slug>[A-Za-z0-9_+-]{1,64})"
    r"(?:/(?P<msg>\d{1,15}))?"
)
# private-channel share link: t.me/c/<channel_id>/<msg_id>
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
        slug = m.group("slug")
        msg_id = int(m.group("msg")) if m.group("msg") else None
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
                # t.me/c/<cid>/<msg> -> private channel
                chat_id = _tme_c_channel_id(parsed["cid"])
                entity = await client.get_entity(chat_id)
                resolved = _to_resolved(entity, "message_link")
                resolved.msg_id = parsed.get("msg_id")
                return resolved
            entity = await client.get_entity(parsed["slug"])
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


def _tme_c_channel_id(n: int) -> int:
    """Convert the number in a t.me/c/<n>/<msg> link to the negative chat id."""
    if n > 1000000000000:
        return -n
    return int(f"-100{n}")


def _to_resolved(entity, input_type: str) -> Resolved:
    title = getattr(entity, "title", None) or getattr(entity, "username", None) or str(entity.id)
    return Resolved(entity=entity, title=title, chat_id=entity.id, input_type=input_type)


async def fetch_sendable_dialogs(client: TelegramClient) -> list[dict]:
    """Group/channel dialogs the account can (likely) post to."""
    from telethon.tl.types import Channel, Chat

    dialogs = []
    try:
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if not isinstance(entity, (Channel, Chat)):
                continue
            if isinstance(entity, Channel):
                if entity.broadcast:
                    # channel: requires admin rights to post
                    if not (getattr(entity, "creator", False) or getattr(entity, "admin_rights", None) is not None):
                        continue
                elif getattr(entity, "read_only", False):
                    # read-only supergroup
                    if not (getattr(entity, "creator", False) or getattr(entity, "admin_rights", None) is not None):
                        continue
            dialogs.append({"id": entity.id, "title": dialog.name or str(entity.id)})
    except Exception as exc:
        log.warning("fetch_sendable_dialogs failed: %s", exc)
    # de-dupe by id
    seen: set[int] = set()
    unique = []
    for d in dialogs:
        if d["id"] not in seen:
            seen.add(d["id"])
            unique.append(d)
    unique.sort(key=lambda d: d["title"].lower())
    return unique
