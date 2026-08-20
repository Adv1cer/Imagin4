// Core load-test engine for the /admin page. Four scenario modes map to the four
// traffic shapes actually worth distinguishing on this backend (see project memory
// capacity_planning_utcc.md and backend/load_tests/*.js for the reasoning):
//
//   burst           -- N requests dispatched within a short window (default 100ms) to
//                       simulate "100 คนพร้อมกัน". Exercises the Redis admission gate
//                       (admission_max_inflight=150, 503 when exceeded) and the
//                       per-user rate limiter (rl_message_per_min=60) if totalRequests
//                       is large relative to virtualUsers.length.
//   batched-rate    -- batchSize requests every batchIntervalMs, repeated batchCount
//                       times, to simulate "มาทีละ 10-20 ต่อนาที". spreadWithinBatch
//                       toggles whether each batch itself fires as a sub-burst or is
//                       evenly spread across the interval.
//   sequential      -- one full request (submit + wait for terminal job state) at a
//                       time. Gives a clean, uncontended baseline generation time with
//                       zero queueing/GPU-contention noise -- compare against the other
//                       modes to see how much slowdown is caused by concurrency itself.
//   concurrency-pool-- a fixed number of workers (poolConcurrency) continuously pull the
//                       next request and run it start-to-finish, refilling as soon as a
//                       slot frees up. This is a sustained/soak load at a *constant*
//                       concurrency level, as opposed to burst's instantaneous spike.
//
// Every request is timed client-side from the moment its POST is fired
// (dispatchedAtMs) to the moment its job reaches a terminal state
// (completedAtMs) -- see types.ts's comment on RequestResult.totalGenerationMs. This is
// necessary, not a shortcut: GET /v1/jobs/{id} does not expose queued_at/started_at/
// finished_at (confirmed against backend/app/api/v1/jobs.py), so there is no
// server-provided generation-time field to read instead.

import {
  AdminApiError,
  TERMINAL_JOB_STATES,
  getJob,
  postAgentMessage,
  type AgentMessagePayload,
} from './adminApi'
import { pickPrompt } from './promptBank'
import type { RequestResult, ScenarioConfig, TestPrompt } from './types'

export interface VirtualUser {
  id: string
  token: string
}

export interface RequestBuilderOptions {
  /** '' = use each prompt's own modelProfile. Set to force one profile, e.g. an
   *  intentionally-wrong value like "staff" to probe the 400 rejection path. */
  modelProfileOverride: string
  stepsOverride: number | null
  cfgScaleOverride: number | null
  /** '' = use each prompt's own negativePrompt. */
  negativePromptOverride: string
  useExactText: boolean
  skipPromptDesign: boolean
  assumeImage: boolean
}

export const DEFAULT_REQUEST_OPTIONS: RequestBuilderOptions = {
  modelProfileOverride: '',
  stepsOverride: null,
  cfgScaleOverride: null,
  negativePromptOverride: '',
  useExactText: false,
  skipPromptDesign: true,
  assumeImage: true,
}

