from __future__ import annotations

"""Pilot contract: the conversational driver envelope.

The pilot is the model the USER talks to (swappable: qwen/deepseek/opus/...).
Unlike the bare DriverIntent (one action, used by the eval harness), the pilot
emits a CONVERSATIONAL TURN: prose for the human + zero-or-more orchestration
actions it wants to fire under the hood. This mirrors how Cursor/Hermes models
emit text + tool calls -- run_swarm is just a tool the pilot calls.

Envelope (what the pilot model must emit):

    {
      "say": "I'll map the auth flow first, then dig into the middleware.",
      "actions": [
        {"kind": "run_swarm", "goal": "Map authentication across the codebase",
         "roles": ["explore"]}
      ]
    }

- `say` is prose shown to the user (the conversation / transcript channel).
- `actions` is zero or more orchestration calls. Empty actions => the pilot is
  just talking (answering / explaining findings / asking) and the turn yields
  back to the user.
- When actions are present, the loop executes them, feeds the resulting
  artifacts back to the pilot, and the pilot reacts (more prose, maybe more
  actions) until it emits a turn with no actions.

The transcript lives in the user<->pilot channel. Swarm workers receive only the
DISTILLED `goal` brief (plus CodeGraph), never the transcript -- decoupling
conversation from the token-heavy investigation.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Optional


VALID_ACTION_KINDS = {"run_swarm", "call_mcp", "read_file", "write_file", "edit_file", "run_command", "list_dir", "web_search", "web_fetch", "read_pdf", "search_codegraph", "search_files", "query_wiki", "run_implement", "run_parallel", "route_task", "view_image", "memory"}


@dataclass
class PilotAction:
    kind: str
    goal: str = ""
    roles: list = field(default_factory=list)
    tool: str = ""              # call_mcp: qualified MCP tool name (server.tool)
    arguments: dict = field(default_factory=dict)  # call_mcp: tool arguments
    path: str = ""
    content: str = ""
    command: str = ""
    query: str = ""
    url: str = ""
    tool_call_id: str = ""
    goals: list = field(default_factory=list)  # run_parallel: list of goals
    adapter: str = ""           # run_implement / run_parallel: optional adapter name
    mode: str = ""              # run_parallel: "implement" or "analysis" / "review"
    instruction: str = ""       # route_task: instruction text
    old_str: str = ""
    new_str: str = ""
    memory_action: str = ""
    memory_content: str = ""
    memory_id: str = ""
    memory_category: str = "general"

    def validate(self) -> "PilotAction":
        if self.kind not in VALID_ACTION_KINDS:
            raise PilotError(f"unknown action kind: {self.kind!r}")
        if self.kind == "memory":
            if not self.memory_action:
                raise PilotError("memory action requires an 'action'")
            if self.memory_action not in ("add", "remove", "update", "list"):
                raise PilotError(f"unknown memory action: {self.memory_action}")
            if self.memory_action in ("add", "update") and not (self.memory_content or "").strip():
                raise PilotError(f"memory action {self.memory_action} requires 'content'")
            if self.memory_action in ("remove", "update") and not (self.memory_id or "").strip():
                raise PilotError(f"memory action {self.memory_action} requires 'entry_id'")
        if self.kind == "run_swarm" and not (self.goal or "").strip():
            raise PilotError("run_swarm action requires a non-empty goal")
        if self.kind == "run_implement" and not (self.goal or "").strip():
            raise PilotError("run_implement action requires a non-empty goal")
        if self.kind == "run_parallel" and not self.goals:
            raise PilotError("run_parallel action requires a list of 'goals'")
        if self.kind == "route_task" and not (self.instruction or "").strip():
            raise PilotError("route_task action requires a non-empty instruction")
        if self.kind == "call_mcp" and not (self.tool or "").strip():
            raise PilotError("call_mcp action requires a 'tool' (server.tool)")
        if self.kind in ("read_file", "write_file", "view_image") and not (self.path or "").strip():
            raise PilotError(f"{self.kind} action requires a 'path'")
        if self.kind == "edit_file" and not (self.path or "").strip():
            raise PilotError("edit_file action requires a 'path'")
        if self.kind == "edit_file" and not self.old_str:
            raise PilotError("edit_file action requires 'old_str'")
        if self.kind == "run_command" and not (self.command or "").strip():
            raise PilotError("run_command action requires a 'command'")
        if self.kind == "web_search" and not (self.query or "").strip():
            raise PilotError("web_search action requires a 'query'")
        if self.kind == "web_fetch" and not (self.url or "").strip():
            raise PilotError("web_fetch action requires a 'url'")
        if self.kind == "read_pdf" and not (self.path or "").strip() and not (self.url or "").strip():
            raise PilotError("read_pdf action requires a 'path' or 'url'")
        if self.kind == "search_codegraph" and not (self.query or "").strip():
            raise PilotError("search_codegraph action requires a 'query'")
        if self.kind == "search_files" and not (self.query or "").strip():
            raise PilotError("search_files action requires a 'query'")
        if self.kind == "query_wiki" and not (self.arguments.get("question") or "").strip():
            raise PilotError("query_wiki action requires a 'question'")
        if self.roles and not isinstance(self.roles, list):
            raise PilotError("roles must be a list")
        return self


@dataclass
class PilotTurn:
    say: str = ""
    thinking: str = ""
    actions: list = field(default_factory=list)  # list[PilotAction]

    @property
    def has_actions(self) -> bool:
        return bool(self.actions)


class PilotError(ValueError):
    """Raised when a pilot envelope cannot be parsed/validated."""


def _coerce_actions(raw_actions) -> list:
    actions = []
    if not raw_actions:
        return actions
    if isinstance(raw_actions, dict):
        raw_actions = [raw_actions]
    if not isinstance(raw_actions, list):
        raise PilotError("actions must be a list")
    for a in raw_actions:
        if not isinstance(a, dict):
            raise PilotError("each action must be an object")
        kind = a.get("kind") or a.get("action") or "run_swarm"
        # if a bare "tool" + "arguments" shape is given, treat as call_mcp
        tool = a.get("tool") or ""
        arguments = a.get("arguments") or a.get("args") or {}
        if tool and kind in ("run_swarm",) and ("goal" not in a and "instruction" not in a):
            kind = "call_mcp"
        goal = a.get("goal") or a.get("instruction") or a.get("task") or ""
        roles = a.get("roles") or []
        if isinstance(roles, str):
            roles = [roles]
        if not isinstance(arguments, dict):
            arguments = {}
        path = a.get("path") or ""
        content = a.get("content") or ""
        command = a.get("command") or ""
        query = a.get("query") or ""
        url = a.get("url") or ""
        goals = a.get("goals") or []
        if isinstance(goals, str):
            goals = [goals]
        adapter = a.get("adapter") or ""
        mode = a.get("mode") or ""
        instruction = a.get("instruction") or ""
        actions.append(PilotAction(kind=str(kind), goal=str(goal), roles=roles,
                                   tool=str(tool), arguments=arguments,
                                   path=str(path), content=str(content), command=str(command),
                                   query=str(query), url=str(url),
                                   goals=goals, adapter=str(adapter), mode=str(mode),
                                   instruction=str(instruction)).validate())
    return actions


def build_tools_schema(mcp_tools: Optional[list] = None, no_delegation: bool = False) -> list:
    schema = []

    # 1. read_file
    schema.append({
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "read a file's contents from the workspace. Requires `path`.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative path to the file to read"}
                },
                "required": ["path"]
            }
        }
    })

    # 2. write_file
    schema.append({
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "write/create a file atomically. Requires `path` and `content`.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative path to the file to write"},
                    "content": {"type": "string", "description": "The exact content to write/overwrite in the file"}
                },
                "required": ["path", "content"]
            }
        }
    })

    # edit_file
    schema.append({
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Make a targeted edit to an existing file by replacing an exact substring. STRONGLY PREFERRED over write_file for editing existing files -- only send the small snippet that changes, never the whole file. Requires path, old_str (the EXACT existing text to replace, including surrounding context to make it unique), and new_str (the replacement). To append, set old_str to a unique trailing snippet of the file and include it at the start of new_str.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative path to the file to edit"},
                    "old_str": {"type": "string", "description": "The EXACT existing text to replace, including surrounding context to make it unique"},
                    "new_str": {"type": "string", "description": "The replacement text (may be empty to delete)"}
                },
                "required": ["path", "old_str", "new_str"]
            }
        }
    })

    # 3. run_command
    schema.append({
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "run a terminal shell command. Requires `command`.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The terminal shell command to execute"}
                },
                "required": ["command"]
            }
        }
    })

    # 4. list_dir
    schema.append({
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "list the files and folders inside a directory. `path` is optional.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path to list. If empty, lists workspace root."}
                }
            }
        }
    })

    # 5. web_search
    schema.append({
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "search the internet and return top results. Requires `query`.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"}
                },
                "required": ["query"]
            }
        }
    })

    # 6. web_fetch
    schema.append({
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "read a web page's text contents. Requires `url`.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL of the webpage to fetch"}
                },
                "required": ["url"]
            }
        }
    })

    # 7. read_pdf
    schema.append({
        "type": "function",
        "function": {
            "name": "read_pdf",
            "description": "extract plain text from a local PDF file or PDF URL. Requires `path` or `url`.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Local path to PDF file"},
                    "url": {"type": "string", "description": "URL to PDF file"}
                }
            }
        }
    })

    # view_image
    schema.append({
        "type": "function",
        "function": {
            "name": "view_image",
            "description": "View/describe an image file (screenshot, diagram, photo, mockup). Use this to SEE an image referenced in the task or repo -- it is transcribed to a precise text description you can reason over. Requires path (path to a .png/.jpg/.jpeg/.webp image, relative to the repo or absolute).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative path to the image file to view"}
                },
                "required": ["path"]
            }
        }
    })

    # 8. run_swarm
    if not no_delegation:
        schema.append({
            "type": "function",
            "function": {
                "name": "run_swarm",
                "description": "dispatch a parallel agent swarm for complex/broad investigations. Requires `goal`.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string", "description": "The specific objective or question for the swarm workers"},
                        "roles": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional worker roles list"
                        },
                        "worker_mode": {
                            "type": "string",
                            "enum": ["subprocess", "inline", "daemon"],
                            "description": "Optional worker process mode"
                        }
                    },
                    "required": ["goal"]
                }
            }
        })

    # 9. search_codegraph
    if not no_delegation:
        schema.append({
            "type": "function",
            "function": {
                "name": "search_codegraph",
                "description": "search the CodeGraph index for symbol usages, definitions, or context. Requires `query`.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "The search query, symbol, or question about the codebase"},
                        "kind": {"type": "string", "enum": ["search", "context"], "description": "Optional search kind: 'search' (symbols/calls/grep) or 'context' (deeper task-enclosing code structure/affected nodes)"}
                    },
                    "required": ["query"]
                }
            }
        })

    # search_files
    if not no_delegation:
        schema.append({
            "type": "function",
            "function": {
                "name": "search_files",
                "description": "plain-text/regex content search over the repository, complementary to symbol search. Requires `query`.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "The plain-text or regex query to search for"},
                        "path": {"type": "string", "description": "Optional subdirectory to scope the search, relative to repo root"},
                        "max_results": {"type": "integer", "description": "Optional max results to return, default 50"}
                    },
                    "required": ["query"]
                }
            }
        })

    # 10. query_wiki
    if not no_delegation:
        schema.append({
            "type": "function",
            "function": {
                "name": "query_wiki",
                "description": "query the durable cross-session architecture and knowledge wiki. Requires `question`.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "description": "The question to ask the knowledge wiki"}
                    },
                    "required": ["question"]
                }
            }
        })

    # 11. run_implement
    if not no_delegation:
        schema.append({
            "type": "function",
            "function": {
                "name": "run_implement",
                "description": "dispatch an edit-capable Puppetmaster worker that edits the repo in an isolated worktree and produces a patch. Requires `goal`.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string", "description": "The coding objective / task description to implement"},
                        "adapter": {"type": "string", "description": "Optional Puppetmaster adapter to run (e.g., hermes, cursor, codex, claude-code)"}
                    },
                    "required": ["goal"]
                }
            }
        })

    # 12. run_parallel
    if not no_delegation:
        schema.append({
            "type": "function",
            "function": {
                "name": "run_parallel",
                "description": "dispatch multiple Puppetmaster workers concurrently. Requires `goals` array.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "goals": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Array of goals/objectives to run in parallel"
                        },
                        "adapter": {"type": "string", "description": "Optional Puppetmaster adapter to run (e.g., hermes, cursor, codex, claude-code)"},
                        "mode": {"type": "string", "enum": ["implement", "analysis", "review"], "description": "Worker execution mode: 'implement' (can edit) or 'analysis'/'review' (read-only)"}
                    },
                    "required": ["goals"]
                }
            }
        })

    # 13. route_task
    schema.append({
        "type": "function",
        "function": {
            "name": "route_task",
            "description": "preview which model the router would pick + estimated cost for a given instruction without executing it. Requires `instruction`.",
            "parameters": {
                "type": "object",
                "properties": {
                    "instruction": {"type": "string", "description": "The task instruction text"},
                    "role": {"type": "string", "description": "Task role (e.g., explore, implement, review). Default: explore"}
                },
                "required": ["instruction"]
            }
        }
    })

    # 14. memory
    schema.append({
        "type": "function",
        "function": {
            "name": "memory",
            "description": "Save or update a durable fact or preference that should persist across ALL future sessions (user preferences, environment details, stable conventions). Use when the user states a preference, corrects you, or reveals a stable fact about their setup. Keep entries compact and high-signal. Do NOT save ephemeral task state or secrets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "remove", "update", "list"],
                        "description": "The memory operation to perform"
                    },
                    "content": {
                        "type": "string",
                        "description": "The fact or preference to save (required for 'add' and 'update')"
                    },
                    "entry_id": {
                        "type": "string",
                        "description": "The unique ID of the memory entry to update or remove (required for 'update' and 'remove')"
                    },
                    "category": {
                        "type": "string",
                        "enum": ["preference", "environment", "fact", "convention"],
                        "description": "Category for classification (optional)"
                    }
                },
                "required": ["action"]
            }
        }
    })

    # MCP tools
    if mcp_tools:
        for tool in mcp_tools:
            mcp_name = f"mcp_{tool.server}_{tool.name}"
            schema.append({
                "type": "function",
                "function": {
                    "name": mcp_name,
                    "description": tool.description or f"MCP tool from server {tool.server}",
                    "parameters": tool.input_schema or {"type": "object", "properties": {}, "required": []}
                }
            })

    return schema


def _tool_name_to_action(name: str, args: dict, tool_call_id: str = "") -> PilotAction:
    if name.startswith("mcp_"):
        parts = name.split("_", 2)
        if len(parts) >= 3:
            server = parts[1]
            tool_name = parts[2]
            kind = "call_mcp"
            tool = f"{server}.{tool_name}"
        else:
            kind = "call_mcp"
            tool = name[4:]

        return PilotAction(
            kind=kind,
            tool=tool,
            arguments=args,
            tool_call_id=tool_call_id
        ).validate()
    elif name in VALID_ACTION_KINDS:
        kind = name
        # Tolerant arg extraction: some models emit aliases (file_path/filename/file,
        # text/code/file_contents) instead of the schema names. Accept the common ones
        # so a slightly-off tool call still does the right thing instead of erroring.
        path = (args.get("path") or args.get("file_path") or args.get("filename")
                or args.get("file") or args.get("filepath") or "")
        content = (args.get("content") or args.get("text") or args.get("code")
                   or args.get("file_contents") or args.get("contents") or "")
        old_str = (args.get("old_str") or args.get("old_string") or args.get("old")
                   or args.get("search") or "")
        new_str = (args.get("new_str") or args.get("new_string") or args.get("new")
                   or args.get("replace") or args.get("content") or args.get("text") or "")
        command = (args.get("command") or args.get("cmd") or args.get("shell") or "")
        query = args.get("query") or ""
        url = args.get("url") or ""
        goal = args.get("goal") or ""
        roles = args.get("roles") or []
        if isinstance(roles, str):
            roles = [roles]
        goals = args.get("goals") or []
        if isinstance(goals, str):
            goals = [goals]
        adapter = args.get("adapter") or ""
        mode = args.get("mode") or ""
        instruction = args.get("instruction") or ""
        memory_action = args.get("action") or ""
        memory_content = args.get("content") or args.get("text") or ""
        memory_id = args.get("entry_id") or args.get("id") or ""
        memory_category = args.get("category") or "general"

        return PilotAction(
            kind=kind,
            path=path,
            content=content,
            command=command,
            query=query,
            url=url,
            goal=goal,
            roles=roles,
            goals=goals,
            adapter=adapter,
            mode=mode,
            instruction=instruction,
            old_str=old_str,
            new_str=new_str,
            memory_action=memory_action,
            memory_content=memory_content,
            memory_id=memory_id,
            memory_category=memory_category,
            arguments=args,
            tool_call_id=tool_call_id
        ).validate()
    else:
        raise PilotError(f"unknown native tool name: {name}")


def _parse_lenient_json(s: str) -> dict:
    s = s.strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    import ast
    try:
        py_s = s.replace("true", "True").replace("false", "False").replace("null", "None")
        val = ast.literal_eval(py_s)
        if isinstance(val, dict):
            return val
    except Exception:
        pass
    raise ValueError("Failed to parse JSON")


def parse_inline_tool_calls(content: str) -> list[PilotAction]:
    actions = []
    if not content:
        return actions

    idx = 0

    # 1. Shape B/C: <tool_call> ... </tool_call>
    for tc_match in re.finditer(r'<tool_call>(.*?)</tool_call>', content, re.DOTALL):
        inside = tc_match.group(1)
        span = _first_balanced_braces(inside)
        if span:
            try:
                obj = _parse_lenient_json(span)
                if isinstance(obj, dict):
                    name = obj.get("name") or obj.get("action") or ""
                    arguments = obj.get("arguments") or obj.get("args") or {}
                    if name:
                        try:
                            idx += 1
                            tc_id = f"call_inline_{idx}"
                            action = _tool_name_to_action(name, arguments, tool_call_id=tc_id)
                            actions.append(action)
                        except Exception:
                            pass
            except Exception:
                pass

    # 2. Shape A: <function=NAME> ... </function>
    matches = list(re.finditer(r'<function=([^>\s]+)>', content))
    for i, m in enumerate(matches):
        name = m.group(1)
        start_idx = m.end()
        end_idx = matches[i+1].start() if i + 1 < len(matches) else len(content)
        sub = content[start_idx:end_idx]
        
        close_idx = sub.find("</function>")
        if close_idx >= 0:
            sub = sub[:close_idx]
            
        args = {}
        p_matches = list(re.finditer(r'<parameter=([^>\s]+)>', sub))
        for j, pm in enumerate(p_matches):
            p_name = pm.group(1)
            p_start = pm.end()
            p_end = p_matches[j+1].start() if j + 1 < len(p_matches) else len(sub)
            p_sub = sub[p_start:p_end]
            p_close = p_sub.find("</parameter>")
            if p_close >= 0:
                p_val = p_sub[:p_close].strip()
            else:
                p_val = p_sub.strip()
            args[p_name] = p_val
            
        try:
            idx += 1
            tc_id = f"call_inline_{idx}"
            action = _tool_name_to_action(name, args, tool_call_id=tc_id)
            actions.append(action)
        except Exception:
            pass
            
    return actions


def strip_inline_tool_calls(content: str) -> str:
    if not content:
        return ""
    content = re.sub(r'<function=[^>\s]+>.*?(?:</function>|$)', '', content, flags=re.DOTALL)
    content = re.sub(r'<tool_call>.*?(?:</tool_call>|$)', '', content, flags=re.DOTALL)
    content = re.sub(r'</tool_call>', '', content)
    return content.strip()


def parse_tool_calls(tool_calls: list) -> list[PilotAction]:
    actions = []
    if not tool_calls:
        return actions

    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        func = tc.get("function")
        if not func:
            continue
        name = func.get("name") or ""
        tc_id = tc.get("id") or ""
        raw_args = func.get("arguments") or {}
        
        args_failed = False
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args)
            except Exception:
                args = {}
                args_failed = True
        elif isinstance(raw_args, dict):
            args = raw_args
        else:
            args = {}
            args_failed = True

        if args_failed:
            actions.append(PilotAction(
                kind="__invalid__",
                tool=name,
                arguments={},
                tool_call_id=tc_id,
                content=(f"INVALID TOOL CALL '{name}': your previous tool call was TRUNCATED (arguments cut off). "
                         f"Use edit_file with a SMALL old_str/new_str snippet instead of writing the whole file."),
            ))
            continue

        try:
            action = _tool_name_to_action(name, args, tool_call_id=tc_id)
            actions.append(action)
        except Exception as e:
            # A single malformed tool call (e.g. a truncated/streamed write_file missing
            # its path) must NOT abort the whole turn and discard the other valid actions.
            # Record it as a failed action carrying the error so the loop can feed the
            # message back to the model and let it retry, instead of silently halting.
            actions.append(PilotAction(
                kind="__invalid__",
                tool=name,
                arguments=args,
                tool_call_id=tc_id,
                content=(f"INVALID TOOL CALL '{name}': {e}. Re-issue the tool call with ALL required "
                         f"arguments (write_file needs both 'path' and 'content'; edit_file needs 'path', 'old_str', and 'new_str')."),
            ))

    return actions


def parse_pilot_turn(text: str) -> PilotTurn:
    """Lenient parse of a pilot envelope from model output. Accepts:
    - a clean JSON object {say, actions}
    - JSON wrapped in prose or ```json fences
    - bare prose with no JSON  => treated as say-only (no actions)
    """
    if text is None:
        raise PilotError("empty pilot output")
    raw = text.strip()
    if not raw:
        raise PilotError("empty pilot output")

    obj = _extract_json_object(raw)
    if obj is None:
        # No JSON at all -> the model just talked. Treat the whole thing as prose.
        return PilotTurn(say=raw, actions=[])

    if not isinstance(obj, dict):
        return PilotTurn(say=raw, actions=[])

    say = obj.get("say") or obj.get("message") or obj.get("text") or ""
    thinking = obj.get("thinking") or obj.get("reasoning") or obj.get("thought") or ""
    actions = _coerce_actions(obj.get("actions") or obj.get("tool_calls"))
    # If there's prose outside the JSON and no `say`, keep the prose.
    if not say:
        outside = _prose_outside_json(raw)
        say = outside or ""
    return PilotTurn(say=str(say).strip(), thinking=str(thinking).strip(), actions=actions)


def _extract_json_object(text: str):
    """Find the first balanced top-level {...} and json.loads it. Tolerates code
    fences and surrounding prose. Returns None if no parseable object."""
    # strip ```json ... ``` fences first
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = []
    if fence:
        candidates.append(fence.group(1))
    # also try the first balanced brace span
    span = _first_balanced_braces(text)
    if span:
        candidates.append(span)
    for c in candidates:
        try:
            return json.loads(c, strict=False)
        except Exception:
            continue
    return None


def _first_balanced_braces(text: str) -> Optional[str]:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _prose_outside_json(text: str) -> str:
    span = _first_balanced_braces(text)
    if not span:
        return text
    return text.replace(span, "").replace("```json", "").replace("```", "").strip()


PILOT_SYSTEM = """You are the pilot of a Puppetmaster-orchestrated coding harness.
You talk directly with the user like a senior engineer pairing with them.

You have direct access to a local CodeGraph-indexed workspace and can explore/edit it using these real actions:
- `read_file`: read a file's contents from the workspace. Requires `path`.
- `edit_file`: make a targeted edit to an existing file by replacing an exact substring. Requires `path`, `old_str`, and `new_str`. STRONGLY PREFERRED over write_file for editing existing files.
- `write_file`: write/create a file atomically. Requires `path` and `content`. Use ONLY to create brand-new files.
- `run_command`: run a terminal shell command. Requires `command`.
- `list_dir`: list the files and folders inside a directory. `path` is optional.
- `run_swarm`: dispatch a parallel agent swarm for complex/broad investigations. Requires `goal`.
- `run_implement`: dispatch an edit-capable Puppetmaster worker that edits the repo in an isolated worktree and produces a patch. Requires `goal`.
- `run_parallel`: dispatch multiple Puppetmaster workers concurrently. Requires `goals` array, optional `adapter`, optional `mode`.
- `route_task`: preview which model the router would pick + estimated cost for a given instruction without executing it. Requires `instruction`.
- `web_search`: search the internet and return top results. Requires `query`.
- `web_fetch`: read a web page's text contents. Requires `url`.
- `read_pdf`: extract plain text from a local PDF file or PDF URL. Requires `path` or `url`.
- `search_codegraph`: search the CodeGraph index for symbol usages, definitions, or context. Requires `query` and optional `kind`.
- `search_files`: plain-text/regex content search over the repository, complementary to symbol search. Requires `query`, optional `path`, and `max_results`.
- `query_wiki`: query the durable cross-session architecture and knowledge wiki. Requires `question`.
- `call_mcp`: call a connected MCP tool. Requires `tool` (the qualified server.tool name) and `arguments` (object). Connected MCP tools may be listed in a "Connected MCP tools" section appended below; use them when relevant.

You are not just an investigator -- you can GET WORK DONE. Use run_implement to make real code changes (it dispatches an edit-capable worker in an isolated worktree that produces a patch). Use run_parallel to fan out multiple implement/analysis workers at once for big multi-part work (audits + fixes + tests in parallel waves). Use run_swarm for read-only investigation, route_task to preview model/cost before a big dispatch. Prefer parallel waves for large work: decompose into independent goals and run_parallel them.

You have search_codegraph (semantic/graph search over THIS repo's code -- prefer it over grep/read_file for 'where is X / what calls Y / how does Z work') and query_wiki (durable cross-session knowledge base -- consult it for prior decisions, architecture, and context). Use search_codegraph to explore code structure before reading whole files. These are first-class: you know the codebase via CodeGraph and your durable memory via the Wiki.

NATIVE TOOL-CALLING (Primary Mode):
If native tool calling (function calling) is enabled, you MUST invoke functions/tools directly rather than writing JSON envelopes. Keep your user-facing message content to a brief, friendly sentence (pure prose) describing your action or findings. Never paste tool outputs, command outputs, or full file contents into your message content.

FALLBACK JSON ENVELOPE MODE (Non-native fallback):
If native tool-calling is NOT supported by the active driver/model, respond ONLY with a JSON object:

  {
    "thinking": "<optional private reasoning/scratchpad -- analysis, plan, what you are considering>",
    "say": "<prose for the user describing your plan and concise explanations>",
    "actions": [
      {"kind": "read_file", "path": "src/main.py"}
    ]
  }

Rules:
- Keep your prose explanation (message content or "say") extremely tight and concise (under 2 sentences). Let the tool chips show the work. Do NOT paste file contents, command output, tracebacks, or large code blocks back into prose -- reference them briefly instead. Never echo or quote tool-result messages.
- Prefer search_codegraph and query_wiki for code exploration and architectural knowledge.
- Prefer your direct tools (read_file, write_file, run_command, list_dir) for precise actions and testing.
- Use `run_swarm` when you need a team of workers to analyze a broad issue or scan the codebase.
- Always verify your work by running tests via `run_command` after editing.
- Be concise and concrete. Never invent file contents; read the files first.
"""


PLAN_SYSTEM_SUFFIX = """PLAN MODE: Do NOT call run_implement, run_parallel, write_file, edit_file, or run_command. Investigate read-only if needed (read_file, search_codegraph, query_wiki, list_dir, web_search), then output a clear, actionable, numbered implementation PLAN in markdown: goal restatement, the concrete steps (each with what/where/why), files likely touched, risks, and a suggested verification. End with a one-line summary. The user will review the plan before any execution."""


WORKER_SYSTEM = """You are an implementation worker. Be FAST and DECISIVE. Your job is to EDIT FILES to complete the task, not to investigate. Read ONLY the specific file(s) you must change (read_file once per file), then make the edit immediately with edit_file, then FINISH. To change an existing file, ALWAYS use edit_file with a small old_str/new_str snippet -- do NOT use write_file to rewrite an existing file (that wastes tokens and can truncate). Use write_file ONLY to create a brand-new file. Do NOT explore the wider codebase. Do NOT call search_codegraph (this workspace has no code index; it returns nothing and wastes time). Do NOT re-read a file you already read. Ideal small change = read target file once, edit the change, done. As soon as all required edits are made, STOP. Do not do extra investigation rounds.

You have direct access to the workspace and can explore/edit it using these real actions:
- `read_file`: read a file's contents from the workspace. Requires `path`.
- `edit_file`: make a targeted edit to an existing file by replacing an exact substring. Requires `path`, `old_str`, and `new_str`. STRONGLY PREFERRED over write_file for editing existing files.
- `write_file`: write/create a file atomically. Requires `path` and `content`. Use ONLY to create brand-new files.
- `run_command`: run a terminal shell command. Requires `command`.
- `list_dir`: list the files and folders inside a directory. `path` is optional.
- `route_task`: preview which model the router would pick + estimated cost for a given instruction without executing it. Requires `instruction`.
- `web_search`: search the internet and return top results. Requires `query`.
- `web_fetch`: read a web page's text contents. Requires `url`.
- `read_pdf`: extract plain text from a local PDF file or PDF URL. Requires `path` or `url`.
- `call_mcp`: call a connected MCP tool. Requires `tool` (the qualified server.tool name) and `arguments` (object). Connected MCP tools may be listed in a "Connected MCP tools" section appended below; use them when relevant.

NATIVE TOOL-CALLING (Primary Mode):
If native tool calling (function calling) is enabled, you MUST invoke functions/tools directly rather than writing JSON envelopes. Keep your user-facing message content to a brief, friendly sentence (pure prose) describing your action or findings. Never paste tool outputs, command outputs, or full file contents into your message content.

FALLBACK JSON ENVELOPE MODE (Non-native fallback):
If native tool-calling is NOT supported by the active driver/model, respond ONLY with a JSON object:

  {
    "thinking": "<optional private reasoning/scratchpad -- analysis, plan, what you are considering>",
    "say": "<prose for the user describing your plan and concise explanations>",
    "actions": [
      {"kind": "read_file", "path": "src/main.py"}
    ]
  }

Rules:
- Keep your prose explanation (message content or "say") extremely tight and concise (under 2 sentences). Let the tool chips show the work. Do NOT paste file contents, command output, tracebacks, or large code blocks back into prose -- reference them briefly instead. Never echo or quote tool-result messages.
- Always verify your work by running tests via `run_command` after editing.
- Be concise and concrete. Never invent file contents; read the files first.
"""

