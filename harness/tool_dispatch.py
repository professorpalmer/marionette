from __future__ import annotations

"""Tool-dispatch mixin: per-tool `_do_*` handlers used by the pilot loop.

Extracted mechanically from harness/conversation.py to begin decomposing the
ConversationalSession god-object. These methods operate purely through
`self` (config, allowed roots, etc.) provided by the concrete class -- the
mixin defines no state and no __init__.

Method Resolution Order keeps behavior identical: the pilot's dispatch
still calls `self._do_read_file(act)` etc., which now resolves to these
methods via inheritance.

`_strip_ansi` lives here; ``is_safe_path`` is re-exported from harness.paths
(single source of truth). harness.conversation re-imports both so external
callers keep working.
"""

import hashlib
import os
import re
import subprocess
import tempfile
from typing import Any

from ._exec import _puppetmaster_cmd
from .internal_uri import InternalUriContext, InternalUriError, is_internal_uri, resolve_internal_uri
from .paths import is_safe_path
from .pilot import PilotAction

# Re-export for callers that historically imported from tool_dispatch /
# conversation (tests, send_loop). Definition lives in harness.paths.
__all__ = ["ToolDispatchMixin", "_ANSI_ESCAPE", "_strip_ansi", "is_safe_path"]


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")

# Inline cap for synchronous run_command output; overflow spills to the
# results registry rather than being discarded.
_FOREGROUND_OUTPUT_CAP = 50 * 1024

# Default read_file window for oversized files (Hermes/Cursor daily-driver parity).
# Files above these triggers return the head window with an explicit truncation
# notice when content remains — never a silent truncate.
_READ_FILE_LARGE_BYTE_TRIGGER = 100_000
_READ_FILE_LARGE_LINE_TRIGGER = 2000
_READ_FILE_DEFAULT_WINDOW_LINES = 2000
_READ_FILE_BYTE_CEILING = 200 * 1024


def _strip_ansi(text: str) -> str:
    """Remove ANSI SGR color codes so CLI output reads cleanly as tool results."""
    return _ANSI_ESCAPE.sub("", text)


# Directories that never carry searchable source in the Python fallback.
_SEARCH_SKIP_DIRS = frozenset({
    ".git", "node_modules", "results", "build", "dist", "__pycache__",
})
# Zero-match probes only need to prove existence, not enumerate everything.
_PROBE_MATCH_CAP = 50


def _slice_header(start_line: int, end_line: int, total_lines: int) -> str:
    """Header line for a ranged read, naming the next offset when more remains.

    The continuation lives in the header rather than a footer because the
    header is stripped before hash-anchor computation -- a footer would be
    hashed as if it were file content and invalidate every anchor.
    """
    header = f"[lines {start_line}-{end_line} of {total_lines}"
    if end_line < total_lines:
        header += f"; next start_line={end_line + 1}"
    return header + "]\n"


def _result_limit(raw: Any, default: int = 50) -> int:
    try:
        return int(raw) if raw is not None else default
    except (ValueError, TypeError):
        return default


def _normalize_result_path(line: str) -> str:
    """Render the path prefix with forward slashes so results read the same everywhere."""
    if os.sep != "\\" or ":" not in line:
        return line
    prefix, rest = line.split(":", 1)
    return prefix.replace("\\", "/") + ":" + rest


def _format_match_lines(lines: list[str], max_results: int) -> str:
    truncated = len(lines) > max_results
    text = "\n".join(lines[:max_results])
    if truncated:
        text += f"\n\n... (results truncated to {max_results} matches) ..."
    return text


def _walk_searchable_files(root: str):
    """Yield text-file paths under ``root``, skipping vendor dirs and binaries."""
    if os.path.isfile(root):
        try:
            with open(root, "rb") as f:
                if b"\x00" not in f.read(8000):
                    yield root
        except Exception:
            pass
        return
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SEARCH_SKIP_DIRS]
        for name in files:
            file_path = os.path.join(current, name)
            try:
                with open(file_path, "rb") as f:
                    if b"\x00" in f.read(8000):
                        continue
            except Exception:
                continue
            yield file_path


def _file_match_lines(
    compiled: "re.Pattern[str]",
    file_path: str,
    rel_path: str,
    multiline: bool,
) -> list[str]:
    """Return ``path:line: text`` rows for every match in one file."""
    rows: list[str] = []
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            if not multiline:
                for line_num, line in enumerate(f, 1):
                    if compiled.search(line):
                        line_text = line.rstrip("\r\n")
                        rows.append(f"{rel_path}:{line_num}: {line_text}")
                return rows
            text = f.read()
    except Exception:
        return []

    for match in compiled.finditer(text):
        line_num = text.count("\n", 0, match.start()) + 1
        first_line = match.group(0).splitlines()[0] if match.group(0) else ""
        rows.append(f"{rel_path}:{line_num}: {first_line}")
    return rows


def _sum_ripgrep_counts(output: str) -> int:
    """Sum the counts in ``--count-matches`` output (``path:count`` per line)."""
    total = 0
    for line in output.splitlines():
        _path, _sep, count = line.rpartition(":")
        if count.strip().isdigit():
            total += int(count.strip())
    return total


def _command_failure_hint(command: str, exit_code: int, output: str) -> str:
    """One-line recovery hint for a genuinely failed command ("" when none)."""
    try:
        from .command_hints import command_failure_hint

        return command_failure_hint(command, int(exit_code), output) or ""
    except Exception:
        return ""


