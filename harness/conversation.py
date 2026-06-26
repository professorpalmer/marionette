from __future__ import annotations

"""ConversationalSession: the PILOT loop (the product UX).

Difference from Session (the eval loop): Session emits one bare intent per task
and is for measuring drivers. ConversationalSession is the human-facing product:
the pilot CONVERSES (prose) and fires orchestration ACTIONS as collapsible
tool-calls, reacting to the artifacts they return, until it finishes a turn with
no actions and yields back to the user.

Transcript model:
- The pilot carries a running transcript (system + user + pilot prose + compact
  action results) ACROSS turns within a session. This is the conversation the
  user follows.
- Swarm workers receive only the distilled `goal` brief (+ CodeGraph). The
  transcript never enters a worker. Conversation and investigation are decoupled.

Events yielded (for GUI/CLI):
- ("thinking", {text})                         -> pilot reasoning (collapsible/dimmed)
- ("message", {role:"assistant", text})        -> pilot prose (conversation)
- ("action_start", {id, kind, goal, cwd})      -> a collapsible card opens
- ("action_result", {id, job_id, num, types,   -> the card's body (artifacts)
       artifacts, adapter, mode})
- ("assistant_done", {turns, swarms})          -> turn complete, yield to user
- ("error", {error})
"""

import os
import threading
import time
import subprocess
from dataclasses import dataclass, field
from typing import Iterator, Optional, Any

from pmharness import registry as reg
from . import providers as prov
from pmharness.intent import DriverIntent
from pmharness.bridge import execute_intent, BridgeResult
from .pilot import (parse_pilot_turn, PilotTurn, PilotError, PILOT_SYSTEM)
from .wiki import WikiClient, session_digest
from .text_clean import clean_say


def is_safe_path(path: str, parent: str) -> bool:
    try:
        real_p = os.path.realpath(path)
        real_parent = os.path.realpath(parent)
        return os.path.commonpath([real_parent, real_p]) == real_parent
    except ValueError:
        return False


from .skill_store import SkillStore
from .skill_distiller import distill_session, distill_rules
from .rule_store import RuleStore


def _mcp_result_text(out: dict) -> str:
    """Flatten an MCP tools/call result into plain text for the transcript."""
    if not isinstance(out, dict):
        return str(out)
    parts = []
    for block in out.get("content", []) or []:
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif "text" in block:
                parts.append(str(block["text"]))
    return "\n".join(parts) if parts else str(out)


def _format_mcp_tools_section(mcp_manager) -> str:
    """Format connected MCP tools for the system prompt."""
    if not mcp_manager:
        return ""
    try:
        tools = mcp_manager.discovered_tools()
    except Exception:
        return ""
    if not tools:
        return ""

    lines = []
    lines.append('## Connected MCP tools (call via {"kind":"call_mcp","tool":"<server>.<tool>","arguments":{...}}):')
    for t in tools:
        schema = t.input_schema or {}
        properties = schema.get("properties", {})
        required = schema.get("required", []) or []
        arg_parts = []
        if isinstance(properties, dict):
            for name, prop in properties.items():
                if not isinstance(prop, dict):
                    prop = {}
                arg_type = prop.get("type", "any")
                is_req = name in required
                req_marker = " (required)" if is_req else ""
                arg_parts.append(f"{name}:{arg_type}{req_marker}")
        
        args_str = ", ".join(arg_parts) if arg_parts else "none"
        desc = t.description.strip() if t.description else "No description"
        lines.append(f"- {t.qualified}: {desc} (args: {args_str})")
    
    return "\n".join(lines)

from .autobudget import AutoBudget
from .config import HarnessConfig
from .state import DurableState


HARD_PILOT_STEPS = 10  # safety cap on pilot<->swarm round-trips per user message


@dataclass
class ConvEvent:
    kind: str
    data: dict = field(default_factory=dict)


