from __future__ import annotations

"""Cursor Agent CLI pilot driver (plan-credit burn via `agent login`).

Spawns the local Cursor Agent CLI (`agent` / `cursor-agent`) in non-interactive
`--print` mode and parses NDJSON `stream-json` events into DriverResponse +
streaming callbacks. Auth is the CLI session store — NOT CURSOR_API_KEY and
NOT CredentialPool bearer rotate (unlike CodexResponsesDriver).
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .base import SYSTEM_PROMPT, DriverResponse

# CreateProcess cmdline budget is ~32k. We spawn node+index.js directly
# (no agent.cmd→PowerShell), so short prompts stay on argv. Only spill to a
# temp file when the packed transcript exceeds this budget — never blanket
# on win32 (that made every turn a "read this huge file" tool chase).
_SAFE_ARGV_PROMPT_MAX = 18_000
_VERSION_DIR_RE = re.compile(
    r"^\d{4}\.\d{1,2}\.\d{1,2}(-\d{2}-\d{2}-\d{2})?-[a-f0-9]+$"
)

# Default curated ids when `agent models` is unavailable. Prefer current plan
# slugs (Composer 2.5 / Grok 4.5 / …); keep a few legacy aliases for tests.
# Omit Cursor's ``auto`` router — Marionette already picks the pilot; nesting
# another router fights that and hides which model actually ran.
DEFAULT_CURSOR_CLI_MODELS = (
    "composer-2.5",
    "composer-2.5-fast",
    "cursor-grok-4.5-high",
    "cursor-grok-4.5-medium",
    "cursor-grok-4.5-low",
    "claude-opus-4-8-thinking-high",
    "claude-opus-4-8-high",
    "gpt-5.5-high",
    "gpt-5.6-sol-medium",
    "gpt-5.4-high",
    "claude-4.5-sonnet-thinking",
    "claude-4.5-sonnet",
    "sonnet-4",
    "sonnet-4-thinking",
    "gpt-5",
    "composer-1",
)

INSTALL_HINT = (
    "Install the Cursor Agent CLI (`agent`), then Sign in with your Cursor account. "
    "See https://cursor.com/docs/cli/overview"
)

# Cursor Agent already loads workspace skills/rules from cwd. Never re-ship
# Marionette's frozen system (skills + MCP schemas + pilot tool docs) — that
# alone forced a temp-file spill on turn 1 and made the agent grep its own
# prompt. Keep a short kernel contract instead.
_CURSOR_CLI_KERNEL_SYSTEM = """You are Marionette's pilot via the Cursor Agent CLI (plan credits).

Identity / small talk: answer in one or two sentences. Do not open tools.

Code exploration: Puppetmaster CodeGraph is the search path — not Grep/Glob crawls.
  python -m puppetmaster codegraph search '<query>'
  python -m puppetmaster codegraph context '<task>' --max-nodes 15 --format markdown
Use Grep only for plain-text/config/log strings CodeGraph cannot see.

