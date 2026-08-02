import asyncio

from bot.handlers import settings


class FakeEvent:
    sender_id = 1


def test_delay_choices_include_extended_values(monkeypatch):
    captured = {}

    async def fake_edit(event, text, buttons):
        captured["text"] = text
        captured["buttons"] = buttons

    def fake_settings_choice_keyboard(prefix, items):
        captured["items"] = items
        return []

    monkeypatch.setattr(settings, "edit", fake_edit)
    monkeypatch.setattr(settings.keyboards, "settings_choice_keyboard", fake_settings_choice_keyboard)

    async def run_test():
        ok = await settings._route(None, FakeEvent(), "set:delay")
        assert ok is True
        values = {value for value, _label in captured["items"]}
        assert {"0", "0.5", "1", "2", "3", "5", "10", "15", "30"}.issubset(values)

    asyncio.run(run_test())
