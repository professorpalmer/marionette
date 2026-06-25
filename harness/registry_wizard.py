from __future__ import annotations

import os
import json
import tempfile
import urllib.request
import urllib.error
from typing import Optional

from .providers import Provider, PROVIDERS, get_provider
from .keys import _read_keys

# Define paths
def get_routing_file_path() -> str:
    state_dir = os.environ.get("HARNESS_STATE_DIR")
    if state_dir:
        return os.path.join(state_dir, "routing.json")
    return os.path.join(os.path.expanduser("~/.pmharness"), "routing.json")

def get_models_file_path() -> str:
    env_path = os.environ.get("PUPPETMASTER_MODELS_PATH")
    if env_path:
        return env_path
    return os.path.expanduser("~/.puppetmaster/models.json")

# Helpers for provider keys
def get_provider_key(p: Provider) -> Optional[str]:
    k = p.key()
    if k:
        return k
    stored = _read_keys()
    k = stored.get(p.name, "")
    if k:
        return k
    return None

# Helpers for atomic file writes
def write_json_atomic(path: str, data: dict, chmod_mode: Optional[int] = None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), prefix="atomic_")
    try:
        with os.fdopen(tmp_fd, 'w') as f:
            json.dump(data, f, indent=2)
        if chmod_mode is not None:
            os.chmod(tmp_path, chmod_mode)
        os.replace(tmp_path, path)
        if chmod_mode is not None:
            try:
                os.chmod(path, chmod_mode)
            except OSError:
                pass
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise

# Probing logic
def probe_provider(p: Provider, key: Optional[str]) -> dict:
    if not key:
        return {
            "provider": p.name,
            "models": [{"id": m} for m in p.pilot_models],
            "source": "static",
            "error": "No API key configured for this provider. Using static defaults."
        }

    if p.api_mode == "anthropic_messages":
        if "anthropic" in p.base_url:
            url = f"{p.base_url.rstrip('/')}/v1/models"
            headers = {
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "Accept": "application/json"
            }
        else:
            url = f"{p.base_url.rstrip('/')}/v1/models"
            headers = {
                "x-api-key": key,
                "Accept": "application/json"
            }
    else:
        url = f"{p.base_url.rstrip('/')}/models"
        headers = {
            "Authorization": f"Bearer {key}",
            "Accept": "application/json"
        }

    headers["User-Agent"] = "pm-harness/0.1.0"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            res_data = response.read()
            parsed = json.loads(res_data.decode("utf-8"))
            if isinstance(parsed, dict) and "data" in parsed and isinstance(parsed["data"], list):
                models_list = []
                for item in parsed["data"]:
                    if isinstance(item, dict) and "id" in item:
                        models_list.append({"id": item["id"]})
                if models_list:
                    return {
                        "provider": p.name,
                        "models": models_list,
                        "source": "live"
                    }
            raise ValueError("Unexpected response format from provider models endpoint")
    except Exception as e:
        err_msg = str(e)
        if isinstance(e, urllib.error.HTTPError):
            try:
                err_body = e.read().decode("utf-8")
                err_json = json.loads(err_body)
                if isinstance(err_json, dict):
                    if "error" in err_json:
                        if isinstance(err_json["error"], dict) and "message" in err_json["error"]:
                            err_msg = err_json["error"]["message"]
                        elif isinstance(err_json["error"], str):
                            err_msg = err_json["error"]
                    elif "message" in err_json:
                        err_msg = err_json["message"]
            except Exception:
                pass
            err_msg = f"HTTP {e.code}: {err_msg}"
        return {
            "provider": p.name,
            "models": [{"id": m} for m in p.pilot_models],
            "source": "static",
            "error": f"Probe failed ({err_msg}). Using static defaults."
        }

def get_valid_models_for_provider(p: Provider) -> list[str]:
    key = get_provider_key(p)
    if key:
        probe_res = probe_provider(p, key)
        if probe_res.get("source") == "live":
            return [m["id"] for m in probe_res["models"]]
    return list(p.pilot_models)

# Roles base score definition and resolution
REAL_BASE_SCORES: dict[str, int] = {}
try:
    from puppetmaster.router import _ROLE_BASE_SCORE as _rbs
    REAL_BASE_SCORES = _rbs
