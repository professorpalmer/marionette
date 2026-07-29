"""Experiment-gated AGNT-inspired tool-schema token estimator.

Cold-start per tool: deterministic ``schema_chars // 4`` with optional caps.
When ``HARNESS_SCHEMA_TOKEN_CALIBRATION`` is on, an EMA calibration factor
learns from provider prompt floors only when tool-prefix attribution is
defensible (stable visible-tool fingerprint). Telemetry-only — never mutates
billed provider tokens, cost, cache savings, or compaction journals.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

CHARS_PER_TOKEN = 4
DEFAULT_PER_TOOL_CAP_TOKENS = 8192
EMA_ALPHA = 0.2
DRIFT_RESET_THRESHOLD = 0.25
SUCCESS_RESIDUAL_THRESHOLD = 0.15
SUCCESS_MIN_OBSERVATIONS = 20
SUSTAINED_DRIFT_OBSERVATIONS = 3


def schema_token_calibration_enabled() -> bool:
    """Return True when the schema-token calibration experiment is active."""
    raw = (os.environ.get("HARNESS_SCHEMA_TOKEN_CALIBRATION") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def per_tool_cap_tokens() -> int:
    """Bounded per-tool token cap (env ``HARNESS_SCHEMA_TOKEN_PER_TOOL_CAP``)."""
    try:
        return max(1, int(os.environ.get("HARNESS_SCHEMA_TOKEN_PER_TOOL_CAP", str(DEFAULT_PER_TOOL_CAP_TOKENS))))
    except (TypeError, ValueError):
        return DEFAULT_PER_TOOL_CAP_TOKENS


def cold_start_tool_tokens(schema_chars: int, cap: Optional[int] = None) -> int:
    """Deterministic cold-start estimate for one tool schema blob."""
    cap_val = per_tool_cap_tokens() if cap is None else max(1, int(cap))
    return min(max(0, schema_chars) // CHARS_PER_TOKEN, cap_val)


def _tool_name(tool: dict) -> str:
    try:
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else {}
        return str(fn.get("name") or tool.get("name") or "")
    except Exception:
        return ""


def estimate_tools_from_schema(
    tools_schema: List[dict],
    *,
    cap: Optional[int] = None,
) -> Tuple[int, List[Dict[str, Any]]]:
    """Return ``(cold_start_total, per_tool_rows)`` for visible tool schemas."""
    cap_val = per_tool_cap_tokens() if cap is None else max(1, int(cap))
    rows: List[Dict[str, Any]] = []
    total = 0
    for tool in tools_schema or []:
        if not isinstance(tool, dict):
            continue
        serialized = json.dumps(tool, sort_keys=True, separators=(",", ":"))
        chars = len(serialized)
        est = cold_start_tool_tokens(chars, cap_val)
        rows.append({"name": _tool_name(tool), "schema_chars": chars, "estimated_tokens": est})
        total += est
    return total, rows


def legacy_whole_schema_tokens(tools_schema: List[dict]) -> int:
    """Whole-blob chars//4 floor used before calibration (safety minimum)."""
    try:
        serialized = json.dumps(tools_schema)
    except Exception:
        return 0
    return len(serialized) // CHARS_PER_TOKEN


def tools_fingerprint(tools_schema: List[dict]) -> str:
    """Stable fingerprint for the visible tool schema set."""
    try:
        payload = json.dumps(tools_schema, sort_keys=True, separators=(",", ":"))
    except Exception:
        payload = "[]"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass
