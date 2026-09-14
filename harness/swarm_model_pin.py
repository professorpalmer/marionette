from __future__ import annotations

"""Resolve exact native Codex pins and enabled direct provider/model rows."""

import re
from dataclasses import dataclass
from typing import Any, Optional

from .diag import note as _diag

_PIN_PROVIDER_ALIASES = {
    "chatgpt-codex": "openai-codex",
    "codex-plan": "openai-codex",
}

# ChatGPT Codex OAuth (OPENAI_CODEX_TOKEN) rejects gpt-5.6-*-pro with HTTP 400.
# Luna/Sol/Terra Max = base id + reasoning_effort=max — never the -pro slug.
_CODEX_OAUTH_PRO_BASE = {
    "gpt-5.6-luna-pro": "gpt-5.6-luna",
    "gpt-5.6-sol-pro": "gpt-5.6-sol",
    "gpt-5.6-terra-pro": "gpt-5.6-terra",
}
_LUNA_MAX_PIN_ALIASES = frozenset({
    "luna max",
    "gpt luna max",
    "gpt-luna-max",
    "luna-max",
    "gpt-5.6-luna-max",
    "gpt-5-6-luna-max",
    "max-tier luna",
    "max tier luna",
})


def _bare_model_tail(pin: str) -> str:
    """Last path/colon segment of a pin, lowercased."""
    body = (pin or "").strip()
    if not body:
        return ""
    if ":" in body:
        body = body.split(":", 1)[1].strip() or body
    if "/" in body:
        body = body.rsplit("/", 1)[-1].strip() or body
    return body.lower()


def is_luna_max_pin(pin: str) -> bool:
    """True when the pilot/user named Luna Max (max effort), not Luna Pro."""
    raw = (pin or "").strip().lower()
    if not raw:
        return False
    if raw in _LUNA_MAX_PIN_ALIASES:
        return True
    compact = re.sub(r"[\s_]+", " ", raw).strip()
    if compact in _LUNA_MAX_PIN_ALIASES:
        return True
    bare = _bare_model_tail(pin)
    if bare in _LUNA_MAX_PIN_ALIASES or bare.endswith("luna-max"):
        return True
    # Free-text phrases inside longer pins / labels.
    if "luna max" in compact or "gpt luna max" in compact:
        return True
    return False


def remap_codex_oauth_pro_model(model_id: str) -> tuple[str, str]:
    """Remap ChatGPT Codex OAuth *-pro ids to the supported base slug.

    Returns ``(model, reason)``. Reason is empty when unchanged. Prefer remap
    over reject so Luna Max pins that wrongly resolved to luna-pro still
    dispatch as gpt-5.6-luna (caller keeps reasoning_effort).
    """
    raw = (model_id or "").strip()
    if not raw:
        return "", ""
    bare = _bare_model_tail(raw)
    base = _CODEX_OAUTH_PRO_BASE.get(bare)
    if not base:
        # Also catch hyphen/dot variants of the same pro tails.
        for pro, target in _CODEX_OAUTH_PRO_BASE.items():
            if bare == pro or bare.endswith("/" + pro) or bare.endswith(":" + pro):
                base = target
                break
    if not base:
        return raw, ""
    # Preserve any provider/agentic prefix, swap only the model tail.
    if ":" in raw:
        prov, _rest = raw.split(":", 1)
        return f"{prov}:{base}", f"codex_oauth_pro_remap:{bare}->{base}"
    if "/" in raw:
        head, _tail = raw.rsplit("/", 1)
        # agentic/openai-codex/gpt-5.6-luna-pro → agentic/openai-codex/gpt-5.6-luna
        return f"{head}/{base}", f"codex_oauth_pro_remap:{bare}->{base}"
    return base, f"codex_oauth_pro_remap:{bare}->{base}"