def _recoverable_command_output(session: Any, output: str) -> tuple[str, dict]:
    """Cap foreground output inline while keeping the overflow recoverable.

    Returns ``(inline_output, recovery_fields)``. Beyond the inline cap the
    full (redacted) output is spilled through the same registry background jobs
    use, so the tail the pilot needs is a read_file away instead of being
    dropped on the floor. Takes ``session`` explicitly so duck-typed dispatch
    hosts without the spill mixin still work.
    """
    text = output if isinstance(output, str) else str(output or "")
    if len(text) <= _FOREGROUND_OUTPUT_CAP:
        return text, {}

    from .command_jobs import spill_oversized_command_output

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    meta = spill_oversized_command_output(session, f"cmd-{digest}-stdout", text)
    spill_uri = meta.get("spill_uri") or ""
    recovery = {
        "output_chars": meta.get("output_chars", len(text)),
        "output_spilled": bool(spill_uri or meta.get("spill_path")),
    }
    capped = text[:_FOREGROUND_OUTPUT_CAP]
    if spill_uri:
        recovery["spill_uri"] = spill_uri
        capped += (
            f"\n\n... (output truncated to 50KB; full {len(text):,} chars saved "
            f"to {spill_uri} — read_file works on that URI) ..."
        )
    else:
        capped += "\n\n... (output truncated to 50KB) ..."
    return capped, recovery


