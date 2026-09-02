from __future__ import annotations

"""SessionActionStore — typed admission machine for mid-session user input.

Domain vocabulary (ranks 1+2+7). DeliveryMode stays the HTTP/composer
vocabulary; this store is what steer/mailbox/redirect/recover admit into.
Stdlib dataclasses only. Snapshot/restore is JSON-safe.
"""

import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


class ActionKind(str, Enum):
    START = "start"
    STEER = "steer"
    REDIRECT = "redirect"
    MAILBOX = "mailbox"
    RECOVER = "recover"


class DeliveryPolicy(str, Enum):
    NEXT_TURN_BOUNDARY = "next_turn_boundary"
    WHEN_RUN_IDLE = "when_run_idle"


class WakePolicy(str, Enum):
    NONE = "none"
    ON_ADMIT = "on_admit"
    ON_IDLE = "on_idle"


class TurnInputMode(str, Enum):
    START_OR_STEER = "start_or_steer"
    START_IF_IDLE = "start_if_idle"
    STEER = "steer"


class SessionActionIllegalTransition(Exception):
    """Named illegal-transition error for admit/restore guards."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = str(code or "illegal_transition")
        super().__init__(message or self.code)


_KIND_DEFAULTS: Dict[ActionKind, Tuple[DeliveryPolicy, WakePolicy]] = {
    ActionKind.START: (DeliveryPolicy.NEXT_TURN_BOUNDARY, WakePolicy.ON_ADMIT),
    ActionKind.STEER: (DeliveryPolicy.NEXT_TURN_BOUNDARY, WakePolicy.NONE),
    ActionKind.REDIRECT: (DeliveryPolicy.NEXT_TURN_BOUNDARY, WakePolicy.ON_ADMIT),
    ActionKind.MAILBOX: (DeliveryPolicy.WHEN_RUN_IDLE, WakePolicy.ON_IDLE),
    ActionKind.RECOVER: (DeliveryPolicy.NEXT_TURN_BOUNDARY, WakePolicy.ON_ADMIT),
}

_INJECTABLE_KINDS = (ActionKind.STEER, ActionKind.MAILBOX)


def normalize_turn_input_mode(requested: Optional[str]) -> Optional[str]:
    """Return a canonical TurnInputMode value, or None when unset/invalid."""
    if requested is None:
        return None
    mode = str(requested).strip().lower().replace("-", "_")
    if not mode:
        return None
    for item in TurnInputMode:
        if item.value == mode:
            return item.value
    return None


def _coerce_enum(value: Any, enum_cls: Any, *, field_name: str) -> Any:
    if isinstance(value, enum_cls):
        return value
    raw = str(value or "").strip().lower().replace("-", "_")
    for item in enum_cls:
        if item.value == raw:
            return item
    raise SessionActionIllegalTransition(
        "unknown_%s" % field_name,
        "unknown %s: %s" % (field_name, value),
    )


def _json_images(images: Optional[Iterable[Any]]) -> List[str]:
    out: List[str] = []
    for item in images or []:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def _optional_turn_id(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass
class SessionAction:
    id: str
    kind: ActionKind
    text: str
    images: List[str] = field(default_factory=list)
    delivery: DeliveryPolicy = DeliveryPolicy.NEXT_TURN_BOUNDARY
    wake: WakePolicy = WakePolicy.NONE
    expected_turn_id: Optional[str] = None
    created_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "kind": self.kind.value,
            "text": str(self.text),
            "images": list(self.images),
            "delivery": self.delivery.value,
            "wake": self.wake.value,
            "expected_turn_id": self.expected_turn_id,
            "created_at": float(self.created_at),
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "SessionAction":
        if not isinstance(data, dict):
            raise SessionActionIllegalTransition(
                "invalid_snapshot", "action snapshot must be an object"
            )
        kind = _coerce_enum(data.get("kind"), ActionKind, field_name="kind")
        delivery = data.get("delivery")
        wake = data.get("wake")
        if delivery in (None, ""):
            delivery = _KIND_DEFAULTS[kind][0]
        if wake in (None, ""):
            wake = _KIND_DEFAULTS[kind][1]
        created = data.get("created_at") or 0.0
        try:
            created_at = float(created)
        except (TypeError, ValueError):
            created_at = 0.0
        return cls(
            id=str(data.get("id") or ""),
            kind=kind,
            text=str(data.get("text") or ""),
            images=_json_images(data.get("images")),
            delivery=_coerce_enum(delivery, DeliveryPolicy, field_name="delivery"),
            wake=_coerce_enum(wake, WakePolicy, field_name="wake"),
            expected_turn_id=_optional_turn_id(data.get("expected_turn_id")),
            created_at=created_at,
        )


class SessionActionStore:
    """In-memory admission queue. Callers that share a session hold ``_steer_lock``."""

    def __init__(self) -> None:
        self._actions: List[SessionAction] = []
        self._closed: bool = False
        self._current_turn_id: Optional[str] = None

    def __iter__(self):
        return iter(self._actions)

    def __len__(self) -> int:
        return len(self._actions)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def current_turn_id(self) -> Optional[str]:
        return self._current_turn_id

    def set_current_turn_id(self, turn_id: Optional[str]) -> None:
        self._current_turn_id = _optional_turn_id(turn_id)

    def close(self) -> None:
        self._closed = True

    def clear(self) -> List[SessionAction]:
        dropped = list(self._actions)
        self._actions = []
        return dropped

    def admit(
        self,
        kind: Any,
        text: str = "",
        *,
        images: Optional[Iterable[Any]] = None,
        delivery: Optional[Any] = None,
        wake: Optional[Any] = None,
        expected_turn_id: Optional[str] = None,
    ) -> SessionAction:
        if self._closed:
            raise SessionActionIllegalTransition(
                "store_closed", "admit after closed"
            )
        resolved = _coerce_enum(kind, ActionKind, field_name="kind")
        expected = _optional_turn_id(expected_turn_id)
        if resolved is ActionKind.RECOVER and not expected:
            raise SessionActionIllegalTransition(
                "recover_requires_expected_turn_id",
                "recover requires expected_turn_id",
            )
        if resolved is ActionKind.STEER and expected is not None:
            current = self._current_turn_id
            if current != expected:
                raise SessionActionIllegalTransition(
                    "steer_turn_mismatch",
                    "steer expected_turn_id does not match current",
                )
        defaults = _KIND_DEFAULTS[resolved]
        delivery_policy = (
            defaults[0]
            if delivery in (None, "")
            else _coerce_enum(delivery, DeliveryPolicy, field_name="delivery")
        )
        wake_policy = (
            defaults[1]
            if wake in (None, "")
            else _coerce_enum(wake, WakePolicy, field_name="wake")
        )
        action = SessionAction(
            id=uuid.uuid4().hex,
            kind=resolved,
            text=str(text or ""),
            images=_json_images(images),
            delivery=delivery_policy,
            wake=wake_policy,
            expected_turn_id=expected,
            created_at=time.time(),
        )
        if resolved is ActionKind.START and not self._current_turn_id:
            self._current_turn_id = expected or action.id
        self._actions.append(action)
        return action

    def admit_turn_input(
        self,
        text: str,
        mode: Any,
        *,
        expected_turn_id: Optional[str] = None,
        idle: bool = True,
        images: Optional[Iterable[Any]] = None,
        delivery: Optional[Any] = None,
        wake: Optional[Any] = None,
    ) -> SessionAction:
        if isinstance(mode, TurnInputMode):
            resolved_mode = mode
        else:
            normalized = normalize_turn_input_mode(mode if isinstance(mode, str) else None)
            if normalized is None:
                raise SessionActionIllegalTransition(
                    "unknown_turn_input_mode",
                    "unknown turn_input_mode: %s" % (mode,),
                )
            resolved_mode = TurnInputMode(normalized)
        if resolved_mode is TurnInputMode.START_IF_IDLE:
            if not idle:
                raise SessionActionIllegalTransition(
                    "start_if_idle_busy",
                    "start_if_idle refused because a run is active",
                )
            kind = ActionKind.START
        elif resolved_mode is TurnInputMode.START_OR_STEER:
            kind = ActionKind.START if idle else ActionKind.STEER
        else:
            kind = ActionKind.STEER
        return self.admit(
            kind,
            text,
            images=images,
            delivery=delivery,
            wake=wake,
            expected_turn_id=expected_turn_id,
        )

    def drain_ready(
        self,
        policy: Any,
        *,
        kinds: Optional[Sequence[Any]] = None,
    ) -> List[SessionAction]:
        delivery = _coerce_enum(policy, DeliveryPolicy, field_name="delivery")
        kind_set = None
        if kinds is not None:
            kind_set = set(
                _coerce_enum(item, ActionKind, field_name="kind") for item in kinds
            )
        ready: List[SessionAction] = []
        kept: List[SessionAction] = []
        for action in self._actions:
            match_kind = kind_set is None or action.kind in kind_set
            if match_kind and action.delivery is delivery:
                ready.append(action)
            else:
                kept.append(action)
        self._actions = kept
        return ready

    def requeue_front(self, actions: Sequence[SessionAction]) -> None:
        """Put previously drained actions back at the front (inject defer)."""
        if not actions:
            return
        self._actions = list(actions) + self._actions

    def admit_front(
        self,
        kind: Any,
        text: str = "",
        **kwargs: Any,
    ) -> SessionAction:
        """Admit, then move that action to the front of the queue."""
        action = self.admit(kind, text, **kwargs)
        if self._actions and self._actions[-1] is action:
            self._actions = [action] + self._actions[:-1]
        return action

    def append_action(self, action: SessionAction) -> None:
        """Append an already-built action (legacy queue view / restore helpers)."""
        self._actions.append(action)

    def snapshot(self) -> Dict[str, Any]:
        payload = {
            "closed": bool(self._closed),
            "current_turn_id": self._current_turn_id,
            "actions": [action.to_dict() for action in self._actions],
        }
        # Fail closed: snapshot must be committible as JSON.
        json.dumps(payload)
        return payload

    def restore(self, data: Optional[Dict[str, Any]]) -> None:
        if data is None:
            self._actions = []
            self._closed = False
            self._current_turn_id = None
            return
        if not isinstance(data, dict):
            raise SessionActionIllegalTransition(
                "invalid_snapshot", "store snapshot must be an object"
            )
        raw_actions = data.get("actions") or []
        if not isinstance(raw_actions, list):
            raise SessionActionIllegalTransition(
                "invalid_snapshot", "actions must be a list"
            )
        restored = [SessionAction.from_dict(row) for row in raw_actions]
        self._actions = restored
        self._closed = bool(data.get("closed"))
        self._current_turn_id = _optional_turn_id(data.get("current_turn_id"))


def injectable_kinds() -> Tuple[ActionKind, ActionKind]:
    return _INJECTABLE_KINDS


class SteerQueueView:
    """Text-facing deque-compatible view over a SessionActionStore.

    Existing tests and late-enqueue race sites treat ``_steer_queue`` as
    strings. The store stays the single queue of SessionAction objects.
    """

    def __init__(self, store: SessionActionStore) -> None:
        self._store = store

    def __iter__(self):
        return (action.text for action in self._store)

    def __len__(self) -> int:
        return len(self._store)

    def __bool__(self) -> bool:
        return len(self._store) > 0

    def append(self, item: Any) -> None:
        if isinstance(item, SessionAction):
            self._store.append_action(item)
            return
        text = str(item or "").strip()
        if not text:
            return
        self._store.admit(
            ActionKind.STEER,
            text,
            delivery=DeliveryPolicy.NEXT_TURN_BOUNDARY,
        )

    def appendleft(self, item: Any) -> None:
        if isinstance(item, SessionAction):
            self._store.requeue_front([item])
            return
        text = str(item or "").strip()
        if not text:
            return
        self._store.admit_front(
            ActionKind.STEER,
            text,
            delivery=DeliveryPolicy.NEXT_TURN_BOUNDARY,
        )

    def clear(self) -> None:
        self._store.clear()
