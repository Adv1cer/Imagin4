# Trying NVIDIA MPS to fix the worker-3/4 CUBLAS crash

See `README.md`'s "Known issue: worker-3/4 CUBLAS crash under 4-worker load" for the
full root-cause writeup. Short version: with 4 independent `comfyui-worker-*` containers
each opening their own CUDA context against ONE physical GB10 GPU (no NVIDIA MPS), the
3rd/4th context landing on top of the two already-resident ~27GB Qwen-Image loads
crashed with `CUDA error: CUBLAS_STATUS_INTERNAL_ERROR`. MPS (Multi-Process Service) lets
multiple processes share ONE GPU context instead of each fighting for their own —
NVIDIA's own documented fix for exactly this class of multi-process contention.

`docker-compose.yml`'s `comfyui-worker-1..4` keep `ipc: host` only. **Do not** set
`CUDA_MPS_PIPE_DIRECTORY` / `CUDA_MPS_LOG_DIRECTORY` (or mount `/tmp/nvidia-mps*`) until
the host MPS control daemon is actually running. Those env vars are **not** inert: on
DGX Spark (2026-08-21) pointing clients at an empty/missing MPS control socket hung
first CUDA context init indefinitely (0% CPU after ComfyUI's "Setting user directory").
The daemon can only run on the host, not inside a container, because it has to broker
access to the real GPU driver across every container that wants to share it.

**Important caveat before you start** (confirmed via NVIDIA's own developer forum,
2026-03, not guessed): GB10's unified-memory architecture has a known gap where
`nvmlDeviceGetMemoryInfo` returns `NVML_ERROR_NOT_SUPPORTED` (no discrete framebuffer to
report on) — this is what backs `nvidia-smi`'s VRAM column. In practice this means
**`nvidia-smi` VRAM numbers on the Spark may be incomplete/unreliable**, so don't judge
success or failure by watching them. Judge success by whether worker-3/4 actually
complete a real generation under load without the CUBLAS error — that's the ground truth
here, not the memory column.

## 1. Start the MPS control daemon on the HOST

```bash
# Pick a UID that will own the MPS pipe -- must match (or be accessible to) whatever UID
# the comfyui-worker containers run as. If unsure, run as root for the first test.
export CUDA_MPS_PIPE_DIRECTORY=/tmp/nvidia-mps
export CUDA_MPS_LOG_DIRECTORY=/tmp/nvidia-mps-log
mkdir -p "$CUDA_MPS_PIPE_DIRECTORY" "$CUDA_MPS_LOG_DIRECTORY"

sudo -E nvidia-cuda-mps-control -d
# Confirm it's actually listening:
ls -la /tmp/nvidia-mps/   # should show `control`, `nvidia-mps` sockets
```

If `nvidia-cuda-mps-control` isn't found, it ships with the NVIDIA driver package
(`nvidia-utils`/`cuda-toolkit` depending on your distro) — check
`dpkg -L nvidia-driver-<version> | grep mps` or the DGX OS's package manager.

## 2. Add MPS *client* wiring to the workers, then recreate

Default `docker-compose.yml` deliberately omits `CUDA_MPS_*` (see note above). For each
`comfyui-worker-N` you want on MPS, add under `environment:` / `volumes:`:

```yaml
    environment:
      PYTHONUNBUFFERED: "1"
      CUDA_MPS_PIPE_DIRECTORY: /tmp/nvidia-mps
      CUDA_MPS_LOG_DIRECTORY: /tmp/nvidia-mps-log
    volumes:
      - ${COMFYUI_HOST_PATH:-/home/nvidia/comfyui/ComfyUI}:/workspace/ComfyUI
      - /tmp/nvidia-mps:/tmp/nvidia-mps
      - /tmp/nvidia-mps-log:/tmp/nvidia-mps-log
```

Confirm the host daemon is up (`ls /tmp/nvidia-mps/` shows a `control` socket) **before**
recreating, or CUDA init will hang again.

```bash
cd backend
docker compose up -d --force-recreate comfyui-worker-1 comfyui-worker-2 comfyui-worker-3 comfyui-worker-4
```

## 3. Widen the API's worker list back to 4 (currently downgraded to 2 in `.env`)

Edit `backend/.env`:

```
APP_COMFY_WORKER_BASE_URLS_CSV=http://10.7.2.63:8188,http://10.7.2.63:8189,http://10.7.2.63:8190,http://10.7.2.63:8191
APP_DEFAULT_COMFY_ACTIVE_SLOTS=4
```

```bash
docker compose up -d --force-recreate api
```

## 4. Reproduce the original crash scenario and watch for the ACTUAL signal

```bash
# Same load test that produced the original crash (see README) -- watch dmesg on the
# HOST in a separate terminal while this runs, for a Xid error alongside any CUBLAS error
# (confirms a real GPU-level fault vs. an application-level context issue):
sudo dmesg -w | grep -i xid &

docker compose --profile load-test run --rm k6 run /scripts/hundred_concurrent_burst.js
```

Then check each worker's own log for the crash signature:

```bash
for w in comfyui-worker-1 comfyui-worker-2 comfyui-worker-3 comfyui-worker-4; do
  echo "=== $w ==="
  docker compose logs --tail=50 "$w" | grep -i "CUBLAS\|Prompt executed"
done
```

**Pass**: all 4 workers show a real `Prompt executed in ~55-60 seconds` line (not
`0.35 seconds`), no `CUBLAS_STATUS_INTERNAL_ERROR` anywhere. **Fail**: the crash still
happens — MPS alone didn't fix it (possible next step: an explicit
`CUDA_MPS_ACTIVE_THREAD_PERCENTAGE` cap per worker so no single context can seize the
whole GPU's thread pool, or falling back to 2 workers permanently and putting the extra
throughput budget toward faster workflow/quantization instead of more processes).

## 5. Roll back if it doesn't help

```bash
# .env: put APP_COMFY_WORKER_BASE_URLS_CSV / APP_DEFAULT_COMFY_ACTIVE_SLOTS back to the
# verified-stable 2-worker values, then:
docker compose up -d --force-recreate api
sudo nvidia-cuda-mps-control -f <<< "quit"   # stop the MPS daemon if you no longer need it
```

Update README.md's "Known issue" section with whatever you observe either way (pass or
fail) — this is exactly the kind of hardware-specific result that shouldn't be re-derived
from scratch next time.
