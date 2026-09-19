from __future__ import annotations

"""Request-only system reminder chain.

Dest-native. Providers append critical or full notes that ride the ephemeral
system prompt for one provider call and are not written to durable history.
Per-model disable via HARNESS_DISABLE_SR_MODELS (comma prefixes).
"""

import os
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Sequence, Tuple

Provider = Callable[[Any], Optional["SystemReminder"]]


@dataclass(frozen=True)
class SystemReminder:
    provider_id: str
    text: str
    critical: bool = False


@dataclass
class SystemReminderDiagnostic:
    model: str
    disabled: bool
    provider_count: int
    reminder_count: int
    chars: int
    approx_tokens: int


@dataclass
class SystemReminderRegistry:
    providers: List[Provider] = field(default_factory=list)

    def register(self, provider: Provider) -> None:
        if provider not in self.providers:
            self.providers.append(provider)

    def resolve(self, session: Any) -> List[SystemReminder]:
        out: List[SystemReminder] = []
        for provider in list(self.providers):
            try:
                item = provider(session)
            except Exception:
                continue
            if item is None:
                continue
            text = str(item.text or "").strip()
            if not text:
                continue
            out.append(item)
        return out


_REGISTRY = SystemReminderRegistry()


def default_registry() -> SystemReminderRegistry:
    return _REGISTRY


def disabled_models() -> Tuple[str, ...]:
    raw = (os.environ.get("HARNESS_DISABLE_SR_MODELS") or "").strip()
    if not raw:
        return ()
    return tuple(part.strip().lower() for part in raw.split(",") if part.strip())


def model_disabled(model: str) -> bool:
    needle = (model or "").strip().lower()
    if not needle:
        return False
    for prefix in disabled_models():
        if needle == prefix or needle.startswith(prefix):
            return True
    return False


def append_critical(session: Any, text: str, *, provider_id: str = "host") -> None:
    bag = getattr(session, "_sr_critical", None)
    if not isinstance(bag, list):
        bag = []
        session._sr_critical = bag
    cleaned = (text or "").strip()
    if cleaned:
        bag.append(SystemReminder(provider_id=provider_id, text=cleaned, critical=True))


def resolve_critical(session: Any) -> List[SystemReminder]:
    bag = getattr(session, "_sr_critical", None)
    if not isinstance(bag, list):
        return []
    session._sr_critical = []
    return [item for item in bag if isinstance(item, SystemReminder)]


def _pending_critical_provider(session: Any) -> Optional[SystemReminder]:
    items = resolve_critical(session)
    if not items:
        return None
    text = "\n".join(item.text for item in items)
    return SystemReminder(provider_id="critical", text=text, critical=True)


def format_reminders(
    reminders: Sequence[SystemReminder],
    *,
    critical_only: bool = False,
) -> str:
    blocks: List[str] = []
    for item in reminders:
        if critical_only and not item.critical:
            continue
        blocks.append(item.text)
    if not blocks:
        return ""
    return "[system-reminder]\n" + "\n\n".join(blocks)


def build_system_reminders(
    session: Any,
    *,
    model: str = "",
    registry: Optional[SystemReminderRegistry] = None,
) -> Tuple[str, SystemReminderDiagnostic]:
    spec = (model or "").strip()
    disabled = model_disabled(spec)
    chain = registry if registry is not None else default_registry()
    if disabled:
        diag = SystemReminderDiagnostic(
            model=spec, disabled=True, provider_count=len(chain.providers),
            reminder_count=0, chars=0, approx_tokens=0,
        )
        return "", diag
    reminders = []
    pending = _pending_critical_provider(session)
    if pending is not None:
        reminders.append(pending)
    reminders.extend(chain.resolve(session))
    text = format_reminders(reminders)
    diag = SystemReminderDiagnostic(
        model=spec,
        disabled=False,
        provider_count=len(chain.providers),
        reminder_count=len(reminders),
        chars=len(text),
        approx_tokens=max(0, (len(text) + 3) // 4),
    )
    return text, diag


def system_reminder_note(session: Any, *, model: str = "") -> str:
    """Hot-path helper. Never raises. Request-only; caller must not persist."""
    try:
        text, _diag = build_system_reminders(session, model=model)
        return text
    except Exception:
        return ""
