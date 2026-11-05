"""Gemini thought_signature round-trip. Gemini 3+ returns a thoughtSignature with
each functionCall and REQUIRES it echoed back in the next turn's history, or the
API rejects with HTTP 400 "Function call is missing a thought_signature".
Parse capture is covered in test_gemini_streaming.py. These tests prove that
GeminiDriver.chat echoes the stored signature back on send.
"""
import io
import json
import urllib.request

from pmharness.drivers.gemini import GeminiDriver


def _send_history(monkeypatch, tool_call):
    driver = GeminiDriver("gemini-3.5-flash", "gemini-3.5-flash")
    driver._key = lambda: "fake-gemini-key"
    captured = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({
                "candidates": [{"content": {"parts": [{"text": "ok"}], "role": "model"},
                                "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
            }).encode("utf-8")

    def mock_urlopen(req, timeout=None):
        captured.append(req)
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)
    history = [
        {"role": "user", "content": "list the dir"},
        {"role": "assistant", "content": "", "tool_calls": [tool_call]},
        {"role": "tool", "tool_call_id": tool_call["id"], "content": "a.txt"},
    ]
    driver.chat(history)
    assert len(captured) == 1
    body = json.loads(captured[0].data.decode("utf-8"))
    model_turns = [c for c in body["contents"] if c["role"] == "model"]
    assert len(model_turns) == 1
    return model_turns[0]["parts"][0]


def test_history_build_echoes_signature_back(monkeypatch):
    part = _send_history(monkeypatch, {
        "id": "call_list_dir_0", "type": "function",
        "function": {"name": "list_dir", "arguments": json.dumps({"path": "."})},
        "thought_signature": "SIG_ABC123",
    })
    assert part == {
        "functionCall": {"name": "list_dir", "args": {"path": "."}},
        "thoughtSignature": "SIG_ABC123",
    }


def test_no_signature_is_safe(monkeypatch):
    # A stored call without a signature must not get a fabricated one.
    part = _send_history(monkeypatch, {
        "id": "call_read_file_0", "type": "function",
        "function": {"name": "read_file", "arguments": "{}"},
    })
    assert part == {"functionCall": {"name": "read_file", "args": {}}}