Your user message is already in this prompt. Never treat OS temp files
(pmh-cursor-cli-*.txt or similar) as codebase tasks — do not read/grep them.
"""


def resolve_agent_binary() -> Optional[str]:
    """Locate the Cursor Agent CLI binary (`agent` preferred over `cursor-agent`)."""
    for name in ("agent", "cursor-agent"):
        found = shutil.which(name)
        if found:
            return found
    home = Path.home()
    # Official Windows install lands shims under %LOCALAPPDATA%\cursor-agent
    # (agent.cmd / agent.ps1), not necessarily on PATH for already-running
    # Electron/harness processes.
    win_dir = home / "AppData" / "Local" / "cursor-agent"
    candidates = [
        home / ".local" / "bin" / "agent",
        home / ".local" / "bin" / "cursor-agent",
        win_dir / "agent.exe",
        win_dir / "agent.cmd",
        win_dir / "agent.CMD",
        win_dir / "cursor-agent.exe",
        win_dir / "cursor-agent.cmd",
        win_dir / "cursor-agent.CMD",
    ]
    for path in candidates:
        if path.is_file():
            return str(path)
    return None


def _latest_cursor_agent_version_dir(root: Path) -> Optional[Path]:
    versions = root / "versions"
    if not versions.is_dir():
        return None
    best: Optional[Tuple[int, Path]] = None
    for child in versions.iterdir():
        if not child.is_dir() or not _VERSION_DIR_RE.match(child.name):
            continue
        date_part = child.name.split("-")[0]
        bits = date_part.split(".")
        if len(bits) != 3:
            continue
        try:
            key = int(bits[0]) * 10000 + int(bits[1]) * 100 + int(bits[2])
        except ValueError:
            continue
        if best is None or key > best[0]:
            best = (key, child)
    return best[1] if best else None


def resolve_agent_exec(binary: Optional[str] = None) -> List[str]:
    """Argv prefix to launch the Agent CLI without cmd.exe/PowerShell shims.

    On Windows, ``agent.cmd`` re-invokes PowerShell and inherits an ~8191-char
    limit (WinError 206 on long chat prompts). Prefer ``node.exe index.js``
    from the versioned install when present.
    """
    bin_path = binary or resolve_agent_binary()
    if not bin_path:
        return []
    p = Path(bin_path)
    # Already a direct executable / script entry.
    if p.suffix.lower() in (".exe", ".js") and p.is_file():
        return [str(p)]

    # agent.cmd / agent.ps1 live next to versions/<ver>/{node.exe,index.js}
    root = p.parent
    ver = _latest_cursor_agent_version_dir(root)
    if ver is not None:
        node = ver / "node.exe"
        index = ver / "index.js"
        if node.is_file() and index.is_file():
            return [str(node), str(index)]

    return [str(p)]


def _latest_user_utterance(prompt: str) -> str:
    """Best-effort last ``User:`` block from a packed transcript."""
    marker = "\nUser:\n"
    idx = prompt.rfind(marker)
    if idx >= 0:
        return prompt[idx + len(marker) :].strip()
    if prompt.startswith("User:\n"):
        return prompt[len("User:\n") :].strip()
    return prompt.strip()


def _prompt_via_temp_file(prompt: str) -> Tuple[str, Optional[str]]:
    """Spill oversized transcript to a UTF-8 temp file; keep the ask on argv.

    Argv carries the latest user utterance plus a hard rule: do not tool-read
    the spill file. Asking the model to "read the UTF-8 file" made Cursor Agent
    grep/chunk its own prompt for 40s on trivial questions.
    """
    fd, path = tempfile.mkstemp(prefix="pmh-cursor-cli-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(prompt)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    # Forward slashes avoid quoting pain on Windows argv.
    path_arg = path.replace("\\", "/")
    latest = _latest_user_utterance(prompt)
    # Cap inline ask so argv stays well under CreateProcess limits.
    if len(latest) > 4_000:
        latest = latest[:4_000] + "\n…[truncated]"
    pointer = (
        f"{latest}\n\n"
        f"[Marionette] Prior system+transcript for this turn is already written "
        f"to {path_arg} (UTF-8). Answer the user message above immediately. "
        f"Do NOT use read/grep/search/shell tools on that path — it is not a "
        f"codebase file. Only mentally use that background if the question "
        f"needs earlier turns. Do not modify or delete the file."
    )
    return pointer, path


def _assistant_text(event: dict) -> str:
    message = event.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        elif isinstance(block, str):
            parts.append(block)
    return "".join(parts)


def _is_partial_assistant_delta(event: dict) -> bool:
    """With --stream-partial-output, only timestamped deltas (no model_call_id)
    carry new text. Buffered flushes and final full-message events are skipped
    to avoid duplicating tokens into on_delta / accumulated text."""
    if event.get("type") != "assistant":
        return False
    if "timestamp_ms" not in event:
        return False
    if "model_call_id" in event:
        return False
    return True


def _tool_call_name_and_args(tool_call: dict) -> tuple[str, dict]:
    if not isinstance(tool_call, dict):
        return ("unknown", {})
    for key, payload in tool_call.items():
        if isinstance(payload, dict):
            args = payload.get("args")
            if not isinstance(args, dict):
                args = {k: v for k, v in payload.items() if k != "result"}
            return (str(key), args)
    return ("unknown", {})


def _openai_tool_call(call_id: str, name: str, args: dict) -> dict:
    return {
        "id": call_id or f"call_{name}",
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args if isinstance(args, dict) else {}),
        },
    }


def consume_stream_json(
    lines,
    *,
    on_delta: Callable[[str], None] | None = None,
    on_reasoning_delta: Callable[[str], None] | None = None,
    on_tool_hint: Callable[[str], None] | None = None,
    expect_partial: bool = True,
) -> dict:
    """Parse agent NDJSON stream-json lines into a terminal result dict.

    Returns keys: text, tool_calls, usage, error, session_id, model, raw_result.
    """
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    seen_call_ids: set[str] = set()
    session_id = ""
    model = ""
    error: Optional[str] = None
    final_result_text = ""
    usage: dict = {}
    saw_partial = False

    for raw in lines:
        if isinstance(raw, bytes):
            line = raw.decode("utf-8", "replace").strip()
        else:
            line = str(raw).strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        etype = event.get("type")
        if etype == "system" and event.get("subtype") == "init":
            session_id = str(event.get("session_id") or session_id)
            model = str(event.get("model") or model)
            continue

        if etype == "assistant":
            chunk = _assistant_text(event)
            if expect_partial:
                if _is_partial_assistant_delta(event):
                    saw_partial = True
                    if chunk:
                        text_parts.append(chunk)
                        if on_delta is not None:
                            on_delta(chunk)
                # Non-partial assistant (full flush): keep as fallback text only
                # when we never saw character deltas.
                elif chunk and not saw_partial and not text_parts:
                    text_parts.append(chunk)
            else:
                if chunk:
                    text_parts.append(chunk)
                    if on_delta is not None:
                        on_delta(chunk)
            continue

        if etype == "thinking":
            # Docs say thinking is suppressed in print mode; still forward if present.
            think = event.get("text") or _assistant_text(event)
            if think and on_reasoning_delta is not None:
                on_reasoning_delta(str(think))
            continue

        if etype == "tool_call":
            subtype = event.get("subtype") or ""
            call_id = str(event.get("call_id") or "")
            name, args = _tool_call_name_and_args(event.get("tool_call") or {})
            if subtype == "started":
                if on_tool_hint is not None and name:
                    on_tool_hint(name)
                if call_id and call_id not in seen_call_ids:
                    seen_call_ids.add(call_id)
                    tool_calls.append(_openai_tool_call(call_id, name, args))
                elif not call_id:
                    tool_calls.append(_openai_tool_call("", name, args))
            continue

        if etype == "result":
            session_id = str(event.get("session_id") or session_id)
            if event.get("is_error"):
                error = str(event.get("result") or event.get("error") or "cursor-cli error")
            else:
                final_result_text = str(event.get("result") or "")
            if isinstance(event.get("usage"), dict):
                usage = event["usage"]
            continue

    accumulated = "".join(text_parts)
    # Prefer streamed deltas when present; otherwise the terminal result string.
    text = accumulated if accumulated else final_result_text
    if not text and final_result_text:
        text = final_result_text

    return {
        "text": text,
        "tool_calls": tool_calls,
        "usage": usage,
        "error": error,
        "session_id": session_id,
        "model": model,
        "raw_result": final_result_text,
    }


# Keep a recent window of prose turns; latest user utterance always full.
_ASK_HISTORY_CHAR_BUDGET = 12_000


def _message_text(msg: dict) -> str:
    content = msg.get("content")
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                texts.append(block)
        return "".join(texts)
    if content is None:
        return ""
    return str(content)


def _system_for_cursor_agent(system: Optional[str]) -> str:
    """Kernel system + optional precomputed CodeGraph/Wiki blocks only."""
    parts: list[str] = [_CURSOR_CLI_KERNEL_SYSTEM.strip()]
    if not system:
        return parts[0]
    for block in str(system).split("\n\n"):
        b = block.strip()
        if not b:
            continue
        head = b[:120].upper()
        if (
            head.startswith("CODEGRAPH HAS ALREADY")
            or head.startswith("WIKI HAS ALREADY")
            or "CODEGRAPH CONTEXT" in head
            or b.lstrip().startswith("## CodeGraph")
            or b.lstrip().startswith("## Wiki")
        ):
            if len(b) > 8_000:
                b = b[:8_000] + "\n…[truncated]"
            parts.append(b)
    return "\n\n".join(parts)


def _is_poison_history_message(role: str, text: str) -> bool:
    """Drop tool-result / invalid-tool spam so it does not re-enter the prompt."""
    if role in ("tool", "function"):
        return True
    lower = text.lower()
    if "invalid tool call" in lower:
        return True
    if "unknown native tool name" in lower:
        return True
    return False


def _messages_to_prompt(
    messages: list,
    system: Optional[str],
    *,
    lean: bool = False,
) -> str:
    parts: list[str] = []
    # Always use the kernel system — never the full Marionette skills/MCP dump.
    sys_text = _system_for_cursor_agent(system)
    if sys_text:
        parts.append(f"System:\n{sys_text}")

    normalized: list[tuple[str, str]] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user")
        text = _message_text(msg)
        if not text.strip():
            continue
        if _is_poison_history_message(role, text):
            continue
        # Skip assistant rows that are only tool-call scaffolding.
        if role == "assistant" and msg.get("tool_calls") and not text.strip():
            continue
        normalized.append((role, text))

    if lean and normalized:
        latest_role, latest_text = normalized[-1]
        kept: list[tuple[str, str]] = [(latest_role, latest_text)]
        budget = _ASK_HISTORY_CHAR_BUDGET
        for role, text in reversed(normalized[:-1]):
            if budget <= 0:
                break
            if len(text) > budget:
                text = text[:budget] + "\n…[truncated]"
            kept.append((role, text))
            budget -= len(text)
        kept.reverse()
        normalized = kept

    for role, text in normalized:
        parts.append(f"{role.capitalize()}:\n{text}")
    return "\n\n".join(parts).strip() or "hello"


class CursorCliDriver:
    """Pilot driver backed by the Cursor Agent CLI subprocess."""

    supports_streaming = True

    def __init__(
        self,
        name: str,
        model: str,
        *,
        max_tokens: int = 8000,
        timeout: int = 600,
        mode: str | None = None,
        agent_binary: str | None = None,
        cwd: str | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self.max_tokens = max_tokens
        self.timeout = timeout
        # ask mode avoids uncontrolled write/shell from Cursor's own tool loop
        # while still burning plan credits for chat. Override via HARNESS_CURSOR_CLI_MODE.
        self.mode = (
            mode
            or os.environ.get("HARNESS_CURSOR_CLI_MODE", "").strip()
            or "ask"
        )
        self.agent_binary = agent_binary
        self.cwd = cwd

    def _binary(self) -> str:
        binary = self.agent_binary or resolve_agent_binary()
        if not binary:
            raise RuntimeError(
                f"Cursor Agent CLI not found. {INSTALL_HINT}"
            )
        return binary

    def _workspace(self) -> Optional[str]:
        """Directory Cursor Agent should treat as the trusted workspace."""
        raw = (self.cwd or os.environ.get("HARNESS_REPO") or "").strip()
        if not raw:
            return None
        try:
            return str(Path(raw).resolve())
        except OSError:
            return raw

    def _build_cmd(self, prompt: str) -> list[str]:
        exec_prefix = resolve_agent_exec(self._binary())
        if not exec_prefix:
            raise RuntimeError(f"Cursor Agent CLI not found. {INSTALL_HINT}")
        # --trust: Marionette already opened this project; headless --print
        # otherwise blocks on "Workspace Trust Required" (no TTY to confirm).
        cmd = [
            *exec_prefix,
            "--print",
            "--trust",
            "--output-format", "stream-json",
            "--stream-partial-output",
            "--model", self.model,
        ]
        workspace = self._workspace()
        if workspace:
            cmd.extend(["--workspace", workspace])
        if self.mode:
            cmd.extend(["--mode", self.mode])
        cmd.append(prompt)
        return cmd

    def _run_stream(
        self,
        messages: list,
        *,
        tools: list | None = None,
        system: str | None = None,
        on_delta: Callable[[str], None] | None = None,
        on_reasoning_delta: Callable[[str], None] | None = None,
        on_tool_hint: Callable[[str], None] | None = None,
    ) -> DriverResponse:
        # Host tools are Marionette schemas. Cursor Agent runs its OWN loop
        # (readToolCall/grepToolCall/…). Never re-dispatch those names through
        # parse_tool_calls — that produced INVALID TOOL CALL spam in the UI.
        _ = tools
        t0 = time.time()
        prompt = _messages_to_prompt(messages, system, lean=True)
        prompt_file: Optional[str] = None
        # Agent CLI does not read prompts from stdin. With node+index.js spawn,
        # moderate prompts fit on argv; only oversized packs spill to a file.
        use_file = len(prompt) > _SAFE_ARGV_PROMPT_MAX
        try:
            argv_prompt = prompt
            if use_file:
                argv_prompt, prompt_file = _prompt_via_temp_file(prompt)
            cmd = self._build_cmd(argv_prompt)
        except RuntimeError as e:
            return DriverResponse(
                text="", model=self.name, error=str(e),
                latency_ms=(time.time() - t0) * 1000.0,
            )
        except OSError as e:
            return DriverResponse(
                text="", model=self.name, error=f"failed to prepare prompt: {e}",
                latency_ms=(time.time() - t0) * 1000.0,
            )

        workspace = self._workspace()
        popen_kwargs: dict = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "stdin": subprocess.DEVNULL,
            "cwd": workspace or self.cwd or None,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }
        if sys.platform == "win32":
            # Hide console flash; CREATE_NO_WINDOW=0x08000000
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        # Leave the composer on the thinking spinner until real model/tool
        # events arrive — no synthetic "cold start" status chrome.

        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)
        except OSError as e:
            if prompt_file:
                try:
                    os.unlink(prompt_file)
                except OSError:
                    pass
            return DriverResponse(
                text="", model=self.name, error=f"failed to spawn agent: {e}",
                latency_ms=(time.time() - t0) * 1000.0,
            )

        try:
            assert proc.stdout is not None
            parsed = consume_stream_json(
                proc.stdout,
                on_delta=on_delta,
                on_reasoning_delta=on_reasoning_delta,
                on_tool_hint=on_tool_hint,
                expect_partial=True,
            )
            try:
                proc.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                return DriverResponse(
                    text=parsed.get("text") or "",
                    model=self.name,
                    error=f"cursor-cli timed out after {self.timeout}s",
                    latency_ms=(time.time() - t0) * 1000.0,
                    # Empty tool_calls: never re-dispatch Cursor-native names.
                    meta={
                        "tool_calls": [],
                        "cursor_cli": True,
                        "cursor_cli_internal_tools": [
                            (tc.get("function") or {}).get("name")
                            for tc in (parsed.get("tool_calls") or [])
                            if isinstance(tc, dict)
                        ],
                    },
                )

            stderr = ""
            if proc.stderr is not None:
                try:
                    stderr = proc.stderr.read()[:500]
                except Exception:
                    stderr = ""

            latency = (time.time() - t0) * 1000.0
            from .token_usage import coerce_token_usage
            tokens_in, tokens_out, provider_cost = coerce_token_usage(
                parsed.get("usage"), parsed
            )

            err = parsed.get("error")
            if proc.returncode not in (0, None) and not err and not parsed.get("text"):
                err = stderr.strip() or f"agent exited with code {proc.returncode}"

            internal_names = [
                (tc.get("function") or {}).get("name")
                for tc in (parsed.get("tool_calls") or [])
                if isinstance(tc, dict)
            ]
            meta = {
                # Critical: Cursor Agent already executed these internally.
                # Returning them as OpenAI tool_calls makes Marionette try
                # readToolCall/grepToolCall as native verbs → INVALID TOOL CALL spam.
                "tool_calls": [],
                "session_id": parsed.get("session_id") or "",
                "cursor_cli": True,
                "cursor_cli_internal_tools": [n for n in internal_names if n],
                "pool_rotate": False,
                "prompt_via_file": bool(prompt_file),
                "host_tools_ignored": True,
                "billing": "plan",
            }
            if provider_cost is not None:
                meta["provider_cost_usd"] = provider_cost

            return DriverResponse(
                text=parsed.get("text") or "",
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=latency,
                model=self.name,
                error=err,
                meta=meta,
            )
        finally:
            if prompt_file:
                try:
                    os.unlink(prompt_file)
                except OSError:
                    pass

    def complete(self, task_prompt: str, *, system: str = SYSTEM_PROMPT) -> DriverResponse:
        return self._run_stream(
            [{"role": "user", "content": task_prompt}],
            system=system,
        )

    def chat(
        self,
        messages: list,
        *,
        tools: list | None = None,
        system: str | None = None,
        session_id: str | None = None,
    ) -> DriverResponse:
        _ = session_id
        return self._run_stream(messages, tools=tools, system=system)

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
        _ = session_id
        return self._run_stream(
            messages,
            tools=tools,
            system=system,
            on_delta=on_delta,
            on_reasoning_delta=on_reasoning_delta,
            on_tool_hint=on_tool_hint,
        )
