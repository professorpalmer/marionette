"""Cheap CodeGraph workspace preflight for huge / mixed trees.

Game clients and asset dumps (Ashita ``polplugins/``, Unreal ``Content/``, etc.)
make a blind ``codegraph init --index`` look like language failure when it is
really scope failure. This module walks a capped tree, scores indexable source,
and recommends excludes or a narrower root before Marionette burns a long index.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any, Dict, List, Optional, Set

# Tunable gates (tests monkeypatch these).
SCOPE_BYTES_THRESHOLD = 2_000_000_000
SCOPE_FILES_THRESHOLD = 15_000
MIN_INDEXABLE_FOR_SCOPE = 20
CHILD_INDEXABLE_SHARE = 0.70

INDEXABLE_EXTENSIONS = frozenset(
    {
        ".py",
        ".pyi",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".kts",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".cc",
        ".cxx",
        ".cs",
        ".rb",
        ".php",
        ".swift",
        ".dart",
        ".vue",
        ".svelte",
        ".lua",
        ".luau",
        ".scala",
        ".sc",
        ".zig",
        ".ex",
        ".exs",
        ".hs",
        ".ml",
        ".mli",
        ".fs",
        ".fsx",
        ".r",
        ".jl",
        ".nim",
        ".clj",
        ".cljs",
        ".elm",
        ".pas",
        ".dpr",
    }
)

_SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        ".codegraph",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".hg",
        ".svn",
        "dist",
        "build",
        ".next",
        ".nuxt",
        "target",
        ".gradle",
        ".idea",
        ".cache",
        "coverage",
        "vendor",
    }
)

DEFAULT_ASSET_EXCLUDES = [
    "**/polplugins/**",
    "**/chatlogs/**",
    "**/screenshots/**",
    "**/logs/**",
    "**/Data/**",
    "**/Content/**",
    "**/Binaries/**",
    "**/Intermediate/**",
    "**/Saved/**",
    "**/*.DAT",
    "**/*.dat",
    "**/*.dll",
    "**/*.exe",
    "**/*.pak",
    "**/*.uasset",
    "**/*.umap",
    "**/*.pdb",
    "**/*.so",
    "**/*.dylib",
    "**/*.bin",
    "**/*.iso",
    "**/*.img",
]

_LUA_INCLUDES = ["**/*.lua", "**/*.luau"]


def _ext(name: str) -> str:
    _, ext = os.path.splitext(name)
    return ext.lower()


def _is_indexable(name: str) -> bool:
    return _ext(name) in INDEXABLE_EXTENSIONS


def preflight_workspace(
    root: str,
    *,
    max_files: int = 50_000,
    time_budget_s: float = 5.0,
) -> Dict[str, Any]:
    """Capped scandir walk; returns a JSON-serializable preflight verdict."""
    abs_root = os.path.abspath(root or "")
    empty = {
        "root": abs_root,
        "files_seen": 0,
        "bytes_seen": 0,
        "indexable_files": 0,
        "capped": False,
        "heavy_children": [],
        "verdict": "ok",
        "suggested_roots": [],
        "suggested_excludes": [],
        "reason": "No workspace root to scan.",
    }
    if not abs_root or not os.path.isdir(abs_root):
        return empty

    started = time.monotonic()
    files_seen = 0
    bytes_seen = 0
    indexable_files = 0
    capped = False
    # Top-level child name -> counters
    children: Dict[str, Dict[str, int]] = {}

    def bump_child(rel_top: str, *, nbytes: int, indexable: bool) -> None:
        row = children.setdefault(
            rel_top, {"files": 0, "bytes": 0, "indexable": 0}
        )
        row["files"] += 1
        row["bytes"] += max(0, nbytes)
        if indexable:
            row["indexable"] += 1

    stack: List[str] = [abs_root]
    while stack:
        if files_seen >= max_files or (time.monotonic() - started) >= time_budget_s:
            capped = True
            break
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if files_seen >= max_files or (
                        time.monotonic() - started
                    ) >= time_budget_s:
                        capped = True
                        break
                    try:
                        name = entry.name
                        if entry.is_dir(follow_symlinks=False):
                            if name in _SKIP_DIR_NAMES:
                                continue
                            stack.append(entry.path)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        try:
                            nbytes = int(entry.stat(follow_symlinks=False).st_size)
                        except OSError:
                            nbytes = 0
                        files_seen += 1
                        bytes_seen += max(0, nbytes)
                        indexable = _is_indexable(name)
                        if indexable:
                            indexable_files += 1
                        # Top-level child relative to abs_root
                        rel = os.path.relpath(entry.path, abs_root)
                        parts = rel.replace("\\", "/").split("/")
                        if len(parts) >= 2:
                            bump_child(parts[0], nbytes=nbytes, indexable=indexable)
                        elif len(parts) == 1:
                            # File at root — attribute to "."
                            bump_child(".", nbytes=nbytes, indexable=indexable)
                    except OSError:
                        continue
        except OSError:
            continue

    heavy_children = [
        {
            "name": name,
            "files": int(row["files"]),
            "bytes": int(row["bytes"]),
            "indexable": int(row["indexable"]),
        }
        for name, row in children.items()
        if name != "."
    ]
    heavy_children.sort(key=lambda r: r["bytes"], reverse=True)
    heavy_children = heavy_children[:8]

    large = (
        bytes_seen > SCOPE_BYTES_THRESHOLD or files_seen > SCOPE_FILES_THRESHOLD
    )
    suggested_roots: List[str] = []
    suggested_excludes: List[str] = []
    verdict = "ok"
    reason = "Workspace looks indexable."

    if large and indexable_files < MIN_INDEXABLE_FOR_SCOPE:
        verdict = "unlikely"
        heavy_names = [c["name"] for c in heavy_children[:3]]
        reason = (
            "This folder looks huge (~{:.1f} GB, {:,} files) with almost no "
            "indexable source ({}). Open a code subdirectory, or exclude asset "
            "dumps{}."
        ).format(
            bytes_seen / 1e9,
            files_seen,
            indexable_files,
            (" like " + ", ".join(heavy_names)) if heavy_names else "",
        )
        suggested_excludes = [
            c["name"]
            for c in heavy_children
            if c["indexable"] == 0 and c["files"] > 0
        ][:8]
    elif large and indexable_files >= MIN_INDEXABLE_FOR_SCOPE:
        # Prefer a child that holds most indexable files.
        best = None
        for c in heavy_children:
            if c["indexable"] <= 0:
                continue
            share = c["indexable"] / float(indexable_files)
            if share >= CHILD_INDEXABLE_SHARE and (
                best is None or c["indexable"] > best["indexable"]
            ):
                best = c
        asset_heavy = [
            c["name"]
            for c in heavy_children
            if c["indexable"] == 0
            and (c["bytes"] > 50_000_000 or c["files"] > 20)
        ]
        if best is not None:
            verdict = "scope_recommended"
            suggested_roots = [best["name"]]
            suggested_excludes = asset_heavy[:8]
            reason = (
                "This folder looks like a large install (~{:.1f} GB). Almost all "
                "indexable code is under `{}/` ({} files). Open that folder, or "
                "exclude asset dumps{} before indexing."
            ).format(
                bytes_seen / 1e9,
                best["name"],
                best["indexable"],
                (" (" + ", ".join(asset_heavy[:3]) + ")") if asset_heavy else "",
            )
        elif asset_heavy:
            verdict = "scope_recommended"
            suggested_excludes = asset_heavy[:8]
            reason = (
                "This folder is large (~{:.1f} GB, {:,} files) with heavy "
                "non-source trees ({}). Exclude those and reindex, or open a "
                "narrower code folder."
            ).format(
                bytes_seen / 1e9,
                files_seen,
                ", ".join(asset_heavy[:3]),
            )
        else:
            reason = (
                "Large workspace (~{:.1f} GB, {:,} files, {} indexable); "
                "indexing may take a while."
            ).format(bytes_seen / 1e9, files_seen, indexable_files)

    return {
        "root": abs_root,
        "files_seen": files_seen,
        "bytes_seen": bytes_seen,
        "indexable_files": indexable_files,
        "capped": capped,
        "heavy_children": heavy_children,
        "verdict": verdict,
        "suggested_roots": suggested_roots,
        "suggested_excludes": suggested_excludes,
        "reason": reason,
    }


def _default_config() -> Dict[str, Any]:
    return {
        "version": 1,
        "include": list(_LUA_INCLUDES)
        + [
            "**/*.ts",
            "**/*.tsx",
            "**/*.js",
            "**/*.jsx",
            "**/*.py",
            "**/*.go",
            "**/*.rs",
            "**/*.java",
            "**/*.c",
            "**/*.h",
            "**/*.cpp",
            "**/*.hpp",
            "**/*.cs",
            "**/*.rb",
            "**/*.php",
            "**/*.swift",
            "**/*.kt",
            "**/*.vue",
            "**/*.svelte",
        ],
        "exclude": [
            "**/.git/**",
            "**/node_modules/**",
            "**/vendor/**",
            "**/dist/**",
            "**/build/**",
            "**/__pycache__/**",
            "**/.venv/**",
            "**/venv/**",
        ],
        "languages": [],
        "frameworks": [],
        "maxFileSize": 1048576,
        "extractDocstrings": True,
        "trackCallSites": True,
    }


def _atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="codegraph-cfg-", suffix=".json", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def merge_codegraph_excludes(
    root: str,
    extra_excludes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Merge asset excludes + lua includes into ``.codegraph/config.json``."""
    abs_root = os.path.abspath(root)
    cg_dir = os.path.join(abs_root, ".codegraph")
    cfg_path = os.path.join(cg_dir, "config.json")
    os.makedirs(cg_dir, exist_ok=True)

    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
            if not isinstance(cfg, dict):
                cfg = _default_config()
        except Exception:
            cfg = _default_config()
    else:
        cfg = _default_config()

    includes: List[str] = list(cfg.get("include") or [])
    for glob in _LUA_INCLUDES:
        if glob not in includes:
            includes.append(glob)
    cfg["include"] = includes

    excludes: List[str] = list(cfg.get("exclude") or [])
    seen: Set[str] = set(excludes)
    for glob in (extra_excludes if extra_excludes is not None else DEFAULT_ASSET_EXCLUDES):
        if glob not in seen:
            excludes.append(glob)
            seen.add(glob)
    # Also exclude suggested top-level asset dirs as globs when passed as bare names
    cfg["exclude"] = excludes

    _atomic_write_json(cfg_path, cfg)
    return cfg


def ensure_lua_includes(root: str) -> bool:
    """Add ``**/*.lua`` / ``**/*.luau`` to include if config exists. Return True if changed."""
    abs_root = os.path.abspath(root)
    cfg_path = os.path.join(abs_root, ".codegraph", "config.json")
    if not os.path.isfile(cfg_path):
        return False
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return False
    if not isinstance(cfg, dict):
        return False
    includes = list(cfg.get("include") or [])
    changed = False
    for glob in _LUA_INCLUDES:
        if glob not in includes:
            includes.append(glob)
            changed = True
    if not changed:
        return False
    cfg["include"] = includes
    _atomic_write_json(cfg_path, cfg)
    return True


def child_exclude_globs(child_names: List[str]) -> List[str]:
    """Turn top-level directory names into ``**/name/**`` exclude globs."""
    out: List[str] = []
    for name in child_names or []:
        name = (name or "").strip().strip("/\\")
        if not name or name in (".", ".."):
            continue
        out.append(f"**/{name}/**")
    return out
