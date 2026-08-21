from __future__ import annotations

"""OpenAICompatDriver: drives any OpenAI-compatible chat endpoint. Kimi
(Moonshot), GLM (z.ai), OpenAI, and most open-weights providers all expose this
schema, so one driver covers the whole registry. stdlib-only (urllib) to keep
the rig dependency-light and auditable.

Keys are read from the environment at call time and never logged.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from .base import DriverResponse, SYSTEM_PROMPT
from .prompt_cache import (
    apply_openai_compat_cache_control,
    maybe_attach_openrouter_session_id,
)
from .retry import with_retry
from pmharness.reasoning import extract_reasoning, strip_think_blocks
from pmharness.think_scrubber import StreamingThinkScrubber

_SUCCESS_CHAT_FINISH = frozenset({"stop", "stop_sequence", "end_turn"})
_TOOL_CHAT_FINISH = frozenset({"tool_calls", "function_call"})
_LENGTH_CHAT_FINISH = frozenset({"length", "max_tokens", "max_output_tokens"})
_FILTER_CHAT_FINISH = frozenset({"content_filter"})
_INCOMPLETE_CHAT_FINISH = frozenset({"incomplete", "empty"})


def _norm_chat_finish(finish) -> str:
    """Case-insensitive provider finish token. Empty stays empty."""
    if finish is None or isinstance(finish, bool):
        return ""
    try:
        return str(finish).strip().lower()
    except Exception:
        return ""


def _tool_arguments_are_complete(arguments) -> bool:
    """True when tool arguments are empty or parse as JSON.

    Empty arguments are valid for no-arg tools. Nonempty text that does
    not parse is truncated mid-stream and must not dispatch.
    """
    raw = arguments if isinstance(arguments, str) else ""
    if not raw.strip():
        return True
    try:
        json.loads(raw)
    except Exception:
        return False
    return True


def _executable_stream_tool_calls(assembled: dict, finish_reason: str) -> list:
    """Return only tool calls that are safe to dispatch.

    Missing ids stay empty so the conversation layer can canonicalize them.
    Truncated arguments and length-capped / filtered streams never become
    executable ``tool_calls``. A tool close is executable only when the
    finish is ``tool_calls`` / ``function_call`` and arguments parse.
    """
    finish = _norm_chat_finish(finish_reason)
    if finish not in _TOOL_CHAT_FINISH:
        return []
    out = []
    for idx in sorted(assembled.keys(), key=lambda k: (isinstance(k, int), k)):
        tc = assembled[idx]
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        if not _tool_arguments_are_complete(fn.get("arguments") or ""):
            continue
        out.append(tc)
    return out


def _split_chat_tool_calls(raw_calls) -> tuple[list, list]:
    """Split provider tool_calls into executable vs truncated/incomplete."""
    assembled: dict = {}
    if isinstance(raw_calls, list):
        for idx, tc in enumerate(raw_calls):
            if isinstance(tc, dict):
                assembled[idx] = tc
    elif isinstance(raw_calls, dict):
        assembled = raw_calls
    executable = _executable_stream_tool_calls(assembled, "tool_calls")
    return executable, _incomplete_stream_tool_calls(assembled, executable)


def _openai_chat_terminal(
    finish: str,
    *,
    executable: list | None = None,
    incomplete_tools: list | None = None,
    stream_started: bool = False,
    malformed_sse_chunks: int = 0,
    transport_error: str | None = None,
) -> tuple[str, str | None]:
    """Classify a chat Completions finish. Never invents a natural stop.

    Returns ``(stream_terminal, error)``. Known reasons are named truthfully.
    Truncated tool JSON is incomplete even when finish_reason is tool_calls.
    """
    if transport_error:
        return "error", transport_error
    finish_raw = "" if finish is None or isinstance(finish, bool) else str(finish).strip()
    finish = _norm_chat_finish(finish_raw)
    executable = executable or []
    incomplete_tools = incomplete_tools or []
    if finish in _LENGTH_CHAT_FINISH:
        return "length", f"OpenAI chat finished with finish_reason={finish_raw}"
    if finish in _FILTER_CHAT_FINISH:
        return "content_filter", f"OpenAI chat finished with finish_reason={finish_raw}"
    if finish in _INCOMPLETE_CHAT_FINISH:
        return "incomplete", f"OpenAI chat finished with finish_reason={finish_raw}"
    if finish in _TOOL_CHAT_FINISH:
        if incomplete_tools and not executable:
            return (
                "incomplete",
                f"OpenAI chat finished with finish_reason={finish_raw} "
                "but tool arguments were truncated",
            )
        if executable:
            return "tool_calls", None
        return (
            "incomplete",
            f"OpenAI chat finished with finish_reason={finish_raw} "
            "but no executable tool calls",
        )
    if finish in _SUCCESS_CHAT_FINISH:
        return "stop", None
    if finish_raw:
        return (
            "incomplete",
            f"OpenAI chat finished with unrecognized finish_reason={finish_raw}",
        )
    if not stream_started:
        if malformed_sse_chunks:
            return "empty", "OpenAI chat stream had no valid chunks"
        return "empty", "OpenAI chat ended without a finish_reason"
    return "incomplete", "OpenAI chat stream ended without a finish_reason"


def _incomplete_stream_tool_calls(assembled: dict, executable: list) -> list:
    executable_ids = {id(tc) for tc in executable}
    return [
        assembled[idx]
        for idx in sorted(assembled.keys(), key=lambda k: (isinstance(k, int), k))
        if isinstance(assembled[idx], dict) and id(assembled[idx]) not in executable_ids
    ]


class _OpenAIChatSseAccumulator:
    """Host-agnostic OpenAI chat Completions SSE parser.

    Ox Alpha, OpenRouter, and OpenCode Go share this contract. Request-body
    host overlays (stream_options / parallel_tool_calls) stay outside.
    """

    def __init__(
        self,
        *,
        on_delta=None,
        on_reasoning_delta=None,
        on_tool_hint=None,
    ) -> None:
        self.on_delta = on_delta
        self.on_reasoning_delta = on_reasoning_delta
        self.on_tool_hint = on_tool_hint
        self.full_text = ""
        self.reasoning_pieces: list = []
        self.assembled_tool_calls: dict = {}
        self.last_tool_call_index = None
        self.finish_reason = ""
        self.tokens_in = 0
        self.tokens_out = 0
        self.cached_tokens = 0
        self.cache_write_tokens = 0
        self.stream_has_cache_read = False
        self.stream_has_cache_write = False
        self.provider_cost_usd = None
        self.stream_raw_usage = None
        self.served_model = None
        self.stream_started = False
        self.saw_done = False
        self.malformed_sse_chunks = 0
        self.think_scrubber = StreamingThinkScrubber()

    def feed(self, line) -> bool:
        """Consume one SSE line. Return False after ``[DONE]``."""
        line_str = (
            line.decode("utf-8", "replace").strip()
            if isinstance(line, bytes)
            else str(line).strip()
        )
        if not line_str:
            return True
        if not line_str.startswith("data:"):
            return True
        data_str = line_str[5:].strip()
        if not data_str:
            return True
        if data_str == "[DONE]":
            self.saw_done = True
            return False
        try:
            chunk = json.loads(data_str)
        except Exception:
            self.malformed_sse_chunks += 1
            return True
        if not isinstance(chunk, dict):
            self.malformed_sse_chunks += 1
            return True

        chunk_served = OpenAICompatDriver._served_model_from_payload(chunk)
        if chunk_served:
            self.served_model = chunk_served

        chunk_usage = chunk.get("usage")
        if chunk_usage:
            from .token_usage import coerce_token_usage_record

            usage_detail = coerce_token_usage_record(chunk_usage)
            self.tokens_in = int(usage_detail.tokens_in)
            self.tokens_out = int(usage_detail.tokens_out)
            if OpenAICompatDriver._usage_reports_cache_read(chunk_usage):
                self.stream_has_cache_read = True
                self.cached_tokens = int(usage_detail.cache_read)
            if OpenAICompatDriver._usage_reports_cache_write(chunk_usage):
                self.stream_has_cache_write = True
                self.cache_write_tokens = int(usage_detail.cache_write)
            self.stream_raw_usage = chunk_usage
            step_cost = OpenAICompatDriver._cost_from_usage(chunk_usage)
            if step_cost is not None:
                self.provider_cost_usd = step_cost

        choices = chunk.get("choices") or []
        if not choices:
            return True
        choice = choices[0]
        if not isinstance(choice, dict):
            return True
        delta = choice.get("delta") or {}
        if not isinstance(delta, dict):
            delta = {}

        content_delta = delta.get("content") or ""
        if content_delta:
            self.stream_started = True
            self.full_text += content_delta
            visible = self.think_scrubber.feed(content_delta)
            if visible and self.on_delta is not None:
                self.on_delta(visible)

        reasoning_delta = (
            delta.get("reasoning")
            or delta.get("reasoning_content")
            or ""
        )
        if reasoning_delta:
            self.stream_started = True
            self.reasoning_pieces.append(reasoning_delta)
            if self.on_reasoning_delta is not None:
                self.on_reasoning_delta(reasoning_delta)

        delta_tool_calls = delta.get("tool_calls") or []
        if delta_tool_calls:
            self.stream_started = True
        for chunk_pos, tc in enumerate(delta_tool_calls):
            if not isinstance(tc, dict):
                continue
            idx = OpenAICompatDriver._resolve_stream_tool_call_index(
                tc,
                self.assembled_tool_calls,
                chunk_pos=chunk_pos,
                chunk_count=len(delta_tool_calls),
                last_index=self.last_tool_call_index,
            )
            self.last_tool_call_index = idx
            tc_func = tc.get("function") or {}
            name_piece = tc_func.get("name") or ""
            if idx not in self.assembled_tool_calls:
                self.assembled_tool_calls[idx] = {
                    "id": tc.get("id") or "",
                    "type": tc.get("type") or "function",
                    "function": {
                        "name": name_piece,
                        "arguments": tc_func.get("arguments") or "",
                    },
                }
            else:
                existing = self.assembled_tool_calls[idx]
                if tc.get("id"):
                    existing["id"] = tc.get("id")
                if tc.get("type"):
                    existing["type"] = tc.get("type")
                if name_piece:
                    existing["function"]["name"] += name_piece
                if tc_func.get("arguments"):
                    existing["function"]["arguments"] += tc_func["arguments"]
            hint = self.assembled_tool_calls[idx]["function"]["name"]
            if name_piece and hint and self.on_tool_hint is not None:
                self.on_tool_hint(hint)

        chunk_finish_reason = choice.get("finish_reason")
        if chunk_finish_reason:
            self.finish_reason = chunk_finish_reason
        return True

    def flush_scrubber(self) -> None:
        try:
            scrub_tail = self.think_scrubber.flush()
        except Exception:
            scrub_tail = ""
        if scrub_tail and self.on_delta is not None:
            self.on_delta(scrub_tail)

    def finalize(self, *, transport_error: str | None = None) -> dict:
        """Apply fail-closed terminal rules. Never drops accumulated progress."""
        self.flush_scrubber()
        message_obj = {"content": self.full_text}
        accumulated_reasoning = "".join(self.reasoning_pieces)
        if accumulated_reasoning:
            message_obj["reasoning"] = accumulated_reasoning
            message_obj["reasoning_content"] = accumulated_reasoning
        reasoning = extract_reasoning(message_obj)
        pure_text = strip_think_blocks(self.full_text)
        executable = _executable_stream_tool_calls(
            self.assembled_tool_calls, self.finish_reason,
        )
        incomplete_tools = _incomplete_stream_tool_calls(
            self.assembled_tool_calls, executable,
        )
        finish = str(self.finish_reason or "")
        stream_terminal, error = _openai_chat_terminal(
            finish,
            executable=executable,
            incomplete_tools=incomplete_tools,
            stream_started=self.stream_started,
            malformed_sse_chunks=self.malformed_sse_chunks,
            transport_error=transport_error,
        )
        return {
            "text": pure_text,
            "reasoning": reasoning,
            "tool_calls": executable,
            "incomplete_tool_calls": incomplete_tools,
            "finish_reason": finish,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "stream_started": self.stream_started,
            "saw_done": self.saw_done,
            "malformed_sse_chunks": self.malformed_sse_chunks,
            "stream_terminal": stream_terminal,
            "error": error,
            "served_model": self.served_model,
            "stream_raw_usage": self.stream_raw_usage,
            "stream_has_cache_read": self.stream_has_cache_read,
            "stream_has_cache_write": self.stream_has_cache_write,
            "cached_tokens": self.cached_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "provider_cost_usd": self.provider_cost_usd,
        }


def _consume_openai_chat_sse(
    lines,
    *,
    on_delta=None,
    on_reasoning_delta=None,
    on_tool_hint=None,
    transport_error: str | None = None,
) -> dict:
    """Shared OpenAI chat Completions SSE parser used by every host."""
    acc = _OpenAIChatSseAccumulator(
        on_delta=on_delta,
        on_reasoning_delta=on_reasoning_delta,
        on_tool_hint=on_tool_hint,
    )
    for line in lines:
        if not acc.feed(line):
            break
    return acc.finalize(transport_error=transport_error)


class OpenAICompatDriver:
    # Explicit capability flag the conversation loop checks (is True) before using the
    # streaming path -- prevents MagicMock test doubles from accidentally streaming.
    supports_streaming = True
    # Network driver: never accept implicit JSON-envelope natural. Stamp an
    # explicit finish / stream_terminal or fail closed.
    requires_explicit_terminal = True

    def __init__(
        self,
        name: str,
        model: str,
        base_url: str,
        api_key_env: str,
        *,
        temperature: float = 0.0,
        max_tokens: int = 1500,
        timeout: int = 90,
        extra_headers: dict | None = None,
        extra_body: dict | None = None,
        enable_reasoning: bool = False,
        session_id: str | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.extra_headers = extra_headers or {}
        # Provider-specific request fields (e.g. an OpenCode Go model's native
        # `thinking` / `reasoning_effort` dialect) merged into every request.
        self.extra_body = extra_body or {}
        self.enable_reasoning = enable_reasoning
        self.session_id = session_id
        # Set by _key() when a credential-pool entry is selected.
        self._pool_provider: str | None = None
        self._pool_entry_id: str | None = None

    def _key(self) -> str:
        """Resolve API key: credential pool first, then process env."""
        self._pool_provider = None
        self._pool_entry_id = None
        try:
            from harness.credential_pool import resolve_entry_for_env
            entry = resolve_entry_for_env(self.api_key_env)
            if entry is not None and entry.runtime_token:
                self._pool_provider = entry.provider
                self._pool_entry_id = entry.id
                return entry.runtime_token
        except Exception:
            pass
        key = os.environ.get(self.api_key_env, "").strip()
        if not key:
            raise RuntimeError(f"missing API key in env var {self.api_key_env}")
        return key

    def _uses_openai_gpt5_chat_parameters(self) -> bool:
        return (
            self.base_url == "https://api.openai.com/v1"
            and self.model.lower().startswith("gpt-5")
        )

    def _is_openrouter_host(self) -> bool:
        try:
            host = (urllib.parse.urlparse(self.base_url or "").hostname or "").lower()
        except Exception:
            return False
        return host == "openrouter.ai" or host.endswith(".openrouter.ai")

    def _is_opencode_go_host(self) -> bool:
        """True for the OpenCode Go relay (``https://opencode.ai/zen/go/v1``)."""
        try:
            parsed = urllib.parse.urlparse(self.base_url or "")
        except Exception:
            return False
        host = (parsed.hostname or "").lower()
        if host != "opencode.ai" and not host.endswith(".opencode.ai"):
            return False
        path = (parsed.path or "").lower()
        return "/zen/go" in path

    def _apply_openrouter_parallel_tool_calls(self, body: dict, tools) -> None:
        """OpenRouter accepts parallel_tool_calls; OpenCode Go/unknown relays do not."""
        if tools and self._is_openrouter_host():
            body["parallel_tool_calls"] = True
        else:
            body.pop("parallel_tool_calls", None)

    @staticmethod
    def _is_empty_chat_completion_400_stub(code: int, detail: str) -> bool:
        """True for the empty chat.completion HTTP 400 Muse/OpenCode Go returns.

        That stub is a completion-shaped JSON body (object chat.completion /
        chatcompletion) with no error message — not a real invalid_request
        explanation. Match narrowly so actionable 400s still surface.
        """
        if code != 400:
            return False
        text = str(detail or "").strip()
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return False
        try:
            obj = json.loads(text[start : end + 1])
        except Exception:
            return False
        if not isinstance(obj, dict):
            return False
        kind = str(obj.get("object") or "").lower()
        if kind not in ("chat.completion", "chatcompletion"):
            return False
        err = obj.get("error")
        if isinstance(err, dict) and str(err.get("message") or "").strip():
            return False
        if isinstance(err, str) and err.strip():
            return False
        choices = obj.get("choices")
        if not isinstance(choices, list) or not choices:
            return False
        first = choices[0]
        if not isinstance(first, dict) or first.get("finish_reason") not in (None, ""):
            return False
        message = first.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return False
        if str(message.get("content") or "").strip():
            return False
        if message.get("tool_calls"):
            return False
        return True

    @staticmethod
    def _next_stream_tool_call_index(assembled: dict) -> int:
        ints = [idx for idx in assembled if isinstance(idx, int)]
        return (max(ints) + 1) if ints else 0

    @staticmethod
    def _resolve_stream_tool_call_index(
        tc: dict,
        assembled: dict,
        *,
        chunk_pos: int,
        chunk_count: int,
        last_index: int | None,
    ) -> int:
        """Pick the assembled-tool-call slot for one streamed tool_call delta.

        Explicit ``index`` wins. Otherwise a nonempty id joins an existing call.
        Multiple indexless calls in one chunk stay distinct via chunk position.
        Idless name/argument fragments continue the sole or most recent call.
        New calls get the next deterministic index.
        """
        if not isinstance(tc, dict):
            return OpenAICompatDriver._next_stream_tool_call_index(assembled)
        raw_idx = tc.get("index")
        if raw_idx is not None:
            try:
                return int(raw_idx)
            except (TypeError, ValueError):
                pass
        call_id = tc.get("id") if isinstance(tc.get("id"), str) else ""
        if call_id:
            for idx, existing in assembled.items():
                if isinstance(existing, dict) and existing.get("id") == call_id:
                    return idx
        if chunk_count > 1:
            if chunk_pos not in assembled:
                return chunk_pos
            if not call_id:
                return chunk_pos
        if not call_id and assembled:
            if len(assembled) == 1:
                return next(iter(assembled))
            if last_index is not None:
                return last_index
            ints = [idx for idx in assembled if isinstance(idx, int)]
            if ints:
                return max(ints)
        return OpenAICompatDriver._next_stream_tool_call_index(assembled)

    def _output_token_limit_field(self) -> str:
        """Return the Chat Completions output-limit field for this endpoint/model."""
        return (
            "max_completion_tokens"
            if self._uses_openai_gpt5_chat_parameters()
            else "max_tokens"
        )

    def _apply_temperature(self, body: dict) -> None:
        if not self._uses_openai_gpt5_chat_parameters():
            body["temperature"] = self.temperature

    def _apply_openai_gpt5_chat_constraints(self, body: dict) -> None:
        """Keep direct OpenAI GPT-5 Chat Completions on fields the API accepts.

        extra_body is applied first and can reintroduce legacy OpenAI /
        OpenRouter knobs (temperature, max_tokens, reasoning). Strip those
        after the merge so a GPT-5 request cannot 400 on rejected fields.
        """
        if not self._uses_openai_gpt5_chat_parameters() or not isinstance(body, dict):
            return
        body.pop("temperature", None)
        limit = body.pop("max_tokens", None)
        if limit is not None:
            body.setdefault("max_completion_tokens", limit)
        body.pop("reasoning", None)
        if body.get("tools"):
            body["reasoning_effort"] = "none"

    def _pool_rotate_backoff(
        self,
        code: int,
        detail: str,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Pause after a pool rotate so the next key is not stampeded immediately.

        Rate limits honor Retry-After (capped); other rotate-eligible codes take
        a short fixed pause. Classification comes from error_classifier so the
        rotate path does not invent its own retry policy.
        """
        from . import error_classifier

        err_class = error_classifier.classify(code, detail)
        if err_class == error_classifier.ErrorClass.RATE_LIMIT:
            retry_after = error_classifier.parse_retry_after(detail)
            delay = float(retry_after) if retry_after is not None else 1.0
            sleep(min(max(delay, 0.5), 20.0))
            return
        sleep(0.25)

    def _pool_rotate_on_http_error(
        self,
        code: int,
        detail: str,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> str | None:
        """On 429 plan-limit / 402 / auth fail, rotate pool and return next key."""
        if not self._pool_provider or not self._pool_entry_id:
            return None
        if code not in (401, 402, 429):
            return None
        try:
            from harness.credential_pool import report_failure
            nxt = report_failure(
                self._pool_provider,
                self._pool_entry_id,
                status_code=code,
                message=detail or "",
            )
            if nxt:
                self._pool_rotate_backoff(code, detail, sleep=sleep)
                # Re-select so _pool_entry_id tracks the new entry
                self._key()
                return nxt
        except Exception:
            pass
        return None

    def _reasoning_unsupported(self, code: int, detail: str) -> bool:
        """True when an endpoint rejected the OpenRouter-style `reasoning` field.

        Many OpenAI-compatible endpoints/models do not accept the `reasoning`
        parameter and return a 400 'Unknown parameter: reasoning'. When we see
        that, we disable reasoning for the rest of the session and let the caller
        retry once -- so a model that lacks reasoning support self-heals instead
        of hard-failing every pilot turn.
        """
        if code != 400 or not self.enable_reasoning:
            return False
        d = (detail or "").lower()
        return "reasoning" in d and ("unknown parameter" in d or "unsupported" in d
                                     or "invalid_request" in d or "not supported" in d
                                     or "unexpected" in d)

    def _prepare_body(
        self,
        body: dict,
        *,
        messages: list | None = None,
        system: str | None = None,
        session_id: str | None = None,
    ) -> dict:
        """Stamp provider extras, explicit cache_control (Claude/Qwen), and the
        OpenRouter session_id.

        Best-effort: never raises; automatic-cache models are left untouched.
        """
        for field, value in self.extra_body.items():
            # Conversation shape and streaming are the caller's; extras only add
            # provider-dialect knobs on top.
            if field not in ("model", "messages", "stream", "tools", "tool_choice"):
                body[field] = value
        try:
            if "openrouter.ai" in (self.base_url or "").lower():
                # Ask OpenRouter for prompt_tokens_details (cached / cache_write).
                body.setdefault("usage", {"include": True})
            apply_openai_compat_cache_control(body, model=self.model)
            maybe_attach_openrouter_session_id(
                body,
                base_url=self.base_url,
                session_id=session_id if session_id is not None else self.session_id,
                messages=messages if messages is not None else body.get("messages"),
                system=system,
            )
        except Exception:
            pass
        self._apply_openai_gpt5_chat_constraints(body)
        return body

    @staticmethod
    def _cache_fields_from_usage(usage: dict) -> tuple[int, int]:
        """Return (cache_read_tokens, cache_write_tokens) via shared normalization."""
        from .token_usage import coerce_token_usage_record

        detail = coerce_token_usage_record(usage or {})
        return int(detail.cache_read), int(detail.cache_write)

    @staticmethod
    def _cost_from_usage(usage: dict):
        """Return provider-billed USD from a usage blob, or None if absent.

        OpenRouter always includes ``usage.cost`` (credits charged to the
        account). Prefer this over token*catalog math -- cache-read multipliers
        and registry prices drift from what was actually billed.
        """
        usage = usage or {}
        raw = usage.get("cost")
        if raw is None:
            return None
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return None
        if val != val or val < 0.0:  # NaN or negative
            return None
        return val

    @staticmethod
    def _usage_reports_cache_read(usage: dict) -> bool:
        """True when the provider usage blob explicitly carries a cache-read field."""
        if not isinstance(usage, dict):
            return False
        keys = (
            "cache_read_tokens",
            "cache_read_input_tokens",
            "cached_input_tokens",
            "cacheReadTokens",
            "cachedInputTokens",
            "cacheReadInputTokens",
            "cacheReadInputTokenCount",
            "cached_tokens",
            "cachedTokens",
            "tokens_cached",
        )
        if any(k in usage for k in keys):
            return True
        for nest_key in (
            "prompt_tokens_details",
            "input_tokens_details",
            "promptTokensDetails",
            "inputTokensDetails",
        ):
            nest = usage.get(nest_key)
            if isinstance(nest, dict) and OpenAICompatDriver._usage_reports_cache_read(nest):
                return True
        return False

    @staticmethod
    def _usage_reports_cache_write(usage: dict) -> bool:
        """True when the provider usage blob explicitly carries a cache-write field."""
        if not isinstance(usage, dict):
            return False
        keys = (
            "cache_write_tokens",
            "cache_creation_input_tokens",
            "cacheWriteTokens",
            "cacheWriteInputTokens",
            "cacheWriteInputTokenCount",
            "cache_write_input_tokens",
            "tokens_cache_write",
            "cache_creation_tokens",
            "cacheCreationTokens",
        )
        if any(k in usage for k in keys):
            return True
        for nest_key in (
            "prompt_tokens_details",
            "input_tokens_details",
            "promptTokensDetails",
            "inputTokensDetails",
        ):
            nest = usage.get(nest_key)
            if isinstance(nest, dict) and OpenAICompatDriver._usage_reports_cache_write(nest):
                return True
        return False

    @classmethod
    def _usage_meta(cls, usage: dict) -> dict:
        """Shared cache + billed-cost fields for DriverResponse.meta.

        Omits cache_read_tokens/cache_write_tokens when the provider usage
        blob has no corresponding field. Explicit provider zeros are kept.
        """
        from .token_usage import attach_modality_fields, coerce_token_usage_record

        usage = usage or {}
        detail = coerce_token_usage_record(usage)
        meta = {
            "raw_usage": usage,
        }
        if cls._usage_reports_cache_read(usage):
            meta["cache_read_tokens"] = int(detail.cache_read)
        if cls._usage_reports_cache_write(usage):
            meta["cache_write_tokens"] = int(detail.cache_write)
        cost = cls._cost_from_usage(usage)
        if cost is not None:
            meta["provider_cost_usd"] = cost
        attach_modality_fields(meta, detail)
        return meta

    @staticmethod
    def _served_model_from_payload(payload: dict) -> str | None:
        """Return non-empty response ``model`` when the provider reports one."""
        if not isinstance(payload, dict):
            return None
        model = payload.get("model")
        if isinstance(model, str) and model.strip():
            return model.strip()
        return None

    def complete(
        self,
        task_prompt: str,
        *,
        system: str = SYSTEM_PROMPT,
        session_id: str | None = None,
    ) -> DriverResponse:
        url = f"{self.base_url}/chat/completions"
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": task_prompt},
            ],
            self._output_token_limit_field(): self.max_tokens,
        }
        self._apply_temperature(body)
        self._prepare_body(
            body,
            messages=body["messages"],
            system=system,
            session_id=session_id,
        )
        data = json.dumps(body).encode("utf-8")

        def _call() -> DriverResponse:
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._key()}",
            }
            headers.update(self.extra_headers)
            t0 = time.time()
            raw = None
            last_err = None
            for attempt in range(2):
                try:
                    req = urllib.request.Request(
                        url, data=data, headers=headers, method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                        raw = json.loads(resp.read().decode("utf-8"))
                    last_err = None
                    break
                except urllib.error.HTTPError as e:
                    detail = e.read().decode("utf-8", "replace")[:500]
                    last_err = (e.code, detail)
                    if attempt == 0:
                        nxt = self._pool_rotate_on_http_error(e.code, detail)
                        if nxt:
                            headers["Authorization"] = f"Bearer {self._key()}"
                            continue
                    return DriverResponse(
                        text="", model=self.name,
                        error=f"HTTP {e.code}: {detail}",
                        latency_ms=(time.time() - t0) * 1000.0,
                    )
                except Exception as e:  # network, timeout, json
                    return DriverResponse(
                        text="", model=self.name, error=repr(e),
                        latency_ms=(time.time() - t0) * 1000.0,
                    )
            if last_err is not None:
                code, detail = last_err
                return DriverResponse(
                    text="", model=self.name, error=f"HTTP {code}: {detail}",
                    latency_ms=(time.time() - t0) * 1000.0,
                )

            latency = (time.time() - t0) * 1000.0
            try:
                text = raw["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, TypeError):
                return DriverResponse(
                    text="", model=self.name, error=f"unexpected response shape: {str(raw)[:300]}",
                    latency_ms=latency,
                )
            usage = raw.get("usage", {}) or {}
            meta = self._usage_meta(usage)
            meta["raw_finish"] = (
                raw["choices"][0].get("finish_reason") if raw.get("choices") else None
            )
            served = self._served_model_from_payload(raw if isinstance(raw, dict) else {})
            if served:
                meta["served_model"] = served
            from .token_usage import coerce_token_usage_record

            usage_detail = coerce_token_usage_record(usage)
            return DriverResponse(
                text=text,
                tokens_in=int(usage_detail.tokens_in),
                tokens_out=int(usage_detail.tokens_out),
                latency_ms=latency,
                model=self.name,
                meta=meta,
            )

        return with_retry(_call)

    def chat(
        self,
        messages: list,
        *,
        tools: list | None = None,
        system: str | None = None,
        session_id: str | None = None,
    ) -> DriverResponse:
        url = f"{self.base_url}/chat/completions"
        full_messages = []
        if system:
            full_messages.append({"role": "system", "content": system})
        full_messages.extend(messages)

        body = {
            "model": self.model,
            "messages": full_messages,
            self._output_token_limit_field(): self.max_tokens,
        }
        self._apply_temperature(body)
        if self.enable_reasoning and not (
            tools and self._uses_openai_gpt5_chat_parameters()
        ):
            body["reasoning"] = {"max_tokens": 1024}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
            if self._uses_openai_gpt5_chat_parameters():
                body["reasoning_effort"] = "none"
        self._prepare_body(
            body,
            messages=full_messages,
            system=system,
            session_id=session_id,
        )
        self._apply_openrouter_parallel_tool_calls(body, tools)

        data = json.dumps(body).encode("utf-8")

        def _call() -> DriverResponse:
            nonlocal data
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._key()}",
            }
            headers.update(self.extra_headers)
            t0 = time.time()
            raw = None
            try:
                req = urllib.request.Request(url, data=data, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:500]
                if self._reasoning_unsupported(e.code, detail) and body.get("reasoning") is not None:
                    # Drop the unsupported reasoning field for the rest of the
                    # session and retry once so the pilot turn succeeds.
                    self.enable_reasoning = False
                    body.pop("reasoning", None)
                    data = json.dumps(body).encode("utf-8")
                    try:
                        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
                        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                            raw = json.loads(resp.read().decode("utf-8"))
                    except urllib.error.HTTPError as e2:
                        d2 = e2.read().decode("utf-8", "replace")[:500]
                        nxt = self._pool_rotate_on_http_error(e2.code, d2)
                        if nxt:
                            headers["Authorization"] = f"Bearer {self._key()}"
                            try:
                                req = urllib.request.Request(
                                    url, data=data, headers=headers, method="POST",
                                )
                                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                                    raw = json.loads(resp.read().decode("utf-8"))
                            except urllib.error.HTTPError as e3:
                                d3 = e3.read().decode("utf-8", "replace")[:500]
                                return DriverResponse(
                                    text="", model=self.name,
                                    error=f"HTTP {e3.code}: {d3}",
                                    latency_ms=(time.time() - t0) * 1000.0,
                                )
                            except Exception as e3:
                                return DriverResponse(
                                    text="", model=self.name, error=repr(e3),
                                    latency_ms=(time.time() - t0) * 1000.0,
                                )
                        else:
                            return DriverResponse(
                                text="", model=self.name,
                                error=f"HTTP {e2.code}: {d2}",
                                latency_ms=(time.time() - t0) * 1000.0,
                            )
                    except Exception as e2:
                        return DriverResponse(text="", model=self.name, error=repr(e2),
                                              latency_ms=(time.time() - t0) * 1000.0)
                else:
                    nxt = self._pool_rotate_on_http_error(e.code, detail)
                    if nxt:
                        headers["Authorization"] = f"Bearer {self._key()}"
                        try:
                            req = urllib.request.Request(
                                url, data=data, headers=headers, method="POST",
                            )
                            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                                raw = json.loads(resp.read().decode("utf-8"))
                        except urllib.error.HTTPError as e2:
                            d2 = e2.read().decode("utf-8", "replace")[:500]
                            return DriverResponse(
                                text="", model=self.name,
                                error=f"HTTP {e2.code}: {d2}",
                                latency_ms=(time.time() - t0) * 1000.0,
                            )
                        except Exception as e2:
                            return DriverResponse(
                                text="", model=self.name, error=repr(e2),
                                latency_ms=(time.time() - t0) * 1000.0,
                            )
                    else:
                        return DriverResponse(
                            text="", model=self.name, error=f"HTTP {e.code}: {detail}",
                            latency_ms=(time.time() - t0) * 1000.0,
                        )
            except Exception as e:
                return DriverResponse(
                    text="", model=self.name, error=repr(e),
                    latency_ms=(time.time() - t0) * 1000.0,
                )

            latency = (time.time() - t0) * 1000.0
            try:
                choice = raw["choices"][0]
                message_obj = choice["message"]
                text = message_obj.get("content") or ""
                tool_calls = message_obj.get("tool_calls") or []
                finish_reason = str(choice.get("finish_reason") or "")
            except (KeyError, IndexError, TypeError):
                return DriverResponse(
                    text="", model=self.name, error=f"unexpected response shape: {str(raw)[:300]}",
                    latency_ms=latency,
                )

            if not tool_calls:
                legacy = message_obj.get("function_call")
                if isinstance(legacy, dict) and str(legacy.get("name") or "").strip():
                    tool_calls = [{
                        "id": str(message_obj.get("function_call_id") or ""),
                        "type": "function",
                        "function": legacy,
                    }]
                    if not finish_reason:
                        finish_reason = "function_call"

            reasoning = extract_reasoning(message_obj)
            pure_text = strip_think_blocks(text)
            executable, incomplete_tools = _split_chat_tool_calls(tool_calls)
            if _norm_chat_finish(finish_reason) not in _TOOL_CHAT_FINISH:
                executable, withheld = [], list(executable) + list(incomplete_tools)
                incomplete_tools = withheld
            stream_terminal, chat_error = _openai_chat_terminal(
                finish_reason,
                executable=executable,
                incomplete_tools=incomplete_tools,
                stream_started=bool(pure_text or executable or incomplete_tools or reasoning),
            )

            usage = raw.get("usage", {}) or {}
            meta = self._usage_meta(usage)
            meta.update({
                "tool_calls": executable,
                "reasoning": reasoning,
                "finish_reason": finish_reason,
                "stream_terminal": stream_terminal,
            })
            if incomplete_tools:
                meta["incomplete_tool_calls"] = incomplete_tools
            served = self._served_model_from_payload(raw if isinstance(raw, dict) else {})
            if served:
                meta["served_model"] = served
            from .token_usage import coerce_token_usage_record

            usage_detail = coerce_token_usage_record(usage)
            return DriverResponse(
                text=pure_text,
                tokens_in=int(usage_detail.tokens_in),
                tokens_out=int(usage_detail.tokens_out),
                latency_ms=latency,
                model=self.name,
                error=chat_error,
                meta=meta,
            )

        return with_retry(_call)

    def chat_stream(
        self,
        messages: list,
        *,
        tools: list | None = None,
        system: str | None = None,
        on_delta: Callable[[str], None],
        session_id: str | None = None,
        on_reasoning_delta: Callable[[str], None] | None = None,
        on_tool_hint: Callable[[str], None] | None = None,
    ) -> DriverResponse:
        url = f"{self.base_url}/chat/completions"
        full_messages = []
        if system:
            full_messages.append({"role": "system", "content": system})
        full_messages.extend(messages)

        body = {
            "model": self.model,
            "messages": full_messages,
            self._output_token_limit_field(): self.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        self._apply_temperature(body)
        if self.enable_reasoning and not (
            tools and self._uses_openai_gpt5_chat_parameters()
        ):
            body["reasoning"] = {"max_tokens": 1024}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
            if self._uses_openai_gpt5_chat_parameters():
                body["reasoning_effort"] = "none"
        self._prepare_body(
            body,
            messages=full_messages,
            system=system,
            session_id=session_id,
        )
        if self._is_opencode_go_host():
            body.pop("stream_options", None)
        self._apply_openrouter_parallel_tool_calls(body, tools)

        data = json.dumps(body).encode("utf-8")

        def _response_from_acc(
            acc: _OpenAIChatSseAccumulator,
            *,
            t0: float,
            transport_error: str | None = None,
        ) -> DriverResponse:
            parsed = acc.finalize(transport_error=transport_error)
            meta = {
                "tool_calls": parsed["tool_calls"],
                "reasoning": parsed["reasoning"],
                "finish_reason": parsed["finish_reason"],
                "stream_started": parsed["stream_started"],
                "stream_terminal": parsed["stream_terminal"],
                "malformed_sse_chunks": parsed["malformed_sse_chunks"],
                "saw_done": parsed["saw_done"],
            }
            if parsed["incomplete_tool_calls"]:
                meta["incomplete_tool_calls"] = parsed["incomplete_tool_calls"]
            if parsed["stream_has_cache_read"]:
                meta["cache_read_tokens"] = parsed["cached_tokens"]
            if parsed["stream_has_cache_write"]:
                meta["cache_write_tokens"] = parsed["cache_write_tokens"]
            if parsed["stream_raw_usage"] is not None:
                meta["raw_usage"] = parsed["stream_raw_usage"]
            if parsed["provider_cost_usd"] is not None:
                meta["provider_cost_usd"] = parsed["provider_cost_usd"]
            if parsed["served_model"]:
                meta["served_model"] = parsed["served_model"]
            return DriverResponse(
                text=parsed["text"],
                tokens_in=parsed["tokens_in"],
                tokens_out=parsed["tokens_out"],
                latency_ms=(time.time() - t0) * 1000.0,
                model=self.name,
                error=parsed["error"],
                meta=meta,
            )

        def _call() -> DriverResponse:
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._key()}",
            }
            headers.update(self.extra_headers)
            t0 = time.time()
            acc = _OpenAIChatSseAccumulator(
                on_delta=on_delta,
                on_reasoning_delta=on_reasoning_delta,
                on_tool_hint=on_tool_hint,
            )

            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    for line in resp:
                        if not acc.feed(line):
                            break
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:500]
                # Endpoint rejected the `reasoning` field: disable it for the
                # session and fall back to the non-streaming chat() (which shares
                # the retry path) so the turn still succeeds. Only safe before any
                # tokens streamed -- otherwise a partial stream would double-emit.
                if (not acc.stream_started and self._reasoning_unsupported(e.code, detail)
                        and body.get("reasoning") is not None):
                    self.enable_reasoning = False
                    return self.chat(
                        messages, tools=tools, system=system, session_id=session_id,
                    )
                # Muse/OpenCode Go sometimes 400s a stream with an empty
                # chat.completion stub and no error text. Retry once via
                # non-streaming chat() (drops stream-only fields) before any
                # content/reasoning/tool delta. Never after stream activity;
                # never for an actionable 400; never via chat_stream.
                if not acc.stream_started and self._is_empty_chat_completion_400_stub(
                    e.code, detail,
                ):
                    return self.chat(
                        messages, tools=tools, system=system, session_id=session_id,
                    )
                # Pool rotate only before any tokens streamed (same safety rule).
                if not acc.stream_started:
                    nxt = self._pool_rotate_on_http_error(e.code, detail)
                    if nxt:
                        return self.chat_stream(
                            messages,
                            tools=tools,
                            system=system,
                            on_delta=on_delta,
                            session_id=session_id,
                            on_reasoning_delta=on_reasoning_delta,
                            on_tool_hint=on_tool_hint,
                        )
                return _response_from_acc(
                    acc, t0=t0, transport_error=f"HTTP {e.code}: {detail}",
                )
            except Exception as e:
                return _response_from_acc(acc, t0=t0, transport_error=repr(e))

            return _response_from_acc(acc, t0=t0)

        return with_retry(_call)

