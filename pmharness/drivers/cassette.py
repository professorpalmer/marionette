from __future__ import annotations

"""Record/replay cassette layer for provider driver calls.

Wrap any driver implementing the pmharness Driver protocol. Modes:

  HARNESS_CASSETTE_MODE=record  append interactions to JSON on disk
  HARNESS_CASSETTE_MODE=replay  serve stored responses (no network)
  unset / other                 passthrough

Requires HARNESS_CASSETTE_DIR when mode is record or replay.
"""
import hashlib
import json
import os
import re
import time
from copy import deepcopy
from typing import Any, List, Optional

from .base import DriverResponse, SYSTEM_PROMPT

_SK_PATTERN = re.compile(r"sk-[A-Za-z0-9_-]{8,}")


def maybe_wrap_cassette(driver):
    """Return a CassetteDriver wrapper when cassette mode is active."""
    mode = os.environ.get("HARNESS_CASSETTE_MODE", "").strip().lower()
    if mode not in ("record", "replay"):
        return driver
    cassette_dir = os.environ.get("HARNESS_CASSETTE_DIR", "").strip()
    if not cassette_dir:
        raise RuntimeError(
            "HARNESS_CASSETTE_MODE is set but HARNESS_CASSETTE_DIR is missing"
        )
    return CassetteDriver(driver, mode=mode, cassette_dir=cassette_dir)


def _sanitize_driver_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "driver")
    return cleaned.strip("._") or "driver"


def _normalize_messages(messages: Optional[list]) -> list:
    out: list[dict[str, str]] = []
    tool_index = 0
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "")
        content = msg.get("content")
        if content is None:
            content = ""
        if not isinstance(content, str):
            content = json.dumps(content, sort_keys=True, default=str)
        entry: dict[str, str] = {"role": role, "content": content}
        if msg.get("tool_call_id") is not None:
            entry["tool_call_id"] = str(tool_index)
            tool_index += 1
        out.append(entry)
    return out


def _tool_names(tools: Optional[list]) -> list:
    names: list[str] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = fn.get("name") if isinstance(fn, dict) else tool.get("name")
        if name:
            names.append(str(name))
    return sorted(names)


def request_hash(
    method: str,
    model: str,
    messages: Optional[list],
    tools: Optional[list] = None,
) -> str:
    payload = {
        "method": method,
        "model": model or "",
        "messages": _normalize_messages(messages),
        "tool_names": _tool_names(tools),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _collect_secret_literals() -> list[str]:
    secrets: list[str] = []
    for key, value in os.environ.items():
        if not value:
            continue
        upper = key.upper()
        if "KEY" in upper or "TOKEN" in upper or "SECRET" in upper:
            if len(value.strip()) >= 8:
                secrets.append(value.strip())
    return secrets


def _scrub_value(value: Any, secrets: list[str], scrubbed: set) -> Any:
    if isinstance(value, str):
        out = value
        for secret in secrets:
            if secret and secret in out:
                out = out.replace(secret, "[REDACTED]")
                scrubbed.add("env_secret")
        if _SK_PATTERN.search(out):
            out = _SK_PATTERN.sub("[REDACTED]", out)
            scrubbed.add("sk_pattern")
        return out
    if isinstance(value, list):
        return [_scrub_value(v, secrets, scrubbed) for v in value]
    if isinstance(value, dict):
        return {k: _scrub_value(v, secrets, scrubbed) for k, v in value.items()}
    return value


def _scrub_interaction(interaction: dict) -> tuple[dict, list[str]]:
    secrets = _collect_secret_literals()
    scrubbed: set[str] = set()
    cleaned = _scrub_value(deepcopy(interaction), secrets, scrubbed)
    return cleaned, sorted(scrubbed)


class CassetteDriver:
    def __init__(self, inner, *, mode: str, cassette_dir: str) -> None:
        self._inner = inner
        self.mode = mode
        self.cassette_dir = os.path.abspath(cassette_dir)
        os.makedirs(self.cassette_dir, exist_ok=True)
        inner_name = getattr(inner, "name", inner.__class__.__name__)
        self.name = f"cassette({inner_name})"
        self._path = os.path.join(
            self.cassette_dir,
            f"{_sanitize_driver_name(inner_name)}.json",
        )
        self._data = self._load()

    @property
    def model(self) -> str:
        return getattr(self._inner, "model", "")

    def _load(self) -> dict:
        if not os.path.isfile(self._path):
            return {
                "version": 1,
                "driver": getattr(self._inner, "name", ""),
                "recorded_at": "",
                "interactions": [],
            }
        with open(self._path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError(f"invalid cassette file: {self._path}")
        data.setdefault("interactions", [])
        return data

    def _save(self) -> None:
        self._data["driver"] = getattr(self._inner, "name", "")
        if not self._data.get("recorded_at"):
            self._data["recorded_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(self._path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(self._data, fh, indent=2, sort_keys=True)

    def _lookup(self, req_hash: str) -> Optional[dict]:
        for item in self._data.get("interactions") or []:
            if item.get("request_hash") == req_hash:
                return item
        return None

    def _record(self, req_hash: str, method: str, request: dict, response: DriverResponse) -> DriverResponse:
        interaction = {
            "request_hash": req_hash,
            "method": method,
            "request": request,
            "response": {
                "text": response.text,
                "tokens_in": response.tokens_in,
                "tokens_out": response.tokens_out,
                "model": response.model or getattr(self._inner, "name", ""),
            },
        }
        scrubbed, fields = _scrub_interaction(interaction)
        scrubbed["scrubbed_fields"] = fields
        self._data.setdefault("interactions", []).append(scrubbed)
        self._save()
        return response

    def _replay(self, req_hash: str) -> DriverResponse:
        item = self._lookup(req_hash)
        if item is None:
            raise KeyError(
                f"cassette miss for hash {req_hash} in {self._path}"
            )
        resp = item.get("response") or {}
        return DriverResponse(
            text=str(resp.get("text") or ""),
            tokens_in=int(resp.get("tokens_in") or 0),
            tokens_out=int(resp.get("tokens_out") or 0),
            latency_ms=0.0,
            model=str(resp.get("model") or getattr(self._inner, "name", "")),
        )

    def complete(self, task_prompt: str, *, system: str = SYSTEM_PROMPT) -> DriverResponse:
        method = "complete"
        model = getattr(self._inner, "model", getattr(self._inner, "name", ""))
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": task_prompt},
        ]
        req_hash = request_hash(method, model, messages)
        if self.mode == "replay":
            return self._replay(req_hash)
        response = self._inner.complete(task_prompt, system=system)
        if self.mode == "record":
            return self._record(
                req_hash,
                method,
                {"messages": _normalize_messages(messages), "tools": []},
                response,
            )
        return response

    def chat(
        self,
        messages: list,
        *,
        tools: Optional[list] = None,
        system: Optional[str] = None,
    ) -> DriverResponse:
        method = "chat"
        model = getattr(self._inner, "model", getattr(self._inner, "name", ""))
        req_hash = request_hash(method, model, messages, tools)
        if self.mode == "replay":
            return self._replay(req_hash)
        response = self._inner.chat(messages, tools=tools, system=system)
        if self.mode == "record":
            return self._record(
                req_hash,
                method,
                {
                    "messages": _normalize_messages(messages),
                    "tools": _tool_names(tools),
                },
                response,
            )
        return response