function buildPayload(
  seq: number,
  vu: VirtualUser,
  prompt: TestPrompt,
  reqOpts: RequestBuilderOptions,
): AgentMessagePayload {
  const modelProfile = reqOpts.modelProfileOverride || prompt.modelProfile
  const negativePrompt = reqOpts.negativePromptOverride || prompt.negativePrompt

  const modelOverrides: NonNullable<AgentMessagePayload['model_overrides']> = {}
  if (reqOpts.stepsOverride !== null) modelOverrides.steps = reqOpts.stepsOverride
  if (reqOpts.cfgScaleOverride !== null) modelOverrides.cfg_scale = reqOpts.cfgScaleOverride
  if (negativePrompt) modelOverrides.negative_prompt = negativePrompt

  const rand = Math.random().toString(36).slice(2, 8)
  return {
    external_conversation_id: `admintest-${vu.id}-${seq}-${rand}`,
    text: prompt.text,
    client_message_id: `admintest-${vu.id}-${seq}-${Date.now()}-${rand}`,
    ...(reqOpts.useExactText && prompt.exactText ? { exact_text: prompt.exactText } : {}),
    model_profile: modelProfile,
    ...(Object.keys(modelOverrides).length ? { model_overrides: modelOverrides } : {}),
    skip_prompt_design: reqOpts.skipPromptDesign,
    assume_image: reqOpts.assumeImage,
    aspect_ratio: prompt.aspectRatio,
    resolution: prompt.resolution,
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function freshResult(seq: number, vu: VirtualUser, prompt: TestPrompt, scheduledAtMs: number): RequestResult {
  return {
    seq,
    virtualUser: vu.id,
    promptId: prompt.id,
    promptCategory: prompt.category,
    scheduledAtMs,
    dispatchedAtMs: null,
    submitRespondedAtMs: null,
    submitLatencyMs: null,
    httpStatus: null,
    jobId: null,
    jobType: null,
    finalState: null,
    completedAtMs: null,
    totalGenerationMs: null,
    pollCount: 0,
    errorCode: null,
    errorDetail: null,
    outcome: 'pending',
    workerName: null,
    outputCount: 0,
  }
}

async function runOneRequest(
  seq: number,
  vu: VirtualUser,
  prompt: TestPrompt,
  cfg: ScenarioConfig,
  reqOpts: RequestBuilderOptions,
  baseUrl: string,
  scheduledAtMs: number,
  isCancelled: () => boolean,
  onUpdate: (r: RequestResult) => void,
): Promise<void> {
  const result = freshResult(seq, vu, prompt, scheduledAtMs)
  const dispatchedAt = performance.now()
  result.dispatchedAtMs = dispatchedAt
  result.outcome = 'submitting'
  onUpdate({ ...result })

  const payload = buildPayload(seq, vu, prompt, reqOpts)

  try {
    const { status, data } = await postAgentMessage(baseUrl, vu.token, payload, cfg.submitTimeoutMs)
    const submitRespondedAt = performance.now()
    result.submitRespondedAtMs = submitRespondedAt
    result.submitLatencyMs = submitRespondedAt - dispatchedAt
    result.httpStatus = status

    if (!data || data.type !== 'image_job' || !data.job) {
      result.outcome = 'non_image'
      result.finalState = data?.type ?? 'unknown'
      result.completedAtMs = submitRespondedAt
      result.totalGenerationMs = submitRespondedAt - dispatchedAt
      onUpdate({ ...result })
      return
    }

    const jobId = data.job.id
    result.jobId = jobId
    result.jobType = data.job.kind
    result.outcome = 'polling'
    onUpdate({ ...result })

    if (TERMINAL_JOB_STATES.has(data.job.state)) {
      // POST /v1/agent/message's own job field (GenerationOut) is the slimmer shape and
      // doesn't carry worker_name/result -- only GET /v1/jobs/{id} does. This branch only
      // fires if the backend somehow resolves a job synchronously within the POST call
      // itself (not observed against this backend's ~300s Qwen-Image jobs in practice);
      // worker attribution for it would need one extra GET, which isn't worth adding for
      // a path that's normally unreachable.
      finalize(result, dispatchedAt, data.job.state, data.job.error_code ?? null, data.job.error_detail ?? null, null, 0)
      onUpdate({ ...result })
      return
    }

    const deadline = dispatchedAt + cfg.maxWaitMs
    while (!isCancelled()) {
      if (performance.now() >= deadline) {
        result.outcome = 'poll_timeout'
        result.completedAtMs = performance.now()
        result.totalGenerationMs = result.completedAtMs - dispatchedAt
        onUpdate({ ...result })
        return
      }
      await sleep(cfg.pollIntervalMs)
      if (isCancelled()) return
      result.pollCount += 1
      try {
        const jobResp = await getJob(baseUrl, vu.token, jobId)
        const job = jobResp.data
        // Worker attribution is known as soon as the job is dispatched, well before it
        // reaches a terminal state -- surface it on every poll (not just the final one)
        // so the gallery/table can show "which worker" while a job is still generating.
        if (job?.worker_name) result.workerName = job.worker_name
        if (job && TERMINAL_JOB_STATES.has(job.state)) {
          const outputs = (job.result as { outputs?: unknown[] } | null)?.outputs
          finalize(result, dispatchedAt, job.state, job.error_code, job.error_detail, job.worker_name ?? null, outputs?.length ?? 0)
          onUpdate({ ...result })
          return
        }
        onUpdate({ ...result })
      } catch (err) {
        // A single flaky poll shouldn't fail the whole request -- keep retrying until
        // the deadline. Record the last poll error in case it explains a later timeout.
        result.errorDetail = err instanceof Error ? err.message : String(err)
        onUpdate({ ...result })
      }
    }
  } catch (err) {
    const submitRespondedAt = performance.now()
    result.submitRespondedAtMs = submitRespondedAt
    if (err instanceof AdminApiError) {
      result.httpStatus = err.status
      result.errorCode = err.kind
      result.errorDetail = err.message
      result.outcome =
        err.kind === 'timeout' ? 'submit_timeout' : err.kind === 'network' ? 'network_error' : 'failed'
    } else {
      result.outcome = 'network_error'
      result.errorDetail = err instanceof Error ? err.message : String(err)
    }
    result.completedAtMs = submitRespondedAt
    result.totalGenerationMs = submitRespondedAt - dispatchedAt
    onUpdate({ ...result })
  }
}

function finalize(
  result: RequestResult,
  dispatchedAt: number,
  state: string,
  errorCode: string | null,
  errorDetail: string | null,
  workerName: string | null,
  outputCount: number,
) {
  result.finalState = state
  result.errorCode = errorCode
  result.errorDetail = errorDetail
  result.completedAtMs = performance.now()
  result.totalGenerationMs = result.completedAtMs - dispatchedAt
  result.outcome = state === 'succeeded' ? 'succeeded' : 'failed'
  if (workerName) result.workerName = workerName
  result.outputCount = outputCount
}

export function computeSchedule(cfg: ScenarioConfig): { seq: number; delayMs: number }[] {
  if (cfg.mode === 'burst') {
    return Array.from({ length: cfg.totalRequests }, (_, seq) => ({
      seq,
      delayMs: cfg.burstWindowMs > 0 ? Math.random() * cfg.burstWindowMs : 0,
    }))
  }
  if (cfg.mode === 'batched-rate') {
    const out: { seq: number; delayMs: number }[] = []
    for (let b = 0; b < cfg.batchCount; b++) {
      for (let i = 0; i < cfg.batchSize; i++) {
        const seq = b * cfg.batchSize + i
        const delayMs = b * cfg.batchIntervalMs + (cfg.spreadWithinBatch ? i * (cfg.batchIntervalMs / cfg.batchSize) : 0)
        out.push({ seq, delayMs })
      }
    }
    return out
  }
  return []
}

export function totalForMode(cfg: ScenarioConfig): number {
  if (cfg.mode === 'batched-rate') return cfg.batchSize * cfg.batchCount
  return cfg.totalRequests
}

export interface RunHandle {
  cancel: () => void
  done: Promise<void>
}

export function runScenario(
  baseUrl: string,
  virtualUsers: VirtualUser[],
  cfg: ScenarioConfig,
  reqOpts: RequestBuilderOptions,
  onUpdate: (r: RequestResult) => void,
): RunHandle {
  let cancelled = false
  const timers: ReturnType<typeof setTimeout>[] = []
  const vuFor = (seq: number) => virtualUsers[seq % virtualUsers.length]

  const cancel = () => {
    cancelled = true
    timers.forEach(clearTimeout)
  }

  const spawnAt = (seq: number, delayMs: number): Promise<void> =>
    new Promise((resolve) => {
      const timer = setTimeout(() => {
        if (cancelled) {
          resolve()
          return
        }
        const vu = vuFor(seq)
        const prompt = pickPrompt(seq)
        runOneRequest(seq, vu, prompt, cfg, reqOpts, baseUrl, performance.now(), () => cancelled, onUpdate).then(
          resolve,
        )
      }, delayMs)
      timers.push(timer)
    })

  async function run() {
    if (virtualUsers.length === 0) return

    if (cfg.mode === 'sequential') {
      for (let seq = 0; seq < cfg.totalRequests; seq++) {
        if (cancelled) break
        const vu = vuFor(seq)
        const prompt = pickPrompt(seq)
        // eslint-disable-next-line no-await-in-loop
        await runOneRequest(seq, vu, prompt, cfg, reqOpts, baseUrl, performance.now(), () => cancelled, onUpdate)
      }
      return
    }

    if (cfg.mode === 'concurrency-pool') {
      let next = 0
      const total = cfg.totalRequests
      const worker = async () => {
        for (;;) {
          if (cancelled) return
          const seq = next
          next += 1
          if (seq >= total) return
          const vu = vuFor(seq)
          const prompt = pickPrompt(seq)
          // eslint-disable-next-line no-await-in-loop
          await runOneRequest(seq, vu, prompt, cfg, reqOpts, baseUrl, performance.now(), () => cancelled, onUpdate)
        }
      }
      const workerCount = Math.max(1, Math.min(cfg.poolConcurrency, total))
      await Promise.all(Array.from({ length: workerCount }, worker))
      return
    }

    const schedule = computeSchedule(cfg)
    await Promise.all(schedule.map(({ seq, delayMs }) => spawnAt(seq, delayMs)))
  }

  const done = run()
  return { cancel, done }
}