class ToolDispatchMixin:
    """Mixin holding per-tool `_do_*` handlers.

    The concrete class (ConversationalSession) supplies the state these
    methods read via `self` (self.config.repo, self._read_allowed_roots(),
    etc.). This mixin defines no __init__ and no instance state of its own.

    Handlers take ``PilotAction`` (attribute access: act.path, act.query, …).
    Duck-typed carriers with the same attributes remain runtime-compatible for
    gradual migration; wire JSON / tool schemas are unchanged.
    """

    def _internal_uri_context(self) -> InternalUriContext:
        live = []
        fn = getattr(self, "live_local_jobs", None)
        if callable(fn):
            try:
                live = list(fn() or [])
            except Exception:
                live = []
        return InternalUriContext(
            state_dir=getattr(self, "state_dir", None) or self.config.state_dir or "",
            repo=self.config.repo or None,
            session_id=getattr(self, "harness_session_id", None) or None,
            live_jobs=live,
        )

    def _do_read_file(self, act: PilotAction) -> tuple[bool, str, str]:
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
                listing = resource.content or "(empty directory)"
                return True, "success", (
                    "(path is a directory — listing contents; use list_dir next time)\n"
                    + listing
                )
            content = resource.content
            if len(content) > _READ_FILE_BYTE_CEILING:
                content = (
                    content[:_READ_FILE_BYTE_CEILING]
                    + "\n\n... (internal URI content truncated to 200KB) ..."
                )
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
                # Auto-redirect: pilots often read_file a directory; return a
                # listing instead of a red IsADirectoryError so the turn is useful.
                ok, status, val = self._do_list_dir(act)
                if not ok:
                    return False, status, val if isinstance(val, str) else str(val)
                result_text = val[1] if isinstance(val, tuple) else str(val)
                return True, "success", (
                    "(path is a directory — listing contents; use list_dir next time)\n"
                    + result_text
                )

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
            
            oversized = (
                len(raw_text) > _READ_FILE_LARGE_BYTE_TRIGGER
                or total_lines > _READ_FILE_LARGE_LINE_TRIGGER
            )
            if oversized and start_line is None and limit is None:
                head_lines = lines[:_READ_FILE_DEFAULT_WINDOW_LINES]
                shown = len(head_lines)
                content = "".join(head_lines)
                content += (
                    f"\n\n[file is large ({total_lines} lines); "
                    "re-read with start_line and limit to see specific sections]"
                )
                if shown < total_lines:
                    content += (
                        f"\n[truncated after line {shown}; "
                        f"continue with start_line={shown + 1}]"
                    )
            else:
                if start_line is not None or limit is not None:
                    s_line = start_line if start_line is not None else 1
                    s_idx = max(0, s_line - 1)
                    if limit is not None:
                        e_idx = min(total_lines, s_idx + limit)
                    else:
                        e_idx = total_lines
                    
                    sliced_lines = lines[s_idx:e_idx]
                    content = _slice_header(s_idx + 1, e_idx, total_lines) + "".join(sliced_lines)
                else:
                    content = raw_text

            if len(content) > _READ_FILE_BYTE_CEILING:
                content = (
                    content[:_READ_FILE_BYTE_CEILING]
                    + "\n\n... (file truncated to 200KB) ..."
                )

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

    def _do_view_image(self, act: PilotAction) -> tuple[bool, str, str]:
        if not self.config.repo:
            return False, "repo_not_open", "No workspace directory (config.repo) is open."
        target_path = act.path
        if not os.path.isabs(target_path):
            target_path = os.path.join(self.config.repo, target_path)
        # Same read roots as read_file (workspace + git toplevel + spill).
        if not any(is_safe_path(target_path, root) for root in self._read_allowed_roots()):
            return False, "path_traversal", f"Path traversal attempt rejected: {act.path}"
        try:
            if not os.path.exists(target_path):
                return False, "error", f"view_image: not an image file or not found: {act.path}"
            if os.path.isdir(target_path):
                return False, "error", f"view_image: not an image file or not found: {act.path}"

            ext = os.path.splitext(target_path)[1].lower()
            if ext not in (".png", ".jpg", ".jpeg", ".webp"):
                return False, "error", f"view_image: not an image file or not found: {act.path}"

            from .vision import session_supports_native_images, transcribe_images
            # Vision pilots get pixels on the next generate — never a weaker
            # sidecar paraphrase (e.g. gpt-5.6-luna must not go through qwen-vl).
            if session_supports_native_images(self):
                return True, "native_image", target_path
            results = transcribe_images([target_path])
            if not results:
                return False, "error", "view_image failed: no transcription returned"
            r = results[0]
            if r.error:
                return False, "error", f"view_image failed: {r.error}"
            return True, "success", r.text
        except Exception as e:
            return False, "exception", str(e)

    def _do_list_dir(self, act: PilotAction) -> tuple[bool, str, Any]:
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
        # Same read roots as read_file (workspace + git toplevel + spill).
        # Writes/edits stay confined to config.repo only.
        if not any(is_safe_path(target_path, root) for root in self._read_allowed_roots()):
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

    def _do_web_search(self, act: PilotAction) -> tuple[bool, str, str]:
        from .web_tools import web_search
        try:
            result_text = web_search(act.query)
            return True, "success", result_text
        except Exception as e:
            return False, "exception", str(e)

    def _do_web_fetch(self, act: PilotAction) -> tuple[bool, str, str]:
        from .web_tools import web_fetch
        try:
            result_text = web_fetch(act.url)
            return True, "success", result_text
        except Exception as e:
            return False, "exception", str(e)

    def _do_read_pdf(self, act: PilotAction) -> tuple[bool, str, str]:
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

    def _do_search_codegraph(self, act: PilotAction) -> tuple[bool, str, Any]:
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
        kind = (act.arguments.get("kind") or "search").strip().lower()
        if kind == "context":
            subcommand = "context"
            cmd = _puppetmaster_cmd("codegraph", subcommand, act.query)
        elif kind == "affected":
            # Cross-platform argv via _puppetmaster_cmd; query may be a single
            # path or whitespace-separated path list.
            files = [
                part for part in re.split(r"[\s,]+", (act.query or "").strip())
                if part
            ]
            if not files:
                return False, "invalid_arguments", "search_codegraph kind=affected requires one or more file paths in query"
            cmd = _puppetmaster_cmd("codegraph", "affected", "-q", *files)
        else:
            subcommand = "query"
            cmd = _puppetmaster_cmd("codegraph", subcommand, act.query)

        try:
            p = subprocess.run(
                cmd,
                cwd=self.config.repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
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

    def _do_search_files(self, act: PilotAction) -> tuple[bool, str, Any]:
        """Content-search the repo, steering the pilot when nothing matches.

        ``path`` keeps its single-string meaning; a list, or a string naming
        several existing paths, searches all of them. A zero-match result
        carries at most one bounded hint explaining the near miss.
        """
        if not self.config.repo:
            return False, "repo_not_open", "No workspace directory (config.repo) is open."

        query = act.query
        if not query:
            return False, "invalid_arguments", "search_files requires a non-empty 'query'"

        from .search_hints import (
            resolve_search_paths,
            skipped_paths_note,
            zero_match_steering_hint,
        )

        requested = act.arguments.get("path")
        if not requested:
            requested = act.arguments.get("paths") or ""
        search_paths, skipped = resolve_search_paths(requested, self.config.repo)
        # Validate skipped paths too. A missing traversal path must not become
        # harmless merely because another requested path exists and was kept.
        for candidate in (*search_paths, *skipped):
            if not is_safe_path(self._absolute_repo_path(candidate), self.config.repo):
                return False, "path_traversal", f"Path traversal attempt rejected: {candidate}"

        max_results = _result_limit(act.arguments.get("max_results"))
        ok, status, matches = self._search_matching_lines(query, search_paths, max_results)
        if not ok:
            return ok, status, matches

        note = skipped_paths_note(skipped)
        if matches:
            result_text = _format_match_lines(matches, max_results)
            return True, "success", f"{result_text}\n\n{note}" if note else result_text

        hint = zero_match_steering_hint(
            query, self._search_match_counter(query, search_paths)
        )
        return True, "success", "\n".join(part for part in (note, hint) if part)

    def _absolute_repo_path(self, path: str) -> str:
        if os.path.isabs(path):
            return path
        return os.path.join(self.config.repo, path)

    def _search_matching_lines(
        self, query: str, paths: list[str], max_results: int
    ) -> tuple[bool, str, Any]:
        """Run the best available search engine; ripgrep first, Python fallback."""
        import shutil

        rg_path = shutil.which("rg")
        if rg_path:
            outcome = self._ripgrep_matching_lines(rg_path, query, paths)
            if outcome is not None:
                return outcome
        return self._python_matching_lines(query, paths, max_results)

    def _ripgrep_matching_lines(
        self, rg_path: str, query: str, paths: list[str]
    ) -> Any:
        """Return ``(ok, status, lines)``, or None when ripgrep is unusable."""
        from .search_hints import is_multiline_query

        cmd = [rg_path, "--line-number", "--no-heading", "--color=never"]
        if is_multiline_query(query):
            # A query containing newlines can only match across lines.
            cmd.append("--multiline")
        cmd += ["-e", query] + [p if p else "." for p in paths]
        try:
            p = subprocess.run(
                cmd,
                cwd=self.config.repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                timeout=20,
            )
        except subprocess.TimeoutExpired:
            return False, "exception", "ripgrep timed out after 20 seconds"
        except Exception:
            return None

        output = p.stdout or ""
        if p.returncode > 1:
            return False, "exception", f"ripgrep failed with code {p.returncode}: {output.strip()}"
        return True, "success", [_normalize_result_path(l) for l in output.splitlines() if l.strip()]

    def _python_matching_lines(
        self, query: str, paths: list[str], max_results: int
    ) -> tuple[bool, str, Any]:
        from .search_hints import is_multiline_query

        multiline = is_multiline_query(query)
        try:
            compiled = re.compile(query)
        except re.error as e:
            return False, "invalid_arguments", f"Invalid regex pattern: {e}"

        matches: list[str] = []
        for path in paths:
            for file_path in _walk_searchable_files(self._absolute_repo_path(path)):
                rel_path = os.path.relpath(file_path, self.config.repo).replace(os.sep, "/")
                matches.extend(_file_match_lines(compiled, file_path, rel_path, multiline))
                if len(matches) > max_results:
                    return True, "success", matches
        return True, "success", matches

    def _search_match_counter(self, query: str, paths: list[str]):
        """Bounded ``probe(kind) -> count`` used only for zero-match steering."""
        import shutil

        rg_path = shutil.which("rg")
        if rg_path:
            return lambda kind: self._ripgrep_probe_count(rg_path, query, paths, kind)
        return lambda kind: self._python_probe_count(query, paths, kind)

    def _ripgrep_probe_count(
        self, rg_path: str, query: str, paths: list[str], kind: str
    ) -> int:
        from .search_hints import (
            PROBE_CASE_INSENSITIVE,
            PROBE_HIDDEN,
            PROBE_LITERAL,
            is_multiline_query,
        )

        cmd = [rg_path, "--count-matches", "--no-heading", "--color=never"]
        if kind == PROBE_CASE_INSENSITIVE:
            cmd.append("-i")
        elif kind == PROBE_HIDDEN:
            # .git is hidden but is never what the pilot meant to search.
            cmd += ["--hidden", "--no-ignore", "--glob=!.git/**"]
        elif kind == PROBE_LITERAL:
            cmd.append("-F")
        else:
            return 0
        if is_multiline_query(query):
            cmd.append("--multiline")
        cmd += ["-e", query] + [p if p else "." for p in paths]
        p = subprocess.run(
            cmd,
            cwd=self.config.repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
            timeout=10,
        )
        return _sum_ripgrep_counts(p.stdout or "")

    def _python_probe_count(self, query: str, paths: list[str], kind: str) -> int:
        from .search_hints import PROBE_CASE_INSENSITIVE, PROBE_LITERAL, is_multiline_query

        if kind == PROBE_CASE_INSENSITIVE:
            compiled = re.compile(query, re.IGNORECASE)
        elif kind == PROBE_LITERAL:
            compiled = re.compile(re.escape(query))
        else:
            # The Python engine never excludes hidden files, so a "hidden
            # files were skipped" hint would be a false positive here.
            return 0

        multiline = is_multiline_query(query)
        found = 0
        for path in paths:
            for file_path in _walk_searchable_files(self._absolute_repo_path(path)):
                found += len(_file_match_lines(compiled, file_path, "", multiline))
                if found >= _PROBE_MATCH_CAP:
                    return found
        return found

    def _do_lsp(self, act: PilotAction) -> tuple[bool, str, str]:
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
        # LSP root stays repo-confined; do not climb to git toplevel like read_file.
        if not is_safe_path(root_path, self.config.repo):
            return False, "path_traversal", f"Path traversal attempt rejected: {root_arg}"

        if mode == "references" and not symbol:
            return False, "invalid_arguments", "lsp references mode requires a 'symbol'"

        try:
            from .lsp_code_intelligence import discover_lsp_tools, get_lsp_report

            tools = discover_lsp_tools(root=root_path)
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

    def _do_search_state(self, act: PilotAction) -> tuple[bool, str, str]:
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

    def _get_scratch_store(self):
        store = getattr(self, "_scratch_store", None)
        if store is not None:
            return store
        from .session_scratch import SessionScratchStore

        state_dir = getattr(self, "state_dir", None) or getattr(self.config, "state_dir", "") or ""
        store = SessionScratchStore(state_dir)
        self._scratch_store = store
        return store

    def _do_store_scratch(self, act: PilotAction) -> tuple[bool, str, str]:
        from .session_scratch import ScratchStoreCorrupt, ScratchStoreError

        args = act.arguments or {}
        key = (act.path or args.get("key") or "").strip()
        value = act.content if act.content not in (None, "") else args.get("value")
        if not key:
            return False, "invalid_arguments", "store_scratch requires a non-empty 'key'"
        if value is None or str(value) == "":
            return False, "invalid_arguments", "store_scratch requires a non-empty 'value'"
        try:
            self._get_scratch_store().set(key, str(value))
            return True, "success", f"stored scratch key={key!r} ({len(str(value))} chars)"
        except ScratchStoreCorrupt as exc:
            return (
                False,
                "corrupt_store",
                f"{exc}; call clear_scratch (no key) to quarantine and reset",
            )
        except ScratchStoreError as exc:
            return False, "cap_exceeded", str(exc)
        except Exception as exc:
            return False, "exception", str(exc)

    def _do_load_scratch(self, act: PilotAction) -> tuple[bool, str, str]:
        from .session_scratch import ScratchStoreCorrupt

        args = act.arguments or {}
        key = (act.path or args.get("key") or "").strip()
        if not key:
            return False, "invalid_arguments", "load_scratch requires a non-empty 'key'"
        try:
            value = self._get_scratch_store().get(key)
            if value is None:
                return False, "not_found", f"scratch key not found: {key!r}"
            return True, "success", value
        except ScratchStoreCorrupt as exc:
            return (
                False,
                "corrupt_store",
                f"{exc}; call clear_scratch (no key) to quarantine and reset",
            )
        except Exception as exc:
            return False, "exception", str(exc)

    def _do_list_scratch(self, act: PilotAction) -> tuple[bool, str, str]:
        from .session_scratch import ScratchStoreCorrupt

        try:
            rows = self._get_scratch_store().list()
            if not rows:
                return True, "success", "(scratch empty)"
            lines = [f"{key}\t{n} chars" for key, n in rows]
            return True, "success", "\n".join(lines)
        except ScratchStoreCorrupt as exc:
            return (
                False,
                "corrupt_store",
                f"{exc}; call clear_scratch (no key) to quarantine and reset",
            )
        except Exception as exc:
            return False, "exception", str(exc)

    def _do_clear_scratch(self, act: PilotAction) -> tuple[bool, str, str]:
        from .session_scratch import ScratchStoreCorrupt, ScratchStoreError

        args = act.arguments or {}
        key = (act.path or args.get("key") or "").strip()
        try:
            store = self._get_scratch_store()
            if key:
                ok = store.delete(key)
                if not ok:
                    return False, "not_found", f"scratch key not found: {key!r}"
                return True, "success", f"cleared scratch key={key!r}"
            n = store.clear()
            if store.last_quarantine_path:
                return (
                    True,
                    "success",
                    f"cleared corrupt scratch store; quarantined "
                    f"{store.last_quarantine_path}",
                )
            return True, "success", f"cleared {n} scratch key(s)"
        except ScratchStoreCorrupt as exc:
            return False, "corrupt_store", str(exc)
        except ScratchStoreError as exc:
            return False, "exception", str(exc)
        except Exception as exc:
            return False, "exception", str(exc)

    def _do_bind_kernel(self, act: PilotAction) -> tuple[bool, str, Any]:
        from .session_repl import SessionReplError, bind_spill, bind_text

        args = act.arguments or {}
        name = (act.path or args.get("name") or "").strip()
        if not name:
            return False, "invalid_arguments", "bind_kernel requires a non-empty 'name'"
        spill_uri = (args.get("spill_uri") or args.get("uri") or "").strip()
        text = act.content if act.content not in (None, "") else args.get("text")
        try:
            if spill_uri:
                bound = bind_spill(self, name, spill_uri)
                return True, "success", f"bound spill into kernel as {bound!r}"
            if text is None or str(text) == "":
                return False, "invalid_arguments", "bind_kernel requires text or spill_uri"
            bound = bind_text(self, name, str(text))
            return True, "success", f"bound {len(str(text))} chars into kernel as {bound!r}"
        except SessionReplError as exc:
            return False, "error", str(exc)

    def _do_show_kernel(self, act: PilotAction) -> tuple[bool, str, str]:
        from .session_repl import SessionReplError, serialize_bindings

        args = act.arguments or {}
        raw = (act.path or args.get("names") or args.get("name") or "").strip()
        names = [n.strip() for n in raw.split(",") if n.strip()] if raw else None
        try:
            text = serialize_bindings(self, names)
            return True, "success", text
        except SessionReplError as exc:
            return False, "error", str(exc)

    def _do_list_kernel(self, act: PilotAction) -> tuple[bool, str, str]:
        from .session_repl import list_bindings

        rows = list_bindings(self)
        if not rows:
            return True, "success", "(kernel bindings empty)"
        lines = [
            f"- {row['name']}: kind={row.get('kind')} chars~{row.get('char_estimate')}"
            for row in rows
        ]
        return True, "success", "\n".join(lines)

    def _do_clear_kernel(self, act: PilotAction) -> tuple[bool, str, str]:
        from .session_repl import SessionReplError, clear_bindings

        args = act.arguments or {}
        name = (act.path or args.get("name") or "").strip()
        try:
            n = clear_bindings(self, name)
            if name:
                return True, "success", f"cleared kernel binding {name!r}"
            return True, "success", f"cleared {n} kernel binding(s)"
        except SessionReplError as exc:
            return False, "error", str(exc)

    def _compaction_generation(self) -> int:
        try:
            fields = {}
            getter = getattr(self, "_history_compaction_fields", None)
            if callable(getter):
                fields = getter() or {}
            else:
                from .history_compaction_journal import history_compaction_payload
                fields = history_compaction_payload(
                    getattr(self, "state_dir", "") or "",
                    getattr(self, "harness_session_id", None) or "default",
                )
            return int(fields.get("history_compactions") or 0)
        except Exception:
            return 0

    def _do_peek_history(self, act: PilotAction) -> tuple[bool, str, str]:
        from .context_budget import truncate_bytes
        from .sessions import load_transcript

        args = act.arguments or {}
        try:
            offset = int(
                args.get("offset")
                if args.get("offset") is not None
                else (act.start_line if act.start_line is not None else 0)
            )
        except (TypeError, ValueError):
            offset = 0
        offset = max(0, offset)
        try:
            limit = int(
                args.get("limit")
                if args.get("limit") is not None
                else (act.limit if act.limit is not None else 10)
            )
        except (TypeError, ValueError):
            limit = 10
        limit = max(1, min(20, limit))
        role_filter = str(args.get("role") or "").strip().lower()
        expected_raw = args.get("expected_generation")
        expected_generation = None
        if expected_raw is not None and expected_raw != "":
            try:
                expected_generation = int(expected_raw)
            except (TypeError, ValueError):
                return False, "invalid_arguments", "expected_generation must be an integer"
            # Optional JSON integers often arrive as 0. That is "unset", not
            # a claim that history_compactions is still zero after compact.
            if expected_generation == 0:
                expected_generation = None

        generation = self._compaction_generation()
        if expected_generation is not None and expected_generation != generation:
            return False, "stale_generation", (
                f"stale: expected_generation={expected_generation} "
                f"current compaction_generation={generation}"
            )

        # Prefer durable on-disk transcript; fall back to live export.
        # Unattached sessions often have an empty harness_session_id; transcripts
        # are still persisted under "default" in that case.
        sid = (getattr(self, "harness_session_id", None) or "").strip() or "default"
        state_dir = getattr(self, "state_dir", None) or getattr(self.config, "state_dir", "") or ""
        messages: list = []
        archive: list = []
        try:
            from .compaction_archive import load_compaction_archive_messages

            archive = load_compaction_archive_messages(state_dir, sid)
        except Exception:
            archive = []
        disk_messages: list = []
        try:
            data = load_transcript(state_dir, sid)
            if isinstance(data, dict):
                disk_messages = list(data.get("history") or [])
            elif isinstance(data, list):
                disk_messages = list(data)
        except Exception:
            disk_messages = []
        live_messages: list = []
        try:
            export = getattr(self, "export_transcript_data", None)
            if callable(export):
                data = export()
                if isinstance(data, dict):
                    live_messages = list(data.get("history") or [])
            if not live_messages:
                history = list(getattr(self, "_history", []) or [])
                live_messages = [
                    m for m in history
                    if isinstance(m, dict) and m.get("role") != "system"
                ]
        except Exception:
            live_messages = []
        # When an archive exists, residual must be the post-compact tail
        # (live first) so a stale pre-compact transcript is not concatenated
        # on top of the elided rows. Without an archive, keep preferring disk.
        if archive:
            residual = live_messages or disk_messages
            messages = list(archive) + residual
        else:
            messages = disk_messages or live_messages

        if role_filter:
            messages = [
                m for m in messages
                if isinstance(m, dict) and str(m.get("role") or "").lower() == role_filter
            ]
        else:
            messages = [m for m in messages if isinstance(m, dict)]

        slice_msgs = messages[offset: offset + limit]
        # Cap returned chars (~8 KiB).
        max_chars = 8 * 1024
        lines = [
            f"compaction_generation={generation}",
            f"offset={offset} limit={limit} returned={len(slice_msgs)} total={len(messages)}",
        ]
        for i, msg in enumerate(slice_msgs):
            role = str(msg.get("role") or "?")
            content = msg.get("content")
            if isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(str(block.get("text") or ""))
                    elif isinstance(block, str):
                        parts.append(block)
                content = " ".join(parts)
            text = str(content or "")
            lines.append(f"[{offset + i}] {role}: {text}")
        body = "\n".join(lines)
        if len(body) > max_chars:
            body = truncate_bytes(body, max_chars) + "\n... [truncated to 8KiB]"
        return True, "success", body

    def _peek_live_local_artifact(self, uri: str) -> str | None:
        """Resolve artifact://local-* from the live in-memory job table.

        Sidecar visibility filters can hide just-finished terminal locals when
        session/cwd stamps are empty; the live table is authoritative for this
        process and matches the URIs the delivery receipt lists for known handles.
        """
        import json

        if not uri.startswith("artifact://"):
            return None
        path = uri[len("artifact://"):].strip("/")
        parts = path.split("/")
        if len(parts) < 2:
            return None
        job_id, artifact_id = parts[0], parts[1]
        if not job_id.startswith("local-"):
            return None
        live = getattr(self, "_local_jobs", None) or {}
        job = live.get(job_id)
        if not isinstance(job, dict):
            return None
        artifacts = job.get("artifacts") or []
        if not isinstance(artifacts, list):
            return None
        for index, artifact in enumerate(artifacts):
            if not isinstance(artifact, dict):
                continue
            aid = str(artifact.get("id") or "").strip()
            if not aid:
                from .local_job_artifacts import finding_artifact_id
                aid = finding_artifact_id(job_id, index)
            if aid != artifact_id:
                continue
            data = dict(artifact)
            data["id"] = aid
            data["job_id"] = job_id
            if len(parts) >= 3:
                field = parts[2]
                if field not in data:
                    return None
                return str(data[field])
            return json.dumps(data, indent=2, ensure_ascii=False)
        return None

    def _do_peek_artifact(self, act: PilotAction) -> tuple[bool, str, str]:
        from .context_budget import truncate_bytes
        from .internal_uri import InternalUriError, resolve_internal_uri

        args = act.arguments or {}
        uri = (act.path or act.url or args.get("uri") or args.get("path") or "").strip()
        job_id = str(args.get("job_id") or "").strip()
        artifact_id = str(args.get("artifact_id") or "").strip()
        if not uri:
            if job_id and artifact_id:
                uri = f"artifact://{job_id}/{artifact_id}"
            else:
                return False, "invalid_arguments", (
                    "peek_artifact requires artifact:// uri or job_id+artifact_id"
                )
        if not uri.startswith("artifact://"):
            if "://" not in uri:
                uri = f"artifact://{uri.lstrip('/')}"
            else:
                return False, "invalid_arguments", "peek_artifact only accepts artifact:// URIs"

        try:
            max_bytes = int(args.get("max_bytes") if args.get("max_bytes") is not None else 8192)
        except (TypeError, ValueError):
            max_bytes = 8192
        # Clamp: 256..64 KiB (default 8 KiB).
        max_bytes = max(256, min(64 * 1024, max_bytes))

        content = self._peek_live_local_artifact(uri)
        if content is None:
            try:
                resource = resolve_internal_uri(uri, self._internal_uri_context())
            except InternalUriError as exc:
                return False, "internal_uri_error", str(exc)
            except Exception as exc:
                return False, "exception", str(exc)
            content = resource.content if resource is not None else ""
        if content is None:
            content = ""
        if not isinstance(content, str):
            content = str(content)
        truncated = False
        encoded_len = len(content.encode("utf-8"))
        if encoded_len > max_bytes:
            content = truncate_bytes(content, max_bytes)
            truncated = True
        header = f"uri={uri} bytes={encoded_len} max_bytes={max_bytes}"
        if truncated:
            header += " truncated=true"
        return True, "success", f"{header}\n{content}"

    def _do_search_tools(self, act: PilotAction) -> tuple[bool, str, str]:
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
            # Always run activate on this catalog. Never treat search_tools
            # activate as a cached no-op even when the query repeats.
            text = catalog.format_search_response(query, limit=limit, activate=activate)
            self._tool_catalog = catalog
            return True, "success", text
        except Exception as e:
            return False, "exception", str(e)

    def _do_hash_edit(self, act: PilotAction, *, write: bool = True) -> tuple[bool, str, str]:
        """Validate (and optionally apply) hash-anchored edits from act.arguments['ops']."""
        from .hash_edit import HashEditOp, apply_hash_edits, atomic_write_text, hash_edit_enabled

        if not hash_edit_enabled():
            return False, "disabled", "hash_edit is disabled (set HARNESS_HASH_EDIT=1 to enable)"
        if not self.config.repo:
            return False, "repo_not_open", "No workspace directory (config.repo) is open."
        # Fail-closed after Stop: dry-run may still validate; never write.
        if write:
            refused = getattr(self, "_refuse_quarantined_disk_mutation", lambda: None)()
            if refused is not None:
                return refused
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
                # Re-check immediately before the atomic write (TOCTOU with Stop).
                refused = getattr(self, "_refuse_quarantined_disk_mutation", lambda: None)()
                if refused is not None:
                    return refused
                atomic_write_text(target_path, new_text)
                # AST preview (round 6, opt-in): stash a structural diff for
                # the conversation layer to merge into the action_result.
                self._last_ast_preview = None
                try:
                    from .ast_preview import ast_preview_enabled, structural_diff

                    if ast_preview_enabled() and target_path.endswith(".py"):
                        self._last_ast_preview = structural_diff(original, new_text)
                except Exception:
                    self._last_ast_preview = None
            return True, "success", result.message
        except Exception as e:
            return False, "exception", str(e)

    def _do_write_file(self, act: PilotAction, *, write: bool = True) -> tuple[bool, str, Any]:
        """Validate (and optionally apply) a write_file action.

        Returns ``(ok, status, val)`` where ``val`` is an error string on
        failure, or ``bytes_written`` (int) on a successful write. Dry-run
        (``write=False``) returns ``0`` on success after path checks.
        """
        if not self.config.repo:
            return False, "repo_not_open", "No workspace directory (config.repo) is open."
        if write:
            refused = getattr(self, "_refuse_quarantined_disk_mutation", lambda: None)()
            if refused is not None:
                return refused
        target_path = act.path
        if not os.path.isabs(target_path):
            target_path = os.path.join(self.config.repo, target_path)
        if not is_safe_path(target_path, self.config.repo):
            return False, "path_traversal", f"Path traversal attempt rejected: {act.path}"
        if not write:
            return True, "success", 0
        # Re-check immediately before the atomic write (TOCTOU with Stop).
        refused = getattr(self, "_refuse_quarantined_disk_mutation", lambda: None)()
        if refused is not None:
            return refused
        try:
            target_dir = os.path.dirname(target_path)
            os.makedirs(target_dir, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(dir=target_dir, prefix=".tmp-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                    f.write(act.content)
                os.replace(temp_path, target_path)
            except Exception:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                raise
            from .edit_hints import verify_written_text

            mismatch = verify_written_text(target_path, act.content)
            if mismatch:
                return False, "verification_failed", mismatch
            bytes_written = len(act.content.encode("utf-8"))
            return True, "success", bytes_written
        except Exception as e:
            return False, "exception", str(e)

    def _do_edit_file(self, act: PilotAction, *, write: bool = True) -> tuple[bool, str, str]:
        """Validate (and optionally apply) a unique-substring edit_file action."""
        if not self.config.repo:
            return False, "repo_not_open", "No workspace directory (config.repo) is open."
        if write:
            refused = getattr(self, "_refuse_quarantined_disk_mutation", lambda: None)()
            if refused is not None:
                return refused
        target_path = act.path
        if not os.path.isabs(target_path):
            target_path = os.path.join(self.config.repo, target_path)
        if not is_safe_path(target_path, self.config.repo):
            return False, "path_traversal", f"Path traversal attempt rejected: {act.path}"

        try:
            if not os.path.exists(target_path):
                return False, "not_found", (
                    f"edit_file: file not found: {act.path} (use write_file to create new files)"
                )
            if os.path.isdir(target_path):
                return False, "is_directory", f"edit_file: path is a directory: {act.path}"

            with open(target_path, "r", encoding="utf-8", errors="replace") as f:
                original_content = f.read()

            from .edit_hints import (
                describe_match_locations,
                is_edit_already_applied,
                verify_written_text,
                whitespace_near_miss_hint,
            )

            old_str = act.old_str
            new_str = act.new_str
            occurrences = original_content.count(old_str)
            if occurrences == 0:
                if is_edit_already_applied(original_content, old_str, new_str):
                    # A re-sent edit that already landed is the pilot's intent
                    # satisfied, not a failure. Report the no-op and write nothing.
                    return True, "no_op", (
                        f"edit_file: {act.path} already contains the target text; "
                        f"no write performed (do not re-send this edit)"
                    )
                message = (
                    f"edit_file: old_str not found in {act.path} "
                    f"(it must match the existing text EXACTLY, including whitespace/indentation)"
                )
                near_miss = whitespace_near_miss_hint(original_content, old_str)
                if near_miss:
                    message += f"\n{near_miss}"
                return False, "not_found", message
            if occurrences > 1:
                locations = describe_match_locations(original_content, old_str)
                message = (
                    f"edit_file: old_str matched {occurrences} times in {act.path}; "
                    f"add more surrounding context to make it unique"
                )
                if locations:
                    message += f"\nMatches at:\n{locations}"
                return False, "ambiguous", message

            new_content = original_content.replace(old_str, new_str, 1)
            headline = (
                f"edited {act.path}: replaced {len(old_str)} chars -> {len(new_str)} chars"
            )
            if not write:
                return True, "success", headline

            # Re-check immediately before the atomic write (TOCTOU with Stop).
            refused = getattr(self, "_refuse_quarantined_disk_mutation", lambda: None)()
            if refused is not None:
                return refused

            target_dir = os.path.dirname(target_path)
            os.makedirs(target_dir, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(dir=target_dir, prefix=".tmp-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                    f.write(new_content)
                os.replace(temp_path, target_path)
            except Exception:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                raise
            mismatch = verify_written_text(target_path, new_content)
            if mismatch:
                return False, "verification_failed", mismatch
            return True, "success", headline
        except Exception as e:
            return False, "exception", str(e)

    def _do_run_command(self, act: PilotAction) -> tuple[bool, str, Any]:
        """Screen (full-auto) and execute a run_command action.

        Still synchronous (foreground). ``run_cancellable`` status is preserved:

        - ``ok`` / ``truncated`` → ``(True, "success", {output, exit_code, status})``
        - ``cancelled`` / ``timeout`` / ``error`` →
          ``(False, <status>, {output, exit_code, status})`` with partial output kept
        - full-auto danger block →
          ``(False, "blocked", {message, category, reason, matched})``

        Every payload also echoes the effective ``cwd``. Unambiguous interpreter
        / package-root rewrites run before execution; when applied the payload
        also carries ``rewritten`` and ``preflight_reason``. Full-auto danger
        classify and the approval hash stay on the original ``act.command`` so
        a rewrite cannot silently bypass a prior approval. Genuine failures
        carry a one-line ``hint`` (and ``failure_class`` for env-prerequisite
        shapes); output beyond the inline cap carries ``spill_uri`` so it stays
        recoverable instead of being discarded.
        """
        if not self.config.repo:
            return False, "repo_not_open", "No workspace directory (config.repo) is open."
        from .command_policy import guard_destructive_command, effective_command_timeout, run_cancellable

        if getattr(self, "_auto_mode", False) and getattr(self, "_auto_command_guard", None):
            verdict = guard_destructive_command(act.command or "")
            cmd_hash = hashlib.sha256((act.command or "").encode()).hexdigest()
            approved_set = getattr(self, "_approved_commands", set())
            consume_approval = getattr(self, "consume_command_approval", None)
            if verdict.danger:
                from .command_allowlist import allowlist_contains
                allowlisted = allowlist_contains(
                    act.command or "",
                    state_dir=getattr(self, "state_dir", None),
                    workspace_root=str(self.config.repo or ""),
                    command_hash=cmd_hash,
                )
                if consume_approval is not None:
                    consumed = bool(consume_approval(cmd_hash))
                elif cmd_hash in approved_set:
                    # Fallback one-shot when consume_command_approval is absent.
                    approved_set.discard(cmd_hash)
                    consumed = True
                else:
                    consumed = False
                # Allowlist remembers across sessions; one-shot still consumes so
                # a fresh same-hash re-approval is required without an allowlist hit.
                approved_for_retry = allowlisted or consumed
            else:
                approved_for_retry = True
            if verdict.danger and not approved_for_retry:
                block_msg = (
                    f"BLOCKED in full-auto: command matches '{verdict.category}' "
                    f"({verdict.reason}; matched: {verdict.matched}). Autonomous "
                    f"execution of irreversible/remote/escalating commands is gated. "
                    f"Choose a safer approach, or the operator can run this manually."
                )
                from .command_hints import blocked_command_recovery

                blocked = {
                    "message": block_msg,
                    "category": verdict.category,
                    "reason": verdict.reason,
                    "matched": verdict.matched,
                    "command_hash": cmd_hash,
                    "cwd": self.config.repo,
                }
                # Recovery metadata only -- the command is neither run nor saved.
                blocked.update(blocked_command_recovery(act.command or "", cmd_hash))
                return False, "blocked", blocked
            # One-shot is already enforced by consume_command_approval above.
            # Do not unlocked-discard again: a fresh same-hash re-approval raced
            # in after consume must survive for its own retry.

        from .command_preflight import (
            classify_env_prerequisite_failure,
            resolve_command_preflight,
        )

        preflight = resolve_command_preflight(act.command or "", self.config.repo)
        effective_command = preflight.get("command") or act.command or ""
        effective_cwd = preflight.get("cwd") or self.config.repo

        cmd_timeout = effective_command_timeout()
        from .command_jobs import reap_tmp_marionette_worktrees
        try:
            output, exit_code, run_status = run_cancellable(
                effective_command,
                cwd=effective_cwd,
                timeout=cmd_timeout,
                cancel_event=getattr(self, "_cancel", None),
            )
        finally:
            try:
                reap_tmp_marionette_worktrees(effective_command)
                original = act.command or ""
                if original and original != effective_command:
                    reap_tmp_marionette_worktrees(original)
            except Exception:
                pass
        # Normalize legacy aliases used by older mocks / callers.
        if run_status in ("success", None, ""):
            run_status = "ok"
        inline_output, recovery = _recoverable_command_output(self, output)
        payload = {
            "output": inline_output,
            "exit_code": exit_code,
            "status": run_status,
            # Models routinely assume the cwd is wherever they last cd'd to.
            # Echoing it makes every relative-path failure self-diagnosing.
            "cwd": effective_cwd,
        }
        if preflight.get("rewritten"):
            payload["rewritten"] = True
            reason = preflight.get("reason")
            if reason:
                payload["preflight_reason"] = reason
        payload.update(recovery)
        hint = _command_failure_hint(effective_command, exit_code, output)
        if hint:
            payload["hint"] = hint
        failure_class = classify_env_prerequisite_failure(
            effective_command, exit_code, output
        )
        if failure_class:
            payload["failure_class"] = failure_class
        # Terminal failures stay on the sync path but must not look like success.
        # truncated keeps ok=True so callers still get the capped output while
        # status remains distinct from a clean ok.
        if run_status in ("cancelled", "timeout", "error"):
            return False, run_status, payload
        return True, "success", payload

    def _do_run_ipython(self, act: PilotAction) -> tuple[bool, str, Any]:
        """Execute code in the session-scoped persistent Python kernel."""
        code = (act.content or "").strip()
        if not code:
            return False, "error", "run_ipython requires non-empty code"
        from .ipython_kernel import MAX_IPYTHON_DEPTH, _ipython_depth, get_or_create_kernel

        depth = int(getattr(_ipython_depth, "n", 0) or 0)
        if depth >= MAX_IPYTHON_DEPTH:
            return False, "error", "run_ipython depth limit reached"
        _ipython_depth.n = depth + 1
        try:
            kernel = get_or_create_kernel(self)
            result = kernel.execute(code)
        finally:
            _ipython_depth.n = depth
        payload = {
            "output": result.output,
            "backend": result.backend,
            "cwd": kernel.cwd,
        }
        if result.error:
            payload["error"] = result.error
        if result.timed_out:
            return False, "timeout", payload
        if not result.ok:
            return False, "error", payload
        output = result.output or ""
        from .session_repl import offload_ipython_output

        display, bound = offload_ipython_output(self, output)
        if bound:
            payload["kernel_binding"] = bound
            payload["output"] = display
        return True, "success", payload
