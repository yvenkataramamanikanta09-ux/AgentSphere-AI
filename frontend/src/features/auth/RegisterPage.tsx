import { zodResolver } from '@hookform/resolvers/zod'
import { useState } from 'react'
import { useForm } from 'react-hook-form'
import { Link, Navigate, useNavigate } from 'react-router'
import { ApiError, authApi } from '../../api/client'
import { AuthLayout } from './AuthLayout'
import { useAuth } from './context'
import { registrationSchema } from './validation'
import type { RegistrationValues } from './validation'

export function RegisterPage() {
  const { status } = useAuth()
  const navigate = useNavigate()
  const [serverError, setServerError] = useState<string | null>(null)
  const [showPassword, setShowPassword] = useState(false)
  const { register, handleSubmit, setError, formState: { errors, isSubmitting } } = useForm<RegistrationValues>({ resolver: zodResolver(registrationSchema) })

  if (status === 'authenticated') return <Navigate to="/" replace />

  async function submit(values: RegistrationValues) {
    setServerError(null)
    try {
      await authApi.register(values.email, values.password, values.display_name)
      navigate('/login', { replace: true, state: { registered: true } })
    } catch (error) {
      if (error instanceof ApiError) {
        for (const field of ['email', 'password', 'display_name'] as const) {
          if (error.fieldErrors[field]) setError(field, { message: error.fieldErrors[field] })
        }
      }
      setServerError(error instanceof Error ? error.message : 'Unable to create the account.')
    }
  }

  return <AuthLayout>
    <div className="auth-head"><span className="section-label">GET STARTED</span><h1 tabIndex={-1}>Create your account</h1><p>Your personal workspace will be ready after you register.</p></div>
    {serverError && <div className="notice error" role="alert">{serverError}</div>}
    <form onSubmit={handleSubmit(submit)} noValidate>
      <div className="field"><label htmlFor="register-name">Full name</label><input id="register-name" autoComplete="name" placeholder="Your name" aria-invalid={!!errors.display_name} aria-describedby={errors.display_name ? 'register-name-error' : undefined} {...register('display_name')} />{errors.display_name && <small id="register-name-error" role="alert">{errors.display_name.message}</small>}</div>
      <div className="field"><label htmlFor="register-email">Email address</label><input id="register-email" type="email" autoComplete="username" placeholder="you@company.com" aria-invalid={!!errors.email} aria-describedby={errors.email ? 'register-email-error' : undefined} {...register('email')} />{errors.email && <small id="register-email-error" role="alert">{errors.email.message}</small>}</div>
      <div className="field"><label htmlFor="register-password">Password</label><div className="password-wrap"><input id="register-password" type={showPassword ? 'text' : 'password'} autoComplete="new-password" placeholder="At least 15 characters" aria-invalid={!!errors.password} aria-describedby={errors.password ? 'register-password-error' : 'password-hint'} {...register('password')} /><button type="button" className="reveal" onClick={() => setShowPassword(!showPassword)} aria-label={showPassword ? 'Hide password' : 'Show password'}>{showPassword ? 'Hide' : 'Show'}</button></div>{errors.password ? <small id="register-password-error" role="alert">{errors.password.message}</small> : <small id="password-hint" className="hint">Use a long, unique passphrase.</small>}</div>
      <div className="field"><label htmlFor="register-confirm">Confirm password</label><input id="register-confirm" type="password" autoComplete="new-password" placeholder="Repeat your password" aria-invalid={!!errors.confirm_password} aria-describedby={errors.confirm_password ? 'register-confirm-error' : undefined} {...register('confirm_password')} />{errors.confirm_password && <small id="register-confirm-error" role="alert">{errors.confirm_password.message}</small>}</div>
      <button className="primary-button" type="submit" disabled={isSubmitting || status === 'loading'}>{isSubmitting ? 'Creating account…' : 'Create account'}<span aria-hidden="true">↗</span></button>
    </form>
    <p className="auth-switch">Already have an account? <Link to="/login">Sign in</Link></p>
  </AuthLayout>
}
