#!/usr/bin/env bash
# Entrypoint for the comfyui-worker-* docker-compose services. ComfyUI's own source is
# bind-mounted to /workspace/ComfyUI (see docker-compose.yml), NOT baked into the image,
# so dependency install has to happen at container start against whatever requirements.txt
# is on the mounted volume right now -- see Dockerfile's comment for why.
set -euo pipefail

cd /workspace/ComfyUI

echo "comfyui-worker: checking GPU visibility..."
nvidia-smi || echo "comfyui-worker: WARNING - nvidia-smi failed; GPU may not be visible to this container (check docker-compose.yml's GPU reservation and that the NVIDIA Container Toolkit is installed on the host)"
python -c "import torch; print('comfyui-worker: torch', torch.__version__, 'cuda available:', torch.cuda.is_available())" \
  || echo "comfyui-worker: WARNING - could not import torch / check CUDA availability"

if [ -f requirements.txt ]; then
  echo "comfyui-worker: installing ComfyUI's requirements.txt (skipping torch/torchvision/torchaudio -- the base image's NGC-matched build must not be overwritten by a generic PyPI wheel)..."
  grep -viE '^(torch|torchvision|torchaudio)([<>=! ]|$)' requirements.txt > /tmp/requirements.filtered.txt || true
  # The NGC base image ships an /etc/pip.conf (or equivalent) with
  # extra-index-url=https://pypi.ngc.nvidia.com -- meant for NVIDIA's own internal build
  # network, unreachable from a normal DGX. `--index-url` on the CLI only overrides the
  # PRIMARY index; it does NOT clear extra-index-url from a config file, and setting
  # PIP_EXTRA_INDEX_URL="" as an env var was ALSO observed NOT to override it in practice
  # (verified live 2026-08 on Chet's DGX -- "Looking in indexes: pypi.org, pypi.ngc..."
  # still printed and every new package still burned 5 retries against the unreachable
  # ngc host before falling back to pypi.org). The only fix that actually works is to
  # bypass the config file entirely via PIP_CONFIG_FILE=/dev/null, so nothing baked into
  # the image can supply an extra-index-url no matter how pip merges env vs config.
  PIP_CONFIG_FILE=/dev/null \
  pip install --no-cache-dir --index-url https://pypi.org/simple -r /tmp/requirements.filtered.txt
else
  echo "comfyui-worker: WARNING - no requirements.txt found at /workspace/ComfyUI (bind mount empty/wrong path?)"
fi

# ComfyUI core (comfy/sd.py -> comfy/ldm/lightricks/vae/audio_vae.py, used for
# Lightricks LTX-Video's audio VAE) unconditionally imports torchaudio at module load
# time -- main.py crashes on startup before any job can ever run, even though Chet's
# actual workflows (qwen_image / checkpoint image generation) never touch LTX audio
# nodes. Verified against the real upstream source (audio_vae.py on GitHub, 2026-08):
# torchaudio.functional / torchaudio.transforms are only ever referenced INSIDE method
# bodies (AudioPreprocessor.resample / .waveform_to_mel), never at module or
# class-definition scope -- so the module only needs `import torchaudio` itself to
# succeed; nothing downstream calls into torchaudio unless someone actually builds and
# runs an LTX audio-VAE graph, which this deployment never does.
#
# Tried and rejected: `pip install --no-deps torchaudio` DOES install and DOES satisfy
# the plain `import torchaudio` line, but torchaudio eagerly loads its own compiled C
# extension (_torchaudio.abi3.so) inside its OWN __init__.py at import time, and that
# extension was built against a different libtorch ABI than this image's NGC-custom
# 2.9.0a0+<hash>.nv25.10 build -- confirmed live on Chet's DGX:
#   OSError: .../_torchaudio.abi3.so: undefined symbol: torch_library_impl
# No public torchaudio wheel is going to be ABI-matched to an NGC-internal torch build,
# so no version/index choice fixes this -- a real torchaudio install is a dead end here.
#
# Fix: don't install real torchaudio at all. Write a minimal stub package directly into
# site-packages so `import torchaudio` succeeds trivially (no C extension, nothing to
# mismatch). `functional`/`transforms` are stub objects that raise a clear, actionable
# error ONLY if something actually tries to use them -- which never happens for image
# generation, so this is invisible in normal operation.
echo "comfyui-worker: stubbing out torchaudio (see comment above -- real installs are ABI-incompatible with this image's NGC torch build, and LTX audio nodes are never used here)..."
SITE_PACKAGES="$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
pip uninstall -y torchaudio >/dev/null 2>&1 || true
rm -rf "${SITE_PACKAGES:?}/torchaudio" "${SITE_PACKAGES:?}"/torchaudio-*.dist-info
mkdir -p "$SITE_PACKAGES/torchaudio"
cat > "$SITE_PACKAGES/torchaudio/__init__.py" <<'PYEOF'
"""Stub torchaudio -- NOT a real install.

Installed by docker/comfyui-worker/entrypoint.sh so ComfyUI core's unconditional
`import torchaudio` (comfy/ldm/lightricks/vae/audio_vae.py, for LTX-Video's audio VAE)
doesn't crash startup. A real torchaudio build is ABI-incompatible with this image's
NGC-custom torch build (see entrypoint.sh). Only actually using audio VAE nodes will
hit this -- ordinary image generation never touches it.
"""


class _MissingTorchaudioFeature:
    def __getattr__(self, name):
        raise RuntimeError(
            "torchaudio is stubbed out in this comfyui-worker container (see "
            "docker/comfyui-worker/entrypoint.sh) -- LTX-Video audio VAE nodes are not "
            "usable here. A real, ABI-matched torchaudio build would be needed for "
            "this image's torch version to support audio nodes."
        )


functional = _MissingTorchaudioFeature()
transforms = _MissingTorchaudioFeature()
PYEOF
echo "comfyui-worker: torchaudio stub written to $SITE_PACKAGES/torchaudio"

# --listen 0.0.0.0 so other containers (the api/scheduler/reconciler services) can reach
# this over the docker network by service name, e.g. http://comfyui-worker-1:8188.
exec python main.py --listen 0.0.0.0 --port 8188 "$@"
