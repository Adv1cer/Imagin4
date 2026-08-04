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
}

export async function apiFetch<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, headers = {} } = opts

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
    })
  } catch {
    throw new ApiError(0, 'Network error: could not reach the server. Is the backend running?')
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