def normalize_swarm_model_pin_request(pin: str) -> tuple[str, dict[str, str]]:
    """Normalize pilot-supplied swarm pins before catalog resolution.

    - Luna Max / GPT Luna Max → gpt-5.6-luna (effort=max is separate).
    - ChatGPT Codex OAuth *-pro → base family id (keep effort).
    """
    requested = (pin or "").strip()
    meta: dict[str, str] = {}
    if not requested:
        return "", meta
    if _parse_pin_provider_model(requested)[0] == "codex":
        return requested, meta
    if is_luna_max_pin(requested):
        # Preserve an explicit openai-codex / agentic provider prefix when present.
        prov, _model = _parse_pin_provider_model(requested)
        if prov in ("openai-codex", "codex", "chatgpt-codex", "codex-plan"):
            out = f"{_normalize_pin_provider(prov)}:gpt-5.6-luna"
        elif requested.lower().startswith("agentic/openai-codex/"):
            out = "agentic/openai-codex/gpt-5.6-luna"
        elif requested.lower().startswith("agentic/"):
            out = "agentic/gpt-5.6-luna"
        else:
            out = "gpt-5.6-luna"
        meta["luna_max"] = "1"
        meta["reasoning_effort_hint"] = "max"
        meta["normalize"] = f"luna_max->{out}"
        _diag("swarm_model_pin.luna_max", msg=meta["normalize"])
        return out, meta
    remapped, reason = remap_codex_oauth_pro_model(requested)
    if reason:
        meta["codex_pro_remap"] = reason
        _diag("swarm_model_pin.codex_pro_remap", msg=reason)
        return remapped, meta
    return requested, meta


@dataclass(frozen=True)
class AgenticModelPin:
    """Immutable worker identity; direct providers default to the agentic adapter."""

    requested: str
    provider: str
    model: str
    router_model_id: str
    reason: str = "exact"
    policy: str = "explicit_pin"
    adapter: str = "agentic"
    reasoning_effort: str = ""

    def payload_fields(self) -> dict[str, Any]:
        if self.adapter == "codex":
            return {
                "model": self.model, "auto_route": False,
                "allowed_adapters": ["codex"], "pinned_adapter": "codex",
                "pinned_model": self.router_model_id,
                "pinned_adapter_model_name": self.model,
                "requested_model": self.requested, "pin_policy": self.policy,
                **({"reasoning_effort": self.reasoning_effort} if self.reasoning_effort else {}),
            }
        return {
            "provider": self.provider,
            "model": self.model,
            "auto_route": False,
            "allowed_adapters": ["agentic"],
            "pinned_adapter": "agentic",
            "pinned_model": self.router_model_id,
            "pinned_adapter_model_name": self.model,
            "pin_policy": self.policy,
            "requested_model": self.requested,
        }


def _normalize_pin_provider(provider: str) -> str:
    raw = (provider or "").strip().lower()
    return _PIN_PROVIDER_ALIASES.get(raw, raw)


def _parse_pin_provider_model(pin: str) -> tuple[str, str]:
    """Best-effort ``(provider, model)`` from a pilot-supplied pin."""
    body = (pin or "").strip()
    if body.lower().startswith("agentic/"):
        body = body.split("/", 1)[1].strip()
    if ":" in body:
        provider, model = body.split(":", 1)
        return _normalize_pin_provider(provider), model.strip()
    known = {
        "cursor",
        "cursor-cli",
        "codex",
        "openai",
        "openai-codex",
        "opencode-go",
        "opencode-zen",
        "openrouter",
        "native",
    }
    if "/" in body:
        head, rest = body.split("/", 1)
        if head.lower() in known:
            return _normalize_pin_provider(head), rest.strip()
    return "", body


def _same_pin_model(left: str, right: str) -> bool:
    """Exact model id, or the OpenCode Go DeepSeek flash live/curated pair."""
    a = (left or "").strip().lower()
    b = (right or "").strip().lower()
    if not a or not b:
        return False
    if a == b:
        return True
    try:
        from .opencode_go import same_go_flash_model
        return same_go_flash_model(a, b)
    except Exception:
        return False


