import { useEffect } from 'react'
import { Link, Route, Routes, useLocation } from 'react-router'
import { AuthProvider } from './features/auth/AuthContext'
import { useAuth } from './features/auth/context'
import { LoginPage } from './features/auth/LoginPage'
import { RegisterPage } from './features/auth/RegisterPage'
import { WorkspaceDashboard } from './features/workspaces/WorkspaceDashboard'

function ApplicationRoutes() {
  const { status } = useAuth()
  const { pathname } = useLocation()
  useEffect(() => {
    window.scrollTo(0, 0)
    if (status === 'loading') return
    document.title = `${pathname === '/login' ? 'Sign in' : pathname === '/register' ? 'Create account' : pathname === '/' ? 'Workspace' : 'Page not found'} | AgentSphere AI`
    const heading = document.querySelector<HTMLElement>('main h1')
    heading?.focus()
  }, [pathname, status])
  if (status === 'loading') return <div className="page-loading" role="status">Checking your session…</div>
  return <Routes>
    <Route path="/" element={<WorkspaceDashboard />} />
    <Route path="/login" element={<LoginPage />} />
    <Route path="/register" element={<RegisterPage />} />
    <Route path="*" element={<main className="not-found"><h1 tabIndex={-1}>Page not found</h1><Link to="/">Return home</Link></main>} />
  </Routes>
}

export default function App() {
  return <AuthProvider><ApplicationRoutes /></AuthProvider>
}
