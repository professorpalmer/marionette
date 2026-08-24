"""Resolve Agent Plugin install sources: local path, git, https, github."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import unquote, urlparse

from .agent_plugins import AgentPluginError

SourceKind = str  # "path" | "git" | "https" | "github"

_WINDOWS_ABS = re.compile(r"^[A-Za-z]:[\\/]")
_GIT_SSH = re.compile(r"^(?:git@|ssh://git@|git://)", re.IGNORECASE)
_GITHUB_HOST = re.compile(r"^(?:www\.)?github\.com$", re.IGNORECASE)
_GITHUB_SHORTHAND = re.compile(
    r"^(?:github:)?([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)/"
    r"([A-Za-z0-9._-]+?)(?:\.git)?(?:[@#](.+))?$"
)
_CLONE_TIMEOUT_SEC = 120


@dataclass(frozen=True)
class ResolvedPluginSource:
    kind: SourceKind
    raw: str
    path: str = ""
    clone_url: str = ""
    owner: str = ""
    repo: str = ""
    ref: str = ""
    subdir: str = ""

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ResolvedPluginSource":
        kind = str(data.get("kind") or "").strip()
        raw = str(data.get("raw") or data.get("cloneUrl") or data.get("clone_url") or "")
        if kind not in {"path", "git", "https", "github"}:
            raise AgentPluginError("unsupported plugin source")
        clone_url = str(data.get("cloneUrl") or data.get("clone_url") or "")
        path = str(data.get("path") or "")
        return cls(
            kind=kind,
            raw=raw,
            path=path,
            clone_url=clone_url,
            owner=str(data.get("owner") or ""),
            repo=str(data.get("repo") or ""),
            ref=str(data.get("ref") or ""),
            subdir=str(data.get("subdir") or ""),
        )


def is_absolute_plugin_path(value: str) -> bool:
    raw = (value or "").strip()
    return raw.startswith("/") or _WINDOWS_ABS.match(raw) is not None


def _parse_git_remote(clone_url: str) -> tuple[str, str]:
    match = re.search(
        r"(?:[:/])([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?$",
        clone_url.rstrip("/"),
    )
    if not match:
        return "", ""
    return match.group(1), re.sub(r"\.git$", "", match.group(2), flags=re.IGNORECASE)


def _split_hash_ref(raw: str) -> tuple[str, str]:
    hash_at = raw.rfind("#")
    if hash_at <= 0:
        return raw, ""
    return raw[:hash_at], unquote(raw[hash_at + 1 :])


def resolve_plugin_source(source: str) -> ResolvedPluginSource:
    """Classify an absolute path or git / https / github source URL."""
    raw = (source or "").strip()
    if not raw:
        raise AgentPluginError("plugin source is required")
    scheme = raw.split(":", 1)[0].lower()
    if scheme in {"file", "javascript", "data", "ftp"}:
        raise AgentPluginError("unsupported plugin source scheme")
    if is_absolute_plugin_path(raw):
        return ResolvedPluginSource(kind="path", raw=raw, path=raw)
    if _GIT_SSH.match(raw):
        base, ref = _split_hash_ref(raw)
        owner, repo = _parse_git_remote(base)
        return ResolvedPluginSource(
            kind="git", raw=raw, clone_url=base, owner=owner, repo=repo, ref=ref
        )
    if re.match(r"^https?://", raw, re.IGNORECASE):
        return _resolve_http_source(raw)
    if "\\" in raw or raw.startswith(".") or raw.count("/") != 1:
        raise AgentPluginError(
            "plugin source must be an absolute path, git URL, https URL, or GitHub source"
        )
    match = _GITHUB_SHORTHAND.match(raw)
    if match is None:
        raise AgentPluginError(
            "plugin source must be an absolute path, git URL, https URL, or GitHub source"
        )
    owner = match.group(1)
    repo = re.sub(r"\.git$", "", match.group(2), flags=re.IGNORECASE)
    ref = match.group(3) or ""
    return ResolvedPluginSource(
        kind="github",
        raw=raw,
        owner=owner,
        repo=repo,
        ref=ref,
        clone_url=f"https://github.com/{owner}/{repo}.git",
    )


def _resolve_http_source(raw: str) -> ResolvedPluginSource:
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        raise AgentPluginError("unsupported plugin source scheme")
    host = (parsed.hostname or "").lower()
    ref_from_hash = unquote((parsed.fragment or "").lstrip("#"))
    if _GITHUB_HOST.match(host or ""):
        parts = [p for p in (parsed.path or "").strip("/").split("/") if p]
        if len(parts) < 2:
            raise AgentPluginError("GitHub source must be owner/repo")
        owner = parts[0]
        repo = re.sub(r"\.git$", "", parts[1], flags=re.IGNORECASE)
        ref = ref_from_hash
        if len(parts) >= 4 and parts[2] in {"tree", "commit"}:
            ref = ref or parts[3]
        return ResolvedPluginSource(
            kind="github",
            raw=raw,
            owner=owner,
            repo=repo,
            ref=ref,
            clone_url=f"https://github.com/{owner}/{repo}.git",
        )
    path = (parsed.path or "").lower()
    kind = "git" if path.endswith(".git") else "https"
    location = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    if parsed.query:
        location = f"{location}?{parsed.query}"
    owner, repo = _parse_git_remote(f"{parsed.scheme}://{parsed.netloc}{parsed.path}")
    return ResolvedPluginSource(
        kind=kind,
        raw=raw,
        clone_url=location,
        ref=ref_from_hash,
        owner=owner,
        repo=repo,
    )


def git_clone_plugin_source(
    url: str, dest: Path, *, ref: Optional[str] = None
) -> None:
    """Shallow-clone ``url`` into ``dest`` (must not already exist)."""
    dest = Path(dest)
    if dest.exists():
        dest.mkdir(parents=True, exist_ok=True)
    cmd = ["git", "clone", "--depth", "1"]
    if ref:
        cmd.extend(["--branch", ref])
    cmd.extend(["--", url, str(dest)])
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_CLONE_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentPluginError(f"failed to clone plugin source: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "git clone failed").strip()
        raise AgentPluginError(f"failed to clone plugin source: {detail}")


def coerce_plugin_source(source: object) -> ResolvedPluginSource:
    """Accept a URL string or a resolved mapping from the web client."""
    if isinstance(source, ResolvedPluginSource):
        return source
    if isinstance(source, Mapping):
        return ResolvedPluginSource.from_mapping(source)
    return resolve_plugin_source(str(source or ""))
