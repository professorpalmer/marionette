"""Frozen normalized inputs and supported provider JSON request bodies."""
from __future__ import annotations

from copy import copy
from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Callable
from uuid import uuid4


def canonical_bytes(value: Any) -> bytes:
    # No default=str: unsupported values cannot silently compare as equal.
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


@dataclass(frozen=True)
class FrozenRequest:
    request_id: str
    payload: bytes = field(repr=False)
    method: Callable[..., Any] = field(repr=False, compare=False)

    boundary: Any = field(default=None, repr=False, compare=False)
    recovery_method: Any = field(default=None, repr=False, compare=False)

    def wire_receipt(self) -> dict:
        return self.boundary.receipt() if self.boundary else {
            "wire_status": "unsupported", "model_status": "observed_only",
            "wire_scope": "none",
        }

    @classmethod
    def capture(cls, method: Callable[..., Any], value: Any,
                kwargs: dict, model: Any = None) -> FrozenRequest:
        # Model is driver-owned state, not a dispatch argument or wire guarantee.
        observed_model = model if isinstance(model, str) else None
        payload = canonical_bytes({'input': value, 'kwargs': kwargs,
                                   'model_observed': observed_model})
        frozen = json.loads(payload)
        method, boundary, recovery_method = _prepare_boundary(
            method, frozen['input'], frozen['kwargs'],
        )
        return cls(uuid4().hex, payload, method, boundary, recovery_method)

    def materialize(self) -> tuple:
        value = json.loads(self.payload)
        return value['input'], value['kwargs']

    def receipt(self, value: Any, kwargs: dict) -> dict:
        source = json.loads(self.payload)
        actual = canonical_bytes({'input': value, 'kwargs': kwargs,
                                  'model_observed': source['model_observed']})
        ok = actual == self.payload
        return {
            'ok': ok, 'status': 'verified' if ok else 'mismatch',
            'reason': '' if ok else 'normalized_inputs',
            'scope': 'normalized_driver_inputs', 'wire_status': 'unverified',
            'model_status': 'observed_only', 'request_id': self.request_id,
            **self.wire_receipt(),
            'source_sha256': hashlib.sha256(self.payload).hexdigest(),
            'dispatch_sha256': hashlib.sha256(actual).hexdigest(),
            'message_count': len(value) if isinstance(value, list) else 0,
            'tool_count': len(kwargs.get('tools') or []),
        }


class _BodyBoundary:
    def __init__(self, body):
        self.payload = canonical_bytes(body)
        self.status = 'absent'
        self.last_digest = None
        self.attempts = 0
        self.recovery = None
        self.continuations = []
        self.fallbacks = []

    def observe(self, data):
        self.attempts += 1
        try:
            actual = canonical_bytes(json.loads(data))
            self.last_digest = hashlib.sha256(actual).hexdigest()
            if actual != self.payload:
                for fallback in self.fallbacks:
                    if actual == fallback.payload:
                        fallback.observe(data)
                        return
                self.status = 'mismatch'
            elif self.status != 'mismatch':
                self.status = 'verified'
        except (TypeError, ValueError, UnicodeError):
            self.status = 'mismatch'

    def receipt(self):
        result = {
            'wire_status': self.status,
            'wire_scope': 'urllib_request_json_body',
            'model_status': 'frozen_body',
            'wire_source_sha256': hashlib.sha256(self.payload).hexdigest(),
            'wire_dispatch_sha256': self.last_digest,
            'wire_attempts': self.attempts,
        }
        if self.recovery is not None and self.recovery.attempts:
            result['recovery_wire'] = self.recovery.receipt()
        continuation_receipts = [item.receipt() for item in self.continuations if item.attempts]
        if continuation_receipts:
            result['continuation_wire'] = continuation_receipts
        fallback_receipts = [item.receipt() for item in self.fallbacks if item.attempts]
        if fallback_receipts:
            result['fallback_wire'] = fallback_receipts
        nested = (([result.get('recovery_wire')] if result.get('recovery_wire') else [])
                  + continuation_receipts + fallback_receipts)
        if any(item['wire_status'] == 'mismatch' for item in nested):
            result['wire_status'] = 'mismatch'
        return result


def _is_length_continuation(original, candidate, marker):
    if not isinstance(candidate, list) or len(candidate) <= len(original):
        return False
    if canonical_bytes(candidate[:len(original)]) != canonical_bytes(original):
        return False
    suffix = candidate[len(original):]
    if len(suffix) % 2:
        return False
    for index in range(0, len(suffix), 2):
        assistant, user = suffix[index:index + 2]
        if (not isinstance(assistant, dict) or set(assistant) != {'role', 'content'}
                or assistant.get('role') != 'assistant'
                or not isinstance(assistant.get('content'), str)):
            return False
        if user != {'role': 'user', 'content': marker}:
            return False
    return True


