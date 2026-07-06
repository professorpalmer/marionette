from __future__ import annotations

"""Tool-dispatch mixin: per-tool `_do_*` handlers used by the pilot loop.

Extracted mechanically from harness/conversation.py to begin decomposing the
ConversationalSession god-object. These methods operate purely through
`self` (config, allowed roots, etc.) provided by the concrete class -- the
mixin defines no state and no __init__.

Method Resolution Order keeps behavior identical: the pilot's dispatch
still calls `self._do_read_file(act)` etc., which now resolves to these
methods via inheritance.

`_strip_ansi` and `is_safe_path` also live here (they are only used by these
handlers); harness.conversation re-imports them so external callers keep
working.
"""

import os
import re
import subprocess
from typing import Any

from ._exec import _puppetmaster_cmd
from .internal_uri import InternalUriContext, InternalUriError, is_internal_uri, resolve_internal_uri
from .paths import path_within


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """Remove ANSI SGR color codes so CLI output reads cleanly as tool results."""
    return _ANSI_ESCAPE.sub("", text)


def is_safe_path(path: str, parent: str) -> bool:
    """True if ``path`` is inside ``parent`` (the workspace root itself counts as
    safe -- file tools legitimately operate on the root, e.g. list_dir). Shares
    the confinement primitive with worktrees._is_confined; see harness.paths."""
    return path_within(path, parent, allow_equal=True)


