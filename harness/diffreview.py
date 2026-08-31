"""Diff review parsing and reconstruction utilities."""

from __future__ import annotations

from typing import Any, Mapping, MutableMapping


def extract_path(line: str, prefix: str) -> str:
    p = line[len(prefix):].strip()
    if p.startswith("a/") or p.startswith("b/"):
        p = p[2:]
    elif p.startswith("\"a/") or p.startswith("\"b/"):
        p = p[3:]
    if p.endswith("\""):
        p = p[:-1]
    return p


def hunk_content_fingerprint(path: str, header: str, lines: Any) -> str:
    """Deterministic content fingerprint shared with the webapp fallback.

    Built from path + header + body lines only — never from array position —
    so reordering unrelated hunks cannot change an existing key.
    """
    parts = [str(path or ""), str(header or "")]
    for line in lines or ():
        parts.append(str(line).rstrip("\n"))
    blob = "\n".join(parts).encode("utf-8", errors="replace")
    # FNV-1a 64-bit (same algorithm as webapp/src/lib/reviewDecisions.ts).
    h = 14695981039346656037
    for b in blob:
        h ^= b
        h = (h * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


def assign_decision_ids(files: list) -> list:
    """Stamp a stable per-hunk ``decision_id`` derived from content.

    Exact duplicate-content hunks get distinct keys via a per-fingerprint
    ordinal (``fp#0``, ``fp#1``, …). Existing ``decision_id`` values are kept.
    """
    seen: dict[str, int] = {}
    for f in files or []:
        if not isinstance(f, Mapping):
            continue
        path = str(f.get("path") or "")
        for hunk in f.get("hunks") or []:
            if not isinstance(hunk, MutableMapping):
                continue
            existing = str(hunk.get("decision_id") or "").strip()
            if existing:
                # Reserve the ordinal so a later identical fingerprint cannot
                # collide with a server-assigned id that used the same scheme.
                if "#" in existing:
                    base, _, suffix = existing.rpartition("#")
                    if base and suffix.isdigit():
                        seen[base] = max(seen.get(base, 0), int(suffix) + 1)
                continue
            fp = hunk_content_fingerprint(path, hunk.get("header") or "", hunk.get("lines") or [])
            n = seen.get(fp, 0)
            seen[fp] = n + 1
            hunk["decision_id"] = f"{fp}#{n}"
    return files


def resolve_hunk_decision_id(
    hunk: Mapping[str, Any],
    path: str,
    fingerprint_counts: dict[str, int],
) -> str:
    """Resolve the apply/UI decision key for one hunk.

    Prefer the server-assigned ``decision_id``. Legacy reviews without it fall
    back to a deterministic content fingerprint + same-fingerprint ordinal.
    """
    existing = str(hunk.get("decision_id") or "").strip()
    if existing:
        return existing
    fp = hunk_content_fingerprint(path, hunk.get("header") or "", hunk.get("lines") or [])
    n = fingerprint_counts.get(fp, 0)
    fingerprint_counts[fp] = n + 1
    return f"{fp}#{n}"


def decision_for_hunk(decisions: dict, hunk: Mapping[str, Any], path: str = "", fingerprint_counts: dict[str, int] | None = None) -> str:
    """Resolve accept/reject for one hunk via stable decision identity.

    Prefer ``decision_id`` (or the legacy content-fingerprint fallback). Fall
    back to plain ``id`` for payloads built before decision keys existed.
    """
    counts = fingerprint_counts if fingerprint_counts is not None else {}
    did = resolve_hunk_decision_id(hunk, path, counts)
    if did in decisions:
        return decisions.get(did) or "reject"
    hunk_id = str(hunk.get("id") or "")
    if hunk_id and hunk_id in decisions:
        return decisions.get(hunk_id) or "reject"
    return "reject"


def parse_unified_diff(diff_text: str) -> list:
    files = []
    current_file = None
    current_hunk = None

    lines = diff_text.splitlines(keepends=True)

    file_index = -1
    hunk_index = -1

    i = 0
    while i < len(lines):
        line = lines[i]

        if line.startswith("diff --git "):
            file_index += 1
            hunk_index = -1

            path = ""
            parts = line.split(" ")
            if len(parts) >= 4:
                b_part = parts[3]
                if b_part.startswith("b/") or b_part.startswith("\"b/"):
                    path = extract_path(b_part, "b/") if b_part.startswith("b/") else extract_path(b_part, "\"b/")

            current_file = {
                "path": path,
                "headers": [line],
                "hunks": []
            }
            files.append(current_file)
            current_hunk = None
            i += 1
            continue

        if current_file is not None and current_hunk is None:
            if line.startswith("@@ "):
                hunk_index += 1
                current_hunk = {
                    "id": f"{file_index}:{hunk_index}",
                    "header": line,
                    "lines": [],
                    "status": "pending"
                }
                current_file["hunks"].append(current_hunk)
            else:
                if line.startswith("+++ b/") or line.startswith("+++ \"b/"):
                    p = extract_path(line, "+++ ")
                    if p != "/dev/null":
                        current_file["path"] = p
                elif line.startswith("--- a/") or line.startswith("--- \"a/"):
                    p = extract_path(line, "--- ")
                    if p != "/dev/null" and not current_file["path"]:
                        current_file["path"] = p
                current_file["headers"].append(line)
            i += 1
            continue

        if current_file is not None and current_hunk is not None:
            if line.startswith("@@ "):
                hunk_index += 1
                current_hunk = {
                    "id": f"{file_index}:{hunk_index}",
                    "header": line,
                    "lines": [],
                    "status": "pending"
                }
                current_file["hunks"].append(current_hunk)
            elif line.startswith("diff --git "):
                current_hunk = None
                continue
            else:
                current_hunk["lines"].append(line)
            i += 1
            continue

        i += 1

    return assign_decision_ids(files)


def reconstruct_diff(files: list, decisions: dict) -> str:
    out_lines = []
    fingerprint_counts: dict[str, int] = {}
    for f in files:
        path = str(f.get("path") or "")
        accepted_hunks = []
        for h in f["hunks"]:
            dec = decision_for_hunk(decisions, h, path, fingerprint_counts)
            if dec == "accept":
                accepted_hunks.append(h)
        if not accepted_hunks:
            continue

        for h_line in f["headers"]:
            if not h_line.endswith("\n"):
                h_line += "\n"
            out_lines.append(h_line)

        for hunk in accepted_hunks:
            h_header = hunk["header"]
            if not h_header.endswith("\n"):
                h_header += "\n"
            out_lines.append(h_header)
            for hunk_line in hunk["lines"]:
                if not hunk_line.endswith("\n"):
                    hunk_line += "\n"
                out_lines.append(hunk_line)

    return "".join(out_lines)
