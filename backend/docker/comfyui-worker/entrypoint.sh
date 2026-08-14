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
  # The NGC base image ships with an extra pip index (pypi.ngc.nvidia.com) meant for
  # NVIDIA's own internal build network -- unreachable from a normal DGX on a regular
  # network, so pip burns 5 retries with backoff per package against it before falling
  # back to the real pypi.org (observed 2026-08: install still eventually succeeds, just
  # very slowly and noisily). Force public PyPI only, ignoring whatever index config is
  # baked into the image, to skip that entirely.
  PIP_INDEX_URL=https://pypi.org/simple \
  PIP_EXTRA_INDEX_URL= \
  pip install --no-cache-dir --index-url https://pypi.org/simple -r /tmp/requirements.filtered.txt
else
  echo "comfyui-worker: WARNING - no requirements.txt found at /workspace/ComfyUI (bind mount empty/wrong path?)"
fi

# --listen 0.0.0.0 so other containers (the api/scheduler/reconciler services) can reach
# this over the docker network by service name, e.g. http://comfyui-worker-1:8188.
exec python main.py --listen 0.0.0.0 --port 8188 "$@"