class ToolDispatchMixin:
    """Mixin holding per-tool `_do_*` handlers.

    The concrete class (ConversationalSession) supplies the state these
    methods read via `self` (self.config.repo, self._read_allowed_roots(),
    etc.). This mixin defines no __init__ and no instance state of its own.
    """

    def _internal_uri_context(self) -> InternalUriContext:
        return InternalUriContext(
            state_dir=getattr(self, "state_dir", None) or self.config.state_dir or "",
            repo=self.config.repo or None,
        )

    def _do_read_file(self, act: Any) -> tuple[bool, str, str]:
        if is_internal_uri(act.path):
            try:
                resource = resolve_internal_uri(
                    act.path,
                    self._internal_uri_context(),
                    start_line=getattr(act, "start_line", None),
                    limit=getattr(act, "limit", None),
                )
            except InternalUriError as exc:
                return False, "internal_uri_error", str(exc)
            if resource.is_directory:
                return False, "is_directory", (
                    f"Path is a directory: {act.path}. Use list_dir for internal URI directories."
                )
            content = resource.content
            if len(content) > 200 * 1024:
                content = content[:200 * 1024] + "\n\n... (internal URI content truncated to 200KB) ..."
            return True, "success", content

        if not self.config.repo:
            return False, "repo_not_open", "No workspace directory (config.repo) is open."
        target_path = act.path
        if not os.path.isabs(target_path):
            target_path = os.path.join(self.config.repo, target_path)
        if not any(is_safe_path(target_path, root) for root in self._read_allowed_roots()):
            return False, "path_traversal", f"Path traversal attempt rejected: {act.path}"
        try:
            if not os.path.exists(target_path):
                raise FileNotFoundError(f"File not found: {act.path}")
            if os.path.isdir(target_path):
                raise IsADirectoryError(f"Path is a directory: {act.path}")
            
            with open(target_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            
            total_lines = len(lines)
            raw_text = "".join(lines)
            
            start_line_raw = getattr(act, "start_line", None)
            limit_raw = getattr(act, "limit", None)
            
            start_line = None
            if start_line_raw is not None:
                try:
                    start_line = int(start_line_raw)
                except ValueError:
                    pass
            
            limit = None
            if limit_raw is not None:
                try:
                    limit = int(limit_raw)
                except ValueError:
                    pass
            
            if (len(raw_text) > 100000 or total_lines > 2000) and start_line is None and limit is None:
                head_lines = lines[:100]
                content = "".join(head_lines)
                content += f"\n\n[file is large ({total_lines} lines); re-read with start_line and limit to see specific sections]"
            else:
                if start_line is not None or limit is not None:
                    s_line = start_line if start_line is not None else 1
                    s_idx = max(0, s_line - 1)
                    if limit is not None:
                        e_idx = min(total_lines, s_idx + limit)
                    else:
                        e_idx = total_lines
                    
                    sliced_lines = lines[s_idx:e_idx]
                    content = f"[lines {s_idx + 1}-{e_idx} of {total_lines}]\n" + "".join(sliced_lines)
                else:
                    content = raw_text

            if len(content) > 200 * 1024:
                content = content[:200 * 1024] + "\n\n... (file truncated to 200KB) ..."

            from .hash_edit import annotate_read_content
            slice_start = None
            slice_end = None
            if start_line is not None or limit is not None:
                s_line = start_line if start_line is not None else 1
                s_idx = max(0, s_line - 1)
                if limit is not None:
                    e_idx = min(total_lines, s_idx + limit)
                else:
                    e_idx = total_lines
                slice_start = s_idx + 1
                slice_end = e_idx
            content = annotate_read_content(
                content,
                total_lines=total_lines,
                start_line=slice_start,
                end_line=slice_end,
            )
                
            return True, "success", content
        except Exception as e:
            return False, "exception", str(e)

    def _do_view_image(self, act: Any) -> tuple[bool, str, str]:
        if not self.config.repo:
            return False, "repo_not_open", "No workspace directory (config.repo) is open."
        target_path = act.path
        if not os.path.isabs(target_path):
            target_path = os.path.join(self.config.repo, target_path)
        if not is_safe_path(target_path, self.config.repo):
            return False, "path_traversal", f"Path traversal attempt rejected: {act.path}"
        try:
            if not os.path.exists(target_path):
                return False, "error", f"view_image: not an image file or not found: {act.path}"
            if os.path.isdir(target_path):
                return False, "error", f"view_image: not an image file or not found: {act.path}"

            ext = os.path.splitext(target_path)[1].lower()
            if ext not in (".png", ".jpg", ".jpeg", ".webp"):
                return False, "error", f"view_image: not an image file or not found: {act.path}"

            from .vision import transcribe_images
            results = transcribe_images([target_path])
            if not results:
                return False, "error", "view_image failed: no transcription returned"
            r = results[0]
            if r.error:
                return False, "error", f"view_image failed: {r.error}"
            return True, "success", r.text
        except Exception as e:
            return False, "exception", str(e)

    def _do_list_dir(self, act: Any) -> tuple[bool, str, Any]:
        uri_path = (act.path or "").strip()
        if is_internal_uri(uri_path):
            try:
                resource = resolve_internal_uri(uri_path, self._internal_uri_context())
            except InternalUriError as exc:
                return False, "internal_uri_error", str(exc)
            if not resource.is_directory:
                return False, "not_a_directory", f"Not a directory: {uri_path}"
            return True, "success", resource.content

        if not self.config.repo:
            return False, "repo_not_open", "No workspace directory (config.repo) is open."
        target_path = act.path
        if not target_path or not target_path.strip():
            target_path = self.config.repo
        else:
            if not os.path.isabs(target_path):
                target_path = os.path.join(self.config.repo, target_path)
        if not is_safe_path(target_path, self.config.repo):
            return False, "path_traversal", f"Path traversal attempt rejected: {act.path}"
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
            return True, "success", (len(entries), result_text)
        except Exception as e:
            return False, "exception", str(e)

    def _do_web_search(self, act: Any) -> tuple[bool, str, str]:
        from .web_tools import web_search
        try:
            result_text = web_search(act.query)
            return True, "success", result_text
        except Exception as e:
            return False, "exception", str(e)

    def _do_web_fetch(self, act: Any) -> tuple[bool, str, str]:
        from .web_tools import web_fetch
        try:
            result_text = web_fetch(act.url)
            return True, "success", result_text
        except Exception as e:
            return False, "exception", str(e)

    def _do_read_pdf(self, act: Any) -> tuple[bool, str, str]:
        from .web_tools import read_pdf
        target = act.path or act.url
        is_remote = target.startswith(("http://", "https://"))
        
        if not is_remote:
            if not self.config.repo:
                return False, "repo_not_open", "No workspace directory (config.repo) is open."
            target_path = act.path
            if not os.path.isabs(target_path):
                target_path = os.path.join(self.config.repo, target_path)
            if not is_safe_path(target_path, self.config.repo):
                return False, "path_traversal", f"Path traversal attempt rejected: {act.path}"
            target = target_path

        try:
            result_text = read_pdf(target)
            return True, "success", result_text
        except Exception as e:
            return False, "exception", str(e)

    def _do_search_codegraph(self, act: Any) -> tuple[bool, str, Any]:
        if not self.config.repo:
            return False, "repo_not_open", "No workspace directory (config.repo) is open."

        # Route through the Puppetmaster CLI passthrough (`python -m puppetmaster
        # codegraph ...`) rather than a bare `codegraph` binary. The bare binary
        # runs under whatever Node is on PATH, whose ABI usually differs from the
        # Node that compiled better-sqlite3 -- so it silently drops to the WASM
        # SQLite fallback (5-10x slower) and prints a fix-it banner that lands in
        # the model's tool output as noise. The passthrough runs under the
        # interpreter driving the backend and auto-rebuilds the native binding,
        # giving clean, fast results.
        kind = act.arguments.get("kind") or "search"
        subcommand = "context" if kind == "context" else "query"
        cmd = _puppetmaster_cmd("codegraph", subcommand, act.query)

        try:
            p = subprocess.run(
                cmd,
                cwd=self.config.repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=60,
            )
            output = _strip_ansi((p.stdout or "").strip())
            if p.returncode != 0:
                if "no module named" in output.lower() or p.returncode == 127:
                    output = "CodeGraph is unavailable: the Puppetmaster CLI is not importable in this environment."
                else:
                    output = f"CodeGraph failed with exit code {p.returncode}: {output}"
            else:
                output = output[:6000]

            return True, "success", (kind, output)
        except FileNotFoundError:
            return False, "filenotfound", "CodeGraph is unavailable: Python interpreter not found."
        except Exception as e:
            return False, "exception", str(e)

    def _do_search_files(self, act: Any) -> tuple[bool, str, Any]:
        if not self.config.repo:
            return False, "repo_not_open", "No workspace directory (config.repo) is open."

        query = act.query
        if not query:
            return False, "invalid_arguments", "search_files requires a non-empty 'query'"

        sub_path = act.arguments.get("path") or ""
        target_path = sub_path
        if not os.path.isabs(target_path):
            target_path = os.path.join(self.config.repo, target_path)
        if not is_safe_path(target_path, self.config.repo):
            return False, "path_traversal", f"Path traversal attempt rejected: {sub_path}"

        max_results = act.arguments.get("max_results")
        if max_results is None:
            max_results = 50
        else:
            try:
                max_results = int(max_results)
            except (ValueError, TypeError):
                max_results = 50

        # Try ripgrep first
        import shutil
        rg_path = shutil.which("rg")
        if rg_path:
            rg_arg_path = sub_path if sub_path else "."
            cmd = [rg_path, "--line-number", "--no-heading", "--color=never", "-e", query, rg_arg_path]
            try:
                p = subprocess.run(
                    cmd,
                    cwd=self.config.repo,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=20
                )
                output = p.stdout or ""
                if p.returncode > 1:
                    return False, "exception", f"ripgrep failed with code {p.returncode}: {output.strip()}"

                lines = [l for l in output.splitlines() if l.strip()]
                if os.sep == "\\":
                    # Normalize the path prefix (everything before the first
                    # ':') to forward slashes so results read identically on
                    # every platform. Relative paths carry no drive colon.
                    lines = [
                        l.split(":", 1)[0].replace("\\", "/") + ":" + l.split(":", 1)[1]
                        if ":" in l else l
                        for l in lines
                    ]
                truncated = len(lines) > max_results
                lines = lines[:max_results]
                result_text = "\n".join(lines)
                if truncated:
                    result_text += f"\n\n... (results truncated to {max_results} matches) ..."
                return True, "success", result_text
            except subprocess.TimeoutExpired:
                return False, "exception", "ripgrep timed out after 20 seconds"
            except Exception:
                pass

        # Fallback to pure-Python os.walk + re scan
        matches = []
        try:
            compiled_re = re.compile(query)
        except re.error as e:
            return False, "invalid_arguments", f"Invalid regex pattern: {e}"

        skip_dirs = {".git", "node_modules", "results", "build", "dist", "__pycache__"}
        
        for root, dirs, files in os.walk(target_path):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for file in files:
                file_path = os.path.join(root, file)
                try:
                    with open(file_path, "rb") as f:
                        chunk = f.read(8000)
                        if b"\x00" in chunk:
                            continue
                except Exception:
                    continue

                try:
                    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                        for line_num, line in enumerate(f, 1):
                            if compiled_re.search(line):
                                rel_path = os.path.relpath(file_path, self.config.repo).replace(os.sep, "/")
                                line_text = line.rstrip("\r\n")
                                matches.append(f"{rel_path}:{line_num}: {line_text}")
                                if len(matches) > max_results:
                                    break
                except Exception:
                    continue
            if len(matches) > max_results:
                break

        truncated = len(matches) > max_results
        matches = matches[:max_results]
        result_text = "\n".join(matches)
        if truncated:
            result_text += f"\n\n... (results truncated to {max_results} matches) ..."
        return True, "success", result_text

    def _do_lsp(self, act: Any) -> tuple[bool, str, str]:
        if not self.config.repo:
            return False, "repo_not_open", "No workspace directory (config.repo) is open."
        args = act.arguments or {}
        language = args.get("language") or "auto"
        mode = args.get("mode") or "diagnostics"
        symbol = (args.get("symbol") or "").strip()
        timeout_ms = args.get("timeout_ms")
        root_arg = args.get("root") or ""
        root_path = root_arg
        if not root_path:
            root_path = self.config.repo
        if not os.path.isabs(root_path):
            root_path = os.path.join(self.config.repo, root_path)
        if not is_safe_path(root_path, self.config.repo):
            return False, "path_traversal", f"Path traversal attempt rejected: {root_arg}"

        if mode == "references" and not symbol:
            return False, "invalid_arguments", "lsp references mode requires a 'symbol'"

        try:
            from .lsp_code_intelligence import discover_lsp_tools, get_lsp_report

            tools = discover_lsp_tools()
            text = get_lsp_report(
                language=language,
                mode=mode,
                root=root_path,
                timeout_ms=timeout_ms,
                tools=tools,
                symbol=symbol or None,
            )
            return True, "success", text
        except Exception as e:
            return False, "exception", str(e)

    def _do_search_state(self, act: Any) -> tuple[bool, str, str]:
        from .internal_uri import search_internal_uris

        args = act.arguments or {}
        query = (act.query or args.get("query") or "").strip()
        if not query:
            return False, "invalid_arguments", "search_state requires a 'query'"
        scheme = args.get("scheme") or None
        max_results = args.get("max_results", 50)
        try:
            text = search_internal_uris(
                query,
                self._internal_uri_context(),
                scheme=scheme,
                max_results=max_results,
            )
            return True, "success", text
        except Exception as e:
            return False, "exception", str(e)

    def _do_search_tools(self, act: Any) -> tuple[bool, str, str]:
        from .tool_discovery import ToolCatalog

        catalog: ToolCatalog = getattr(self, "_tool_catalog", None) or ToolCatalog()
        mcp = getattr(self, "_mcp", None)
        mcp_tools = mcp.discovered_tools() if mcp else None
        catalog.refresh(
            mcp_tools=mcp_tools,
            no_delegation=getattr(self.config, "no_delegation", False),
            browser_enabled=getattr(self.config, "browser_enabled", True),
        )
        args = act.arguments or {}
        query = (act.query or args.get("query") or "").strip()
        limit = args.get("limit", 10)
        activate = args.get("activate") or []
        if isinstance(activate, str):
            activate = [activate]
        try:
            text = catalog.format_search_response(query, limit=limit, activate=activate)
            self._tool_catalog = catalog
            return True, "success", text
        except Exception as e:
            return False, "exception", str(e)

    def _do_hash_edit(self, act: Any, *, write: bool = True) -> tuple[bool, str, str]:
        """Validate (and optionally apply) hash-anchored edits from act.arguments['ops']."""
        from .hash_edit import HashEditOp, apply_hash_edits, atomic_write_text, hash_edit_enabled

        if not hash_edit_enabled():
            return False, "disabled", "hash_edit is disabled (set HARNESS_HASH_EDIT=1 to enable)"
        if not self.config.repo:
            return False, "repo_not_open", "No workspace directory (config.repo) is open."
        target_path = act.path
        if not os.path.isabs(target_path):
            target_path = os.path.join(self.config.repo, target_path)
        if not is_safe_path(target_path, self.config.repo):
            return False, "path_traversal", f"Path traversal attempt rejected: {act.path}"

        if not os.path.exists(target_path):
            return False, "not_found", f"hash_edit: file not found: {act.path}"
        if os.path.isdir(target_path):
            return False, "is_directory", f"hash_edit: path is a directory: {act.path}"

        raw_ops = act.arguments.get("ops") if act.arguments else None
        if not isinstance(raw_ops, list) or not raw_ops:
            return False, "invalid_arguments", "hash_edit requires a non-empty 'ops' list"

        try:
            ops = [HashEditOp.from_dict(o) for o in raw_ops]
        except (ValueError, TypeError) as e:
            return False, "invalid_arguments", f"hash_edit: invalid op: {e}"

        try:
            with open(target_path, "r", encoding="utf-8", errors="replace", newline="") as f:
                original = f.read()
            new_text, result = apply_hash_edits(original, ops)
            if not result.ok:
                status = "stale_anchor" if result.stale_anchors else "validation_error"
                return False, status, f"hash_edit failed: {result.message}"
            if write:
                atomic_write_text(target_path, new_text)
            return True, "success", result.message
        except Exception as e:
            return False, "exception", str(e)
