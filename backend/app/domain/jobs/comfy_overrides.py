"""Per-request ComfyUI parameter overrides, layered on top of a resolved model_profile
(see app/domain/jobs/comfy_profiles.py).

Why this exists (2026-08-19, Chet): the external agentflow team's own master prompt
decides per-request quality knobs (steps/cfg/model/vae/clip/sampler) instead of just
picking between "student"/"personnel" -- so this adds a SECOND, narrower door next to
model_profile: `inputs.model_overrides`, a dict of individual field overrides applied
on top of whichever profile was resolved.

This is deliberately NOT arbitrary passthrough -- string fields (checkpoint/diffusion
model/clip/vae/sampler/scheduler names) are checked against a server-side allowlist of
files that actually exist on the worker (APP_COMFY_ALLOWED_*_CSV), and numeric fields
(steps/cfg_scale) are clamped to a configured range (APP_COMFY_OVERRIDE_MIN/MAX_*).
Two concrete failure modes this prevents (see app/core/rate_limit.py's 2026-08-18
burst-test incident notes for why "no cap" is a proven real risk here, not a
hypothetical): a typo'd/nonexistent filename failing every job it touches, and an
unbounded steps/cfg/resolution value starving GPU capacity for every OTHER job on the
same shared worker. `model_family` is intentionally NOT overridable here -- switching
families changes which graph shape is built entirely (see
app/adapters/comfyui/live.py's checkpoint vs qwen_image graphs), so it stays tied to
model_profile only, never mixed-and-matched per field.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from app.domain.jobs.comfy_profiles import ComfyProfile

# workflow_payload["model_overrides"] key -> which OverrideAllowlists attribute checks it.
_STRING_ALLOWLIST_FIELDS: dict[str, str] = {
    "checkpoint_name": "checkpoints",
    "diffusion_model_name": "diffusion_models",
    "clip_name": "clips",
    "vae_name": "vaes",
    "sampler_name": "samplers",
    "scheduler": "schedulers",
}
_NUMERIC_FIELDS = {"steps", "cfg_scale"}
_FREEFORM_FIELDS = {"negative_prompt"}
ALL_OVERRIDE_KEYS = frozenset(_STRING_ALLOWLIST_FIELDS) | _NUMERIC_FIELDS | _FREEFORM_FIELDS


@dataclass(frozen=True)
class OverrideAllowlists:
    checkpoints: frozenset[str]
    diffusion_models: frozenset[str]
    clips: frozenset[str]
    vaes: frozenset[str]
    samplers: frozenset[str]
    schedulers: frozenset[str]
    min_steps: int
    max_steps: int
    min_cfg_scale: float
    max_cfg_scale: float
    max_negative_prompt_chars: int


def build_allowlists(settings: Any) -> OverrideAllowlists:
    def _csv(value: str) -> frozenset[str]:
        return frozenset(v.strip() for v in value.split(",") if v.strip())

    return OverrideAllowlists(
        checkpoints=_csv(settings.comfy_allowed_checkpoints_csv),
        diffusion_models=_csv(settings.comfy_allowed_diffusion_models_csv),
        clips=_csv(settings.comfy_allowed_clips_csv),
        vaes=_csv(settings.comfy_allowed_vaes_csv),
        samplers=_csv(settings.comfy_allowed_samplers_csv),
        schedulers=_csv(settings.comfy_allowed_schedulers_csv),
        min_steps=settings.comfy_override_min_steps,
        max_steps=settings.comfy_override_max_steps,
        min_cfg_scale=settings.comfy_override_min_cfg_scale,
        max_cfg_scale=settings.comfy_override_max_cfg_scale,
        max_negative_prompt_chars=settings.comfy_override_max_negative_prompt_chars,
    )


class InvalidComfyOverrideError(ValueError):
    """Raised for an unrecognized override key, a string value not in its allowlist, or
    a numeric value outside its configured range. Callers map this to HTTP 400, same
    treatment as UnknownModelProfileError."""


def validate_overrides(overrides: Any, allowlists: OverrideAllowlists) -> dict[str, Any]:
    """Validates+normalizes `inputs.model_overrides`. None/missing -> {} (no-op, exactly
    today's behavior for every caller that doesn't send this field). Every returned
    value is guaranteed allowlisted/in-range -- callers never need to re-check."""
    if overrides is None:
        return {}
    if not isinstance(overrides, dict):
        raise InvalidComfyOverrideError("model_overrides must be an object")

    unknown = set(overrides) - ALL_OVERRIDE_KEYS
    if unknown:
        raise InvalidComfyOverrideError(f"unknown model_overrides keys: {sorted(unknown)}")

    validated: dict[str, Any] = {}

    for field, allowlist_attr in _STRING_ALLOWLIST_FIELDS.items():
        if field not in overrides:
            continue
        value = str(overrides[field]).strip()
        allowlist = getattr(allowlists, allowlist_attr)
        if not allowlist:
            raise InvalidComfyOverrideError(
                f"{field} overrides are not enabled on this deployment "
                f"(no APP_COMFY_ALLOWED_* configured for it)"
            )
        if value not in allowlist:
            raise InvalidComfyOverrideError(f"{field}={value!r} is not in the allowlist")
        validated[field] = value

    if "steps" in overrides:
        try:
            steps = int(overrides["steps"])
        except (TypeError, ValueError):
            raise InvalidComfyOverrideError("steps must be an integer")
        if not (allowlists.min_steps <= steps <= allowlists.max_steps):
            raise InvalidComfyOverrideError(
                f"steps must be between {allowlists.min_steps} and {allowlists.max_steps}"
            )
        validated["steps"] = steps

    if "cfg_scale" in overrides:
        try:
            cfg = float(overrides["cfg_scale"])
        except (TypeError, ValueError):
            raise InvalidComfyOverrideError("cfg_scale must be a number")
        if not (allowlists.min_cfg_scale <= cfg <= allowlists.max_cfg_scale):
            raise InvalidComfyOverrideError(
                f"cfg_scale must be between {allowlists.min_cfg_scale} and "
                f"{allowlists.max_cfg_scale}"
            )
        validated["cfg_scale"] = cfg

    if "negative_prompt" in overrides:
        validated["negative_prompt"] = str(overrides["negative_prompt"])[
            : allowlists.max_negative_prompt_chars
        ]

    return validated


def apply_overrides(profile: ComfyProfile, validated_overrides: dict[str, Any]) -> ComfyProfile:
    """Layers already-validated overrides onto a resolved profile. Never call with raw,
    unvalidated input -- see validate_overrides above."""
    if not validated_overrides:
        return profile
    return replace(profile, **validated_overrides)
