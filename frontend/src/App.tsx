import { useEffect, useState } from 'react'
import { Link, Navigate, Route, Routes, useLocation } from 'react-router'
import { AuthProvider } from './features/auth/AuthContext'
import { useAuth } from './features/auth/context'
import { Brand } from './features/auth/AuthLayout'
import { LoginPage } from './features/auth/LoginPage'
import { RegisterPage } from './features/auth/RegisterPage'

function Home() {
  const { status, user, logout } = useAuth()
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  if (status === 'loading') return <div className="page-loading" role="status">Checking your session…</div>
  if (status === 'guest') return <Navigate to="/login" replace />
  async function signOut() {
    setBusy(true)
    setError(null)
    try { await logout() } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not sign out.')
    } finally { setBusy(false) }
  }
  return <div className="signed-in-page">
    <header className="signed-in-header"><Brand /><button type="button" className="text-button" onClick={signOut} disabled={busy}>{busy ? 'Signing out…' : 'Sign out'}</button></header>
    <main className="signed-in-card"><span className="section-label">ACCOUNT CONNECTED</span><div className="signed-in-icon" aria-hidden="true">✓</div><h1>Welcome, {user?.display_name}.</h1><p>Your secure session is active. Your workspace dashboard is the next stage of AgentSphere AI.</p><div className="account-detail"><span>Signed in as</span><strong>{user?.email}</strong></div>{error && <div className="notice error" role="alert">{error}</div>}</main>
  </div>
}

function ApplicationRoutes() {
  const { status } = useAuth()
  const { pathname } = useLocation()
  useEffect(() => { window.scrollTo(0, 0) }, [pathname])
  if (status === 'loading') return <div className="page-loading" role="status">Checking your session…</div>
  return <Routes>
    <Route path="/" element={<Home />} />
    <Route path="/login" element={<LoginPage />} />
    <Route path="/register" element={<RegisterPage />} />
    <Route path="*" element={<main className="not-found"><h1>Page not found</h1><Link to="/">Return home</Link></main>} />
  </Routes>
}

export default function App() {
  return <AuthProvider><ApplicationRoutes /></AuthProvider>
}