def settings_enabled_pin_specs(
    pin: str,
    *,
    enabled: Optional[list[str]] = None,
) -> list[str]:
    """Settings specs matching the requested provider and exact model ID."""
    requested = (pin or "").strip()
    if not requested:
        return []
    if enabled is None:
        try:
            from .model_visibility import get_enabled
            enabled = get_enabled()
        except Exception as exc:
            _diag("swarm_model_pin.enabled_specs", exc)
            enabled = []
    pin_provider, pin_model = _parse_pin_provider_model(requested)
    pin_model_l = (pin_model or "").strip().lower()
    out: list[str] = []
    seen: set[str] = set()
    for spec in enabled or []:
        raw = str(spec or "").strip()
        if ":" not in raw:
            continue
        prov, mid = raw.split(":", 1)
        prov = _normalize_pin_provider(prov)
        mid = mid.strip()
        if not prov or not mid:
            continue
        if pin_provider and prov != pin_provider:
            continue
        exact = bool(pin_model_l) and _same_pin_model(mid, pin_model_l)
        if not exact:
            continue
        key = raw.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(raw)
    return out


def _registry_rows(*, adapters: Optional[set[str]] = None) -> list[dict]:
    try:
        from .registry_wizard import get_models_file_path
        import json
        import os

        path = get_models_file_path()
        if not path or not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, list):
            return []
        allow = adapters
        out: list[dict] = []
        for row in models:
            if not isinstance(row, dict):
                continue
            adapter = str(row.get("adapter") or "").strip().lower()
            if allow is not None and adapter not in allow:
                continue
            out.append(row)
        return out
    except Exception as e:
        _diag("swarm_model_pin.registry_rows", e)
        return []


def _row_is_keyed_agentic(row: dict, keyed: set) -> bool:
    adapter = str(row.get("adapter") or "").strip().lower()
    if adapter != "agentic":
        return True
    defaults = row.get("payload_defaults")
    provider = ""
    if isinstance(defaults, dict):
        provider = str(defaults.get("provider") or "").strip()
    # An empty keyed set is authoritative: an agentic row is never usable
    # merely because it exists in the static registry.
    return bool(provider) and provider in {
        str(item).strip().lower() for item in (keyed or set()) if str(item).strip()
    }


def _settings_enabled_specs() -> Optional[list[str]]:
    """Return curated settings, preserving the distinction from unset."""
    try:
        from . import model_visibility
        current = model_visibility.get_enabled()
        if current:
            return [str(x).strip() for x in current if str(x).strip()]
        data = model_visibility._load()
        if isinstance(data, dict) and "enabled" in data:
            enabled = data.get("enabled")
            if isinstance(enabled, list):
                return [str(x).strip() for x in enabled if str(x).strip()]
            return []
    except Exception as exc:
        _diag("swarm_model_pin.settings_state", exc)
        return []
    return None


def _row_matches_enabled_settings(row: dict, enabled: Optional[list[str]]) -> bool:
    """Require an exact provider/model Settings pair when curated."""
    if enabled is None:
        return True
    defaults = row.get("payload_defaults")
    if not isinstance(defaults, dict):
        return False
    provider = str(defaults.get("provider") or "").strip().lower()
    model = str(row.get("adapter_model_name") or defaults.get("model") or "").strip().lower()
    if not provider or not model:
        return False
    return any(
        _normalize_pin_provider(_provider) == provider and _same_pin_model(_model, model)
        for spec in enabled
        if ":" in str(spec)
        for _provider, _model in [str(spec).split(":", 1)]
    )


def _usable_registry_rows(
    *, adapters: Optional[set[str]] = None,
    enabled_specs: Optional[list[str]] = None,
) -> list[dict]:
    try:
        from .auto_registry import keyed_agentic_providers

        keyed = keyed_agentic_providers()
    except Exception as e:
        _diag("swarm_model_pin.keyed_rows", e)
        keyed = set()
    out: list[dict] = []
    seen: set[str] = set()
    enabled = _settings_enabled_specs() if enabled_specs is None else enabled_specs
    for row in _registry_rows(adapters=adapters):
        mid = str(row.get("id") or "").strip()
        if not mid or mid.lower() in seen:
            continue
        if not _row_is_keyed_agentic(row, keyed):
            continue
        if row.get("enabled", True) is not True or row.get("retired", False) is True:
            continue
        if not _row_matches_enabled_settings(row, enabled):
            continue
        seen.add(mid.lower())
        out.append(row)
    return out


