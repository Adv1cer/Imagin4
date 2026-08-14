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
FROM nvcr.io/nvidia/pytorch:24.10-py3

WORKDIR /workspace/ComfyUI

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
