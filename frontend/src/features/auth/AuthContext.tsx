import { useCallback, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { ApiError, authApi, clearCsrf, obtainCsrf, onSessionExpired } from '../../api/client'
import { AuthContext } from './context'
import type { SessionState } from './context'

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<SessionState>({ status: 'loading', user: null, error: null })

  useEffect(() => onSessionExpired(() => {
    setState((current) => current.status === 'authenticated'
      ? { status: 'guest', user: null, error: 'Your session has expired. Sign in again to continue.' }
      : current)
  }), [])

  useEffect(() => {
    let active = true
    async function restore() {
      try {
        const user = await authApi.me()
        await obtainCsrf()
        if (active) setState({ status: 'authenticated', user, error: null })
      } catch (error) {
        if (!active) return
        const offline = error instanceof ApiError && error.status !== 401
        setState({ status: 'guest', user: null, error: offline ? (error as Error).message : null })
      }
    }
    void restore()
    return () => { active = false }
  }, [])

  useEffect(() => {
    if (state.status !== 'authenticated') return
    let active = true
    async function checkSession() {
      try {
        const user = await authApi.me()
        if (active) setState((current) => current.status === 'authenticated' ? { ...current, user } : current)
      } catch { /* 401 is handled by the shared expiry listener; transient failures keep the session UI. */ }
    }
    window.addEventListener('focus', checkSession)
    return () => { active = false; window.removeEventListener('focus', checkSession) }
  }, [state.status])

  const login = useCallback(async (email: string, password: string) => {
    const user = await authApi.login(email, password)
    setState({ status: 'authenticated', user, error: null })
  }, [])

  const logout = useCallback(async () => {
    await authApi.logout()
    clearCsrf()
    setState({ status: 'guest', user: null, error: null })
  }, [])

  return <AuthContext.Provider value={{ ...state, login, logout }}>{children}</AuthContext.Provider>
}
