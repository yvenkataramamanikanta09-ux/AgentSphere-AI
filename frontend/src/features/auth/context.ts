import { createContext, useContext } from 'react'
import type { User } from '../../api/client'

export type SessionState = {
  status: 'loading' | 'guest' | 'authenticated'
  user: User | null
  error: string | null
}

export interface AuthContextValue extends SessionState {
  login: (email: string, password: string) => Promise<void>
  logout: () => Promise<void>
}

export const AuthContext = createContext<AuthContextValue | null>(null)

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used within AuthProvider')
  return context
}
