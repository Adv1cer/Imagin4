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

Comfy-Org packs Z-Image under `split_files/…` — **not** repo root (a bare
`hf download … z_image_turbo_bf16.safetensors` returns “File not found”).

```bash
export COMFYUI_ROOT="${COMFYUI_HOST_PATH:-/home/nvidia/comfyui/ComfyUI}"
mkdir -p "$COMFYUI_ROOT/models/"{diffusion_models,text_encoders,vae}

# --- student: Z-Image Turbo (paths under split_files/) ---
hf download Comfy-Org/z_image_turbo \
  split_files/diffusion_models/z_image_turbo_bf16.safetensors \
  --local-dir /tmp/zit-dl
hf download Comfy-Org/z_image_turbo \
  split_files/text_encoders/qwen_3_4b.safetensors \
  --local-dir /tmp/zit-dl
hf download Comfy-Org/z_image_turbo \
  split_files/vae/ae.safetensors \
  --local-dir /tmp/zit-dl

cp -n /tmp/zit-dl/split_files/diffusion_models/z_image_turbo_bf16.safetensors \
  "$COMFYUI_ROOT/models/diffusion_models/"
cp -n /tmp/zit-dl/split_files/text_encoders/qwen_3_4b.safetensors \
  "$COMFYUI_ROOT/models/text_encoders/"
cp -n /tmp/zit-dl/split_files/vae/ae.safetensors \
  "$COMFYUI_ROOT/models/vae/"

# --- personnel: Qwen Lightning 4-step UNet (file IS at repo root) ---
hf download lightx2v/Qwen-Image-2512-Lightning \
  qwen_image_2512_fp8_e4m3fn_scaled_comfyui_4steps_v1.0.safetensors \
  --local-dir "$COMFYUI_ROOT/models/diffusion_models"

# Qwen TE + VAE (skip if already present)
hf download Comfy-Org/Qwen-Image_ComfyUI \
  split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors \
  --local-dir /tmp/qwen-dl
hf download Comfy-Org/Qwen-Image_ComfyUI \
  split_files/vae/qwen_image_vae.safetensors \
  --local-dir /tmp/qwen-dl
cp -n /tmp/qwen-dl/split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors \
  "$COMFYUI_ROOT/models/text_encoders/"
cp -n /tmp/qwen-dl/split_files/vae/qwen_image_vae.safetensors \
  "$COMFYUI_ROOT/models/vae/"

ls -lh "$COMFYUI_ROOT/models/diffusion_models"/z_image_turbo_bf16.safetensors \
       "$COMFYUI_ROOT/models/diffusion_models"/qwen_image_2512_fp8_e4m3fn_scaled_comfyui_4steps_v1.0.safetensors \
       "$COMFYUI_ROOT/models/text_encoders"/qwen_3_4b.safetensors \
       "$COMFYUI_ROOT/models/text_encoders"/qwen_2.5_vl_7b_fp8_scaled.safetensors \
       "$COMFYUI_ROOT/models/vae"/ae.safetensors \
       "$COMFYUI_ROOT/models/vae"/qwen_image_vae.safetensors
```

Optional smaller ZIT UNet on hub (same folder): `z_image_turbo_int8_convrot.safetensors`
(~6.2GB) or `z_image_turbo_nvfp4.safetensors` (~4.5GB) — only switch
`APP_COMFY_DIFFUSION_MODEL_NAME` after confirming ComfyUI on GB10 loads that quant.

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
