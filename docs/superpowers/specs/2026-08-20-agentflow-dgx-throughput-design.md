# Agentflow + DGX Spark throughput / reliability

**Goal:** `POST /v1/agent/message` can admit 100–200 concurrent end users while one ComfyUI worker on DGX Spark stays busy with minimal idle, and jobs never silently stick.

## §1 Capacity (GPU turnaround)

- Claim budget = `default_*_active_slots + comfy_pending_buffer - count(DB dispatched|running for backend kinds)`.
- Heartbeat still marks workers online/offline; no longer the sole free-slot signal.
- `APP_COMFY_PENDING_BUFFER` default `0` (serialize on Spark); set `1` to prefetch into Comfy pending.
- `mark_succeeded` clears stale `error_code` / `error_detail`.

## §2 Retry / orphan

- `get_status`: not in history+queue → `unknown` (not eternal `running`).
- Reconciler: `unknown` + lease expired → fail/retry; genuine `running` → renew lease in DB.
- Use existing `generation_jobs.available_at` as retry `not_before`; claim skips rows with `available_at > now()`.
- `mark_retry_wait` sets `available_at = now + backoff`.

## §3 Agent admission (shared API key)

- Rate limit `/v1/agent/message` by `(user_id, external_conversation_id)`.
- Queue cap for agent/chat admits with `conversation_id` uses per-conversation backlog instead of service-user backlog.
- Persist `conversation_id` on enqueue; keep `global_queue_cap` + AdmissionGate as fleet backstops.
- `wait=true` remains opt-in / low-volume only (unchanged).

## Out of scope

- Comfy WebSocket event-driven claim
- Multi-GPU worker scoring / WRR
- Changing default `default_comfy_active_slots` above 1
