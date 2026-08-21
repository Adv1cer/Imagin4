# DGX Spark — production model download + 2-worker bring-up

Target layout on the **host** ComfyUI checkout (`COMFYUI_HOST_PATH`, default
`/home/nvidia/comfyui/ComfyUI`):

| Profile | Family | Role |
|---------|--------|------|
| `student` (default) | `z_image_turbo` | Fast lane (~8 steps) |
| `personnel` | `qwen_image` Lightning 4-step | Text / poster quality |

Workers: **2 only** (`comfyui-worker-1` `:8188`, `comfyui-worker-2` `:8189`). Do not
enable `CUDA_MPS_*` unless the host MPS daemon is running (see `MPS_RUNBOOK.md`).

## 1. Download models (run on the DGX host)

Files are flat basenames under the usual ComfyUI folders (from
[Comfy-Org/z_image_turbo](https://huggingface.co/Comfy-Org/z_image_turbo) and
[lightx2v/Qwen-Image-2512-Lightning](https://huggingface.co/lightx2v/Qwen-Image-2512-Lightning)).

```bash
export COMFYUI_ROOT="${COMFYUI_HOST_PATH:-/home/nvidia/comfyui/ComfyUI}"
mkdir -p "$COMFYUI_ROOT/models/"{diffusion_models,text_encoders,vae}
pip install -U "huggingface_hub[cli]"

# --- student: Z-Image Turbo ---
hf download Comfy-Org/z_image_turbo z_image_turbo_bf16.safetensors \
  --local-dir "$COMFYUI_ROOT/models/diffusion_models"
hf download Comfy-Org/z_image_turbo qwen_3_4b.safetensors \
  --local-dir "$COMFYUI_ROOT/models/text_encoders"
hf download Comfy-Org/z_image_turbo ae.safetensors \
  --local-dir "$COMFYUI_ROOT/models/vae"

# --- personnel: Qwen-Image-2512 Lightning 4-step UNet ---
hf download lightx2v/Qwen-Image-2512-Lightning \
  qwen_image_2512_fp8_e4m3fn_scaled_comfyui_4steps_v1.0.safetensors \
  --local-dir "$COMFYUI_ROOT/models/diffusion_models"

# Qwen TE + VAE (skip if already present from prior Qwen setup)
hf download Comfy-Org/Qwen-Image_ComfyUI \
  split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors \
  --local-dir /tmp/qwen-te
cp -n /tmp/qwen-te/split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors \
  "$COMFYUI_ROOT/models/text_encoders/"
hf download Comfy-Org/Qwen-Image_ComfyUI \
  split_files/vae/qwen_image_vae.safetensors \
  --local-dir /tmp/qwen-vae
cp -n /tmp/qwen-vae/split_files/vae/qwen_image_vae.safetensors \
  "$COMFYUI_ROOT/models/vae/"

ls -lh "$COMFYUI_ROOT/models/diffusion_models"/z_image_turbo_bf16.safetensors \
       "$COMFYUI_ROOT/models/diffusion_models"/qwen_image_2512_fp8_e4m3fn_scaled_comfyui_4steps_v1.0.safetensors \
       "$COMFYUI_ROOT/models/text_encoders"/qwen_3_4b.safetensors \
       "$COMFYUI_ROOT/models/text_encoders"/qwen_2.5_vl_7b_fp8_scaled.safetensors \
       "$COMFYUI_ROOT/models/vae"/ae.safetensors \
       "$COMFYUI_ROOT/models/vae"/qwen_image_vae.safetensors
```

If a hub path nests under `split_files/…`, copy the `.safetensors` up to the flat
folder so the basename matches `APP_COMFY_*` / allowlists exactly.

## 2. Sync Imaginv4 config + rebuild workers

On the DGX, from the Imaginv4 `backend/` checkout (after pulling/copying these changes):

```bash
cd ~/Imaginv4/backend   # adjust path

# Confirm .env matches production dual-lane (student=ZIT, personnel=Qwen Lightning,
# slots=2, workers :8188+:8189). See repo backend/.env template on the Windows copy.

docker compose build comfyui-worker-1 comfyui-worker-2
docker compose up -d --force-recreate \
  comfyui-worker-1 comfyui-worker-2 \
  api scheduler reconciler

docker compose ps
curl -sS http://127.0.0.1:8188/system_stats | head
curl -sS http://127.0.0.1:8189/system_stats | head
```

Expect worker logs: `Starting server`, `Device: cuda:0 NVIDIA GB10`, then on first job
`got prompt` / `Prompt executed`. First cold load of each UNet is slow; warm ZIT should
be far under a minute.

## 3. Smoke-test both profiles

```bash
# student (default) → Z-Image Turbo
curl -sS -X POST http://127.0.0.1:8000/v1/generations \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"workflow_name":"image_basic","workflow_version":"v1","inputs":{"prompt":"a sunny campus courtyard","aspect_ratio":"1:1","resolution":"1K"}}'

# personnel → Qwen Lightning
curl -sS -X POST http://127.0.0.1:8000/v1/generations \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"workflow_name":"image_basic","workflow_version":"v1","inputs":{"prompt":"event poster with clear Thai title","aspect_ratio":"3:4","resolution":"1K","model_profile":"personnel"}}'
```

Admin load-test: keep concurrency ≤ `APP_DEFAULT_COMFY_ACTIVE_SLOTS` (2).

## 4. Notes

- Switching profiles on the **same** worker with `--highvram` reloads weights (cold cost).
  That is expected; with 2 workers and mixed traffic both stacks will eventually stay warm.
- Do **not** use `run_nvidia_gpu_fast_fp16_accumulation` for these FP8/AuraFlow stacks
  expecting a free win — that flag targets FP16 GEMMs.
- Scale to 3–4 workers only after `MPS_RUNBOOK.md` is validated on this GB10.