def _enabled_registry_ids(rows: list[dict]) -> list[str]:
    """Order eligible registry rows by their exact Settings provider/model pair."""
    enabled = _settings_enabled_specs() or []
    out: list[str] = []
    for spec in enabled:
        for row in rows:
            model_id = str(row.get("id") or "").strip()
            if model_id and model_id not in out and _row_matches_enabled_settings(row, [spec]):
                out.append(model_id)
    return out


def _ids_from_rows(rows: list[dict], limit: int) -> list[str]:
    out: list[str] = []
    for row in rows:
        mid = str(row.get("id") or "").strip()
        if not mid:
            continue
        out.append(mid)
        if len(out) >= max(1, int(limit)):
            break
    return out


def list_available_agentic_worker_models(*, limit: int = 24) -> list[str]:
    """Registry ids the agentic swarm router can actually pick right now."""
    rows = _usable_registry_rows(adapters={"agentic"})
    preferred = _enabled_registry_ids(rows)
    if preferred:
        return preferred[: max(1, int(limit))]
    return _ids_from_rows(rows, limit)


def list_available_worker_models(
    *,
    limit: int = 24,
    adapters: Optional[set[str]] = None,
) -> list[str]:
    """Registry ids across the allowed worker adapter union."""
    allow = set(adapters) if adapters is not None else None
    rows = _usable_registry_rows(adapters=allow)
    preferred = _enabled_registry_ids(rows)
    if preferred:
        return preferred[: max(1, int(limit))]
    return _ids_from_rows(rows, limit)


def swarm_model_pin_hint(*, limit: int = 16) -> str:
    """Describe the exact model choices available to product workers."""
    try:
        from .swarm_worker_allowlist import resolve_swarm_worker_allowlist
        allow = set(resolve_swarm_worker_allowlist().get("allowed_adapters") or [])
    except Exception:
        allow = set()
    available = list_available_worker_models(limit=limit, adapters=allow)
    rule = (
        "Explicit codex/model pins use the native Codex CLI when allowed and available. "
        "openai-codex:model uses the direct agentic provider. Omit model for auto-routing within "
        "enabled, available provider/model pairs. An explicit model must match "
        "an enabled provider:model pair or registry ID; unavailable pins fail "
        "without choosing a different model."
    )
    if not available:
        return rule + " No eligible worker models are currently available."
    return rule + " Live catalog: " + ", ".join(available) + "."


def pin_candidates(pin: str) -> list[str]:
    """Ordered alias candidates for a pilot-supplied swarm model pin."""
    original = (pin or "").strip()
    if not original:
        return []
    normalized, _meta = normalize_swarm_model_pin_request(original)
    raw = normalized or original
    out: list[str] = []
    seen: set[str] = set()

    def _add(value: str) -> None:
        v = (value or "").strip()
        if not v:
            return
        key = v.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(v)

    # Remapped Codex/Luna Max ids first so apply_model_pin never pins *-pro.
    _add(raw)
    if original.lower() != raw.lower():
        _add(original)

    # provider:model / engine/model → bare model
    bare = raw
    if ":" in bare:
        bare = bare.split(":", 1)[1].strip() or bare
    _add(bare)
    prefix_heads = {
        "cursor",
        "cursor-cli",
        "codex",
        "openai",
        "openai-codex",
        "agentic",
        "native",
        "opencode-go",
        "opencode-zen",
        "opencode",
    }
    while "/" in bare:
        head, rest = bare.split("/", 1)
        if head.lower() not in prefix_heads:
            break
        next_bare = rest.strip()
        if not next_bare:
            break
        bare = next_bare
        _add(bare)

    # Cursor registry uses hyphens in gpt-5-6-*; OpenCode Go uses dots (gpt-5.6-*).
    dotted = re.sub(r"(gpt-\d+)-(\d+)", r"\1.\2", bare, count=1, flags=re.I)
    _add(dotted)
    hyphenated = re.sub(r"(gpt-\d+)\.(\d+)", r"\1-\2", bare, count=1, flags=re.I)
    _add(hyphenated)

    # OpenCode Go live id is deepseek-flash; curated/OpenRouter rows stay
    # deepseek-v4-flash. Pins of either must resolve.
    flash_aliases = {
        "deepseek-flash",
        "deepseek-v4-flash",
        "deepseek-v4.1-flash",
        "deepseek-v4-1-flash",
    }
    tail = bare.rsplit("/", 1)[-1].lower()
    if tail in flash_aliases:
        _add("deepseek-flash")
        _add("deepseek-v4-flash")
        _add("agentic/deepseek-flash")
        _add("agentic/deepseek-v4-flash")
        _add("opencode-go/deepseek-flash")
        _add("opencode-go/deepseek-v4-flash")
        _add("agentic/opencode-go/deepseek-flash")
        _add("agentic/opencode-go/deepseek-v4-flash")

    for body in (bare, dotted, hyphenated):
        if not body:
            continue
        _add(f"agentic/{body}")
        # Codex curated rows use a namespaced registry id so they do not
        # collide with OpenCode Go's flat agentic/gpt-5.6-* rows.
        _add(f"openai-codex/{body}")
        _add(f"agentic/openai-codex/{body}")
        # OpenCode Go registry rows are payload_defaults.provider=opencode-go
        # with id agentic/<model> *or* agentic/opencode-go/<model>. A pin of
        # agentic/opencode/<model> (missing -go) used to demote to auto-route.
        _add(f"opencode-go/{body}")
        _add(f"agentic/opencode-go/{body}")
        # Platform cursor registry peers (only useful when bridge allows cursor).
        _add(f"cursor/{body}")
    # Common typo: agentic/opencode/X vs the real opencode-go provider slug.
    if "agentic/opencode/" in raw.lower() and "opencode-go" not in raw.lower():
        _add(re.sub(r"(?i)agentic/opencode/", "agentic/opencode-go/", raw, count=1))
    return out


