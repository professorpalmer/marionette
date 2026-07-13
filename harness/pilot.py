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


VALID_ACTION_KINDS = {"run_swarm", "call_mcp", "read_file", "write_file", "edit_file", "hash_edit", "run_command", "list_dir", "web_search", "web_fetch", "read_pdf", "search_codegraph", "search_files", "search_state", "search_tools", "query_wiki", "run_implement", "run_parallel", "route_task", "view_image", "memory", "open_project",
                      "relocate_session", "session_bank",
                      "lsp",
                      "browser_navigate", "browser_snapshot", "browser_click", "browser_type",
                      "browser_scroll", "browser_back", "browser_get_text", "browser_screenshot"}


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
    # Browser tools: element ref (@e3), text to type, scroll direction.
    ref: str = ""
    text: str = ""
    direction: str = ""
    # run_implement / run_parallel: optional absolute path to a DIFFERENT git
    # repository to run the dispatch in (defaults to the open workspace when
    # empty). Threaded to the worker via a per-dispatch HarnessConfig copy so
    # the existing edit engines transparently target it.
    repo: str = ""
    start_line: Optional[int] = None
    limit: Optional[int] = None

    def validate(self) -> "PilotAction":
        if self.kind not in VALID_ACTION_KINDS:
            raise PilotError(f"unknown action kind: {self.kind!r}")
        if self.kind == "lsp":
            args = self.arguments or {}
            if not isinstance(args, dict):
                raise PilotError("lsp action arguments must be an object")
            language = args.get("language") or "auto"
            if language not in ("auto", "python", "typescript"):
                raise PilotError(f"lsp action language must be 'auto', 'python', or 'typescript' (got {language!r})")
            mode = args.get("mode") or "diagnostics"
            if mode not in ("status", "diagnostics"):
                raise PilotError("lsp action mode must be 'status' or 'diagnostics'")
            root = args.get("root")
            if root is not None and not str(root).strip():
                raise PilotError("lsp action root must be a non-empty string when provided")
            timeout_ms = args.get("timeout_ms")
            if timeout_ms is not None:
                try:
                    timeout_ms_int = int(timeout_ms)
                except (ValueError, TypeError):
                    raise PilotError("lsp action timeout_ms must be an integer (milliseconds)")
                if timeout_ms_int <= 0:
                    raise PilotError("lsp action timeout_ms must be > 0")
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
        if self.kind in ("read_file", "write_file", "view_image", "open_project") and not (self.path or "").strip():
            raise PilotError(f"{self.kind} action requires a 'path'")
        if self.kind == "relocate_session":
            target = (self.path or self.repo or (self.arguments or {}).get("workspace_root") or "").strip()
            if not target:
                raise PilotError("relocate_session requires workspace_root (or path)")
        if self.kind == "edit_file" and not (self.path or "").strip():
            raise PilotError("edit_file action requires a 'path'")
        if self.kind == "edit_file" and not self.old_str:
            raise PilotError("edit_file action requires 'old_str'")
        if self.kind == "hash_edit":
            if not (self.path or "").strip():
                raise PilotError("hash_edit action requires a 'path'")
            ops = self.arguments.get("ops")
            if not isinstance(ops, list) or not ops:
                raise PilotError("hash_edit action requires a non-empty 'ops' list")
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
        if self.kind == "search_state" and not (self.query or "").strip():
            raise PilotError("search_state action requires a 'query'")
        if self.kind == "search_tools" and not (self.query or "").strip() and not self.arguments.get("activate"):
            raise PilotError("search_tools action requires a 'query' and/or 'activate' list")
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
        # Envelope actions often put relocate's target on workspace_root /
        # repo rather than path; keep those aliases in arguments and promote
        # to path so action_start goal + dispatch share one resolved root.
        if kind == "relocate_session" and not str(path).strip():
            path = (
                a.get("workspace_root")
                or a.get("repo")
                or (arguments.get("workspace_root") if isinstance(arguments, dict) else None)
                or (arguments.get("repo") if isinstance(arguments, dict) else None)
                or ""
            )
        if kind == "relocate_session":
            if not isinstance(arguments, dict):
                arguments = {}
            else:
                arguments = dict(arguments)
            if path and not (arguments.get("workspace_root") or "").strip():
                arguments["workspace_root"] = str(path)
        actions.append(PilotAction(kind=str(kind), goal=str(goal), roles=roles,
                                   tool=str(tool), arguments=arguments,
                                   path=str(path), content=str(content), command=str(command),
                                   query=str(query), url=str(url),
                                   goals=goals, adapter=str(adapter), mode=str(mode),
                                   instruction=str(instruction)).validate())
    return actions


