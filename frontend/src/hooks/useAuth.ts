import { useCallback, useEffect, useState } from 'react'
import { login as apiLogin, logout as apiLogout, me as apiMe } from '../api/endpoints'
import { ApiError } from '../api/client'
import type { MeResponse } from '../api/types'

type AuthStatus = 'checking' | 'authed' | 'anon'

export function useAuth() {
  const [status, setStatus] = useState<AuthStatus>('checking')
  const [user, setUser] = useState<MeResponse | null>(null)

  // On app load, try GET /v1/auth/me first to detect an already-active session.
  useEffect(() => {
    let cancelled = false
    apiMe()
      .then((u) => {
        if (cancelled) return
        setUser(u)
        setStatus('authed')
      })
      .catch(() => {
        if (cancelled) return
        setStatus('anon')
      })
    return () => {
      cancelled = true
    }
  }, [])

  const login = useCallback(async (email: string, password: string) => {
    await apiLogin({ email, password })
    const u = await apiMe()
    setUser(u)
    setStatus('authed')
  }, [])

  const logout = useCallback(async () => {
    try {
      await apiLogout()
    } catch {
      // even if logout fails server-side, drop the local session state
    }
    setUser(null)
    setStatus('anon')
  }, [])

  return { status, user, login, logout }
}

export { ApiError }
