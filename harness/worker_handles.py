from __future__ import annotations

"""Handle-first formatting for worker/swarm results (token-cheap recall).

Default model-visible swarm results carry job_id + a few headlines +
``artifact://`` URIs so the pilot FETCHes full bodies via peek_artifact /
read_file instead of pasting fat digests into history.
"""

from typing import Any, Mapping, Optional, Sequence

from .local_job_artifacts import finding_artifact_id

_DEFAULT_MAX_HEADLINES = 3
_DEFAULT_MAX_CHARS = 1500
_FETCH_HINT = (
    "FETCH full bodies with peek_artifact or read_file on the artifact:// URIs."
)


def _artifact_id(job_id: str, artifact: Mapping[str, Any], index: int) -> str:
    explicit = str(artifact.get("id") or "").strip()
    if explicit:
        return explicit
    return finding_artifact_id(job_id, index)


def _headline(artifact: Mapping[str, Any]) -> str:
    text = str(
        artifact.get("headline")
        or artifact.get("claim")
        or artifact.get("summary")
        or ""
    ).strip()
    return text or "(no headline)"


def format_handle_first_result(
    job_id: str,
    arts: Optional[Sequence[Mapping[str, Any]]],
    *,
    max_headlines: int = _DEFAULT_MAX_HEADLINES,
    max_chars: int = _DEFAULT_MAX_CHARS,
    fetch_hint: bool = True,
) -> str:
    """Compact handle-first swarm/worker result for model-visible history.

    Always includes ``job_id`` and (when ``fetch_hint``) a FETCH reminder.
    Headlines cite ``artifact://`` URIs so full bodies remain directly addressable.
    """
    jid = str(job_id or "").strip() or "unknown"
    lines = [f"job_id={jid}"]
    rows = [a for a in (arts or ()) if isinstance(a, Mapping)]
    if not rows:
        lines.append("  (no artifacts)")
    else:
        limit = max(0, int(max_headlines))
        for index, art in enumerate(rows[:limit]):
            kind = str(art.get("type") or "finding").strip() or "finding"
            aid = _artifact_id(jid, art, index)
            uri = f"artifact://{jid}/{aid}"
            headline = _headline(art)
            if len(headline) > 160:
                headline = headline[:157] + "..."
            lines.append(f"  - [{kind}] {headline}")
            lines.append(f"    {uri}")
        omitted = len(rows) - limit
        if omitted > 0:
            lines.append(f"  ... (+{omitted} more; FETCH via artifact://)")
    if fetch_hint:
        lines.append(_FETCH_HINT)
    text = "\n".join(lines)
    cap = max(64, int(max_chars))
    if len(text) > cap:
        text = text[: cap - 3] + "..."
    return text
