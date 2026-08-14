# Runtime image for the comfyui-worker-* docker-compose services. Does NOT bundle
# ComfyUI's source/models -- those are bind-mounted from the host at container start
# (see docker-compose.yml's COMFYUI_HOST_PATH) so every worker shares the exact same
# checkpoints/custom_nodes/output directory a natively-run ComfyUI already uses, with no
# duplication and no rebuild needed when you update ComfyUI or drop in a new model.
#
# Base image: NVIDIA's own NGC PyTorch container, NOT a generic `nvidia/cuda` + `pip
# install torch`. On cutting-edge/ARM64 hardware (e.g. a DGX Spark's Grace Blackwell
# Superchip) a plain PyPI torch wheel can easily fail to detect the GPU at all or pick
# the wrong CUDA build; NGC containers are built and tested by NVIDIA against current
# DGX hardware and are the supported path for exactly this situation.
#
# IMPORTANT -- verify this tag matches your host before relying on it:
#   1. On the DGX, in whatever environment currently runs ComfyUI natively:
#        python -c "import torch; print(torch.__version__, torch.version.cuda)"
#   2. Pick an NGC PyTorch release with matching (or newer, backward-compatible) CUDA at
#        https://docs.nvidia.com/deeplearning/frameworks/pytorch-release-notes/
#   3. If `nvidia-smi` inside the built container (see entrypoint.sh) doesn't show the
#      GPU, or `torch.cuda.is_available()` is False, this tag is the first thing to
#      change -- rebuild with `docker compose build comfyui-worker-1 comfyui-worker-2`.
#
# DGX Spark (Grace Blackwell GB10, compute capability sm_121) specifically: 24.10-py3
# PREDATES Blackwell entirely -- torch loads and reports cuda_available=True, but at
# runtime warns "NVIDIA GB10 with CUDA capability sm_121 is not compatible with the
# current PyTorch installation" and ComfyUI crashes on startup with
# `ModuleNotFoundError: No module named 'torchaudio'` (24.10 doesn't bundle a matching
# torchaudio build at all, and pip can't be allowed to install a generic PyPI torchaudio
# without breaking ABI compatibility with the NGC-custom torch build -- see
# entrypoint.sh's requirements filtering). Confirmed 2026-08: NGC pytorch:25.10-py3 is
# the first tag with verified GB10/sm_121 support (torch 2.9.0a0, CUDA 13.0,
# compute_120 PTX) and bundles a matching torchaudio, so it resolves BOTH problems at
# once. Avoid 25.12-py3 for now -- there are open reports of its own separate
# torchaudio/aarch64 incompatibility on GB10
# (https://forums.developer.nvidia.com/t/incompatibility-of-torchaudio-in-ngc-pytorch-container-25-12-on-dgx-spark-blackwell-gb10/357745).
# If 25.10 itself turns out to have issues, check
# https://forums.developer.nvidia.com/tag/dgx-spark for the current recommended tag.
FROM nvcr.io/nvidia/pytorch:25.10-py3

WORKDIR /workspace/ComfyUI

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
