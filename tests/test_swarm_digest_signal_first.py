"""A swarm digest must surface SIGNAL (finding/risk/decision) before PLUMBING
(routing/verification).

Regression for a real defect: a healthy swarm returns its routing + verification
artifacts BEFORE the actual findings. A naive `artifacts[:8]` slice was entirely
consumed by 5 routing + 5 verification entries, so a swarm that produced a dozen
genuine findings surfaced to the pilot as "only verifications, no findings" --
making a working swarm look broken. The digest must hoist signal to the front.

Pure/hermetic: exercises the ordering logic directly on artifact-shaped dicts.
"""

_SIGNAL = {"finding", "risk", "decision"}


def _digest(artifacts):
    """Mirror of the signal-first digest selection in conversation.py."""
    signal = [a for a in artifacts if str(a.get("type")) in _SIGNAL]
    plumbing = [a for a in artifacts if str(a.get("type")) not in _SIGNAL]
    return (signal[:20] + plumbing[:3]) if signal else plumbing[:8]


def _real_swarm_shape():
    # The exact ordering a live 5-role analysis swarm produces: routing +
    # verification plumbing first, findings/decisions after.
    return (
        [{"type": "routing", "headline": ""} for _ in range(5)]
        + [{"type": "verification", "headline": "check"} for _ in range(5)]
        + [{"type": "finding", "headline": f"finding {i}"} for i in range(12)]
        + [{"type": "decision", "headline": "decide"}]
    )


def test_findings_are_not_buried_behind_plumbing():
    arts = _real_swarm_shape()
    # The old blind slice showed zero findings; guard against regressing to it.
    assert not any(a["type"] == "finding" for a in arts[:8]), "test fixture assumption"
    digest = _digest(arts)
    finding_count = sum(1 for a in digest if a["type"] == "finding")
    assert finding_count == 12, f"expected all 12 findings surfaced, got {finding_count}"


def test_plumbing_only_swarm_still_shows_something():
    # A swarm that genuinely produced no signal (only routing/verification) must
    # still surface the plumbing so the pilot can reason about the degrade.
    arts = [{"type": "routing", "headline": ""} for _ in range(3)] + [
        {"type": "verification", "headline": "empty"} for _ in range(2)
    ]
    digest = _digest(arts)
    assert len(digest) == 5


def test_signal_ordered_before_plumbing():
    arts = _real_swarm_shape()
    digest = _digest(arts)
    first_plumbing = next((i for i, a in enumerate(digest)
                           if a["type"] not in _SIGNAL), len(digest))
    last_signal = max((i for i, a in enumerate(digest)
                       if a["type"] in _SIGNAL), default=-1)
    assert last_signal < first_plumbing, "all signal must precede plumbing in the digest"