def _exact_registry_pin(pin: str, rows: list[dict]) -> Optional[dict[str, Any]]:
    """Return payload fields for one exact agentic registry row."""
    requested = (pin or "").strip()
    wanted_id = requested.lower()
    # Canonical ids are opaque. In particular, agentic/vendor/model must not
    # be interpreted as provider=vendor when the registry row says the wire
    # provider is something else (OpenRouter and OpenCode Go do this).
    for row in rows:
        row_id = str(row.get("id") or "").strip()
        if row_id and row_id.lower() == wanted_id:
            defaults = row.get("payload_defaults")
            if not isinstance(defaults, dict):
                return None
            provider = str(defaults.get("provider") or "").strip().lower()
            model = str(row.get("adapter_model_name") or defaults.get("model") or "").strip()
            if provider and model:
                return {
                    "provider": provider,
                    "model": model,
                    "pinned_model": row_id,
                    "pinned_adapter_model_name": model,
                    "_registry_row": row,
                }
            return None
    body = requested
    if body.lower().startswith("agentic/"):
        body = body[8:].strip()
    provider = ""
    model = body
    if ":" in body:
        provider, model = body.split(":", 1)
    elif "/" in body:
        provider, model = body.split("/", 1)
    provider = _normalize_pin_provider(provider)
    model = model.strip()
    matches: list[dict] = []
    for row in rows:
        defaults = row.get("payload_defaults")
        if not isinstance(defaults, dict):
            continue
        row_provider = str(defaults.get("provider") or "").strip().lower()
        row_model = str(row.get("adapter_model_name") or "").strip()
        if not row_model:
            row_model = str(defaults.get("model") or "").strip()
        row_id = str(row.get("id") or "").strip()
        if not row_id or not _same_pin_model(row_model, model):
            continue
        if provider and row_provider != provider:
            continue
        matches.append({
            "provider": row_provider,
            "model": row_model,
            "pinned_model": row_id,
            "pinned_adapter_model_name": row_model,
            "_registry_row": row,
        })
    if len(matches) != 1:
        return None
    return matches[0]


def _apply_exact_registry_pin(row: dict) -> dict[str, Any]:
    """Stamp the same eligible registry snapshot used to resolve the pin."""
    from puppetmaster.model_registry import ModelSpec, apply_model_pin

    spec = ModelSpec(**{
        key: value for key, value in row.items()
        if key in ModelSpec.__dataclass_fields__
    })
    stamped = apply_model_pin({}, spec.id, adapter="agentic", registry=[spec])
    if stamped.get("pinned_model") != spec.id:
        raise ValueError(f"Could not stamp selected worker model {spec.id!r}")
    return stamped