def build_tools_schema(
    mcp_tools: Optional[list] = None,
    no_delegation: bool = False,
    browser_enabled: bool = True,
    *,
    include_search_tools: bool = False,
    visible_names: Optional[set] = None,
) -> list:
    schema = []

    if include_search_tools:
        schema.append({
            "type": "function",
            "function": {
                "name": "search_tools",
                "description": (
                    "Search the catalog of available pilot and MCP tools by keyword. "
                    "Returns matching tool names/descriptions and whether each tool is hidden. "
                    "Pass activate with tool names or qualified MCP ids to enable hidden tools for later turns."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Keyword query over tool names and descriptions (optional when activating by name).",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max results to return (default 10, max 25).",
                        },
                        "activate": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Tool names or MCP qualified ids to activate for subsequent turns.",
                        },
                    },
                },
            },
        })

    # open_project
    schema.append({
        "type": "function",
        "function": {
            "name": "open_project",
            "description": "Open a local directory as a project/workspace so its files and graph become available. Use when the user says 'open <dir> as a project' or asks to work in a directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the directory to open as a project"}
                },
                "required": ["path"]
            }
        }
    })

    # relocate_session -- move the current (or named) chat into a project
    schema.append({
        "type": "function",
        "function": {
            "name": "relocate_session",
            "description": (
                "Move this conversation into a project/workspace without creating a new blank session. "
                "Use when the user says 'move this convo into that project' or similar. "
                "Updates the session's workspace_root, opens/records the project, and keeps the transcript."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "workspace_root": {
                        "type": "string",
                        "description": "Absolute path of the target project directory",
                    },
                    "path": {
                        "type": "string",
                        "description": "Alias for workspace_root",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Session id to move (defaults to the active session)",
                    },
                    "title": {
                        "type": "string",
                        "description": "Optional new title for the session",
                    },
                },
                "required": [],
            },
        },
    })

    # session_bank -- Hermes-style transcript bank browse/search/read
    schema.append({
        "type": "function",
        "function": {
            "name": "session_bank",
            "description": (
                "Browse or search prior chat sessions across all workspaces (transcript bank). "
                "With no session_id, lists recent sessions (optional query filter). "
                "With session_id, returns a compact transcript summary for that session."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Optional filter matching title, id, or workspace path",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "When set, read/summarize that session's transcript",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max sessions to list (default 20)",
                    },
                },
                "required": [],
            },
        },
    })

    # 1. read_file
    from .hash_edit import hash_edit_enabled as _hash_edit_on
    read_desc = (
        "Read a file's contents. For large files, use start_line and limit. "
        "Prefer search_codegraph/search_files to explore code structure."
    )
    if _hash_edit_on():
        read_desc += (
            " When hash_edit is enabled, output includes [@anchor ...] tags with "
            "stable content hashes for the file or range; use those anchors with hash_edit."
        )
    schema.append({
        "type": "function",
        "function": {
            "name": "read_file",
            "description": read_desc,
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative path to the file to read"},
                    "start_line": {"type": "integer", "description": "1-based starting line number to read"},
                    "limit": {"type": "integer", "description": "Maximum number of lines to read"}
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

    if _hash_edit_on():
        schema.append({
            "type": "function",
            "function": {
                "name": "hash_edit",
                "description": (
                    "Apply hash-anchored replace/insert/delete ops to an existing file. "
                    "Each op references anchor hashes from read_file [@anchor ...] tags; "
                    "stale anchors are rejected with no partial writes. Prefer over edit_file "
                    "when anchors are available."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File to edit"},
                        "ops": {
                            "type": "array",
                            "description": "Ordered list of replace/insert/delete operations",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "op": {
                                        "type": "string",
                                        "enum": ["replace", "insert", "delete"],
                                        "description": "Operation kind",
                                    },
                                    "anchor": {
                                        "type": "string",
                                        "description": "Content hash from read_file anchor tag",
                                    },
                                    "start_line": {"type": "integer", "description": "1-based start line (replace/delete)"},
                                    "end_line": {"type": "integer", "description": "1-based end line inclusive (replace/delete)"},
                                    "after_line": {"type": "integer", "description": "Insert after this line (0 = before first line)"},
                                    "text": {"type": "string", "description": "New text for replace/insert"},
                                },
                                "required": ["op"],
                            },
                        },
                    },
                    "required": ["path", "ops"],
                },
            },
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
                "description": (
                    "Dispatch a PARALLEL agent swarm for complex/broad investigations. Requires `goal`. "
                    "One worker runs PER role, each with its own lens and its own right-sized model -- so "
                    "for a broad ask (audit, 'review the platform', 'find ways to improve quality/robustness/scale', "
                    "'how does the whole system work') pass SEVERAL roles to fan out. Passing no roles runs a single "
                    "general explorer, which is only right for a narrow, single-facet question."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string", "description": "The specific objective or question for the swarm workers"},
                        "roles": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": ["explore", "pipeline-mapper", "decision-explainer", "conflict-auditor", "test-coverage-reviewer"],
                            },
                            "description": (
                                "Worker roles to fan out across (one worker each). Choose several for broad work: "
                                "explore = structural/architecture tour; pipeline-mapper = end-to-end data/control flow; "
                                "decision-explainer = design decisions & trade-offs; conflict-auditor = conflicts, dead code, "
                                "correctness/robustness risks; test-coverage-reviewer = test coverage gaps & quality. "
                                "For a full audit, pass all five. Omit only for a single narrow question."
                            ),
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

    # search_state
    if not no_delegation:
        schema.append({
            "type": "function",
            "function": {
                "name": "search_state",
                "description": (
                    "Search durable state (past jobs, artifacts, agent transcripts, merge conflicts, "
                    "spilled tool outputs) by keyword. Returns matching internal URIs (job://, "
                    "artifact://, agent://, conflict://, spill://) that can be opened with "
                    "read_file or list_dir."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Keyword to search durable state indexes"},
                        "scheme": {
                            "type": "string",
                            "enum": ["job", "artifact", "agent", "conflict", "spill"],
                            "description": "Optional URI scheme to restrict the search",
                        },
                        "max_results": {"type": "integer", "description": "Optional max results, default 50"},
                    },
                    "required": ["query"],
                },
            },
        })

    # lsp (status + diagnostics via local tools; small first-pass)
    if not no_delegation:
        schema.append({
            "type": "function",
            "function": {
                "name": "lsp",
                "description": (
                    "Fetch IDE-style status/diagnostics for Python/TypeScript by invoking locally "
                    "available tools (pyright for Python; tsc/tsserver/TS language servers for TS). "
                    "Should gracefully report 'no tools found' when the toolchain is absent."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "language": {
                            "type": "string",
                            "enum": ["auto", "python", "typescript"],
                            "description": "Target language/toolchain. Default: auto."
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["status", "diagnostics", "references"],
                            "description": "What to fetch. Default: diagnostics."
                        },
                        "symbol": {
                            "type": "string",
                            "description": "Symbol name to locate usages of (required when mode is references).",
                        },
                        "root": {
                            "type": "string",
                            "description": "Project root to run tools from. Default: workspace root."
                        },
                        "timeout_ms": {
                            "type": "integer",
                            "description": "Timeout for the underlying command (milliseconds)."
                        }
                    }
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
                "description": (
                    "dispatch an edit-capable worker that edits the repo in an "
                    "isolated git worktree and produces a patch. Requires `goal`. "
                    "Target must be a real git checkout (not Marionette Home / a "
                    "plain folder). For compare/list/report tasks use run_command "
                    "or run_swarm; pass repo=<absolute git path> when the open "
                    "workspace is wrong."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string", "description": "The coding objective / task description to implement"},
                        "adapter": {"type": "string", "description": "Optional edit engine. Default is 'agentic' -- Puppetmaster's standalone keys-only worker (routes directly through your provider API, no external CLI). 'native' forces Marionette's own richer pilot loop. 'cursor'/'codex'/'claude-code' use those external agent CLIs when installed."},
                        "mode": {"type": "string", "enum": ["implement", "analysis", "review"], "description": "Worker execution mode: 'implement' (expects a patch; default) or 'analysis'/'review' (read-only report; empty diff is success)."},
                        "repo": {"type": "string", "description": "Optional absolute path to a DIFFERENT git repository to run this implementation in (defaults to the open workspace). Use when the task edits a repo other than the current one. Must be a git work tree."}
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
                "description": (
                    "dispatch multiple workers concurrently. Requires `goals` "
                    "array. Same git-workspace rules as run_implement (not Home / "
                    "non-git); pass repo= when needed."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "goals": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Array of goals/objectives to run in parallel"
                        },
                        "adapter": {"type": "string", "description": "Optional edit engine (default 'agentic' -- standalone keys-only; 'native' for the richer pilot; 'cursor'/'codex'/'claude-code' for external CLIs when installed)"},
                        "mode": {"type": "string", "enum": ["implement", "analysis", "review"], "description": "Worker execution mode: 'implement' (can edit) or 'analysis'/'review' (read-only)"},
                        "repo": {"type": "string", "description": "Optional absolute path to a DIFFERENT git repository to run this implementation in (defaults to the open workspace). Use when the task edits a repo other than the current one. Must be a git work tree."}
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
            "description": "Queue or manage durable facts/preferences that persist across sessions (user preferences, environment details, stable conventions). On 'add', the fact is QUEUED for an end-of-turn Save/Skip card -- it is NOT persisted mid-turn; the user confirms after the answer. Autopilot ignores adds. Use list/update/remove for already-saved entries. Keep entries compact and high-signal. Do NOT save ephemeral task state or secrets.",
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

    # Native browser / computer-use tools (raw CDP over local Chrome).
    # Model workflow: browser_navigate -> browser_snapshot (to get @e refs) ->
    # browser_click / browser_type on those refs.
    if browser_enabled:
        schema.append({
            "type": "function",
            "function": {
                "name": "browser_navigate",
                "description": "Open a URL in a real Chrome browser (native, not via MCP). Navigates and returns the page title/url. After navigating, call browser_snapshot to get element refs before interacting.",
                "parameters": {
                    "type": "object",
                    "properties": {"url": {"type": "string", "description": "The URL to open (http/https)."}},
                    "required": ["url"]
                }
            }
        })
        schema.append({
            "type": "function",
            "function": {
                "name": "browser_snapshot",
                "description": "Return a compact accessibility-tree snapshot of the current page, assigning stable ref ids (@e1, @e2, ...) to interactable elements (links, buttons, inputs) with their role and name. ALWAYS call this before browser_click/browser_type so you have current refs.",
                "parameters": {"type": "object", "properties": {}}
            }
        })
        schema.append({
            "type": "function",
            "function": {
                "name": "browser_click",
                "description": "Click the element identified by a ref (e.g. @e3) from the latest browser_snapshot.",
                "parameters": {
                    "type": "object",
                    "properties": {"ref": {"type": "string", "description": "Element ref like @e3 from browser_snapshot."}},
                    "required": ["ref"]
                }
            }
        })
        schema.append({
            "type": "function",
            "function": {
                "name": "browser_type",
                "description": "Focus the element for a ref (e.g. @e5) and type text into it. Get the ref from browser_snapshot first.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "ref": {"type": "string", "description": "Element ref like @e5 from browser_snapshot."},
                        "text": {"type": "string", "description": "The text to type."}
                    },
                    "required": ["ref", "text"]
                }
            }
        })
        schema.append({
            "type": "function",
            "function": {
                "name": "browser_scroll",
                "description": "Scroll the current page up or down.",
                "parameters": {
                    "type": "object",
                    "properties": {"direction": {"type": "string", "enum": ["up", "down"], "description": "Scroll direction."}}
                }
            }
        })
        schema.append({
            "type": "function",
            "function": {
                "name": "browser_back",
                "description": "Navigate back to the previous page in browser history.",
                "parameters": {"type": "object", "properties": {}}
            }
        })
        schema.append({
            "type": "function",
            "function": {
                "name": "browser_get_text",
                "description": "Return the main readable text of the current page (document body innerText, truncated).",
                "parameters": {"type": "object", "properties": {}}
            }
        })
        schema.append({
            "type": "function",
            "function": {
                "name": "browser_screenshot",
                "description": "Capture a PNG screenshot of the current page and save it; returns the file path (use view_image to look at it).",
                "parameters": {"type": "object", "properties": {}}
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

    if visible_names is not None:
        schema = [
            item for item in schema
            if (item.get("function") or {}).get("name") in visible_names
        ]

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
        if kind == "relocate_session" and not path:
            path = (args.get("workspace_root") or args.get("repo") or "")
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
        start_line = args.get("start_line")
        if start_line is not None:
            try:
                start_line = int(start_line)
            except ValueError:
                pass
        limit = args.get("limit")
        if limit is not None:
            try:
                limit = int(limit)
            except ValueError:
                pass

        # Optional target-repo for run_implement / run_parallel dispatches --
        # points the worker at a DIFFERENT git repo than the open workspace.
        repo_arg = ""
        if kind in ("run_implement", "run_parallel"):
            repo_arg = (args.get("repo") or args.get("target_dir") or "").strip()

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
            start_line=start_line,
            limit=limit,
            # Browser tool args (url reused from above).
            ref=(args.get("ref") or "").strip(),
            text=(args.get("text") or args.get("value") or "").strip() if kind == "browser_type" else "",
            direction=(args.get("direction") or "").strip(),
            repo=repo_arg,
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


class StreamingSayExtractor:
    """Incrementally extract the human-facing `say` string from a streaming
    pilot JSON envelope ({"say": "...", "actions": [...]}) so prose renders
    token-by-token in real time instead of dumping after the full parse.

    feed(delta) returns the NEW clean prose characters for that delta (empty
    when the delta is JSON scaffolding / the actions array / etc.). JSON string
    escapes (newline, tab, quote, backslash, unicode) are decoded. If the
    stream is NOT a JSON envelope (the model just talked), bare-prose mode
    streams everything verbatim.

    Character state machine over the raw stream:
      START   -> first non-space decides json vs bare
      SEEK    -> scanning for a "say"/"message"/"text" key + ':' + opening quote
      IN_SAY  -> inside the say value; emit decoded chars until closing quote
      DONE    -> say value closed; emit nothing more
      BARE    -> not a JSON envelope; emit everything verbatim
    """

    _KEYS = ("say", "message", "text")

    def __init__(self):
        self._raw = ""
        self._state = "START"
        self._escape = False
        self._uni = None          # str of collected hex digits, or None
        self._bare_emitted = 0
        self._key_search_from = 0  # index to resume key scanning from

    def feed(self, delta: str) -> str:
        if not delta:
            return ""
        self._raw += delta

        if self._state == "START":
            stripped = self._raw.lstrip()
            if not stripped:
                return ""
            self._state = "SEEK" if stripped[0] == "{" else "BARE"

        if self._state == "BARE":
            out = self._raw[self._bare_emitted:]
            self._bare_emitted = len(self._raw)
            return out

        if self._state == "DONE":
            return ""

        out = []
        # Process only newly-arrived characters. We track absolute position via
        # _key_search_from for the SEEK phase, and consume char-by-char in IN_SAY.
        i = len(self._raw) - len(delta)
        n = len(self._raw)
        while i < n:
            ch = self._raw[i]
            if self._state == "SEEK":
                # Look for a key followed by ':' then '"'. Cheap check: whenever
                # we hit a '"', test whether the run ending here matches a key
                # and is followed (after optional ws + ':') by an opening quote.
                if ch == '"':
                    # Find the matching opening quote of this key token.
                    # Scan backward to the previous unescaped '"'.
                    seg = self._raw[: i + 1]
                    m = _re_say_key_open(seg)
                    if m:
                        # The opening quote of the value is the LAST char matched.
                        self._state = "IN_SAY"
                i += 1
                continue
            if self._state == "IN_SAY":
                if self._escape:
                    self._escape = False
                    if ch == "u":
                        self._uni = ""  # begin collecting 4 hex digits
                    else:
                        out.append({"n": "\n", "t": "\t", "r": "\r",
                                    '"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f"}.get(ch, ch))
                    i += 1
                    continue
                if self._uni is not None:
                    self._uni += ch
                    if len(self._uni) == 4:
                        try:
                            out.append(chr(int(self._uni, 16)))
                        except Exception:
                            pass
                        self._uni = None
                    i += 1
                    continue
                if ch == "\\":
                    self._escape = True
                    i += 1
                    continue
                if ch == '"':
                    self._state = "DONE"
                    i += 1
                    break
                out.append(ch)
                i += 1
                continue
            break

        return "".join(out)


def _re_say_key_open(seg: str) -> bool:
    """True if `seg` ends with a `"say"`/`"message"`/`"text"` key, optional
    whitespace, a ':', optional whitespace, and the opening '"' of the value."""
    import re
    return bool(re.search(r'"(?:say|message|text)"\s*:\s*"$', seg))


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
- `read_file`: read a file's contents from the workspace. Requires `path`, with optional `start_line` and `limit` for large files.
- `edit_file`: make a targeted edit to an existing file by replacing an exact substring. Requires `path`, `old_str`, and `new_str`. STRONGLY PREFERRED over write_file for editing existing files.
- `write_file`: write/create a file atomically. Requires `path` and `content`. Use ONLY to create brand-new files.
- `run_command`: run a terminal shell command. Requires `command`.
- `list_dir`: list the files and folders inside a directory. `path` is optional.
- `run_swarm`: dispatch a parallel agent swarm for complex/broad investigations. Requires `goal`. One worker runs per role -- for a broad ask (audit, "review the platform", "find ways to improve quality/robustness/scale") pass SEVERAL `roles` (explore, pipeline-mapper, decision-explainer, conflict-auditor, test-coverage-reviewer) so it fans out into real parallel coverage; pass all five for a full audit. Omit roles only for a single narrow question.
- `run_implement`: dispatch an edit-capable worker that edits the repo in an isolated worktree and produces a reviewable patch. Requires `goal`. Default engine is standalone `agentic` (routes directly through your provider keys, no external CLI); pass `adapter` only to force a specific engine. Optional `mode` (`implement` default, or `analysis`/`review` for read-only reports).
- `run_parallel`: dispatch multiple Puppetmaster workers concurrently. Requires `goals` array, optional `adapter`, optional `mode`.
- `route_task`: preview which model the router would pick + estimated cost for a given instruction without executing it. Requires `instruction`.
- `web_search`: search the internet and return top results. Requires `query`.
- `web_fetch`: read a web page's text contents. Requires `url`.
- `read_pdf`: extract plain text from a local PDF file or PDF URL. Requires `path` or `url`.
- `search_codegraph`: search the CodeGraph index for symbol usages, definitions, or context. Requires `query` and optional `kind`.
- `search_files`: plain-text/regex content search over the repository, complementary to symbol search. Requires `query`, optional `path`, and `max_results`.
- `lsp`: fetch IDE-style status/diagnostics for Python/TypeScript by invoking locally available tools (pyright/tsc/tsserver). Requires optional `language` ('python'/'typescript'/'auto') and `mode` ('status'/'diagnostics').
- `search_tools`: search the catalog of available pilot and MCP tools; use `activate` to enable hidden tools for later turns.
- `query_wiki`: query the durable cross-session architecture and knowledge wiki. Requires `question`.
- `open_project`: open a local directory as the active project/workspace. Requires `path`.
- `relocate_session`: move the current (or named) conversation into a project without starting a blank session. Requires `workspace_root` (or `path`); optional `session_id`, `title`.
- `session_bank`: list/search prior sessions across workspaces, or read a transcript summary by `session_id`.
- `call_mcp`: call a connected MCP tool. Requires `tool` (the qualified server.tool name) and `arguments` (object). Connected MCP tools may be listed in a "Connected MCP tools" section appended below; use them when relevant.

MATCH EFFORT TO THE REQUEST (read this first):
Not every message is a task. Greetings ("hi", "hello", "hey"), thanks, small
talk, and simple factual questions get a SHORT conversational reply with ZERO
tool calls. Do NOT investigate, run tests, read files, or call CodeGraph for a
greeting or a question you can answer directly -- that is a defect, not
diligence. Only investigate when the user actually asks you to find, change,
explain, or build something in the code. When in doubt on a trivial message,
just answer in one friendly sentence and stop.

ARCHITECTURE CONTRACT (mandatory, not optional -- applies ONLY once you are
actually doing code work, never to greetings or small talk):
1. SWARM FIRST for broad work -- mandatory. Broad tasks (audit, review the codebase/platform, "look through the codebase", "find all X", "map the structure", improve quality/robustness, refactor plans, sweeps) MUST open with run_swarm using MULTIPLE roles (explore, pipeline-mapper, decision-explainer, conflict-auditor, test-coverage-reviewer) and auto-routed models. Do NOT inline list_dir/search_files/grep/read sweeps on a broad ask -- the harness blocks that until you delegate. Every swarm writes durable artifacts; artifact recall on later turns is zero-token, so delegation compounds and future queries get cheaper. After a swarm returns, use native exploration ONLY to validate concrete findings (e.g. read_file of a path the swarm cited, or search_codegraph on a named symbol). If swarm findings are empty, vague, verification-only, or clearly insufficient for the user's ask, you MUST re-dispatch a narrowed run_swarm (or run_parallel analysis roles) with a sharper objective -- NEVER open a broad inline exploration campaign (list_dir/search_files/grep/read sweeps) as a substitute. Swarms are the product; thin results mean sharpen and re-dispatch, not grind yourself.
2. CodeGraph FIRST for narrow symbol questions. For targeted lookups -- "where is X", "what calls/defines/implements Y", "how does Z work" -- your FIRST action MUST be search_codegraph (kind="search" for symbols/usages, kind="context" for task-enclosing structure/affected nodes). This repo is always indexed (it auto-indexes on open and refreshes when stale). Do NOT open a narrow symbol question with blind grep/list_dir. Sequence on narrow work: query the graph, THEN search_files/grep only for plain-text matches CodeGraph cannot resolve, THEN read only the specific lines the graph pointed you to (read_file supports start_line + limit). Dumping whole files to "familiarize yourself" is a defect, not a strategy.
3. Delegate real work through Puppetmaster. For multi-file edits, refactors, migrations, audits, "find all X", or any non-trivial implementation, you MUST dispatch a Puppetmaster worker (run_implement for an isolated-worktree patch, run_parallel for concurrent waves, run_swarm for read-only investigation) rather than grinding through the edits yourself inline. Inline edit_file/write_file is for single-file, surgical changes only. route_task previews model/cost before a big dispatch.

NEVER shell the Puppetmaster CLI via run_command (`python -m puppetmaster`, `puppetmaster status`, etc.). Use native verbs (run_swarm, run_implement, run_parallel, route_task) and read prior action_result / swarm_result records in history for status; use search_state when you need to look up durable job/artifact state.

You are not just an investigator -- you can GET WORK DONE. Prefer fan-out:
- `run_swarm` with MULTIPLE roles for any investigation that spans more than one concern (default to several roles; omit roles only for a single narrow question).
- `run_parallel` with DISJOINT file-scoped goals whenever a build/fix has independent slices (backend vs install scripts vs tests vs docs, etc.). Parallel non-colliding workers are our advantage -- gas them whenever file sets do not overlap. The router then picks cheaper models per slice.
- `run_implement` ONLY for one tightly-coupled change that must land as a single coherent PATCH (shared types, interlocking modules, one atomic feature). Do NOT use a single implement worker for work that cleanly splits.
Use route_task to preview model/cost before a big dispatch.

DISPATCH IS A PAUSE-POINT (mandatory):
When you dispatch background work (run_implement, run_parallel, or a backgrounded run_swarm), that dispatch is a PAUSE-POINT, not a completion. Emit AT MOST ONE background dispatch verb per turn (never two run_implement calls for the same objective). Do not treat the job as done at dispatch time. The worker finishes later and its result arrives as a "[swarm result ...]" record followed by a "[background job ... finished]" continuation. When you see that, you MUST first report the outcome to the user in plain language -- what the worker did, whether it passed or applied, and the key findings -- and THEN take the next step yourself. For a finished read-only analysis swarm whose findings are shallow or empty: diagnose why (too broad, wrong roles, missing repo focus) and re-dispatch a narrowed run_swarm / run_parallel analysis wave -- do NOT "validate with native tools" or grind an inline exploration campaign yourself. For implement/patch jobs: validate, run tests, fix, apply, or run a narrowed follow-up. It is not the user's job to prompt you to interpret the result or continue; do it on your own.

NO DUPLICATE IMPLEMENT SWARMS (mandatory): One objective gets ONE in-flight implement/parallel worker. Do NOT emit two run_implement tool calls in the same turn for the same work, do NOT re-dispatch the same change while its worker is still running, and do NOT fan out multiple workers that edit the SAME files -- the first patch applies and every overlapping one then fails with "PATCH DID NOT APPLY" because the files already moved. Decompose parallel waves into DISJOINT file sets. When a "[background job ... finished]" continuation arrives you are already being resumed automatically to handle it -- assess it, do not fire a redundant copy.

ADAPTERS FOLLOW THE LIVE PLATFORM LOCK (mandatory): Each turn's system prompt lists ACTIVE IMPLEMENT PLATFORMS. Omit `adapter` on run_implement/run_parallel unless you need a listed platform. Never request a disabled adapter (especially cursor when it is off) -- the harness remaps it, but requesting it wastes a turn.

WHEN A PATCH DID NOT APPLY: If a result says "PATCH DID NOT APPLY", the stale diff is dead -- never re-apply or re-dispatch the identical change. The files moved (an overlapping edit landed, or the base shifted). Re-derive the change against the CURRENT file contents: read the target file(s) as they are now, confirm whether the intended change is already present (if so, report it as done), and only if it is genuinely missing dispatch a fresh run_implement scoped to the current state.

TWO FRONTENDS -- PICK THE RIGHT ONE (mandatory): This repo ships a desktop app whose UI is the React/TypeScript app under `webapp/src/` (entry `webapp/src/components/Conversation.tsx`). There is ALSO a legacy, browser-served stdlib GUI under `harness/web/` (`app.js`, `app.css`, `index.html`) that the desktop app does NOT use. For ANY UI, chat, streaming, or composer change to the shipping app, target `webapp/src/` -- NOT `harness/web/app.js`. Only touch `harness/web/` when the task explicitly names the legacy browser GUI. Verify which file actually implements a feature with CodeGraph before scoping an implement worker, so a UI task never lands on the dead-end frontend.

LIVE SELF-EDITING (how your changes take effect): Marionette ALWAYS runs from the source checkout -- the backend is `python -m harness.cli` from the repo's venv, so your edits to this repo ARE the running code. Edits to backend Python (`harness/**`) require restarting the backend to load (Settings > Advanced > "Restart backend", or POST `/api/restart`); the conversation auto-resumes across the restart, so an in-flight turn is not lost. Never claim a fundamental/backend change is "live" without a restart -- state plainly that a restart is required and, when appropriate, trigger it. Edits to the React UI (`webapp/src/**`) land on the next renderer build (`npm run build` + reload); enable "Live UI (Vite HMR)" (Settings > Advanced) to serve the UI from a Vite dev server instead, so UI edits hot-reload INSTANTLY with no rebuild.

EXECUTE, DON'T DICTATE (mandatory):
You have a REAL terminal via `run_command` that runs in the user's workspace
with their full login-shell environment -- their PATH, ssh-agent, and SSH config
host aliases are all live. That means `ssh`, `scp`, `git`, `gh`, build tools,
test runners, deploy scripts, and any CLI on their machine WORK when you call
run_command. When the user asks you to "check it", "is it working", "run it",
"deploy it", "ssh in and validate", or anything you can verify or do from a
shell -- CALL run_command AND DO IT YOURSELF. Read the actual output and report
the real result. Do NOT hand the user a numbered list of commands to type by
hand and ask them to paste the output back; that is a defect. Typing the
commands for them, running them, and interpreting the results IS the job.
Only fall back to giving manual instructions when a command genuinely cannot run
from this workspace (e.g. it requires credentials or a network path you have
verified you do not have) -- and say specifically what you tried and why it
failed before doing so. When in doubt, run it and find out.

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
- Use `run_swarm` when you need a team of workers to analyze a broad issue or scan the codebase -- and give it a TEAM: pass multiple `roles` (up to all five) so workers fan out across architecture, flow, design decisions, conflicts, and test coverage in parallel. A single-worker swarm is only for a narrow, single-facet question.
- Always verify your work by running tests via `run_command` after editing.
- Be concise and concrete. Never invent file contents; read the files first.
"""


PLAN_SYSTEM_SUFFIX = """PLAN MODE: Do NOT call run_implement, run_parallel, write_file, edit_file, or run_command. Investigate read-only if needed (read_file, search_codegraph, query_wiki, list_dir, web_search), then output a clear, actionable, numbered implementation PLAN in markdown: goal restatement, the concrete steps (each with what/where/why), files likely touched, risks, and a suggested verification. End with a one-line summary. The user will review the plan before any execution."""


WORKER_SYSTEM = """You are an implementation worker. Be FAST and DECISIVE. Your job is to EDIT FILES to complete the task, not to investigate. Read ONLY the specific file(s) you must change (read_file once per file), then make the edit immediately with edit_file, then FINISH. To change an existing file, ALWAYS use edit_file with a small old_str/new_str snippet -- do NOT use write_file to rewrite an existing file (that wastes tokens and can truncate). Use write_file ONLY to create a brand-new file. Do NOT explore the wider codebase. Do NOT call search_codegraph (this workspace has no code index; it returns nothing and wastes time). Do NOT re-read a file you already read. Ideal small change = read target file once, edit the change, done. As soon as all required edits are made, STOP. Do not do extra investigation rounds.

You have direct access to the workspace and can explore/edit it using these real actions:
- `read_file`: read a file's contents from the workspace. Requires `path`, with optional `start_line` and `limit` for large files.
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

