import { useCallback, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { ApiError, authApi, clearCsrf, obtainCsrf } from '../../api/client'
import { AuthContext } from './context'
import type { SessionState } from './context'

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<SessionState>({ status: 'loading', user: null, error: null })

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
