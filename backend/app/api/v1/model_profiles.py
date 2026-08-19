"""GET /v1/model-profiles: lets a caller (agentflow's own backend) discover which
`model_profile` values are currently valid and what each one means, instead of us
hand-maintaining a static doc that goes stale every time a profile is added/removed in
`.env`.

Deliberately does NOT expose checkpoint_name/diffusion_model_name/clip_name/vae_name --
those are internal implementation details (which physical file backs a profile), not
something a caller needs to make a request. Exposing them would also invite exactly the
"agentflow assembles its own checkpoint+clip+vae combo" pattern this endpoint exists to
avoid -- see app/domain/jobs/comfy_profiles.py's and app/domain/jobs/comfy_overrides.py's
docstrings for why a mismatched combo fails silently-wrong (bad image, no clear error)
rather than loudly. A caller picks a profile by NAME; the exact files behind it are ours
to manage.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.deps import get_app_settings, get_current_user
from app.core.config import Settings
from app.db.models import User
from app.domain.jobs.comfy_overrides import build_allowlists
from app.domain.jobs.comfy_profiles import DEFAULT_PROFILE_KEY, build_profiles

router = APIRouter(prefix="/model-profiles", tags=["model-profiles"])


class ModelProfileOut(BaseModel):
    key: str
    is_default: bool
    model_family: str
    default_steps: int
    default_cfg_scale: float


class OverrideRangeOut(BaseModel):
    # Applies uniformly across every profile -- model_overrides.steps/cfg_scale are
    # clamped by one global range (APP_COMFY_OVERRIDE_MIN/MAX_*), not per-profile. See
    # app/domain/jobs/comfy_overrides.py.
    min_steps: int
    max_steps: int
    min_cfg_scale: float
    max_cfg_scale: float


class ModelProfilesOut(BaseModel):
    profiles: list[ModelProfileOut]
    override_range: OverrideRangeOut


@router.get("", response_model=ModelProfilesOut)
async def list_model_profiles(
    settings: Settings = Depends(get_app_settings),
    user: User = Depends(get_current_user),
) -> ModelProfilesOut:
    profiles = build_profiles(settings)
    allowlists = build_allowlists(settings)
    return ModelProfilesOut(
        profiles=[
            ModelProfileOut(
                key=p.key,
                is_default=(p.key == DEFAULT_PROFILE_KEY),
                model_family=p.model_family,
                default_steps=p.steps,
                default_cfg_scale=p.cfg_scale,
            )
            for p in profiles.values()
        ],
        override_range=OverrideRangeOut(
            min_steps=allowlists.min_steps,
            max_steps=allowlists.max_steps,
            min_cfg_scale=allowlists.min_cfg_scale,
            max_cfg_scale=allowlists.max_cfg_scale,
        ),
    )
