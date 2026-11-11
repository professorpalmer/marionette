"""Metering proxy for leaderboard-style runs of Marionette.

It copies the rules of the Harness Leaderboard meter
(huggingface.co/spaces/joelniklaus/harness-leaderboard):

- One model for every call. The proxy replaces the model id in each request.
- One reasoning effort for every call, set by the proxy.
- When a request has no reply limit, the proxy sends the model's longest reply.
- Each call writes one JSONL row with token counts, cost, provider and latency.

Upstream is OpenRouter, pinned to one provider tag with no fallback, so every
call reaches the same weights and precision. The agent gets a local token that
can only call this proxy. The real key stays on the host.

Run:  python bench/harbor/meter.py --port 8790 --log meter.jsonl
Key:  OPENROUTER_API_KEY in the environment.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import threading
import time
import urllib.error
import urllib.request

UPSTREAM = "https://openrouter.ai/api/v1/chat/completions"
_LOG_LOCK = threading.Lock()


class MeterConfig:
    def __init__(self, args: argparse.Namespace) -> None:
        self.model = args.model
        self.provider = args.provider
        self.effort = args.effort
        self.max_output = args.max_output
        self.log_path = args.log
        self.key = os.environ.get("OPENROUTER_API_KEY", "")
        if not self.key:
            raise SystemExit("OPENROUTER_API_KEY is not set")


def rewrite_body(body: dict, cfg: MeterConfig) -> dict:
    """The request the upstream gets: model, effort, provider pin, reply limit."""
    out = dict(body)
    out["model"] = cfg.model
    out.pop("reasoning_effort", None)
    out["reasoning"] = {"effort": cfg.effort}
    out["provider"] = {"only": [cfg.provider], "allow_fallbacks": False}
    out["usage"] = {"include": True}
    if not any(out.get(k) for k in ("max_tokens", "max_completion_tokens")):
        out["max_tokens"] = cfg.max_output
    if out.get("stream"):
        opts = dict(out.get("stream_options") or {})
        opts["include_usage"] = True
        out["stream_options"] = opts
    return out


def trial_tag(path: str) -> str:
    """The trial name in ``/t/<trial>/v1/...``, or empty."""
    parts = path.strip("/").split("/")
    return parts[1] if len(parts) > 2 and parts[0] == "t" else ""


def request_params(body: dict) -> dict:
    """The scalar sampling fields the agent sent, plus replayed reasoning."""
    keep = {k: v for k, v in body.items() if k not in ("messages", "tools", "model") and not isinstance(v, (dict, list))}
    replay = sum(1 for m in body.get("messages") or [] if isinstance(m, dict) and m.get("reasoning_content"))
    if replay:
        keep["reasoning_replayed"] = replay
    return keep


def usage_row(usage: dict | None) -> dict:
    usage = usage or {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    reply_details = usage.get("completion_tokens_details") or {}
    return {
        "prompt_tokens": usage.get("prompt_tokens"),
        "cached_tokens": prompt_details.get("cached_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": reply_details.get("reasoning_tokens"),
        "cost": usage.get("cost"),
    }


class Handler(http.server.BaseHTTPRequestHandler):
    cfg: MeterConfig
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # quiet access log
        return

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {"object": "list", "data": [{"id": self.cfg.model, "object": "model"}]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._json(404, {"error": "only /v1/chat/completions"})
            return
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            body = json.loads(raw)
        except ValueError:
            self._json(400, {"error": "body is not JSON"})
            return
        sent = rewrite_body(body, self.cfg)
        row = {"ts": time.time(), "trial": trial_tag(self.path), "params": request_params(body),
               "asked_model": body.get("model"), "stream": bool(sent.get("stream")),
               "asked_max_tokens": body.get("max_tokens") or body.get("max_completion_tokens"),
               "n_messages": len(body.get("messages") or []), "n_tools": len(body.get("tools") or [])}
        started = time.monotonic()
        req = urllib.request.Request(UPSTREAM, json.dumps(sent).encode(), {
            "Authorization": "Bearer " + self.cfg.key, "Content-Type": "application/json"})
        try:
            upstream = urllib.request.urlopen(req, timeout=1800)
        except urllib.error.HTTPError as exc:
            detail = exc.read()
            row.update(status=exc.code, error=detail[:500].decode(errors="replace"))
            self._finish(row, started)
            self.send_response(exc.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(detail)))
            self.end_headers()
            self.wfile.write(detail)
            return
        except Exception as exc:  # connection failure: tell the agent, keep the row
            row.update(status=502, error=str(exc)[:500])
            self._finish(row, started)
            self._json(502, {"error": {"message": "meter upstream failed: %s" % exc}})
            return
        with upstream:
            if sent.get("stream"):
                self._relay_stream(upstream, row, started)
            else:
                data = upstream.read()
                try:
                    reply = json.loads(data)
                except ValueError:
                    reply = {}
                row.update(status=upstream.status, provider=reply.get("provider"),
                           finish=((reply.get("choices") or [{}])[0]).get("finish_reason"),
                           **usage_row(reply.get("usage")))
                self._finish(row, started)
                self.send_response(upstream.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

    def _relay_stream(self, upstream, row: dict, started: float) -> None:
        self.send_response(upstream.status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        first_token = None
        usage = None
        finish = None
        provider = None
        try:
            for line in upstream:
                if line.startswith(b"data: ") and line.strip() != b"data: [DONE]":
                    try:
                        chunk = json.loads(line[6:])
                    except ValueError:
                        chunk = {}
                    if first_token is None and chunk.get("choices"):
                        first_token = time.monotonic() - started
                    usage = chunk.get("usage") or usage
                    provider = chunk.get("provider") or provider
                    for choice in chunk.get("choices") or []:
                        finish = choice.get("finish_reason") or finish
                self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            row["status"] = upstream.status
        except (BrokenPipeError, ConnectionResetError):
            # The agent stops reading once it has its tool call. Usage is
            # already captured from the stream at this point.
            row.update(status=upstream.status, client_closed=True)
        except Exception as exc:  # upstream dropped mid-stream
            row.update(status=499, error=str(exc)[:300])
        row.update(provider=provider, finish=finish, first_token_s=first_token, **usage_row(usage))
        self._finish(row, started)

    def _finish(self, row: dict, started: float) -> None:
        row["seconds"] = round(time.monotonic() - started, 3)
        with _LOG_LOCK, open(self.cfg.log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--model", default="deepseek/deepseek-v4.1-flash")
    ap.add_argument("--provider", default="deepinfra/fp8")
    ap.add_argument("--effort", default="high", choices=["low", "high"])
    ap.add_argument("--max-output", type=int, default=393216)
    ap.add_argument("--log", default="meter.jsonl")
    args = ap.parse_args()
    Handler.cfg = MeterConfig(args)
    server = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print("meter on %s:%d -> %s via %s, effort %s" % (args.host, args.port, args.model, args.provider, args.effort), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