def _allow_reasoning_fallback(boundary):
    body = json.loads(boundary.payload)
    if 'reasoning' in body:
        body.pop('reasoning')
        boundary.fallbacks.append(_BodyBoundary(body))
    return boundary


def _build_from_detached_inputs(builder, *args, **kwargs):
    detached_args = json.loads(canonical_bytes(list(args)))
    detached_kwargs = json.loads(canonical_bytes(kwargs))
    return builder(*detached_args, **detached_kwargs)


def _prepare_boundary(method, value, kwargs):
    # Exact classes only: subclasses may override transport or body construction.
    from pmharness.drivers.openai_compat import OpenAICompatDriver
    from pmharness.drivers.anthropic import AnthropicDriver
    from pmharness.drivers.codex_responses import CodexResponsesDriver

    owner = getattr(method, '__self__', None)
    name = getattr(method, '__name__', '')
    if type(owner) not in (OpenAICompatDriver, AnthropicDriver, CodexResponsesDriver):
        return method, None, None
    if name not in ('chat', 'chat_stream'):
        return method, None, None
    if getattr(method, '__func__', None) is not getattr(type(owner), name):
        return method, None, None
    # Preserve transport/client references and resolve credentials at send time.
    # Only the generation body is serialized; no auth headers enter the snapshot.
    driver = copy(owner)
    comparison_value = json.loads(canonical_bytes(value))
    if type(owner) is OpenAICompatDriver:
        # A shallow driver copy must not retain mutable request settings. Length
        # continuations rebuild their body later, after the initial dispatch.
        driver.extra_body = json.loads(canonical_bytes(owner.extra_body))
        body = _build_from_detached_inputs(
            driver._build_chat_body, value, **kwargs,
            stream=name == 'chat_stream',
        )
        builder = '_build_chat_body'
    else:
        body = _build_from_detached_inputs(driver._build_body, value, **kwargs)
        builder = '_build_body'
        if type(owner) is AnthropicDriver and name == 'chat_stream':
            body['stream'] = True
    boundary = _BodyBoundary(body)
    if type(owner) is OpenAICompatDriver and name == 'chat':
        _allow_reasoning_fallback(boundary)
    if type(owner) is OpenAICompatDriver and name == 'chat_stream':
        from pmharness.drivers.openai_compat import _OPENAI_LENGTH_CONTINUE

        original_builder = driver._build_chat_body
        boundary.recovery = _allow_reasoning_fallback(
            _BodyBoundary(_build_from_detached_inputs(
                driver._build_chat_body, value, **kwargs,
            ))
        )

        def frozen_chat_body(*a, **kw):
            messages = a[0] if a else kw.get('messages')
            if not kw.get('stream', False):
                if canonical_bytes(messages) == canonical_bytes(comparison_value):
                    selected = boundary.recovery
                    recovery_body = json.loads(selected.payload)
                    if not driver.enable_reasoning and 'reasoning' in recovery_body:
                        recovery_body.pop('reasoning')
                        selected = _BodyBoundary(recovery_body)
                        boundary.fallbacks.append(selected)
                elif _is_length_continuation(
                    comparison_value, messages, _OPENAI_LENGTH_CONTINUE,
                ):
                    selected = _allow_reasoning_fallback(
                        _BodyBoundary(_build_from_detached_inputs(
                            original_builder, *a, **kw,
                        ))
                    )
                    boundary.continuations.append(selected)
                else:
                    raise ValueError('frozen request rejected unexpected message mutation')
            else:
                if canonical_bytes(messages) == canonical_bytes(comparison_value):
                    selected = boundary
                elif _is_length_continuation(
                    comparison_value, messages, _OPENAI_LENGTH_CONTINUE,
                ):
                    selected = _BodyBoundary(_build_from_detached_inputs(
                        original_builder, *a, **kw,
                    ))
                    boundary.continuations.append(selected)
                else:
                    raise ValueError('frozen request rejected unexpected message mutation')
            driver._request_body_observer = selected.observe
            return json.loads(selected.payload)

        setattr(driver, builder, frozen_chat_body)
    else:
        setattr(driver, builder, lambda *a, **kw: json.loads(boundary.payload))
    driver._request_body_observer = boundary.observe
    recovery_method = driver.chat if type(owner) is OpenAICompatDriver else None
    return getattr(driver, name), boundary, recovery_method
