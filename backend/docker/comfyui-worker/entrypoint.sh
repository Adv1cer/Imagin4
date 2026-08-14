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
# time -- main.py crashes on startup with ModuleNotFoundError before any job can ever
# run, even though Chet's actual workflows (qwen_image / checkpoint image generation)
# never touch LTX audio nodes. Neither 24.10-py3 nor 25.10-py3 NGC images bundle a
# matching torchaudio (confirmed 2026-08), and it is deliberately excluded from the
# requirements.txt install above (see that step's comment) since a normal `pip install
# torchaudio` would pull in a torchaudio built against a PUBLIC torch version like
# 2.9.0, which does NOT satisfy the installed local build 2.9.0a0+<hash>.nv25.10 under
# pip's version comparison (alpha "a0" sorts BEFORE the final release) -- pip would try
# to "helpfully" reinstall/upgrade torch to satisfy torchaudio's declared dependency,
# destroying the carefully NGC-matched build this whole image exists to protect.
# --no-deps sidesteps that entirely: install torchaudio's files only, accept whatever
# CUDA/ABI mismatch results (it only needs to IMPORT cleanly -- nothing here ever
# exercises actual audio ops), and if that one line fails at runtime instead of import
# time the next fallback is stubbing an empty `torchaudio` package in site-packages.
echo "comfyui-worker: installing torchaudio --no-deps (see comment above -- needed only so ComfyUI's LTX audio_vae import doesn't crash startup; never exercised for image workflows)..."
PIP_CONFIG_FILE=/dev/null \
pip install --no-cache-dir --no-deps --index-url https://pypi.org/simple torchaudio \
  || echo "comfyui-worker: WARNING - torchaudio install failed; ComfyUI will likely still crash on the audio_vae import -- see entrypoint.sh's comment for the stub-module fallback"

# --listen 0.0.0.0 so other containers (the api/scheduler/reconciler services) can reach
# this over the docker network by service name, e.g. http://comfyui-worker-1:8188.
exec python main.py --listen 0.0.0.0 --port 8188 "$@"
