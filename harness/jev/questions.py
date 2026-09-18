from __future__ import annotations

"""Jev question text, criteria, and thresholds. Tune only this file."""

MODEL = "~typesafe/jev-latest"
ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
TIMEOUT_SECONDS = 1.5
GATE_THRESHOLD = 0.30
FITS_THRESHOLD = 0.30
SHORTLIST = 3
EXCERPT_CHARS = 700
DESC_CHARS = 120

WHICH_INSTRUCTIONS = (
    "Which of these skills, if any, is the right one to load to help "
    "with the user's latest request in `request`?"
)
RERANK_INSTRUCTIONS = (
    "Exactly one of these skills is the right one to load for the user's "
    "latest request in `request`. Which one? Read what each actually does, "
    "not just its name."
)
GATE_ACTS = (
    "Is the assistant being asked to act on the user's files, accounts, "
    "devices, or online services, rather than only to explain or advise?"
)
GATE_PROCEDURE = (
    "Would a careful expert answering this consult a specific documented "
    "procedure or set of commands, rather than answering from general "
    "understanding?"
)
GATE_PROSE = (
    "Could a knowledgeable generalist fully satisfy this request in prose, "
    "with no tools, no documentation, and no access to the user's files "
    "or accounts?"
)
GATE_ONELINER = (
    "Is this a one-line typo, rename, ack, or single-file nit that should "
    "load no skill?"
)
PLAYBOOK_INSTRUCTIONS = (
    "Which cary-mode playbook should own this turn? Choose none when the "
    "ask is a skill, a one-liner, or casual chat."
)
DEPTH_INSTRUCTIONS = (
    "How deep should orchestration be for `request`? MICRO is a typo, "
    "rename, one-file one-liner, or one-word ack. DEEP is an audit, "
    "find-all, architecture, or codebase-wide ask. STANDARD is everything "
    "else, including recap/status questions."
)
SILLY_HUMANS = (
    "Should the silly-humans stance be in force? Yes only when the user is "
    "asking to beat a claimed-solved GPU, CUDA, local-inference, kernel, or "
    "authorized review of a system they own. A blog post, a JWT decode, or "
    "a casual mention of GPUs is no."
)
LANE_INSTRUCTIONS = (
    "Which execution lane should own this turn after the judgment?"
)

DEPTH_CRITERIA = {
    "MICRO": "Typo, rename/comment, one explicit file, or a one-word ping",
    "STANDARD": "Normal feature, explanation, or recap of existing work",
    "DEEP": "Audit, find-all, refactor, architecture, codebase-wide",
}
LANE_CRITERIA = {
    "inline": "Answer or edit here without a worker or extra skill load",
    "skill": "Load one existing skill and follow it",
    "swarm": "Read-only Puppetmaster swarm / audit",
    "implement": "Coupled multi-file build via Puppetmaster implement",
}
PRODUCT_PLAYBOOKS = {
    "investigation": "Read-only cited explanation; no code",
    "bug-fix": "Defect to reproduce and fix",
    "feature": "New or changed behavior",
    "none": "No playbook; stay inline or use a named skill only",
}


def gate_score(acts, procedure, prose, oneliner):
    """Mean of action signals; prose and oneliner invert."""
    try:
        return (
            float(acts)
            + float(procedure)
            + (1.0 - float(prose))
            + (1.0 - float(oneliner))
        ) / 4.0
    except (TypeError, ValueError):
        return 0.0
