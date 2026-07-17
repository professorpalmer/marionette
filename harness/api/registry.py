"""Model registry / roles HTTP route bodies (peeled from ``harness.server``)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Optional, Union


@dataclass
class RegistryServices:
    """Explicit deps for registry/roles HTTP handlers."""

    diag: Callable[..., Any]


JsonPayload = Union[dict, list, str]


def post_registry(body: dict) -> tuple[int, JsonPayload]:
    """POST /api/registry."""
    models = body.get("models")
    if not isinstance(models, list):
        return 400, {"error": "models must be a list"}

    validated_models = []
    for m in models:
        if not isinstance(m, dict):
            return 400, {"error": "each model must be a dictionary"}

        model_id = m.get("id")
        if not isinstance(model_id, str) or not model_id.strip():
            return 400, {"error": "id must be a non-empty string"}

        adapter = m.get("adapter")
        if not isinstance(adapter, str):
            return 400, {"error": "adapter must be a string"}

        try:
            score = int(m.get("capability_score", 0))
            score = max(0, min(100, score))
        except (ValueError, TypeError):
            return 400, {"error": "capability_score must be an integer"}

        m["id"] = model_id.strip()
        m["adapter"] = adapter
        m["capability_score"] = score
        validated_models.append(m)

    from ..registry_wizard import get_models_file_path, write_json_atomic
    dest_path = get_models_file_path()
    try:
        write_json_atomic(dest_path, {"models": validated_models})
        return 200, {"ok": True, "models": validated_models}
    except Exception as e:
        return 500, {"error": f"Failed to write registry: {str(e)}"}


def post_roles(body: dict, svc: RegistryServices) -> tuple[int, JsonPayload]:
    """POST /api/roles."""
    overrides = body.get("overrides", {})
    policy = body.get("routing_policy")

    if not isinstance(overrides, dict):
        return 400, {"error": "overrides must be a dictionary"}

    validated_overrides = {}
    from ..registry_wizard import REAL_BASE_SCORES
    for role, score in overrides.items():
        if role not in REAL_BASE_SCORES:
            return 400, {"error": f"Unknown role: {role}"}
        try:
            clamped_score = max(0, min(100, int(score)))
            validated_overrides[role] = clamped_score
        except (ValueError, TypeError):
            return 400, {"error": f"Invalid score for role {role}: {score}"}

    if policy is not None:
        valid_policies = {"balanced", "cheap", "quality", "escalating"}
        if policy not in valid_policies:
            return 400, {
                "error": f"Invalid policy: {policy}; expected one of {list(valid_policies)}"
            }

    from ..registry_wizard import get_routing_file_path, write_json_atomic
    dest_path = get_routing_file_path()
    current_data = {}
    if os.path.exists(dest_path):
        try:
            with open(dest_path, encoding="utf-8", errors="replace") as f:
                current_data = json.load(f)
        except Exception as e:
            svc.diag("server.routing_overrides_load", e)

    current_overrides = current_data.get("overrides", {})
    current_overrides.update(validated_overrides)
    current_data["overrides"] = current_overrides

    if policy is not None:
        current_data["routing_policy"] = policy
    elif "routing_policy" not in current_data:
        current_data["routing_policy"] = "balanced"

    try:
        write_json_atomic(dest_path, current_data, chmod_mode=0o600)
        return 200, {
            "ok": True,
            "overrides": current_data["overrides"],
            "routing_policy": current_data["routing_policy"],
        }
    except Exception as e:
        return 500, {"error": f"Failed to save roles config: {str(e)}"}


def post_pilot_validate(body: dict) -> tuple[int, JsonPayload]:
    """POST /api/pilot/validate."""
    driver = body.get("driver")
    if not isinstance(driver, str):
        return 400, {"error": "driver must be a string"}

    from ..registry_wizard import validate_pilot_driver
    try:
        res = validate_pilot_driver(driver)
        return 200, res
    except Exception as e:
        return 500, {"error": str(e)}


def get_registry() -> tuple[int, JsonPayload]:
    """GET /api/registry.

    When the on-disk file exists, returns its raw text (already JSON) so the
    Handler can send it without a second ``json.dumps`` pass.
    """
    from ..registry_wizard import get_models_file_path
    path = get_models_file_path()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return 200, f.read()
        except Exception as e:
            return 500, {"error": f"Failed to read registry: {str(e)}"}
    return 200, {"models": []}


def get_roles(svc: RegistryServices) -> tuple[int, JsonPayload]:
    """GET /api/roles."""
    from ..registry_wizard import REAL_BASE_SCORES, get_routing_file_path
    path = get_routing_file_path()
    overrides = {}
    policy = "balanced"
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                data = json.load(f)
                overrides = data.get("overrides", {})
                policy = data.get("routing_policy", "balanced")
        except Exception as e:
            svc.diag("server.roles_routing_load", e)

    roles_mapping = {}
    for k, v in REAL_BASE_SCORES.items():
        roles_mapping[k] = overrides.get(k, v)

    return 200, {
        "roles": roles_mapping,
        "policies": ["balanced", "cheap", "quality", "escalating"],
        "routing_policy": policy,
        "overrides": overrides,
    }


def get_registry_recommend() -> tuple[int, JsonPayload]:
    """GET /api/registry/recommend."""
    from ..registry_wizard import get_recommendations
    try:
        return 200, get_recommendations()
    except Exception as e:
        return 500, {"error": str(e)}