class ConversationalSession:
    def __init__(self, config: HarnessConfig) -> None:
        self.config = config
        import tempfile
        self.state_dir = config.state_dir or tempfile.mkdtemp(prefix="pilot-")
        # Provider-aware pilot: 'provider:model' spans any provider whose key is
        # set; a bare model resolves against available providers, else OpenRouter.
        try:
            self.pilot = prov.build_pilot(config.driver)
        except prov.ProviderError:
            # fall back to the eval registry (OpenRouter field) for known names
            self.pilot = reg.build(config.driver, reach=config.reach)
        # propagate repo/adapter so the bridge runs real analysis when configured
        if config.repo:
            os.environ["HARNESS_REPO"] = config.repo
        if config.swarm_adapter:
            os.environ["HARNESS_SWARM_ADAPTER"] = config.swarm_adapter
        # self-learning: load ACTIVE skills into the pilot's system context so
        # the loop compounds (procedural memory). Pending skills are NOT loaded.
        self._skills = SkillStore()
        system = PILOT_SYSTEM
        active = self._skills.list("active")
        if active:
            skills_block = "\n\n".join(
                f"## Skill: {s.name}\n{s.description}\n{s.body}" for s in active)
            system = (system + "\n\n# Learned skills (apply when relevant)\n"
                      + skills_block)
        # standing conventions (always-on, terse) -- distinct from task skills
        self._rules = RuleStore()
        active_rules = self._rules.list("active")
        if active_rules:
            rules_block = "\n".join(f"- {r.text}" for r in active_rules)
            system = (system + "\n\n# Standing rules (ALWAYS honor)\n" + rules_block)
        # the running transcript with the pilot (conversation memory)
        self._history: list[dict] = [{"role": "system", "content": system}]
        # optional durable-knowledge integration (portable-llm-wiki)
        self._wiki = WikiClient()
        self._wiki_auto = os.environ.get("HARNESS_WIKI_AUTO", "").strip() in ("1", "true", "yes")
        # optional MCP integration -- set by the server so the pilot can call MCP tools
        self._mcp = None
        # self-learning: accumulate this session's real findings for distillation
        self._session_findings: list = []
        self._first_objective: str = ""
        # token accounting for the autobudget governor (real metering, not a stub)
        self._tokens_used: int = 0
        # concurrency: a single ConversationalSession is single-flight. Two
        # concurrent send()/run_auto() calls would interleave self._history and
        # corrupt the transcript, so we reject re-entrant streams rather than
        # silently corrupting. (The harness is a local single-user tool.)
        self._busy = threading.Lock()
        # cooperative cancel: set by the server when the SSE client disconnects
        # so run_auto halts promptly instead of burning budget for a gone client.
        self._cancel = threading.Event()
        # auto-distill: when on, run_auto proposes PENDING skill/rule candidates on
        # completion (still human-gated for approval). Off by default.
        self._auto_distill = os.environ.get("HARNESS_AUTO_DISTILL", "").strip() in ("1", "true", "yes")

    @property
    def durable(self) -> DurableState:
        return DurableState(self.state_dir)

    def export_history(self) -> list:
        """Returns the non-system messages (self._history minus the seeded system prompt) as a serializable list."""
        if len(self._history) <= 1:
            return []
        return [dict(m) for m in self._history[1:]]

    def load_history(self, messages: list) -> None:
        """Replaces the conversation turns (keep the freshly-built system prompt at index 0 -- which contains current skills/rules -- then append the loaded user/assistant messages). Do NOT persist the system prompt; only persist the user/assistant turns."""
        if not self._history:
            self._history = [{"role": "system", "content": ""}]
        system_prompt = self._history[0]
        cleaned = [m for m in messages if m.get("role") != "system"]
        self._history = [system_prompt] + cleaned

    def _render_history(self) -> str:
        """Flatten transcript into a single prompt for completion-style drivers."""
        lines = []
        for m in self._history:
            role = m["role"].upper()
            content = m.get("content") or ""
            if m.get("tool_calls"):
                tc_strs = []
                for tc in m["tool_calls"]:
                    func = tc.get("function") or {}
                    tc_strs.append(f"({func.get('name')} with arguments {func.get('arguments')})")
                if tc_strs:
                    content = (content + "\n" + "\n".join(tc_strs)).strip()
            elif m.get("role") == "tool":
                role = "USER"
                tc_id = m.get("tool_call_id") or ""
                content = f"(tool result for {tc_id}):\n{content}"
            lines.append(f"{role}: {content}")
        lines.append("ASSISTANT:")
        return "\n\n".join(lines)

    def _append_action_result(self, act: Any, aid: str, content: str, is_native: bool) -> None:
        if is_native:
            tc_id = getattr(act, "tool_call_id", None) or aid
            self._history.append({"role": "tool", "tool_call_id": tc_id, "content": content})
        else:
            self._history.append({"role": "user", "content": content})

    def cancel(self) -> None:
        """Signal any in-flight run_auto/send to stop at the next checkpoint."""
        self._cancel.set()

    def send(self, user_message: str, images: Optional[list] = None) -> Iterator[ConvEvent]:
        """Process one user message: drive the pilot loop until it yields back."""
        if not self._busy.acquire(blocking=False):
            yield ConvEvent("error", {"error": "session busy: another request is in flight"})
            return
        try:
            yield from self._send_locked(user_message, images=images)
        finally:
            self._busy.release()

    def _send_locked(self, user_message: str, images: Optional[list] = None) -> Iterator[ConvEvent]:
        processed_message = user_message
        if images:
            from .vision import transcribe_images
            yield ConvEvent("vision", {"count": len(images), "status": "transcribing"})
            results = transcribe_images(images)
            blocks = []
            for path, r in zip(images, results):
                if r.error:
                    yield ConvEvent("vision", {"path": path, "error": r.error})
                else:
                    blocks.append(f"[Image: {path}]\n{r.text}")
                    yield ConvEvent("vision", {"path": path,
                        "chars": len(r.text), "model": r.model,
                        "preview": r.text[:200]})
            if blocks:
                processed_message = ("The user attached image(s). Transcription(s) below "
                                     "(you cannot see the image, only this text):\n\n"
                                     + "\n\n".join(blocks) + "\n\n---\n" + user_message)

        self._history.append({"role": "user", "content": processed_message})
        swarms = 0
        action_seq = 0
        demo_swarms = 0  # count swarms that returned the demo substrate
        turn_findings: list = []   # accumulate real findings for wiki ingest
        turn_prose: list = []      # accumulate pilot prose for the digest

        for step in range(HARD_PILOT_STEPS):
            # 1. Ask the pilot for its next conversational turn.
            base_sys = self._history[0]["content"]
            cg_section = ""
            if self.config.repo:
                try:
                    from puppetmaster.codegraph import codegraph_context, codegraph_prompt_section
                    cg_slice = codegraph_context(task=user_message, cwd=self.config.repo)
                    if cg_slice:
                        cg_section = codegraph_prompt_section(cg_slice)
                except Exception:
                    pass

            sys_prompt = base_sys
            if cg_section:
                sys_prompt += "\n\n" + cg_section
            mcp_section = _format_mcp_tools_section(self._mcp)
            if mcp_section:
                sys_prompt += "\n\n" + mcp_section

            self._history[0]["content"] = sys_prompt
            prompt = self._render_history()

            try:
                if hasattr(self.pilot, "chat"):
                    from .pilot import build_tools_schema
                    mcp_tools = self._mcp.discovered_tools() if self._mcp else None
                    tools_schema = build_tools_schema(mcp_tools)
                    resp = self.pilot.chat(self._history[1:], tools=tools_schema, system=sys_prompt)
                else:
                    resp = self.pilot.complete(prompt, system=sys_prompt)
            except Exception as e:
                yield ConvEvent("error", {"error": f"pilot transport: {e}"})
                return
            finally:
                self._history[0]["content"] = base_sys

            # real token metering: prompt + completion (drivers report tokens_out;
            # estimate tokens_in from prompt length when not provided).
            self._tokens_used += int(getattr(resp, "tokens_out", 0) or 0)
            self._tokens_used += int(getattr(resp, "tokens_in", 0) or len(prompt) // 4)
            if resp.error:
                yield ConvEvent("error", {"error": f"pilot: {resp.error}"})
                return

            is_native = False
            tool_calls = []
            reasoning = ""
            pure_content = ""

            if hasattr(self.pilot, "chat"):
                tool_calls = resp.meta.get("tool_calls") or []
                reasoning = resp.meta.get("reasoning") or ""
                pure_content = resp.text or ""

                if tool_calls or reasoning:
                    is_native = True
                elif pure_content:
                    from .pilot import _extract_json_object
                    obj = _extract_json_object(pure_content)
                    if obj and isinstance(obj, dict) and ("say" in obj or "actions" in obj or "thinking" in obj):
                        is_native = False
                    else:
                        is_native = True
                else:
                    is_native = True

            if is_native:
                try:
                    from .pilot import parse_tool_calls, PilotTurn, parse_inline_tool_calls, strip_inline_tool_calls
                    if not tool_calls and pure_content:
                        inline_actions = parse_inline_tool_calls(pure_content)
                        if inline_actions:
                            import json
                            synthetic_tool_calls = []
                            for act in inline_actions:
                                name = act.kind
                                if act.kind == "call_mcp" and act.tool:
                                    name = f"mcp_{act.tool.replace('.', '_')}"
                                synthetic_tool_calls.append({
                                    "id": act.tool_call_id,
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": json.dumps(act.arguments)
                                    }
                                })
                            tool_calls = synthetic_tool_calls
                            actions = inline_actions
                            pure_content = strip_inline_tool_calls(pure_content)
                        else:
                            actions = parse_tool_calls(tool_calls)
                    else:
                        actions = parse_tool_calls(tool_calls)

                    turn = PilotTurn(say=pure_content, thinking=reasoning, actions=actions)
                except Exception as e:
                    yield ConvEvent("error", {"error": f"native tool parsing error: {e}"})
                    return
            else:
                try:
                    turn = parse_pilot_turn(resp.text)
                except PilotError as e:
                    # one lenient retry: tell the pilot to fix its envelope
                    self._history.append({"role": "user",
                        "content": f"(system) Your last reply was not valid. {e}. "
                                   f"Reply with the JSON envelope {{\"say\":...,\"actions\":[...]}}."})
                    continue

            # 2. Emit the pilot's prose to the user.
            cleaned_thinking_text = clean_say(turn.thinking) if turn.thinking else ""
            if cleaned_thinking_text:
                yield ConvEvent("thinking", {"text": cleaned_thinking_text})

            cleaned_say_text = clean_say(turn.say) if turn.say else ""
            if cleaned_say_text:
                yield ConvEvent("message", {"role": "assistant", "text": cleaned_say_text})
                turn_prose.append(cleaned_say_text)
            # record the pilot's turn in transcript (prose only -- the conversation)
            if is_native:
                assistant_msg: dict[str, Any] = {"role": "assistant"}
                if cleaned_say_text:
                    assistant_msg["content"] = cleaned_say_text
                else:
                    assistant_msg["content"] = ""
                if tool_calls:
                    assistant_msg["tool_calls"] = tool_calls
                self._history.append(assistant_msg)
            else:
                self._history.append({"role": "assistant", "content": cleaned_say_text or "(acting)"})

            # 3. No actions => the pilot is done talking; yield back to the user.
            if not turn.has_actions:
                self._maybe_ingest(user_message, turn_prose, turn_findings)
                yield ConvEvent("assistant_done", {"turns": step + 1, "swarms": swarms})
                return

            # 4. Execute each action as a collapsible tool-call.
            for act in turn.actions:
                action_seq += 1
                aid = f"a{action_seq}"
                act_goal = act.goal
                if act.kind in ("read_file", "write_file", "list_dir"):
                    act_goal = act.path or "(workspace root)"
                elif act.kind == "run_command":
                    act_goal = act.command
                elif act.kind == "call_mcp":
                    act_goal = act.tool
                elif act.kind == "web_search":
                    act_goal = act.query
                elif act.kind == "web_fetch":
                    act_goal = act.url
                elif act.kind == "read_pdf":
                    act_goal = act.path or act.url

                yield ConvEvent("action_start", {
                    "id": aid, "kind": act.kind, "goal": act_goal or act.tool,
                    "cwd": self.config.repo or None,
                    "adapter": self.config.swarm_adapter,
                })
                # ---- read_file branch -----------------------------------------
                if act.kind == "read_file":
                    if not self.config.repo:
                        error_msg = "No workspace directory (config.repo) is open."
                        yield ConvEvent("action_result", {"id": aid, "error": error_msg})
                        self._append_action_result(act, aid, f"(read_file {aid} failed: {error_msg})", is_native)
                        continue
                    target_path = act.path
                    if not os.path.isabs(target_path):
                        target_path = os.path.join(self.config.repo, target_path)
                    if not is_safe_path(target_path, self.config.repo):
                        error_msg = f"Path traversal attempt rejected: {act.path}"
                        yield ConvEvent("action_result", {"id": aid, "error": error_msg})
                        self._append_action_result(act, aid, f"(read_file {aid} failed: {error_msg})", is_native)
                        continue
                    try:
                        if not os.path.exists(target_path):
                            raise FileNotFoundError(f"File not found: {act.path}")
                        if os.path.isdir(target_path):
                            raise IsADirectoryError(f"Path is a directory: {act.path}")
                        with open(target_path, "r", encoding="utf-8", errors="replace") as f:
                            content = f.read(200 * 1024)
                        is_truncated = os.path.getsize(target_path) > 200 * 1024
                        if is_truncated:
                            content += "\n\n... (file truncated to 200KB) ..."
                        yield ConvEvent("action_result", {
                            "id": aid, "num": 1, "types": ["file"], "adapter": "local", "mode": "tool",
                            "artifacts": [{"type": "file", "headline": f"Read {len(content)} chars from {act.path}"}],
                        })
                        self._append_action_result(act, aid, f"(read_file {act.path} returned)\n{content}", is_native)
                    except Exception as e:
                        yield ConvEvent("action_result", {"id": aid, "error": str(e)})
                        self._append_action_result(act, aid, f"(read_file {act.path} failed: {e})", is_native)
                    continue
                # ---- write_file branch ----------------------------------------
                if act.kind == "write_file":
                    if not self.config.repo:
                        error_msg = "No workspace directory (config.repo) is open."
                        yield ConvEvent("action_result", {"id": aid, "error": error_msg})
                        self._append_action_result(act, aid, f"(write_file {aid} failed: {error_msg})", is_native)
                        continue
                    target_path = act.path
                    if not os.path.isabs(target_path):
                        target_path = os.path.join(self.config.repo, target_path)
                    if not is_safe_path(target_path, self.config.repo):
                        error_msg = f"Path traversal attempt rejected: {act.path}"
                        yield ConvEvent("action_result", {"id": aid, "error": error_msg})
                        self._append_action_result(act, aid, f"(write_file {aid} failed: {error_msg})", is_native)
                        continue
                    try:
                        target_dir = os.path.dirname(target_path)
                        os.makedirs(target_dir, exist_ok=True)
                        import tempfile
                        fd, temp_path = tempfile.mkstemp(dir=target_dir, prefix=".tmp-")
                        try:
                            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                                f.write(act.content)
                            os.replace(temp_path, target_path)
                        except Exception as e:
                            if os.path.exists(temp_path):
                                os.remove(temp_path)
                            raise e
                        bytes_written = len(act.content.encode('utf-8'))
                        yield ConvEvent("action_result", {
                            "id": aid, "num": 1, "types": ["file"], "adapter": "local", "mode": "tool",
                            "artifacts": [{"type": "file", "headline": f"Wrote {bytes_written} bytes to {act.path}"}],
                        })
                        self._append_action_result(act, aid, f"(write_file {act.path} successfully wrote {bytes_written} bytes)", is_native)
                    except Exception as e:
                        yield ConvEvent("action_result", {"id": aid, "error": str(e)})
                        self._append_action_result(act, aid, f"(write_file {act.path} failed: {e})", is_native)
                    continue
                # ---- run_command branch ---------------------------------------
                if act.kind == "run_command":
                    if not self.config.repo:
                        error_msg = "No workspace directory (config.repo) is open."
                        yield ConvEvent("action_result", {"id": aid, "error": error_msg})
                        self._append_action_result(act, aid, f"(run_command {aid} failed: {error_msg})", is_native)
                        continue
                    try:
                        p = subprocess.run(
                            act.command,
                            shell=True,
                            cwd=self.config.repo,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True,
                            timeout=120
                        )
                        output = p.stdout or ""
                        exit_code = p.returncode
                    except subprocess.TimeoutExpired as te:
                        out_str = te.stdout.decode('utf-8', errors='replace') if isinstance(te.stdout, bytes) else (te.stdout or "")
                        output = out_str + f"\n\n[TimeoutExpired after 120 seconds]"
                        exit_code = -1
                    except Exception as e:
                        output = f"Failed to execute command: {e}"
                        exit_code = -1
                    MAX_CAP = 50 * 1024
                    if len(output) > MAX_CAP:
                        output = output[:MAX_CAP] + "\n\n... (output truncated to 50KB) ..."
                    yield ConvEvent("action_result", {
                        "id": aid, "num": 1, "types": ["command"], "adapter": "local", "mode": "tool",
                        "artifacts": [{"type": "command", "headline": f"Command exited with {exit_code}"}],
                    })
                    self._append_action_result(act, aid, f"(run_command '{act.command}' completed with exit code {exit_code})\n{output}", is_native)
                    continue
                # ---- list_dir branch ------------------------------------------
                if act.kind == "list_dir":
                    if not self.config.repo:
                        error_msg = "No workspace directory (config.repo) is open."
                        yield ConvEvent("action_result", {"id": aid, "error": error_msg})
                        self._append_action_result(act, aid, f"(list_dir {aid} failed: {error_msg})", is_native)
                        continue
                    target_path = act.path
                    if not target_path or not target_path.strip():
                        target_path = self.config.repo
                    else:
                        if not os.path.isabs(target_path):
                            target_path = os.path.join(self.config.repo, target_path)
                    if not is_safe_path(target_path, self.config.repo):
                        error_msg = f"Path traversal attempt rejected: {act.path}"
                        yield ConvEvent("action_result", {"id": aid, "error": error_msg})
                        self._append_action_result(act, aid, f"(list_dir {aid} failed: {error_msg})", is_native)
                        continue
                    try:
                        if not os.path.exists(target_path):
                            raise FileNotFoundError(f"Directory not found: {act.path}")
                        if not os.path.isdir(target_path):
                            raise IsADirectoryError(f"Path is not a directory: {act.path}")
                        entries = []
                        skip_names = {".git", "node_modules", ".venv", ".codegraph"}
                        for entry in os.scandir(target_path):
                            if entry.name in skip_names:
                                continue
                            is_dir = entry.is_dir()
                            entries.append({
                                "name": entry.name,
                                "is_dir": is_dir,
                                "size": entry.stat().st_size if not is_dir else 0
                            })
                        entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
                        text_list = []
                        for e in entries:
                            suffix = "/" if e["is_dir"] else ""
                            size_str = f" ({e['size']} bytes)" if not e["is_dir"] else ""
                            text_list.append(f"{e['name']}{suffix}{size_str}")
                        result_text = "\n".join(text_list) if text_list else "(empty directory)"
                        yield ConvEvent("action_result", {
                            "id": aid, "num": 1, "types": ["dir"], "adapter": "local", "mode": "tool",
                            "artifacts": [{"type": "dir", "headline": f"Listed {len(entries)} items in {act.path or '/'}"}],
                        })
                        self._append_action_result(act, aid, f"(list_dir {act.path or '/'} returned)\n{result_text}", is_native)
                    except Exception as e:
                        yield ConvEvent("action_result", {"id": aid, "error": str(e)})
                        self._append_action_result(act, aid, f"(list_dir {act.path or '/'} failed: {e})", is_native)
                    continue
                # ---- web_search branch ----------------------------------------
                if act.kind == "web_search":
                    from .web_tools import web_search
                    try:
                        result_text = web_search(act.query)
                        yield ConvEvent("action_result", {
                            "id": aid, "num": 1, "types": ["web_search"], "adapter": "local", "mode": "tool",
                            "artifacts": [{"type": "web_search", "headline": f"Searched for '{act.query}'"}],
                        })
                        self._append_action_result(act, aid, f"(web_search '{act.query}' returned)\n{result_text}", is_native)
                    except Exception as e:
                        yield ConvEvent("action_result", {"id": aid, "error": str(e)})
                        self._append_action_result(act, aid, f"(web_search '{act.query}' failed: {e})", is_native)
                    continue
                # ---- web_fetch branch -----------------------------------------
                if act.kind == "web_fetch":
                    from .web_tools import web_fetch
                    try:
                        result_text = web_fetch(act.url)
                        yield ConvEvent("action_result", {
                            "id": aid, "num": 1, "types": ["web_fetch"], "adapter": "local", "mode": "tool",
                            "artifacts": [{"type": "web_fetch", "headline": f"Fetched {act.url}"}],
                        })
                        self._append_action_result(act, aid, f"(web_fetch '{act.url}' returned)\n{result_text}", is_native)
                    except Exception as e:
                        yield ConvEvent("action_result", {"id": aid, "error": str(e)})
                        self._append_action_result(act, aid, f"(web_fetch '{act.url}' failed: {e})", is_native)
                    continue
                # ---- read_pdf branch ------------------------------------------
                if act.kind == "read_pdf":
                    from .web_tools import read_pdf
                    target = act.path or act.url
                    is_remote = target.startswith(("http://", "https://"))
                    
                    if not is_remote:
                        if not self.config.repo:
                            error_msg = "No workspace directory (config.repo) is open."
                            yield ConvEvent("action_result", {"id": aid, "error": error_msg})
                            self._append_action_result(act, aid, f"(read_pdf {aid} failed: {error_msg})", is_native)
                            continue
                        target_path = act.path
                        if not os.path.isabs(target_path):
                            target_path = os.path.join(self.config.repo, target_path)
                        if not is_safe_path(target_path, self.config.repo):
                            error_msg = f"Path traversal attempt rejected: {act.path}"
                            yield ConvEvent("action_result", {"id": aid, "error": error_msg})
                            self._append_action_result(act, aid, f"(read_pdf {aid} failed: {error_msg})", is_native)
                            continue
                        target = target_path

                    try:
                        result_text = read_pdf(target)
                        yield ConvEvent("action_result", {
                            "id": aid, "num": 1, "types": ["read_pdf"], "adapter": "local", "mode": "tool",
                            "artifacts": [{"type": "read_pdf", "headline": f"Read PDF from {act.path or act.url}"}],
                        })
                        self._append_action_result(act, aid, f"(read_pdf '{act.path or act.url}' returned)\n{result_text}", is_native)
                    except Exception as e:
                        yield ConvEvent("action_result", {"id": aid, "error": str(e)})
                        self._append_action_result(act, aid, f"(read_pdf '{act.path or act.url}' failed: {e})", is_native)
                    continue
                # ---- MCP tool call branch -------------------------------------
                if act.kind == "call_mcp":
                    if self._mcp is None:
                        yield ConvEvent("action_result", {"id": aid, "error": "MCP not available"})
                        self._append_action_result(act, aid, f"(mcp {aid} unavailable)", is_native)
                        continue
                    try:
                        out = self._mcp.call(act.tool, act.arguments)
                        text = _mcp_result_text(out)
                    except Exception as e:
                        yield ConvEvent("action_result", {"id": aid, "error": f"mcp: {e}"})
                        self._append_action_result(act, aid, f"(mcp {act.tool} failed: {e})", is_native)
                        continue
                    yield ConvEvent("action_result", {
                        "id": aid, "tool": act.tool, "num": 1,
                        "types": ["mcp"], "adapter": "mcp", "mode": "tool",
                        "artifacts": [{"type": "mcp", "headline": f"{act.tool}: {text[:120]}"}],
                    })
                    self._append_action_result(act, aid, f"(mcp {act.tool} returned)\n{text[:2000]}", is_native)
                    continue
                # ---- swarm branch --------------------------------------------
                intent = DriverIntent(action="run_swarm", goal=act.goal,
                                      roles=act.roles or None, rationale="pilot")
                try:
                    result: BridgeResult = execute_intent(intent, state_dir=self.state_dir)
                except Exception as e:
                    yield ConvEvent("action_result", {"id": aid, "error": f"execute: {e}"})
                    self._append_action_result(act, aid, f"(swarm {aid} failed: {e})", is_native)
                    continue
                swarms += 1
                if result.adapter == "demo":
                    demo_swarms += 1
                yield ConvEvent("action_result", {
                    "id": aid, "job_id": result.job_id, "num": result.num_artifacts,
                    "types": result.artifact_types, "artifacts": result.artifacts[:8],
                    "adapter": result.adapter, "mode": result.mode,
                })
                # collect non-substrate findings for durable knowledge capture
                if result.adapter != "demo":
                    turn_findings.extend(
                        a for a in result.artifacts if a.get("type") != "verification")
                # 5. Feed DISTILLED artifacts back into the transcript (not raw files).
                digest = "\n".join(f"  - [{a['type']}] {a['headline']}"
                                   for a in result.artifacts[:8]) or "  (no artifacts)"
                stall = ""
                if demo_swarms >= 2:
                    stall = ("\n(NOTE: swarms are running on the DEMO substrate, which "
                             "returns generic artifacts -- not real codebase analysis. "
                             "Do NOT keep retrying; explain this to the user and finish "
                             "with no actions. Real analysis needs --repo + "
                             "--swarm-adapter openai.)")
                self._append_action_result(act, aid, f"(swarm {aid} '{act.goal}' returned {result.num_artifacts} artifacts via {result.adapter}:\n{digest}\nExplain these findings to the user and either run a narrowed follow-up swarm or finish with no actions.){stall}", is_native)

        # Hit the step cap -- close the turn gracefully.
        self._maybe_ingest(user_message, turn_prose, turn_findings)
        yield ConvEvent("message", {"role": "assistant",
            "text": "(Reached the investigation step limit for this message.)"})
        yield ConvEvent("assistant_done", {"turns": HARD_PILOT_STEPS, "swarms": swarms})

    def _maybe_ingest(self, user_message: str, prose: list, findings: list) -> None:
        """Auto-ingest a session digest to the wiki when enabled and there are
        real findings worth capturing. Never fires the orchestrator (token-spend)."""
        # accumulate for self-learning distillation (independent of wiki config)
        if findings:
            self._session_findings.extend(findings)
            if not self._first_objective:
                self._first_objective = user_message
        if not (self._wiki_auto and self._wiki.configured and findings):
            return
        try:
            digest = session_digest(user_message, prose, findings)
            slug = f"harness-{_slugify(user_message)}"
            self._wiki.ingest(slug, digest, note="auto-captured by pm-harness",
                              run_orchestrator=False)
        except Exception:
            pass  # wiki capture is best-effort; never break the conversation


    def _maybe_auto_distill(self):
        """If auto-distill is enabled and the run produced findings, propose
        PENDING candidates and yield a 'distilled' event. Best-effort."""
        if not self._auto_distill:
            return None
        real = [f for f in self._session_findings if f.get("type") != "verification"]
        if len(real) < 2:
            return None
        try:
            return self.distill()
        except Exception as e:
            return {"status": "error", "reason": str(e)}

    def distill(self) -> dict:
        """Propose PENDING candidate skill(s) AND rule(s) from this session's
        accumulated findings. Human approval required before either loads into
        context. Returns a combined status dict."""
        out = {}
        try:
            out["skill"] = distill_session(self.pilot, self._first_objective or "(session)",
                                           self._session_findings, self._skills)
        except Exception as e:
            out["skill"] = {"status": "error", "reason": str(e)}
        try:
            out["rules"] = distill_rules(self.pilot, self._first_objective or "(session)",
                                         self._session_findings, self._rules)
        except Exception as e:
            out["rules"] = {"status": "error", "reason": str(e)}
        return out

    def run_auto(self, objective: str, budget: "AutoBudget" = None,
                 *, require_codegraph: bool = True):
        """FULLY-AUTO (unattended) mode: pursue an objective across many pilot
        turns WITHOUT user re-prompting, bounded by an AutoBudget governor. Yields
        the same ConvEvents as send(), plus 'auto_status' (governor snapshots) and
        a terminal 'auto_halt' with the reason.

        SAFETY PRECONDITIONS (refused otherwise):
          - a governor is required (no ceilings == no unattended run)
          - if real analysis is configured, the repo MUST be CodeGraph-indexed
            (the accuracy benchmark proved unindexed -> ~30% blind guessing, which
            is exactly the confident-garbage failure mode you must not run all
            night). Override only with require_codegraph=False.
        """
        budget = (budget or AutoBudget.from_env()).start()

        # Precondition: real analysis on an unindexed repo is refused unattended.
        if (require_codegraph and self.config.swarm_adapter == "openai"
                and self.config.repo):
            import os.path as _op
            if not _op.isdir(_op.join(self.config.repo, ".codegraph")):
                yield ConvEvent("auto_halt", {"reason":
                    f"REFUSED: {self.config.repo} has no .codegraph index. Unattended "
                    f"analysis would run blind (~30% accuracy). Run: python -m "
                    f"puppetmaster codegraph init --index", "snapshot": budget.snapshot()})
                return

        # Seed the objective + an instruction to self-continue until done.
        message = (f"{objective}\n\n(AUTONOMOUS MODE: pursue this objective to "
                   f"completion across multiple investigation rounds. After each "
                   f"round, if more investigation is warranted and useful, continue "
                   f"with another swarm; finish with no actions only when the "
                   f"objective is genuinely met or no further progress is possible.)")

        cycle = 0
        self._cancel.clear()
        while True:
            if self._cancel.is_set():
                yield ConvEvent("auto_halt", {"reason": "cancelled (client disconnect)",
                                              "snapshot": budget.snapshot()})
                return
            halt = budget.check()
            if halt:
                yield ConvEvent("auto_halt", {"reason": halt, "snapshot": budget.snapshot()})
                d = self._maybe_auto_distill()
                if d:
                    yield ConvEvent("distilled", d)
                self._maybe_ingest(objective, [], [])
                return
            cycle += 1
            findings_before = 0
            tokens_at_cycle_start = self._tokens_used
            # one pilot turn (send() drives say->act->react until it yields back)
            turn_findings_count = 0
            tripped = None
            for ev in self.send(message if cycle == 1 else
                                 "(continue toward the objective, or finish if met)"):
                # meter the governor off the stream
                if ev.kind == "action_result" and not ev.data.get("error"):
                    budget.add_swarm()
                    turn_findings_count += int(ev.data.get("num", 0) or 0)
                yield ev
                if ev.kind == "assistant_done":
                    break
                # CHECK THE CEILING MID-STREAM: a never-stopping pilot fires swarms
                # inside one send() call; without this the governor only catches it
                # between cycles and burns the whole inner budget first.
                if self._cancel.is_set():
                    tripped = "cancelled (client disconnect)"
                    break
                tripped = budget.check()
                if tripped:
                    break
            if tripped:
                yield ConvEvent("auto_halt", {"reason": tripped, "snapshot": budget.snapshot()})
                d = self._maybe_auto_distill()
                if d:
                    yield ConvEvent("distilled", d)
                self._maybe_ingest(objective, [], [])
                return
            # account for stall + emit a governor heartbeat
            budget.note_findings(turn_findings_count)
            # REAL token metering: feed the delta consumed this cycle into the
            # governor so the documented token ceiling actually trips.
            delta = self._tokens_used - tokens_at_cycle_start
            if delta > 0:
                budget.add_tokens(delta)
            yield ConvEvent("auto_status", {"cycle": cycle, "snapshot": budget.snapshot()})
            # if the pilot finished a turn with no swarms at all, it considers the
            # objective met -> stop the autonomous loop.
            if turn_findings_count == 0 and budget.idle_steps >= 1:
                yield ConvEvent("auto_halt", {"reason": "pilot reports objective met "
                    "(no further investigation)", "snapshot": budget.snapshot()})
                d = self._maybe_auto_distill()
                if d:
                    yield ConvEvent("distilled", d)
                self._maybe_ingest(objective, [], [])
                return


def _slugify(s: str) -> str:
    import re
    return (re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "session")[:60]
