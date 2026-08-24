#!/usr/bin/env python3
"""Chrome native messaging host for the browser-relay snapshot shape.

Reads length-prefixed JSON from stdin (Chrome native messaging) and POSTs the
same object to the harness /api/browser/relay endpoint. Off unless the harness
has PM_BROWSER_RELAY enabled.
"""
from __future__ import annotations

import json
import os
import struct
import sys
import urllib.error
import urllib.request


def read_native_message() -> dict:
    header = sys.stdin.buffer.read(4)
    if len(header) < 4:
        raise EOFError("no native-host frame")
    (length,) = struct.unpack("<I", header)
    raw = sys.stdin.buffer.read(length)
    return json.loads(raw.decode("utf-8"))


def main() -> int:
    try:
        body = read_native_message()
    except Exception:
        return 1
    if "kind" not in body and "type" not in body:
        body["kind"] = "tab_snapshot"
    body.setdefault("source", "native_host")
    url = (os.environ.get("HARNESS_RELAY_URL") or "http://127.0.0.1:8765").rstrip("/")
    token = (os.environ.get("HARNESS_TOKEN") or "").strip()
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Harness-Token"] = token
    req = urllib.request.Request(
        url + "/api/browser/relay", data=data, headers=headers, method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except (urllib.error.URLError, TimeoutError, OSError):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
