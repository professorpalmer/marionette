from __future__ import annotations

"""Claude Code CLI center-pilot driver (Max/Pro subscription via `claude`).

Spawns the official Claude Code CLI in non-interactive ``--print`` mode and
parses ``stream-json`` events into DriverResponse. Auth stays in the CLI
session store (``~/.claude.json`` oauthAccount). The child environment drops
``ANTHROPIC_API_KEY`` so a console key cannot steal the spawn onto PAYG.

Host tool schemas are ignored: Claude Code runs its own loop, same as
CursorCliDriver. Marionette still owns session chrome, history, and workers.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from .base import SYSTEM_PROMPT, DriverResponse

DEFAULT_CLAUDE_CLI_MODELS = (
    "claude-opus-4-8",
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
    "claude-opus-4-6",
    "claude-opus-5",
)

INSTALL_HINT = (
    "Install Claude Code (`npm install -g @anthropic-ai/claude-code`), "
    "then Sign in with your Anthropic account. "
    "See https://code.claude.com/docs/en/cli-overview"
)

_API_BILLING_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
)

_KERNEL_SYSTEM = """You are Marionette's pilot via the Claude Code CLI (Claude Max/Pro).
Marionette owns the chat UI and swarm/implement workers. You own local tools
in this subprocess. Answer the user's latest turn. Do not pretend you are the
Anthropic Messages API.
"""


def claude_cli_login_is_sentinel(value: str) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes")


def resolve_claude_binary() -> Optional[str]:
    explicit = (os.environ.get("CLAUDE_CODE_COMMAND") or "").strip()
    if explicit:
        if os.path.isabs(explicit) and os.path.isfile(explicit) and os.access(explicit, os.X_OK):
            return explicit
        found = shutil.which(explicit)
        if found:
            return found
    found = shutil.which("claude")
    if found:
        return found
    home = Path.home()
    for candidate in (
        home / ".local" / "bin" / "claude",
        home / ".npm-global" / "bin" / "claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
    ):
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        except OSError:
            continue
    return None


def subscription_child_env(base: Optional[dict] = None) -> dict:
    """Copy env and drop vars that force Claude Code onto API/Bedrock billing."""
    env = dict(base if base is not None else os.environ)
    for name in _API_BILLING_ENV:
        env.pop(name, None)
    return env


def resolve_permission_mode(*, plan: bool = False, explicit: str | None = None) -> str:
    env = (os.environ.get("HARNESS_CLAUDE_CLI_PERMISSION") or "").strip()
    if env:
        return env
    if explicit is not None:
        override = str(explicit).strip()
        if override:
            return override
    return "plan" if plan else "acceptEdits"


def _messages_to_prompt(messages: list, system: str | None) -> str:
    chunks: List[str] = []
    kernel = (system or "").strip() or _KERNEL_SYSTEM
    chunks.append(kernel)
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user")
        content = msg.get("content")
        if isinstance(content, list):
            text = " ".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict)
            ).strip()
        else:
            text = str(content or "").strip()
        if not text:
            continue
        if role == "system":
            chunks.append(text)
            continue
        chunks.append(f"{role.upper()}: {text}")
    return "\n\n".join(chunks).strip()


def _current_turn_prompt(messages: list) -> str:
    for msg in reversed(messages or []):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, list):
            text = " ".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict)
            ).strip()
        else:
            text = str(content or "").strip()
        if text:
            return text
    return ""


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
    return "".join(parts)


def consume_stream_json(
    lines: Iterable[str],
    *,
    on_delta: Callable[[str], None] | None = None,
) -> Dict[str, Any]:
    """Parse Claude Code ``stream-json`` NDJSON into a DriverResponse-shaped dict."""
    text_parts: List[str] = []
    streamed = ""
    session_id = ""
    model = ""
    usage: Dict[str, Any] = {}
    error = None
    for raw in lines:
        line = (raw or "").strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind = str(event.get("type") or "")
        if kind == "system" and not session_id:
            session_id = str(event.get("session_id") or "").strip()
            model = str(event.get("model") or model).strip()
            continue
        if kind == "assistant":
            message = event.get("message")
            chunk = ""
            if isinstance(message, dict):
                chunk = _text_from_content(message.get("content"))
                if not model:
                    model = str(message.get("model") or "").strip()
            if chunk:
                if streamed and chunk.startswith(streamed):
                    addition = chunk[len(streamed):]
                elif streamed and streamed.endswith(chunk):
                    addition = ""
                else:
                    addition = chunk
                if addition and on_delta:
                    on_delta(addition)
                streamed += addition
                text_parts.append(addition)
            continue
        if kind == "result":
            session_id = str(event.get("session_id") or session_id).strip()
            raw_usage = event.get("usage")
            if isinstance(raw_usage, dict):
                usage = raw_usage
            result_text = str(event.get("result") or "").strip()
            if result_text and not "".join(text_parts).strip():
                text_parts.append(result_text)
            if event.get("is_error") or str(event.get("subtype") or "") == "error":
                error = result_text or str(event.get("errors") or "claude result error")
            continue
    text = "".join(text_parts).strip() or streamed.strip()
    tokens_in = 0
    tokens_out = 0
    try:
        tokens_in = int(usage.get("input_tokens") or 0)
    except (TypeError, ValueError):
        tokens_in = 0
    try:
        tokens_out = int(usage.get("output_tokens") or 0)
    except (TypeError, ValueError):
        tokens_out = 0
    return {
        "text": text,
        "session_id": session_id,
        "model": model,
        "usage": usage,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "error": error,
        "tool_calls": [],
    }


class ClaudeCliDriver:
    """Pilot driver backed by the Claude Code CLI subprocess."""

    supports_streaming = True
    requires_explicit_terminal = True

    def __init__(
        self,
        name: str,
        model: str,
        *,
        max_tokens: int = 8000,
        timeout: int = 600,
        permission_mode: str | None = None,
        claude_binary: str | None = None,
        cwd: str | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self.max_tokens = max_tokens
        self.timeout = timeout
        self._permission_override = permission_mode
        self.permission_mode = resolve_permission_mode(explicit=permission_mode)
        self.claude_binary = claude_binary
        self.cwd = cwd
        self._harness_session_id: Optional[str] = None
        self._native_session_id: Optional[str] = None

    def apply_host_mode(self, *, plan: bool = False) -> str:
        self.permission_mode = resolve_permission_mode(
            plan=plan, explicit=self._permission_override,
        )
        return self.permission_mode

    def _binary(self) -> str:
        binary = self.claude_binary or resolve_claude_binary()
        if not binary:
            raise RuntimeError(f"Claude Code CLI not found. {INSTALL_HINT}")
        return binary

    def _workspace(self) -> Optional[str]:
        raw = (self.cwd or os.environ.get("HARNESS_REPO") or "").strip()
        if not raw:
            return None
        try:
            return str(Path(raw).resolve())
        except OSError:
            return raw

    def _build_cmd(self, *, resume_session_id: Optional[str] = None) -> list[str]:
        cmd = [
            self._binary(),
            "--print",
            "--output-format", "stream-json",
            "--verbose",
            "--permission-mode", self.permission_mode,
            "--model", self.model,
        ]
        if resume_session_id:
            cmd.extend(["--resume", resume_session_id])
        return cmd

    def _run_stream(
        self,
        messages: list,
        *,
        tools: list | None = None,
        system: str | None = None,
        session_id: str | None = None,
        on_delta: Callable[[str], None] | None = None,
        on_reasoning_delta: Callable[[str], None] | None = None,
        on_tool_hint: Callable[[str], None] | None = None,
    ) -> DriverResponse:
        _ = tools, on_reasoning_delta, on_tool_hint
        t0 = time.time()
        harness_sid = (session_id or "").strip() or None
        resume = None
        if harness_sid and harness_sid == self._harness_session_id:
            resume = self._native_session_id
        if resume:
            prompt = _current_turn_prompt(messages)
        else:
            prompt = _messages_to_prompt(messages, system)
        try:
            cmd = self._build_cmd(resume_session_id=resume)
        except RuntimeError as e:
            return DriverResponse(
                text="", model=self.name, error=str(e),
                latency_ms=(time.time() - t0) * 1000.0,
            )
        workspace = self._workspace()
        popen_kwargs: dict = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "stdin": subprocess.PIPE,
            "cwd": workspace or self.cwd or None,
            "env": subscription_child_env(),
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)
        except OSError as e:
            return DriverResponse(
                text="", model=self.name, error=f"failed to start claude: {e}",
                latency_ms=(time.time() - t0) * 1000.0,
            )
        try:
            stdout, stderr = proc.communicate(prompt, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            return DriverResponse(
                text="", model=self.name,
                error=f"claude timed out after {self.timeout}s",
                latency_ms=(time.time() - t0) * 1000.0,
            )
        parsed = consume_stream_json(
            (stdout or "").splitlines(),
            on_delta=on_delta,
        )
        err = parsed.get("error")
        if proc.returncode not in (0, None) and not err and not parsed.get("text"):
            err = (stderr or "").strip() or f"claude exited with code {proc.returncode}"
        native = str(parsed.get("session_id") or "").strip()
        if harness_sid and native and not err:
            self._harness_session_id = harness_sid
            self._native_session_id = native
        meta = {
            "tool_calls": [],
            "session_id": native,
            "claude_cli": True,
            "host_tools_ignored": True,
            "billing": "plan",
            "api_mode": "claude_cli",
            "permission_mode": self.permission_mode,
            "requested_model": self.model,
            "claude_cli_resume": resume or "",
        }
        served = str(parsed.get("model") or "").strip()
        if served:
            meta["served_model"] = served
        if isinstance(parsed.get("usage"), dict) and parsed["usage"]:
            meta["raw_usage"] = parsed["usage"]
        if not err and proc.returncode in (0, None):
            meta["finish_reason"] = "stop"
            meta["stream_terminal"] = "completed"
        return DriverResponse(
            text=parsed.get("text") or "",
            tokens_in=int(parsed.get("tokens_in") or 0),
            tokens_out=int(parsed.get("tokens_out") or 0),
            latency_ms=(time.time() - t0) * 1000.0,
            model=self.name,
            error=err,
            meta=meta,
        )

    def complete(
        self,
        task_prompt: str,
        *,
        system: str = SYSTEM_PROMPT,
        session_id: str | None = None,
    ) -> DriverResponse:
        return self._run_stream(
            [{"role": "user", "content": task_prompt}],
            system=system,
            session_id=session_id,
        )

    def chat(
        self,
        messages: list,
        *,
        tools: list | None = None,
        system: str | None = None,
        session_id: str | None = None,
    ) -> DriverResponse:
        return self._run_stream(
            messages, tools=tools, system=system, session_id=session_id,
        )

    def chat_stream(
        self,
        messages: list,
        *,
        tools: list | None = None,
        system: str | None = None,
        session_id: str | None = None,
        on_delta: Callable[[str], None] | None = None,
        on_reasoning_delta: Callable[[str], None] | None = None,
        on_tool_hint: Callable[[str], None] | None = None,
    ) -> DriverResponse:
        return self._run_stream(
            messages,
            tools=tools,
            system=system,
            session_id=session_id,
            on_delta=on_delta,
            on_reasoning_delta=on_reasoning_delta,
            on_tool_hint=on_tool_hint,
        )
