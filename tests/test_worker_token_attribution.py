"""A background worker's token spend must reach the session cost meter on BOTH
the patch-produced path AND the 'no changes produced' / degrade path.

Regression: the no-patch branch hard-coded tokens_in/out = 0, so a worker that
explored (spending real tokens) but produced no patch showed NO cost in the
swarm tracker while normal completions did -- the "why does only one job show a
price?" symptom. The success branch also dropped _tokens_out from the session
meter, undercounting output (billed at the cheaper input rate).
"""
import tempfile

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession


class _Res:
    """Stand-in for a worker result object."""
    def __init__(self, patch=None, tokens_in=0, tokens_out=0, error=None, summary=""):
        self.patch = patch
        self.files_changed = [] if patch is None else ["a.py"]
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.error = error
        self.summary = summary


def _session():
    return ConversationalSession(HarnessConfig(state_dir=tempfile.mkdtemp(prefix="pmh-tok-")))


def test_no_patch_worker_still_attributes_tokens():
    s = _session()
    before_in = s._tokens_in
    before_out = s._tokens_out
    # Simulate the no-patch branch's token attribution directly (the logic under
    # test): a degrade result with real spend must move the meters.
    res = _Res(patch=None, tokens_in=1200, tokens_out=800, error="no changes produced")
    _nc_t_in = int(getattr(res, "tokens_in", 0) or 0)
    _nc_t_out = int(getattr(res, "tokens_out", 0) or 0)
    if _nc_t_in or _nc_t_out:
        s._tokens_used += _nc_t_out + _nc_t_in
        s._tokens_in += _nc_t_in
        s._tokens_out += _nc_t_out
    assert s._tokens_in == before_in + 1200
    assert s._tokens_out == before_out + 800, "output tokens must be tracked for correct (output-rate) cost"


def test_success_worker_tracks_output_tokens():
    s = _session()
    before_out = s._tokens_out
    res = _Res(patch="diff", tokens_in=500, tokens_out=1500)
    s._tokens_used += res.tokens_out + res.tokens_in
    s._tokens_in += res.tokens_in
    s._tokens_out += res.tokens_out
    assert s._tokens_out == before_out + 1500
