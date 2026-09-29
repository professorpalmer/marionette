"""An invalid pilot envelope gets one correction, not a retry per step."""
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from pmharness.drivers.openai_compat import DriverResponse

BAD = '{"actions":[{"kind":"run_swarm"}]}'  # run_swarm without a goal


class _Pilot:
    name = "envelope-pilot"

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def complete(self, prompt, *, system=None):
        self.prompts.append(prompt)
        text = self.replies.pop(0) if self.replies else BAD
        return DriverResponse(text=text, tokens_out=5, latency_ms=1.0)


def _session(tmp_path, monkeypatch, pilot):
    monkeypatch.setenv("HARNESS_MAX_PILOT_STEPS", "40")
    s = ConversationalSession(HarnessConfig(driver="stub-oracle-v2", state_dir=str(tmp_path)))
    s.pilot = pilot
    return s


def test_repeated_invalid_envelope_stops_after_one_correction(tmp_path, monkeypatch):
    pilot = _Pilot([])
    s = _session(tmp_path, monkeypatch, pilot)
    events = list(s.send("do it"))
    assert len(pilot.prompts) == 2
    assert any("No productive reply" in str(e.data.get("text", "")) for e in events if e.kind == "message")


def test_correction_shows_the_rejected_reply_and_recovers(tmp_path, monkeypatch):
    pilot = _Pilot([BAD, '{"say":"Fixed.","actions":[]}'])
    s = _session(tmp_path, monkeypatch, pilot)
    events = list(s.send("do it"))
    assert len(pilot.prompts) == 2
    assert BAD in str(pilot.prompts[1])
    assert any("Fixed." in str(e.data.get("text", "")) for e in events if e.kind == "message")
