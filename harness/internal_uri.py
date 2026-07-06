"""OMP-inspired internal URI read surfaces for Marionette/Puppetmaster.

Agents interact with durable state through a filesystem-shaped interface
instead of bespoke per-resource schemas. Supported schemes:

  job://       durable job store (jobs, tasks, artifact index, events)
  artifact://  full artifact payloads and fields
  agent://     worker run records and fields
  conflict://  git merge-conflict listing and resolution dry-run previews
  spill://     oversized tool outputs persisted by the context budget layer

Read-only by design; existing store APIs and HTTP endpoints are untouched.
Paths are normalized to POSIX segments, traversal is rejected, and backslashes
in URI paths are rejected so Windows drive-letter confusion cannot bypass checks.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import unquote, urlparse

from puppetmaster.models import to_jsonable
from puppetmaster.store_factory import create_store

from .paths import path_within
from .state import DurableState

SUPPORTED_SCHEMES = frozenset({"job", "artifact", "agent", "conflict", "spill"})

_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")
_CONFLICT_MARKER = re.compile(r"^(<{7}|={7}|>{7})")

_LINE_SELECTOR = re.compile(r":(\d+)(?:-(\d+))?$")


class InternalUriError(ValueError):
    """Invalid or unsafe internal URI, or missing backing resource."""


@dataclass(frozen=True)
class ParsedInternalUri:
    scheme: str
    path: str
    raw: str
    start_line: Optional[int] = None
    end_line: Optional[int] = None


@dataclass(frozen=True)
class InternalResource:
    url: str
    content: str
    content_type: str = "text/plain"
    is_directory: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class InternalUriContext:
    state_dir: str
    repo: Optional[str] = None

    def store(self):
        if not self.state_dir:
            raise InternalUriError("state_dir is required for internal URI reads")
        store = create_store("sqlite", self.state_dir)
        store.init()
        return store

    def durable(self) -> DurableState:
        return DurableState(self.state_dir)


def is_internal_uri(path: str) -> bool:
    if not path or "://" not in path:
        return False
    scheme = path.split("://", 1)[0].lower()
    return scheme in SUPPORTED_SCHEMES


def parse_internal_uri(uri: str) -> ParsedInternalUri:
    """Parse and validate an internal URI. Rejects traversal and backslashes."""
    raw = (uri or "").strip()
    if not raw:
        raise InternalUriError("empty URI")

    start_line: Optional[int] = None
    end_line: Optional[int] = None
    path_part = raw
    selector = _LINE_SELECTOR.search(raw)
    if selector and "://" in raw[: selector.start()]:
        path_part = raw[: selector.start()]
        start_line = int(selector.group(1))
        end_line = int(selector.group(2) or selector.group(1))

    if "://" not in path_part:
        raise InternalUriError(f"not an internal URI: {uri!r}")

    scheme, _, remainder = path_part.partition("://")
    scheme = scheme.lower()
    if scheme not in SUPPORTED_SCHEMES:
        raise InternalUriError(f"unsupported scheme {scheme!r}")

    if not remainder and scheme not in ("job", "agent", "conflict", "spill"):
        raise InternalUriError(f"{scheme}:// requires a path")

    if "?" in remainder:
        remainder = remainder.split("?", 1)[0]
    if "#" in remainder:
        remainder = remainder.split("#", 1)[0]

    # Reject Windows-style separators and null bytes in the URI itself.
    if "\\" in remainder or "\x00" in remainder:
        raise InternalUriError("backslashes and null bytes are not allowed in internal URI paths")

    remainder = unquote(remainder)
    if remainder.startswith("/"):
        remainder = remainder[1:]
    if remainder.endswith("/") and len(remainder) > 1:
        remainder = remainder.rstrip("/")

    segments = [segment for segment in remainder.split("/") if segment]
    for segment in segments:
        if segment in (".", ".."):
            raise InternalUriError(f"path traversal rejected in {uri!r}")
        if "\\" in segment or "\x00" in segment:
            raise InternalUriError(f"unsafe path segment in {uri!r}")

    path = "/".join(segments)
    return ParsedInternalUri(
        scheme=scheme,
        path=path,
        raw=raw,
        start_line=start_line,
        end_line=end_line,
    )


def resolve_internal_uri(
    uri: str,
    ctx: InternalUriContext,
    *,
    start_line: Optional[int] = None,
    limit: Optional[int] = None,
) -> InternalResource:
    """Resolve an internal URI to readable text content."""
    parsed = parse_internal_uri(uri)
    if parsed.start_line is not None:
        start_line = parsed.start_line
        limit = (parsed.end_line - parsed.start_line + 1) if parsed.end_line else None

    if parsed.scheme == "job":
        resource = _resolve_job(parsed, ctx)
    elif parsed.scheme == "artifact":
        resource = _resolve_artifact(parsed, ctx)
    elif parsed.scheme == "agent":
        resource = _resolve_agent(parsed, ctx)
    elif parsed.scheme == "spill":
        resource = _resolve_spill(parsed, ctx)
    else:
        resource = _resolve_conflict(parsed, ctx)

    if not resource.is_directory and (start_line is not None or limit is not None):
        resource = _apply_line_slice(resource, start_line, limit)
    return resource


def search_internal_uris(
    query: str,
    ctx: InternalUriContext,
    *,
    scheme: Optional[str] = None,
    max_results: int = 50,
) -> str:
    """Search internal URI surfaces for ``query`` (case-insensitive)."""
    needle = (query or "").strip().lower()
    if not needle:
        return "search_internal_uris: empty query"
    if not ctx.state_dir:
        return "search_internal_uris: state_dir not configured"

    schemes = [scheme] if scheme else sorted(SUPPORTED_SCHEMES)
    hits: list[str] = []

    # Store-backed schemes need Puppetmaster state; spill and conflict do not.
    # Build the store lazily so those schemes remain searchable without it.
    store = None
    durable = None
    if any(sch in ("job", "artifact", "agent") for sch in schemes):
        store = ctx.store()
        durable = ctx.durable()

    for sch in schemes:
        if len(hits) >= max_results:
            break
        if sch == "job":
            for job in store.list_jobs():
                goal = getattr(job, "goal", "") or ""
                jid = getattr(job, "id", "")
                if needle in goal.lower() or needle in jid.lower():
                    hits.append(f"job://{jid}\t{goal[:120]}")
                    if len(hits) >= max_results:
                        break
        elif sch == "artifact":
            for job in store.list_jobs():
                for art in durable.format_artifacts(store.list_artifacts(job.id)):
                    headline = str(art.get("headline") or "")
                    aid = str(art.get("id") or "")
                    if needle in headline.lower() or needle in aid.lower():
                        hits.append(
                            f"artifact://{job.id}/{aid}\t{art.get('type', '')}: {headline[:100]}"
                        )
                        if len(hits) >= max_results:
                            break
        elif sch == "agent":
            for job in store.list_jobs():
                for run in _list_runs(store, job.id):
                    blob = json.dumps(to_jsonable(run), sort_keys=True).lower()
                    if needle in blob:
                        hits.append(f"agent://{job.id}/{run['id']}\t{run.get('role', '')}")
                        if len(hits) >= max_results:
                            break
        elif sch == "conflict":
            for rel in _git_unmerged_files(ctx.repo):
                if needle in rel.lower():
                    hits.append(f"conflict://{rel}\tunmerged")
                    if len(hits) >= max_results:
                        break
        elif sch == "spill":
            from .spill_registry import list_spills

            for row in list_spills(ctx.state_dir):
                haystack = f"{row['session_id']}/{row['tool_call_id']} {row['path']}".lower()
                if needle in haystack:
                    hits.append(
                        f"spill://{row['session_id']}/{row['tool_call_id']}"
                        f"\t{row['chars']:,} chars"
                    )
                    if len(hits) >= max_results:
                        break

    if not hits:
        return f"(no internal URI matches for {query!r})"
    body = "\n".join(hits[:max_results])
    if len(hits) > max_results:
        body += f"\n... truncated to {max_results} results ..."
    return body


def _apply_line_slice(
    resource: InternalResource,
    start_line: Optional[int],
    limit: Optional[int],
) -> InternalResource:
    lines = resource.content.splitlines(keepends=True)
    total = len(lines)
    s_idx = max(0, (start_line or 1) - 1)
    e_idx = total if limit is None else min(total, s_idx + limit)
    sliced = lines[s_idx:e_idx]
    prefix = f"[lines {s_idx + 1}-{e_idx} of {total}]\n"
    return InternalResource(
        url=resource.url,
        content=prefix + "".join(sliced),
        content_type=resource.content_type,
        is_directory=False,
        notes=list(resource.notes),
    )


def _json_resource(url: str, payload: Any, *, is_directory: bool = False) -> InternalResource:
    if is_directory:
        if isinstance(payload, list):
            content = "\n".join(str(item) for item in payload) or "(empty directory)"
        else:
            content = str(payload)
        content_type = "text/plain"
    else:
        content = json.dumps(payload, indent=2, sort_keys=True, default=str)
        content_type = "application/json"
    return InternalResource(url=url, content=content, content_type=content_type, is_directory=is_directory)


def _resolve_job(parsed: ParsedInternalUri, ctx: InternalUriContext) -> InternalResource:
    store = ctx.store()
    url = f"job://{parsed.path}" if parsed.path else "job://"

    if not parsed.path:
        entries = [f"{job.id}\t{getattr(job, 'status', '')}\t{getattr(job, 'goal', '')[:80]}"
                   for job in store.list_jobs()]
        return _json_resource(url, entries, is_directory=True)

    parts = parsed.path.split("/")
    job_id = parts[0]
    _require_safe_id(job_id, "job id")

    if len(parts) == 1:
        job = store.get_job(job_id)
        return _json_resource(url, to_jsonable(job))

    section = parts[1]
    if section == "tasks":
        if len(parts) == 2:
            tasks = [to_jsonable(t) for t in store.list_tasks(job_id)]
            lines = [f"{t['id']}\t{t.get('role', '')}\t{t.get('status', '')}" for t in tasks]
            return _json_resource(url, lines, is_directory=True)
        task_id = parts[2]
        _require_safe_id(task_id, "task id")
        task = store.get_task_by_id(task_id)
        if task.job_id != job_id:
            raise InternalUriError(f"task {task_id} does not belong to job {job_id}")
        return _json_resource(url, to_jsonable(task))

    if section == "artifacts":
        durable = ctx.durable()
        formatted = durable.format_artifacts(store.list_artifacts(job_id))
        if len(parts) == 2:
            lines = [
                f"{a.get('id')}\t{a.get('type')}\t{a.get('headline', '')[:100]}"
                for a in formatted
            ]
            return _json_resource(url, lines, is_directory=True)
        artifact_id = parts[2]
        _require_safe_id(artifact_id, "artifact id")
        for art in store.list_artifacts(job_id):
            if art.id == artifact_id:
                return _json_resource(url, to_jsonable(art))
        raise InternalUriError(f"artifact not found: {artifact_id}")

    if section == "events":
        cursor = 0
        if len(parts) >= 3 and parts[2] == "since":
            try:
                cursor = int(parts[3])
            except (IndexError, ValueError) as exc:
                raise InternalUriError("events cursor must be an integer") from exc
        payload = ctx.durable().events_since(job_id, cursor)
        return _json_resource(url, payload)

    raise InternalUriError(f"unknown job path: {parsed.path}")


def _resolve_artifact(parsed: ParsedInternalUri, ctx: InternalUriContext) -> InternalResource:
    store = ctx.store()
    parts = parsed.path.split("/")
    if len(parts) < 2:
        raise InternalUriError("artifact:// requires job_id/artifact_id[/field...]")

    job_id, artifact_id = parts[0], parts[1]
    _require_safe_id(job_id, "job id")
    _require_safe_id(artifact_id, "artifact id")
    url = f"artifact://{parsed.path}"

    artifact = None
    for art in store.list_artifacts(job_id):
        if art.id == artifact_id:
            artifact = art
            break
    if artifact is None:
        raise InternalUriError(f"artifact not found: {artifact_id}")

    data = to_jsonable(artifact)
    if len(parts) == 2:
        return _json_resource(url, data)

    if parts[2] == "payload":
        payload = data.get("payload") or {}
        if len(parts) == 3:
            return _json_resource(url, payload)
        key = "/".join(parts[3:])
        if key not in payload:
            raise InternalUriError(f"payload field not found: {key}")
        return _text_resource(url, _stringify(payload[key]))

    field_name = parts[2]
    if field_name not in data:
        raise InternalUriError(f"artifact field not found: {field_name}")
    return _text_resource(url, _stringify(data[field_name]))


def _resolve_agent(parsed: ParsedInternalUri, ctx: InternalUriContext) -> InternalResource:
    store = ctx.store()
    url = f"agent://{parsed.path}" if parsed.path else "agent://"

    if not parsed.path:
        entries: list[str] = []
        for job in store.list_jobs():
            for run in _list_runs(store, job.id):
                entries.append(
                    f"{job.id}/{run['id']}\t{run.get('role', '')}\t{run.get('status', '')}"
                )
        return _json_resource(url, entries, is_directory=True)

    parts = parsed.path.split("/")
    job_id = parts[0]
    _require_safe_id(job_id, "job id")

    if len(parts) == 1:
        runs = _list_runs(store, job_id)
        lines = [f"{r['id']}\t{r.get('role', '')}\t{r.get('worker_id', '')}" for r in runs]
        return _json_resource(url, lines, is_directory=True)

    run_id = parts[1]
    _require_safe_id(run_id, "run id")
    run = _get_run(store, job_id, run_id)
    if len(parts) == 2:
        return _json_resource(url, run)

    field_name = parts[2]
    if field_name not in run:
        raise InternalUriError(f"agent field not found: {field_name}")
    return _text_resource(url, _stringify(run[field_name]))


def _resolve_spill(parsed: ParsedInternalUri, ctx: InternalUriContext) -> InternalResource:
    from .spill_registry import list_spills, resolve_spill

    if not ctx.state_dir:
        raise InternalUriError("spill:// requires a configured state_dir")

    url = f"spill://{parsed.path}" if parsed.path else "spill://"

    if not parsed.path:
        entries = [
            f"{row['session_id']}/{row['tool_call_id']}\t{row['chars']:,} chars"
            for row in list_spills(ctx.state_dir)
        ]
        return _json_resource(url, entries, is_directory=True)

    parts = parsed.path.split("/")
    session_id = parts[0]
    _require_safe_id(session_id, "session id")

    if len(parts) == 1:
        entries = [
            f"{row['tool_call_id']}\t{row['chars']:,} chars"
            for row in list_spills(ctx.state_dir, session_id=session_id)
        ]
        return _json_resource(url, entries, is_directory=True)

    if len(parts) != 2:
        raise InternalUriError("spill:// takes session_id/tool_call_id")

    tool_call_id = parts[1]
    _require_safe_id(tool_call_id, "tool call id")

    row = resolve_spill(ctx.state_dir, session_id, tool_call_id)
    if row is None:
        raise InternalUriError(f"spill not found: {session_id}/{tool_call_id}")

    file_path = str(row["path"])
    # The registry only ever indexes files under pmharness-results, but the db
    # is on-disk state -- re-verify confinement before serving the content.
    results_root = os.path.join(os.path.abspath(ctx.state_dir), "pmharness-results")
    if not path_within(os.path.realpath(file_path), results_root, allow_equal=False):
        raise InternalUriError(f"spill path escapes the results directory: {tool_call_id}")
    if not os.path.isfile(file_path):
        raise InternalUriError(f"spill file no longer exists: {tool_call_id}")

    with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
        return _text_resource(url, fh.read())


def _resolve_conflict(parsed: ParsedInternalUri, ctx: InternalUriContext) -> InternalResource:
    repo = ctx.repo
    if not repo:
        raise InternalUriError("conflict:// requires an open git repository (config.repo)")

    url = f"conflict://{parsed.path}" if parsed.path else "conflict://"

    if not parsed.path:
        files = _git_unmerged_files(repo)
        return _json_resource(url, files, is_directory=True)

    parts = parsed.path.split("/")
    if parts[0] == "resolve":
        if len(parts) < 2:
            raise InternalUriError("conflict://resolve/<path> requires a file path")
        rel_path = "/".join(parts[1:])
        strategy = _parse_conflict_strategy(parsed.raw)
        preview = _conflict_resolution_dry_run(repo, rel_path, strategy)
        notes = [f"dry-run only; no files were modified (strategy={strategy})"]
        return InternalResource(
            url=url,
            content=preview,
            content_type="text/plain",
            notes=notes,
        )

    rel_path = parsed.path
    _require_repo_relative_path(repo, rel_path)
    abs_path = os.path.join(repo, rel_path.replace("/", os.sep))
    if not os.path.isfile(abs_path):
        raise InternalUriError(f"conflict file not found: {rel_path}")

    with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()

    regions = _extract_conflict_regions(text)
    payload = {
        "path": rel_path,
        "unmerged": rel_path in _git_unmerged_files(repo),
        "regions": regions,
    }
    return _json_resource(url, payload)


def _parse_conflict_strategy(raw_uri: str) -> str:
    parsed = urlparse(raw_uri)
    query = parsed.query.lower()
    for part in query.split("&"):
        if part.startswith("strategy="):
            strategy = part.split("=", 1)[1]
            if strategy in ("ours", "theirs"):
                return strategy
    return "ours"


def _conflict_resolution_dry_run(repo: str, rel_path: str, strategy: str) -> str:
    _require_repo_relative_path(repo, rel_path)
    abs_path = os.path.join(repo, rel_path.replace("/", os.sep))
    if not os.path.isfile(abs_path):
        raise InternalUriError(f"conflict file not found: {rel_path}")

    with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
        original = fh.read()

    resolved, changed = _apply_conflict_strategy(original, strategy)
    header = (
        f"# conflict resolution dry-run\n"
        f"path: {rel_path}\n"
        f"strategy: {strategy}\n"
        f"would_change_file: {changed}\n\n"
    )
    return header + resolved


def _apply_conflict_strategy(text: str, strategy: str) -> tuple[str, bool]:
    """Return (resolved_text, changed). Does not write to disk."""
    out: list[str] = []
    changed = False
    idx = 0
    lines = text.splitlines(keepends=True)

    while idx < len(lines):
        line = lines[idx]
        if line.startswith("<<<<<<<"):
            changed = True
            idx += 1
            ours: list[str] = []
            while idx < len(lines) and not lines[idx].startswith("======="):
                ours.append(lines[idx])
                idx += 1
            if idx >= len(lines) or not lines[idx].startswith("======="):
                raise InternalUriError("malformed conflict marker block (missing =======)")
            idx += 1
            theirs: list[str] = []
            while idx < len(lines) and not lines[idx].startswith(">>>>>>>"):
                theirs.append(lines[idx])
                idx += 1
            if idx >= len(lines) or not lines[idx].startswith(">>>>>>>"):
                raise InternalUriError("malformed conflict marker block (missing >>>>>>>)")
            idx += 1
            out.extend(ours if strategy == "ours" else theirs)
            continue
        out.append(line)
        idx += 1

    resolved = "".join(out)
    return resolved, changed


def _extract_conflict_regions(text: str) -> list[dict[str, Any]]:
    regions: list[dict[str, Any]] = []
    idx = 0
    line_no = 1
    lines = text.splitlines(keepends=True)
    while idx < len(lines):
        if lines[idx].startswith("<<<<<<<"):
            start = line_no
            idx += 1
            line_no += 1
            while idx < len(lines) and not lines[idx].startswith("======="):
                idx += 1
                line_no += 1
            if idx < len(lines) and lines[idx].startswith("======="):
                idx += 1
                line_no += 1
            while idx < len(lines) and not lines[idx].startswith(">>>>>>>"):
                idx += 1
                line_no += 1
            if idx < len(lines) and lines[idx].startswith(">>>>>>>"):
                idx += 1
                line_no += 1
            regions.append({"start_line": start, "end_line": line_no - 1})
            continue
        idx += 1
        line_no += 1
    return regions


def _list_runs(store, job_id: str) -> list[dict[str, Any]]:
    runs_dir = store.job_dir(job_id) / "runs"
    if runs_dir.exists():
        files = sorted(runs_dir.glob("*.json"))
        if files:
            return [store.read_json(path) for path in files]

    if getattr(store, "backend_name", "") == "sqlite":
        connection = store.connect()
        try:
            rows = connection.execute(
                "SELECT data FROM runs WHERE job_id = ? ORDER BY id",
                (job_id,),
            ).fetchall()
        finally:
            connection.close()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                out.append(json.loads(row[0]))
            except (TypeError, json.JSONDecodeError):
                continue
        return out
    return []


def _get_run(store, job_id: str, run_id: str) -> dict[str, Any]:
    path = store.job_dir(job_id) / "runs" / f"{run_id}.json"
    if path.exists():
        return store.read_json(path)

    if getattr(store, "backend_name", "") == "sqlite":
        connection = store.connect()
        try:
            row = connection.execute(
                "SELECT data FROM runs WHERE job_id = ? AND id = ?",
                (job_id, run_id),
            ).fetchone()
        finally:
            connection.close()
        if row:
            return json.loads(row[0])

    raise InternalUriError(f"agent run not found: {run_id}")


def _git_unmerged_files(repo: Optional[str]) -> list[str]:
    if not repo:
        return []
    try:
        proc = subprocess.run(
            ["git", "-C", repo, "diff", "--name-only", "--diff-filter=U"],
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return [line.strip().replace("\\", "/") for line in proc.stdout.splitlines() if line.strip()]


def _require_safe_id(value: str, label: str) -> None:
    if not value or not _SAFE_SEGMENT.match(value):
        raise InternalUriError(f"invalid {label}: {value!r}")


def _require_repo_relative_path(repo: str, rel_path: str) -> None:
    if not rel_path or rel_path.startswith("/"):
        raise InternalUriError(f"invalid repository-relative path: {rel_path!r}")
    if ".." in rel_path.split("/"):
        raise InternalUriError(f"path traversal rejected: {rel_path!r}")
    abs_path = os.path.realpath(os.path.join(repo, rel_path.replace("/", os.sep)))
    if not path_within(abs_path, repo, allow_equal=True):
        raise InternalUriError(f"path escapes repository: {rel_path!r}")


def _text_resource(url: str, content: str) -> InternalResource:
    return InternalResource(url=url, content=content, content_type="text/plain")


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, indent=2, sort_keys=True, default=str)