except ImportError:
    REAL_BASE_SCORES = {
        "verify-runtime": 25,
        "shell": 20,
        "demo": 25,
        "explore": 50,
        "review": 55,
        "plan": 60,
        "implement": 75,
        "refactor": 75,
        "patch": 75,
        "fix": 70,
        "test-coverage-reviewer": 60,
        "architect": 85,
        "audit": 85,
        "security-review": 90,
        "decision-explainer": 70,
        "conflict-auditor": 75,
        "pipeline-mapper": 65,
    }

# Recommendation logic
def classify_models(models: list[str]) -> tuple[list[str], list[str]]:
    cheap = []
    strong = []
    for m in models:
        m_lower = m.lower()
        if "flash" in m_lower or "mini" in m_lower or "haiku" in m_lower or "fast" in m_lower or "cheap" in m_lower:
            cheap.append(m)
        else:
            strong.append(m)
    if not cheap and strong:
        cheap = strong
    if not strong and cheap:
        strong = cheap
    return cheap, strong

def get_recommendations() -> dict:
    active_provs = []
    preferred_order = ["openrouter", "anthropic", "openai", "deepseek", "gemini", "zai", "minimax", "xai", "nvidia"]
    for pname in preferred_order:
        p = get_provider(pname)
        if p and get_provider_key(p) is not None:
            active_provs.append(p)
            
    if not active_provs:
        rec_provider = get_provider("openrouter") or PROVIDERS[0]
    else:
        rec_provider = active_provs[0]
        
    valid_models = get_valid_models_for_provider(rec_provider)
    cheap_list, strong_list = classify_models(valid_models)
    
    cheap_model = cheap_list[0] if cheap_list else (rec_provider.pilot_models[0] if rec_provider.pilot_models else "unknown")
    strong_model = strong_list[0] if strong_list else (rec_provider.pilot_models[0] if rec_provider.pilot_models else "unknown")
    
    pilot_driver = f"{rec_provider.name}:{strong_model}"
    
    role_mapping = {}
    for role, base_score in REAL_BASE_SCORES.items():
        if base_score >= 70:
            role_mapping[role] = f"{rec_provider.name}:{strong_model}"
        else:
            role_mapping[role] = f"{rec_provider.name}:{cheap_model}"
            
    return {
        "pilot_driver": pilot_driver,
        "roles": role_mapping
    }

# Pilot validation
def validate_pilot_driver(driver: str) -> dict:
    if not isinstance(driver, str) or not driver.strip():
        return {
            "valid": False,
            "resolved_model_id": None,
            "provider": None,
            "reason": "Driver ID must be a non-empty string"
        }
    
    driver = driver.strip()
    
    # 1. Try pmharness catalog short-name lookup first
    try:
        from pmharness.registry import _entry as cat_entry
        m = cat_entry(driver)
        slug = m.get("openrouter")
        if slug:
            return {
                "valid": True,
                "resolved_model_id": slug,
                "provider": "openrouter",
                "reason": f"Resolved catalog short-name '{driver}' to OpenRouter slug '{slug}'"
            }
    except KeyError:
        pass
    
    # 2. Check if provider is explicit (provider:model)
    if ":" in driver:
        pname, model_id = driver.split(":", 1)
        p = get_provider(pname)
        if not p:
            return {
                "valid": False,
                "resolved_model_id": None,
                "provider": pname,
                "reason": f"Unknown provider '{pname}'"
            }
        
        valid_models = get_valid_models_for_provider(p)
        if model_id in valid_models:
            return {
                "valid": True,
                "resolved_model_id": model_id,
                "provider": p.name,
                "reason": f"Model '{model_id}' is valid for provider '{p.name}'"
            }
        else:
            return {
                "valid": False,
                "resolved_model_id": None,
                "provider": p.name,
                "reason": f"Model '{model_id}' not found in provider '{p.name}' (tried probed and static list)"
            }
    
    # 3. Bare slug check across all providers
    for p in PROVIDERS:
        valid_models = get_valid_models_for_provider(p)
        if driver in valid_models:
            return {
                "valid": True,
                "resolved_model_id": driver,
                "provider": p.name,
                "reason": f"Model '{driver}' is valid for provider '{p.name}'"
            }
            
    return {
        "valid": False,
        "resolved_model_id": None,
        "provider": None,
        "reason": f"Driver ID '{driver}' not found in catalog, nor as any provider:model, nor in any provider's model list"
    }
