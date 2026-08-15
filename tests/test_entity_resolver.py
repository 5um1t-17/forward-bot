import asyncio
from types import SimpleNamespace

from bot.entity_resolver import parse_input, resolve


class FakeEntity:
    def __init__(self, id, title="Chat"):
        self.id = id
        self.title = title
        self.username = None


class FakeInviteClient:
    def __init__(self):
        self.requests = []

    async def get_entity(self, value):
        self.requests.append(value)
        if value == "joinchat/abc123":
            return FakeEntity(-100444, "Restricted Group")
        raise ValueError("unknown")


def test_parse_input_supports_joinchat_message_links():
    parsed = parse_input("https://t.me/joinchat/abc123/12")
    assert parsed["kind"] == "message_link"
    assert parsed["identifier"] == "joinchat/abc123"
    assert parsed["msg_id"] == 12


def test_resolve_imports_joinchat_links_for_restricted_groups():
    client = FakeInviteClient()
    resolved = asyncio.run(resolve(client, "https://t.me/joinchat/abc123/12"))

    assert resolved is not None
    assert resolved.chat_id == -100444
    assert resolved.title == "Restricted Group"
    assert resolved.msg_id == 12
    assert len(client.requests) == 1