class SchemaTokenCalibrator:
    """Session-local EMA calibrator for tool-schema token estimates."""

    ema_factor: float = 1.0
    observation_count: int = 0
    residual_sum: float = 0.0
    provider_floor_sum: float = 0.0
    last_fingerprint: str = ""
    disabled: bool = False
    _recent_drifts: List[float] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ema_factor": self.ema_factor,
            "observation_count": self.observation_count,
            "residual_sum": self.residual_sum,
            "provider_floor_sum": self.provider_floor_sum,
            "last_fingerprint": self.last_fingerprint,
            "disabled": self.disabled,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "SchemaTokenCalibrator":
        if not isinstance(raw, dict):
            return cls()
        try:
            return cls(
                ema_factor=float(raw.get("ema_factor") or 1.0),
                observation_count=max(0, int(raw.get("observation_count") or 0)),
                residual_sum=float(raw.get("residual_sum") or 0.0),
                provider_floor_sum=float(raw.get("provider_floor_sum") or 0.0),
                last_fingerprint=str(raw.get("last_fingerprint") or ""),
                disabled=bool(raw.get("disabled")),
            )
        except (TypeError, ValueError):
            return cls()

    def calibrated_tool_tokens(self, cold_start_total: int) -> int:
        """Apply the EMA factor; cold-start when disabled or zero tools."""
        if cold_start_total <= 0:
            return 0
        if self.disabled:
            return cold_start_total
        scaled = cold_start_total * self.ema_factor
        return max(1, int(round(scaled)))

    def fuse_tool_tokens(
        self,
        tools_schema: List[dict],
        *,
        cold_start_total: int,
    ) -> Tuple[int, int, List[Dict[str, Any]]]:
        """Return ``(billed_tool_tokens, cold_start_total, per_tool_rows)``.

        Never undercuts the legacy whole-blob chars//4 safety floor.
        """
        _, rows = estimate_tools_from_schema(tools_schema)
        legacy_floor = legacy_whole_schema_tokens(tools_schema)
        calibrated = self.calibrated_tool_tokens(cold_start_total)
        billed = max(legacy_floor, cold_start_total, calibrated)
        return billed, cold_start_total, rows

    def maybe_update(
        self,
        *,
        provider_floor: int,
        non_tool_heuristic: int,
        cold_start_tools: int,
        fingerprint: str,
    ) -> Optional[float]:
        """Update EMA when tool-prefix attribution is defensible."""
        if self.disabled or provider_floor <= 0 or cold_start_tools <= 0:
            return None
        if not fingerprint:
            return None

        # First observation seeds fingerprint; changes invalidate attribution.
        if self.last_fingerprint and fingerprint != self.last_fingerprint:
            self.last_fingerprint = fingerprint
            return None
        self.last_fingerprint = fingerprint

        measured_tools = provider_floor - non_tool_heuristic
        if measured_tools <= 0:
            return None

        target_factor = measured_tools / float(cold_start_tools)
        self.ema_factor = (1.0 - EMA_ALPHA) * self.ema_factor + EMA_ALPHA * target_factor

        calibrated = self.calibrated_tool_tokens(cold_start_tools)
        residual = float(provider_floor - non_tool_heuristic - calibrated)
        self.residual_sum += residual
        self.provider_floor_sum += float(provider_floor)
        self.observation_count += 1

        drift = abs(residual) / float(provider_floor)
        self._recent_drifts.append(drift)
        if len(self._recent_drifts) > SUSTAINED_DRIFT_OBSERVATIONS:
            self._recent_drifts.pop(0)
        if (
            len(self._recent_drifts) >= SUSTAINED_DRIFT_OBSERVATIONS
            and all(d > DRIFT_RESET_THRESHOLD for d in self._recent_drifts)
        ):
            self.reset()
        return residual

    def reset(self) -> None:
        """Disable calibration and return to cold-start (post drift breach)."""
        self.ema_factor = 1.0
        self.observation_count = 0
        self.residual_sum = 0.0
        self.provider_floor_sum = 0.0
        self._recent_drifts.clear()
        self.disabled = True

    def mean_residual_pct(self) -> Optional[float]:
        if self.observation_count <= 0 or self.provider_floor_sum <= 0:
            return None
        mean_residual = self.residual_sum / float(self.observation_count)
        mean_floor = self.provider_floor_sum / float(self.observation_count)
        if mean_floor <= 0:
            return None
        return abs(mean_residual) / mean_floor

    def calibration_success(self) -> bool:
        pct = self.mean_residual_pct()
        if pct is None:
            return False
        return (
            self.observation_count >= SUCCESS_MIN_OBSERVATIONS
            and pct <= SUCCESS_RESIDUAL_THRESHOLD
        )

    def telemetry(
        self,
        *,
        cold_start_total: int,
        billed_tool_tokens: int,
        legacy_floor: int,
    ) -> Dict[str, Any]:
        mean_pct = self.mean_residual_pct()
        return {
            "schema_token_calibration_basis": "estimated",
            "schema_token_cold_start": int(cold_start_total),
            "schema_token_legacy_floor": int(legacy_floor),
            "schema_token_calibrated": int(billed_tool_tokens),
            "schema_token_ema_factor": round(float(self.ema_factor), 6),
            "schema_token_observations": int(self.observation_count),
            "schema_token_mean_residual_pct": (
                round(mean_pct, 6) if mean_pct is not None else None
            ),
            "schema_token_calibration_success": bool(self.calibration_success()),
            "schema_token_calibration_disabled": bool(self.disabled),
        }
