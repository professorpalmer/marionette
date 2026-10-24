"""Fake OpenAI-compatible model server.

A user line "TOOL <name> <json args>" makes the model call that tool. With
several TOOL lines, the model calls them one per step. Otherwise it echoes the
last user text and the last tool result.
"""
from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _text(c):
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return str(c or "")


def _plan(messages):
    """Return (tool_call or None, echo text)."""
    last_user = -1
    for i, m in enumerate(messages or []):
        if m.get("role") == "user" and "TOOL " in _text(m.get("content")) or (
                m.get("role") == "user" and "CNRY" in _text(m.get("content"))):
            last_user = i
    if last_user < 0:
        for i, m in enumerate(messages or []):
            if m.get("role") == "user":
                last_user = i
    if last_user < 0:
        return None, "ECHO: (none)"
    user_text = _text(messages[last_user].get("content"))
    tools_done = sum(1 for m in messages[last_user + 1:] if m.get("role") == "tool")
    lines = [ln[5:] for ln in user_text.splitlines() if ln.startswith("TOOL ")]
    if tools_done < len(lines):
        name, _, args = lines[tools_done].partition(" ")
        return {"name": name, "arguments": args or "{}", "id": "call_%d_%d" % (last_user, tools_done)}, ""
    tail = ""
    for m in reversed(messages[last_user + 1:]):
        if m.get("role") == "tool":
            tail = " | TOOLRESULT: " + _text(m.get("content"))[:300]
            break
    return None, "ECHO: " + user_text.split("\n[context")[0][:400] + tail


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._json({"data": [{"id": "glm-4.7-flash", "object": "model"}]})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(n) or b"{}")
        call, text = _plan(req.get("messages"))
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        if not req.get("stream"):
            msg = {"role": "assistant", "content": text or None}
            if call:
                msg["tool_calls"] = [{"id": call["id"], "type": "function",
                                      "function": {"name": call["name"], "arguments": call["arguments"]}}]
            self._json({"id": "c1", "object": "chat.completion", "model": "glm-4.7-flash",
                        "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if call else "stop"}],
                        "usage": usage})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        base = {"id": "c1", "object": "chat.completion.chunk", "created": int(time.time()), "model": "glm-4.7-flash"}

        def send(delta, finish=None, extra=None):
            chunk = dict(base, choices=[{"index": 0, "delta": delta, "finish_reason": finish}], **(extra or {}))
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")

        if call:
            send({"role": "assistant", "tool_calls": [{"index": 0, "id": call["id"], "type": "function",
                                                       "function": {"name": call["name"], "arguments": ""}}]})
            send({"tool_calls": [{"index": 0, "function": {"arguments": call["arguments"]}}]})
            send({}, "tool_calls", {"usage": usage})
        else:
            send({"role": "assistant", "content": text[: len(text) // 2]})
            send({"content": text[len(text) // 2:]})
            send({}, "stop", {"usage": usage})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def serve(port: int = 0) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), H)


if __name__ == "__main__":
    serve(int(sys.argv[1])).serve_forever()
