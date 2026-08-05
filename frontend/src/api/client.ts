// Small fetch wrapper: always sends cookies (`credentials: 'include'`) for the
// cookie-based session auth used by the backend (see app/api/v1/auth.py), and
// centralizes the base URL / error handling.
//
// IMPORTANT: keep this hostname matching whatever hostname the frontend page itself
// is served from (see .env.example). "127.0.0.1" and "localhost" are different sites
// to a browser, so a SameSite=Lax session cookie set by one will not be sent back on
// a fetch() to the other -- every authenticated call after login/register would
// silently look unauthenticated. This app runs on http://localhost:3000.
export const API_BASE_URL: string =
  (import.meta.env.VITE_API_BASE_URL as string | undefined) || 'http://localhost:8000'

export class ApiError extends Error {
  status: number
  detail: unknown

  constructor(status: number, message: string, detail?: unknown) {
    super(message)
    this.status = status
    this.detail = detail
  }
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'DELETE'
  body?: unknown
  headers?: Record<string, string>
  timeoutMs?: number
}

// Plain fetch() has no timeout: if the backend accepts the TCP connection but never
// responds (e.g. still warming up right after `docker compose up -d --build`, or a
// container mid-restart), the promise just hangs forever -- observed as an infinite
// loading spinner that only clears after mashing refresh a few times until a request
// happens to land after the backend is actually ready. Bound every request so a slow
// backend fails fast with a clear, retryable error instead.
const DEFAULT_TIMEOUT_MS = 12_000

export async function apiFetch<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, headers = {}, timeoutMs = DEFAULT_TIMEOUT_MS } = opts

  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)

  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      method,
      credentials: 'include',
      headers: {
        ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
        ...headers,
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    })
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new ApiError(
        0,
        'The server took too long to respond. It may still be starting up -- try again in a few seconds.',
      )
    }
    throw new ApiError(0, 'Network error: could not reach the server. Is the backend running?')
  } finally {
    clearTimeout(timer)
  }

  if (response.status === 204) {
    return undefined as T
  }

  const isJson = response.headers.get('content-type')?.includes('application/json')
  const data = isJson ? await response.json().catch(() => undefined) : undefined

  if (!response.ok) {
    const detail = (data as { detail?: unknown })?.detail
    const message =
      typeof detail === 'string' ? detail : `Request failed with status ${response.status}`
    throw new ApiError(response.status, message, detail)
  }

  return data as T
}
