from __future__ import annotations

from types import SimpleNamespace

from harness.system_reminder import (
    SystemReminder,
    SystemReminderRegistry,
    append_critical,
    build_system_reminders,
    format_reminders,
    model_disabled,
    system_reminder_note,
)


def test_critical_then_full_order():
    session = SimpleNamespace()
    append_critical(session, "stop now")
    registry = SystemReminderRegistry()
    registry.register(lambda _s: SystemReminder("full", "later note", critical=False))
    text, diag = build_system_reminders(session, model="x", registry=registry)
    assert text.index("stop now") < text.index("later note")
    assert "[system-reminder]" in text
    assert diag.reminder_count == 2
    assert diag.approx_tokens > 0


def test_resolve_critical_is_one_shot():
    session = SimpleNamespace()
    append_critical(session, "once")
    first, _ = build_system_reminders(session, model="x", registry=SystemReminderRegistry())
    second, _ = build_system_reminders(session, model="x", registry=SystemReminderRegistry())
    assert "once" in first
    assert second == ""


def test_per_model_disable(monkeypatch):
    monkeypatch.setenv("HARNESS_DISABLE_SR_MODELS", "openai/,local")
    assert model_disabled("openai/gpt-4")
    assert model_disabled("local-qwen")
    assert not model_disabled("anthropic/claude")
    session = SimpleNamespace()
    append_critical(session, "hidden")
    text, diag = build_system_reminders(session, model="openai/gpt-4")
    assert text == ""
    assert diag.disabled is True


def test_format_critical_only():
    items = [
        SystemReminder("a", "crit", True),
        SystemReminder("b", "full", False),
    ]
    assert "full" not in format_reminders(items, critical_only=True)
    assert "crit" in format_reminders(items, critical_only=True)


def test_provider_failure_is_swallowed():
    registry = SystemReminderRegistry()

    def boom(_session):
        raise RuntimeError("nope")

    registry.register(boom)
    registry.register(lambda _s: SystemReminder("ok", "survived"))
    text, diag = build_system_reminders(SimpleNamespace(), model="x", registry=registry)
    assert "survived" in text
    assert diag.reminder_count == 1


def test_hot_path_helper_never_raises(monkeypatch):
    monkeypatch.setattr(
        "harness.system_reminder.build_system_reminders",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")),
    )
    assert system_reminder_note(SimpleNamespace(), model="x") == ""
