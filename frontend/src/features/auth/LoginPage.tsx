import { zodResolver } from '@hookform/resolvers/zod'
import { useState } from 'react'
import { useForm } from 'react-hook-form'
import { Link, Navigate, useLocation, useNavigate } from 'react-router'
import { ApiError } from '../../api/client'
import { AuthLayout } from './AuthLayout'
import { useAuth } from './context'
import { loginSchema } from './validation'
import type { LoginValues } from './validation'

export function LoginPage() {
  const { status, login, error: restoreError } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const registered = (location.state as { registered?: boolean } | null)?.registered === true
  const [serverError, setServerError] = useState<string | null>(null)
  const [showPassword, setShowPassword] = useState(false)
  const { register, handleSubmit, setError, formState: { errors, isSubmitting } } = useForm<LoginValues>({ resolver: zodResolver(loginSchema) })

  if (status === 'authenticated') return <Navigate to="/" replace />

  async function submit(values: LoginValues) {
    setServerError(null)
    try {
      await login(values.email, values.password)
      navigate('/', { replace: true })
    } catch (error) {
      if (error instanceof ApiError) {
        for (const field of ['email', 'password'] as const) {
          if (error.fieldErrors[field]) setError(field, { message: error.fieldErrors[field] })
        }
      }
      setServerError(error instanceof Error ? error.message : 'Unable to sign in.')
    }
  }

  return <AuthLayout>
    <div className="auth-head"><span className="section-label">WELCOME BACK</span><h1 tabIndex={-1}>Sign in to your account</h1><p>Pick up where your work left off.</p></div>
    {registered && <div className="notice success" role="status">Your account is ready. Sign in to continue.</div>}
    {restoreError && <div className="notice error" role="alert">{restoreError}</div>}
    {serverError && <div className="notice error" role="alert">{serverError}</div>}
    <form onSubmit={handleSubmit(submit)} noValidate>
      <div className="field"><label htmlFor="login-email">Email address</label><input id="login-email" type="email" autoComplete="username" placeholder="you@company.com" aria-invalid={!!errors.email} aria-describedby={errors.email ? 'login-email-error' : undefined} {...register('email')} />{errors.email && <small id="login-email-error" role="alert">{errors.email.message}</small>}</div>
      <div className="field"><div className="field-top"><label htmlFor="login-password">Password</label></div><div className="password-wrap"><input id="login-password" type={showPassword ? 'text' : 'password'} autoComplete="current-password" placeholder="Enter your password" aria-invalid={!!errors.password} aria-describedby={errors.password ? 'login-password-error' : undefined} {...register('password')} /><button type="button" className="reveal" onClick={() => setShowPassword(!showPassword)} aria-label={showPassword ? 'Hide password' : 'Show password'}>{showPassword ? 'Hide' : 'Show'}</button></div>{errors.password && <small id="login-password-error" role="alert">{errors.password.message}</small>}</div>
      <button className="primary-button" type="submit" disabled={isSubmitting || status === 'loading'}>{isSubmitting ? 'Signing in…' : 'Sign in'}<span aria-hidden="true">↗</span></button>
    </form>
    <p className="auth-switch">New to AgentSphere? <Link to="/register">Create an account</Link></p>
  </AuthLayout>
}
