// Types for the /admin load-test tool. Kept separate from ../api/types.ts because this
// tool talks to an arbitrary backend host + bearer token the operator types in (see
// adminApi.ts), not the cookie-authed same-origin backend the rest of the app assumes.

export interface TestPrompt {
  id: string
  category: string
  text: string
  exactText?: string[]
  negativePrompt?: string
  aspectRatio: string
  resolution: string
  modelProfile: 'student' | 'personnel'
  note?: string
}

export type ScenarioMode = 'burst' | 'batched-rate' | 'sequential' | 'concurrency-pool'

export interface ScenarioConfig {
  mode: ScenarioMode
  /** Total number of requests to fire. Ignored by batched-rate (derived from batchSize x batchCount). */
  totalRequests: number
  /** burst only: spread the N dispatches randomly across this window instead of all at t=0. */
  burstWindowMs: number
  /** batched-rate only */
  batchSize: number
  batchIntervalMs: number
  batchCount: number
  spreadWithinBatch: boolean
  /** concurrency-pool only: number of simultaneous in-flight submit+poll workers */
  poolConcurrency: number
  /** shared: how often to poll GET /v1/jobs/{id} while a job is non-terminal */
  pollIntervalMs: number
  /** shared: give up polling (mark as "timed_out") after this long since dispatch */
  maxWaitMs: number
  /** shared: abort the initial POST if it hangs this long */
  submitTimeoutMs: number
}

export const DEFAULT_SCENARIO: ScenarioConfig = {
  mode: 'burst',
  // Match APP_DEFAULT_COMFY_ACTIVE_SLOTS (2 on DGX Spark). Bursting far above worker
  // count only builds a queue and produces poll_timeout rows -- not higher GPU throughput.
  totalRequests: 10,
  burstWindowMs: 100,
  batchSize: 2,
  batchIntervalMs: 60_000,
  batchCount: 6,
  spreadWithinBatch: false,
  poolConcurrency: 2,
  pollIntervalMs: 1500,
  // Warm Lightning 4-step jobs are ~20–50s; cold first load of the ~20GB UNet can still
  // approach ~5 minutes. 600s keeps headroom for cold start + a short queue behind 2
  // workers. Raise further only when intentionally testing deep backlog.
  maxWaitMs: 600_000,
  submitTimeoutMs: 15_000,
}

export type RequestOutcome =
  | 'pending'      // scheduled, not dispatched yet
  | 'submitting'   // POST in flight
  | 'polling'      // 202 accepted, job non-terminal, polling for completion
  | 'succeeded'    // job reached terminal state "succeeded"
  | 'failed'       // job reached terminal state "failed" / "cancelled", or POST returned non-2xx
  | 'network_error'// POST or poll threw (DNS/CORS/connection refused/etc)
  | 'submit_timeout' // POST itself didn't respond within submitTimeoutMs
  | 'poll_timeout' // job never reached a terminal state within maxWaitMs
  | 'non_image'    // backend classified the message as chat / confirmation_required, not image_job

export interface RequestResult {
  seq: number
  virtualUser: string
  promptId: string
  promptCategory: string
  scheduledAtMs: number
  dispatchedAtMs: number | null
  submitRespondedAtMs: number | null
  submitLatencyMs: number | null
  httpStatus: number | null
  jobId: string | null
  jobType: string | null
  finalState: string | null
  completedAtMs: number | null
  /** completedAtMs - dispatchedAtMs -- the number the user actually cares about ("เจนกี่นาที") */
  totalGenerationMs: number | null
  pollCount: number
  errorCode: string | null
  errorDetail: string | null
  outcome: RequestOutcome
  /** Which comfyui-worker-N instance handled this job (see backend/app/api/v1/jobs.py's
   *  JobOut.worker_name) -- null until the first successful poll response that includes
   *  it, or permanently null against an unpatched backend that doesn't return the field. */
  workerName: string | null
  /** Number of generated images in the job's terminal result.outputs[] -- 0 until known. */
  outputCount: number
}

export function isTerminalOutcome(o: RequestOutcome): boolean {
  return (
    o === 'succeeded' ||
    o === 'failed' ||
    o === 'network_error' ||
    o === 'submit_timeout' ||
    o === 'poll_timeout' ||
    o === 'non_image'
  )
}

export interface RunSummary {
  startedAtMs: number
  finishedAtMs: number | null
  total: number
  dispatched: number
  succeeded: number
  failed: number
  networkError: number
  submitTimeout: number
  pollTimeout: number
  nonImage: number
}
