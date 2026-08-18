"""Settings + config HTTP route bodies (peeled from ``harness.server``)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Union


@dataclass
class SettingsServices:
    """Explicit deps for settings / config HTTP handlers."""

    cfg: Any
    get_pilot: Callable[[], Any]
    get_session: Callable[[], Any]
    parse_bool: Callable[[Any], bool]
    set_api_key: Callable[[str, str], None]
    clear_api_key: Callable[[str], None]
    rebuild_pilot_and_session: Callable[[], None]
    available_pilots: Callable[[], list]
    save_workspace_driver: Callable[[Any, str], None]
    persist_env_setting: Callable[[str, str], None]
    get_settings_dict: Callable[[], dict]
    driver_provider_available: Callable[[str], bool] = lambda _spec: True
    resolve_available_driver: Callable[[], None] = lambda: None


JsonPayload = Union[dict, list]


def get_config(svc: SettingsServices) -> tuple[int, JsonPayload]:
    """GET /api/config."""
    cfg = svc.cfg
    session = svc.get_session()
    try:
        from ..edit_engines import pilot_keys_ready, select_edit_engine, workers_ready
        edit_engine = select_edit_engine(cfg)
        # workers_ready / agentic_ready: Full stack key or CURSOR_API_KEY — the
        # banner that claims swarms can run. pilot_ready is the broader chat
        # lane (includes Pilot-only cursor-cli login).
        workers = workers_ready()
        pilot = pilot_keys_ready()
        agentic_ready = workers
    except Exception:
        edit_engine, agentic_ready, workers, pilot = "native", False, False, False
    try:
        from ..reasoning_effort import current_reasoning_effort
        reasoning_effort = current_reasoning_effort()
    except Exception:
        reasoning_effort = "low"
    identity = _backend_source_identity()
    return 200, {
        "driver": cfg.driver,
        "reach": cfg.reach,
        "budget": cfg.budget,
        "state_dir": session.state_dir,
        "models": svc.available_pilots(),
        "repo": cfg.repo,
        "swarm_adapter": cfg.swarm_adapter,
        "edit_engine": edit_engine,
        "agentic_ready": agentic_ready,
        "workers_ready": workers,
        "pilot_ready": pilot,
        "preflight": session.preflight(),
        "reasoning_effort": reasoning_effort,
        "package_version": identity.get("package_version", ""),
        "checkout_sha": identity.get("checkout_sha", ""),
        "app_root": identity.get("app_root", ""),
    }


def _backend_source_identity() -> dict:
    """Identity of the checkout this backend was started from.

    Exposed on ``/api/config`` so Electron can refuse to adopt a live process
    whose source no longer matches the current tree after an update.
    """
    out = {"package_version": "", "checkout_sha": "", "app_root": ""}
    try:
        from harness import __version__
        out["package_version"] = str(__version__ or "").strip()
    except Exception:
        pass
    app_root = (os.environ.get("MARIONETTE_APP_ROOT") or "").strip()
    out["app_root"] = app_root
    if not app_root:
        return out
    try:
        import subprocess
        sha = subprocess.check_output(
            ["git", "-C", app_root, "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            timeout=3,
        ).decode("utf-8", "replace").strip()
        out["checkout_sha"] = sha
    except Exception:
        pass
    return out


def get_settings(svc: SettingsServices) -> tuple[int, JsonPayload]:
    """GET /api/settings."""
    return 200, svc.get_settings_dict()


def post_settings(body: dict, svc: SettingsServices) -> tuple[int, JsonPayload]:
    """POST /api/settings."""
    cfg = svc.cfg
    pilot = svc.get_pilot()

    requires_rebuild = False
    if "api_key" in body or body.get("clear_api_key") is True:
        requires_rebuild = True
    driver = body.get("driver")
    if driver is not None and driver != cfg.driver:
        requires_rebuild = True
    if requires_rebuild:
        if not pilot._busy.acquire(blocking=False):
            return 409, {"error": "pilot busy, try again"}
        pilot._busy.release()

    reach_to_use = body.get("reach", cfg.reach)
    if "api_key" in body:
        val = str(body["api_key"]).strip()
        if val:
            svc.set_api_key(reach_to_use, val)
            svc.rebuild_pilot_and_session()
            from ..auto_registry import sync_agentic_registry_safe
            sync_agentic_registry_safe()
    elif body.get("clear_api_key") is True:
        svc.clear_api_key(reach_to_use)
        # Match /api/providers/key clear: scrub is inside clear_api_key; if the
        # active driver just lost its provider, pick another before rebuild so
        # Settings disconnect cannot 500 on a dead openrouter/bedrock pilot.
        try:
            if not svc.driver_provider_available(svc.cfg.driver):
                svc.resolve_available_driver()
        except Exception:
            pass
        # Clearing the last key can leave no runnable replacement. Skip
        # rebuild so Settings disconnect returns 200 instead of 500.
        driver = str(getattr(svc.cfg, "driver", "") or "")
        if driver.startswith("stub") or svc.driver_provider_available(driver):
            svc.rebuild_pilot_and_session()
        from ..auto_registry import sync_agentic_registry_safe
        sync_agentic_registry_safe()

    driver = body.get("driver")
    if driver is not None:
        from .. import model_visibility as _mv
        catalog_specs = {c["spec"] for c in _mv.catalog(available_only=True)}
        av = set(svc.available_pilots()) | catalog_specs
        if driver not in av:
            return 400, {"error": f"Unknown or unavailable driver: {driver}"}
        if driver != cfg.driver:
            try:
                cfg.driver = driver
                svc.rebuild_pilot_and_session()
                svc.save_workspace_driver(cfg.repo, driver)
            except Exception as e:
                return 500, {"error": f"Failed to swap driver: {str(e)}"}

    budget = body.get("budget")
    if budget is not None:
        try:
            b_val = int(budget)
            cfg.budget = max(1, min(50, b_val))
        except (ValueError, TypeError):
            return 400, {"error": "Invalid budget value"}

    def _set_env_setting(env_var: str, value: str) -> None:
        os.environ[env_var] = value
        svc.persist_env_setting(env_var, value)

    if "auto_distill" in body:
        ad_val = svc.parse_bool(body["auto_distill"])
        pilot._auto_distill = ad_val
        _set_env_setting("HARNESS_AUTO_DISTILL", "true" if ad_val else "false")
    if "reviewEditsBeforeApply" in body:
        rev_val = svc.parse_bool(body["reviewEditsBeforeApply"])
        pilot._review_edits_before_apply = rev_val
        _set_env_setting(
            "HARNESS_REVIEW_EDITS_BEFORE_APPLY", "true" if rev_val else "false"
        )
    if "autoCommandGuard" in body:
        g_val = svc.parse_bool(body["autoCommandGuard"])
        pilot._auto_command_guard = g_val
        _set_env_setting("HARNESS_AUTO_COMMAND_GUARD", "true" if g_val else "off")
    if "autoVerify" in body:
        av_val = svc.parse_bool(body["autoVerify"])
        cfg.auto_verify = av_val
        _set_env_setting("HARNESS_AUTO_VERIFY", "true" if av_val else "false")
    if "hash_edit_enabled" in body:
        he_val = svc.parse_bool(body["hash_edit_enabled"])
        _set_env_setting("HARNESS_HASH_EDIT", "1" if he_val else "0")
    if "verifyCommand" in body:
        vc_val = str(body["verifyCommand"]).strip()
        cfg.verify_command = vc_val
        _set_env_setting("HARNESS_VERIFY_COMMAND", vc_val)
    if "commandTimeout" in body:
        raw = str(body["commandTimeout"]).strip().lower()
        if raw in ("0", "off", "none", "unbounded"):
            _set_env_setting("HARNESS_COMMAND_TIMEOUT", "0")
        else:
            try:
                _set_env_setting("HARNESS_COMMAND_TIMEOUT", str(max(1, int(raw))))
            except (ValueError, TypeError):
                return 400, {"error": "Invalid commandTimeout"}
    if "maxPilotSteps" in body:
        raw = str(body["maxPilotSteps"]).strip().lower()
        if raw in ("0", "off", "none", "unlimited"):
            _set_env_setting("HARNESS_MAX_PILOT_STEPS", "0")
        else:
            try:
                _set_env_setting("HARNESS_MAX_PILOT_STEPS", str(max(1, int(raw))))
            except (ValueError, TypeError):
                return 400, {"error": "Invalid maxPilotSteps"}
    if "pilotToolBudget" in body:
        raw = str(body["pilotToolBudget"]).strip().lower()
        if raw in ("0", "off", "none", "unlimited"):
            _set_env_setting("HARNESS_PILOT_TOOL_BUDGET", "0")
        else:
            try:
                cap = int(raw)
                if cap < 1:
                    return 400, {"error": "Invalid pilotToolBudget"}
                _set_env_setting("HARNESS_PILOT_TOOL_BUDGET", str(cap))
            except (ValueError, TypeError):
                return 400, {"error": "Invalid pilotToolBudget"}
    if "workerTokenBudget" in body:
        raw = str(body["workerTokenBudget"]).strip().lower()
        try:
            _set_env_setting(
                "HARNESS_WORKER_TOKEN_BUDGET", str(max(1, int(raw)))
            )
        except (ValueError, TypeError):
            return 400, {"error": "Invalid workerTokenBudget"}
    if "reasoning_effort" in body:
        from ..reasoning_effort import normalize_reasoning_effort
        normalized = normalize_reasoning_effort(body["reasoning_effort"])
        _set_env_setting("HARNESS_CODEX_REASONING_EFFORT", normalized)
    if "compactionResidual" in body:
        from ..compaction_residual import SETTINGS_RESIDUAL_CHOICES

        raw = str(body["compactionResidual"] or "").strip().lower()
        if raw not in SETTINGS_RESIDUAL_CHOICES:
            return 400, {"error": "Invalid compactionResidual"}
        _set_env_setting("HARNESS_COMPACTION_RESIDUAL", raw)

    return 200, svc.get_settings_dict()
