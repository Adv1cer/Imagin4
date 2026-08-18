"""Server-side allowlist of ComfyUI "model profiles" (quality/model tiers).

Why this exists (2026-08-19): an external agentflow team wants image jobs to use
different models depending on the requesting user's role -- "student" (default) vs.
"personnel" (better/slower model) today, more tiers later -- but per their own
description, that access-control decision is made entirely on THEIR side (their own
master prompt / UI decides who can pick what). Our backend's job is only "receive
params, generate, return" -- so callers select a profile by NAME from this fixed,
server-side allowlist, never by supplying a raw checkpoint filename/sampler/steps/cfg
directly. This mirrors the exact same invariant app/domain/jobs/workflow_registry.py
already enforces for workflow selection, and the one app/adapters/comfyui/live.py's
module docstring documents for the graph itself: "Client-supplied arbitrary ComfyUI
workflows are forbidden."

Concretely, this also protects capacity: steps/cfg/resolution directly control how
long a generation takes and how much VRAM it needs (see the 2026-08-18 burst-test
incident notes in app/core/rate_limit.py) -- if a caller could set those per-request
without bound, a single bad/malicious request could starve every other job. Keeping
them bundled into a small, fixed set of named profiles means every accepted value is
one we've deliberately sized, not whatever a client happened to send.

Add a new tier by adding both a `comfy_<tier>_*` settings block in app/core/config.py
and an entry in `build_profiles()` below -- two small, mechanical edits, not a
schema/migration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# "student" is the always-present default profile (backed by the original comfy_*
# fields -- unchanged from before profiles existed), used whenever a caller sends no
# model_profile at all. Renamed from "standard" (2026-08-19, Chet) to match the
# agentflow team's actual two roles instead of a generic placeholder name.
DEFAULT_PROFILE_KEY = "student"


@dataclass(frozen=True)
class ComfyProfile:
    key: str
    checkpoint_name: str = ""
    model_family: str = "checkpoint"
    diffusion_model_name: str = ""
    clip_name: str = ""
    vae_name: str = ""
    model_sampling_shift: float = 3.1
    sampler_name: str = "euler"
    scheduler: str = "normal"
    steps: int = 20
    cfg_scale: float = 7.0
    negative_prompt: str = ""


class UnknownModelProfileError(ValueError):
    """Raised when a caller-supplied `model_profile` isn't in the allowlist. Mirrors
    workflow_registry.UnknownWorkflowError -- callers map this to HTTP 400, same as
    an unrecognized workflow."""


def build_profiles(settings: Any) -> dict[str, ComfyProfile]:
    """Assembles the fixed profile allowlist from Settings.

    "student" (== DEFAULT_PROFILE_KEY) always exists, backed by the original comfy_*
    fields -- so a caller that sends no model_profile at all keeps getting exactly the
    same behavior as before this feature existed.

    "personnel" is only registered when APP_COMFY_PERSONNEL_CHECKPOINT_NAME or
    APP_COMFY_PERSONNEL_DIFFUSION_MODEL_NAME is actually set -- an unconfigured tier
    fails admission with a clear 400 ("unknown model_profile") instead of silently
    reusing the student model under a different name, which would let a caller believe
    they got a better model when they didn't.
    """
    profiles: dict[str, ComfyProfile] = {
        DEFAULT_PROFILE_KEY: ComfyProfile(
            key=DEFAULT_PROFILE_KEY,
            checkpoint_name=settings.comfy_checkpoint_name,
            model_family=settings.comfy_model_family,
            diffusion_model_name=settings.comfy_diffusion_model_name,
            clip_name=settings.comfy_clip_name,
            vae_name=settings.comfy_vae_name,
            model_sampling_shift=settings.comfy_model_sampling_shift,
            sampler_name=settings.comfy_sampler_name,
            scheduler=settings.comfy_scheduler,
            steps=settings.comfy_steps,
            cfg_scale=settings.comfy_cfg_scale,
            negative_prompt=settings.comfy_negative_prompt,
        )
    }
    if settings.comfy_personnel_checkpoint_name or settings.comfy_personnel_diffusion_model_name:
        profiles["personnel"] = ComfyProfile(
            key="personnel",
            checkpoint_name=settings.comfy_personnel_checkpoint_name,
            model_family=settings.comfy_personnel_model_family,
            diffusion_model_name=settings.comfy_personnel_diffusion_model_name,
            clip_name=settings.comfy_personnel_clip_name,
            vae_name=settings.comfy_personnel_vae_name,
            model_sampling_shift=settings.comfy_personnel_model_sampling_shift,
            sampler_name=settings.comfy_personnel_sampler_name,
            scheduler=settings.comfy_personnel_scheduler,
            steps=settings.comfy_personnel_steps,
            cfg_scale=settings.comfy_personnel_cfg_scale,
            negative_prompt=settings.comfy_personnel_negative_prompt,
        )
    return profiles


def resolve_profile_key(requested: str | None, available: dict[str, ComfyProfile]) -> str:
    """Validates a caller-supplied `model_profile` input against the allowlist.

    None/empty -> DEFAULT_PROFILE_KEY ("student") -- unchanged behavior for every
    existing caller that doesn't send this field at all (chat_router.py's internal
    admission calls, in particular, never will). Anything else not in `available`
    raises UnknownModelProfileError -- fails fast with a 400 rather than silently
    falling back to "student", which would let a caller believe they got "personnel"
    when they actually got "student".
    """
    key = (requested or "").strip() or DEFAULT_PROFILE_KEY
    if key not in available:
        raise UnknownModelProfileError(f"unknown model_profile {key!r}")
    return key


def list_profile_keys(settings: Any) -> frozenset[str]:
    return frozenset(build_profiles(settings).keys())
