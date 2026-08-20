// Standalone fetch helpers for the /admin load-test tool.
//
// Deliberately NOT reusing ../api/client.ts's apiFetch: that helper hardcodes
// credentials:'include' + a single build-time API_BASE_URL, because the rest of the app
// always talks to one cookie-authed same-origin backend. This tool needs to hit an
// arbitrary operator-supplied host (e.g. http://10.7.2.63:8000) with an arbitrary
// operator-supplied bearer API key (possibly several, round-robined across "virtual
// users" -- see scenarios.ts), so both the base URL and the auth header are per-call
// parameters instead.
//
// Auth: backend/app/api/deps.py accepts `Authorization: Bearer imgn_...` (API key) as an
// alternative to the cookie-based session. Bearer is what this tool uses -- see
// backend/scripts/create_api_key.py to mint one. Browser EventSource can't set custom
// headers, so job completion is tracked by polling GET /v1/jobs/{id}, matching what the
// project's own k6 scripts (backend/load_tests/hundred_concurrent_burst.js) do.

export interface AgentMessagePayload {
  external_conversation_id: string
  text: string
  client_message_id?: string
  exact_text?: string[]
  model_profile?: string
  model_overrides?: {
    steps?: number
    cfg_scale?: number
    negative_prompt?: string
  }
  skip_prompt_design?: boolean
  assume_image?: boolean
  aspect_ratio?: string
  resolution?: string
}

export interface AgentMessageResponse {
  type: string
  user_message: unknown
  assistant_message?: unknown
  job?: {
    id: string
    state: string
    kind: string
    error_code?: string | null
    error_detail?: string | null
  } | null
  pending_action?: unknown
}

export interface JobStatusResponse {
  id: string
  state: string
  kind: string
  current_attempt: number
  error_code: string | null
  error_detail: string | null
  result: unknown
}

export interface ModelProfilesResponse {
  profiles: {
    key: string
    is_default: boolean
    model_family: string
    default_steps: number
    default_cfg_scale: number
  }[]
  override_range: {
    min_steps: number
    max_steps: number
    min_cfg_scale: number
    max_cfg_scale: number
  }
}

export class AdminApiError extends Error {
  kind: 'http' | 'network' | 'timeout'
  status: number | null
  detail: unknown

  constructor(kind: 'http' | 'network' | 'timeout', message: string, status: number | null = null, detail?: unknown) {
    super(message)
    this.kind = kind
    this.status = status
    this.detail = detail
  }
}

function normalizeBaseUrl(baseUrl: string): string {
  return baseUrl.trim().replace(/\/+$/, '')
}

async function rawFetch<T>(
  baseUrl: string,
  path: string,
  token: string,
  opts: { method?: 'GET' | 'POST'; body?: unknown; timeoutMs: number },
): Promise<{ status: number; data: T | null }> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), opts.timeoutMs)
  let response: Response
  try {
    response = await fetch(`${normalizeBaseUrl(baseUrl)}${path}`, {
      method: opts.method ?? 'GET',
      headers: {
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(opts.body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      },
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      signal: controller.signal,
      // No credentials: this tool authenticates purely via the bearer header so it works
      // cross-origin against a remote host without relying on (HttpOnly, same-site) cookies.
      credentials: 'omit',
    })
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new AdminApiError('timeout', `Request to ${path} timed out after ${opts.timeoutMs}ms`)
    }
    // Covers DNS failure, connection refused, and CORS rejection alike -- the browser
    // collapses all three into the same opaque TypeError, so we can't distinguish them here.
    throw new AdminApiError(
      'network',
      `Network error calling ${path}. If the target host is correct, this is very likely CORS (origin not in the backend's APP_CORS_ALLOW_ORIGINS_CSV allowlist) or the API key being wrong/expired -- check the browser devtools Network/Console tab for the real reason.`,
    )
  } finally {
    clearTimeout(timer)
  }

  const isJson = response.headers.get('content-type')?.includes('application/json')
  const data = isJson ? ((await response.json().catch(() => null)) as T | null) : null

  if (!response.ok) {
    const detail = (data as { detail?: unknown } | null)?.detail
    throw new AdminApiError(
      'http',
      typeof detail === 'string' ? detail : `HTTP ${response.status}`,
      response.status,
      detail,
    )
  }

  return { status: response.status, data }
}

export function postAgentMessage(
  baseUrl: string,
  token: string,
  payload: AgentMessagePayload,
  timeoutMs: number,
): Promise<{ status: number; data: AgentMessageResponse | null }> {
  return rawFetch<AgentMessageResponse>(baseUrl, '/v1/agent/message', token, {
    method: 'POST',
    body: payload,
    timeoutMs,
  })
}

export function getJob(
  baseUrl: string,
  token: string,
  jobId: string,
  timeoutMs = 10_000,
): Promise<{ status: number; data: JobStatusResponse | null }> {
  return rawFetch<JobStatusResponse>(baseUrl, `/v1/jobs/${jobId}`, token, { timeoutMs })
}

export function getModelProfiles(
  baseUrl: string,
  token: string,
  timeoutMs = 10_000,
): Promise<{ status: number; data: ModelProfilesResponse | null }> {
  return rawFetch<ModelProfilesResponse>(baseUrl, '/v1/model-profiles', token, { timeoutMs })
}

export const TERMINAL_JOB_STATES = new Set(['succeeded', 'failed', 'cancelled'])