def _allowed_pin_adapters(
    allowed_adapters: Optional[list[str] | set[str] | tuple[str, ...]],
) -> list[str]:
    if allowed_adapters is None:
        try:
            from .swarm_worker_allowlist import resolve_swarm_worker_allowlist
            allowed_adapters = resolve_swarm_worker_allowlist().get("allowed_adapters") or []
        except Exception:
            return []
    return [a for a in ("agentic", "codex") if a in allowed_adapters]


def resolve_swarm_model_pin(
    pin: str,
    *,
    allowed_adapters: Optional[list[str] | set[str] | tuple[str, ...]] = None,
) -> dict[str, Any]:
    """Resolve a swarm model pin against one exact live agentic row.

    An unavailable explicit pin is returned as demoted for callers that need
    to render an actionable error; product dispatch must not auto-route it.

    Returns:
      {
        "pin_fields": dict,   # merged into worker payload when pinned
        "auto_route": bool,
        "requested": str,
        "resolved": str,      # empty when demoted
        "demoted": bool,
        "reason": str,
        "adapter": str,       # adapter that accepted the pin (or "")
      }
    """
    original = (pin or "").strip()
    requested, norm_meta = normalize_swarm_model_pin_request(original)
    empty = {
        "pin_fields": {},
        "auto_route": True,
        "requested": original,
        "resolved": "",
        "demoted": False,
        "reason": "empty",
        "adapter": "",
    }
    if not original:
        return empty
    if not requested:
        requested = original

    provider, model = _parse_pin_provider_model(requested)
    if provider == "codex":
        from .swarm_worker_allowlist import native_codex_available
        adapters = ["codex"] if allowed_adapters is None else _allowed_pin_adapters(allowed_adapters)
        if "codex" not in adapters or not native_codex_available() or not model:
            return {**empty, "auto_route": False, "demoted": True,
                    "reason": "Native Codex pin requires an allowed, available Codex CLI/platform."}
        native_pin = AgenticModelPin(
            requested=original, provider="", model=model,
            router_model_id=f"codex/{model}", adapter="codex",
        )
        return {**empty, "pin_fields": native_pin.payload_fields(),
                "auto_route": False, "resolved": native_pin.router_model_id,
                "adapter": "codex", "reason": "exact_native_codex_pin"}

    # Refresh catalog against live keys so alias resolution sees OpenCode Go /
    # OpenRouter / etc. as they exist *now*, not a stale peer machine catalog.
    try:
        from .auto_registry import ensure_keyed_provider_registry_health

        ensure_keyed_provider_registry_health()
    except Exception as e:
        _diag("swarm_model_pin.health", e)

    adapters = _allowed_pin_adapters(allowed_adapters)
    if "agentic" in adapters:
        exact = _exact_registry_pin(requested, _usable_registry_rows(adapters={"agentic"}))
        if exact:
            row = exact.pop("_registry_row", {})
            pin_fields = {
                **_apply_exact_registry_pin(row),
                "auto_route": False,
                "pinned_adapter": "agentic",
            }
            # Fail closed: never dispatch ChatGPT Codex OAuth *-pro ids.
            for key in ("model", "pinned_adapter_model_name"):
                cur = str(pin_fields.get(key) or "").strip()
                remapped, reason = remap_codex_oauth_pro_model(cur)
                if reason and remapped:
                    pin_fields[key] = remapped
                    _diag("swarm_model_pin.codex_pro_remap_fields", msg=f"{key}:{reason}")
            provider = str(pin_fields.get("provider") or "").strip().lower()
            if provider in ("opencode-go", "opencode_go"):
                try:
                    from .opencode_go import wire_model_id
                    for key in ("model", "pinned_adapter_model_name"):
                        cur = str(pin_fields.get(key) or "").strip()
                        wired = wire_model_id(cur)
                        if wired and wired != cur:
                            pin_fields[key] = wired
                except Exception as exc:
                    _diag("swarm_model_pin.go_flash_wire", exc)
            if norm_meta.get("reasoning_effort_hint") and not pin_fields.get("reasoning_effort"):
                pin_fields["reasoning_effort"] = norm_meta["reasoning_effort_hint"]
            reason = "exact_registry_row"
            if norm_meta.get("normalize"):
                reason = f"{norm_meta['normalize']};{reason}"
            elif norm_meta.get("codex_pro_remap"):
                reason = f"{norm_meta['codex_pro_remap']};{reason}"
            return {
                "pin_fields": pin_fields,
                "auto_route": False,
                "requested": original,
                "resolved": str(pin_fields["pinned_model"]).strip(),
                "demoted": False,
                "reason": reason,
                "adapter": "agentic",
            }

    available = list_available_worker_models(limit=8, adapters=set(adapters))
    reason = (
        f"pin {original!r} not in keyed worker registry "
        f"(adapters={adapters}); "
        f"choose an enabled model from {available or ['(none keyed)']}"
    )
    _diag("swarm_model_pin.demote", msg=reason)
    return {
        "pin_fields": {},
        "auto_route": True,
        "requested": requested,
        "resolved": "",
        "demoted": True,
        "reason": reason,
        "adapter": "",
    }


def resolve_agentic_model_pin(pin: str) -> tuple[Optional[AgenticModelPin], str]:
    """Resolve an explicit agentic pin without swarm's auto-route demotion."""

    requested = (pin or "").strip()
    if not requested:
        return None, ""

    resolved = resolve_swarm_model_pin(
        requested,
        allowed_adapters=["agentic"],
    )
    if not resolved.get("demoted") and resolved.get("adapter") == "agentic":
        fields = dict(resolved.get("pin_fields") or {})
        provider = str(fields.get("provider") or "").strip().lower()
        model = str(
            fields.get("pinned_adapter_model_name")
            or fields.get("model")
            or ""
        ).strip()
        router_model_id = str(
            fields.get("pinned_model") or resolved.get("resolved") or ""
        ).strip()
        if provider and model and router_model_id:
            return AgenticModelPin(
                requested=requested,
                provider=provider,
                model=model,
                router_model_id=router_model_id,
                reason=str(resolved.get("reason") or "exact"),
            ), ""

    available = list_available_agentic_worker_models(limit=8)
    reason = str(resolved.get("reason") or "").strip()
    if not reason:
        reason = f"pin {requested!r} is not available to the agentic adapter"
    hints = ", ".join(available) if available else "(none keyed)"
    return None, f"{reason}. Available agentic models: {hints}"


def resolve_worker_model_pin(pin: str, reasoning_effort: str = "") -> tuple[Optional[AgenticModelPin], str]:
    """Resolve an explicit native CLI pin or a direct provider pin."""
    if _parse_pin_provider_model(pin)[0] != "codex":
        return resolve_agentic_model_pin(pin)
    resolved = resolve_swarm_model_pin(pin)
    if resolved.get("demoted"):
        return None, str(resolved["reason"])
    fields = resolved["pin_fields"]
    return AgenticModelPin(
        requested=pin, provider="", model=fields["model"],
        router_model_id=resolved["resolved"], adapter="codex",
        reasoning_effort=reasoning_effort,
    ), ""


def codex_worker_payload(payload: dict, *, expects_diff: bool) -> dict:
    """Constrain the official CLI and translate effort to its config key."""
    from .reasoning_effort import current_swarm_reasoning_effort
    out = dict(payload)
    out.pop("provider", None)
    effort = str(out.get("reasoning_effort") or current_swarm_reasoning_effort())
    out.update(sandbox="workspace-write" if expects_diff else "read-only",
               approval_policy="never", auto_route=False,
               allowed_adapters=["codex"], reasoning_effort=effort,
               extra_args=["-c", f"model_reasoning_effort={effort}"])
    return out


def agentic_pin_matches_routed_model(
    pin: Optional[AgenticModelPin],
    routed_model: str,
) -> bool:
    """Fail closed only when the provider reports a different known model."""

    if pin is None or not (routed_model or "").strip():
        return True

    def _normalized(value: str) -> str:
        text = (value or "").strip().lower()
        if text.startswith("agentic/"):
            text = text.split("/", 1)[1]
        return text

    served = _normalized(routed_model)
    accepted = {
        _normalized(pin.router_model_id),
        _normalized(f"{pin.provider}/{pin.model}"),
        _normalized(pin.model),
    }
    return served in accepted
