import { zodResolver } from '@hookform/resolvers/zod'
import { useState } from 'react'
import { useForm } from 'react-hook-form'
import { z } from 'zod'
import { ApiError } from '../../api/client'
import type { Workspace } from '../../api/client'
import { workspaceApi } from '../../api/workspaces'

const schema = z.object({ name: z.string().trim().min(1, 'Enter a workspace name.').max(100, 'Use 100 characters or fewer.') })
type Values = z.infer<typeof schema>

export function CreateWorkspaceForm({ onCreated, onCancel }: { onCreated: (workspace: Workspace) => void; onCancel: () => void }) {
  const [error, setErrorMessage] = useState<string | null>(null)
  const { register, handleSubmit, setError, formState: { errors, isSubmitting } } = useForm<Values>({ resolver: zodResolver(schema) })
  async function submit(values: Values) {
    setErrorMessage(null)
    try {
      onCreated(await workspaceApi.create(values.name))
    } catch (cause) {
      if (cause instanceof ApiError && cause.fieldErrors.name) setError('name', { message: cause.fieldErrors.name })
      setErrorMessage(cause instanceof Error ? cause.message : 'Could not create this workspace.')
    }
  }
  return <section className="workspace-create panel" aria-labelledby="create-title">
    <div><span className="section-label">A NEW SPACE FOR YOUR WORK</span><h2 id="create-title">Create a workspace</h2><p>You’ll be the owner. Give it a name your team can recognize.</p></div>
    <form onSubmit={handleSubmit(submit)} noValidate>
      <div className="field"><label htmlFor="workspace-name">Workspace name</label><input id="workspace-name" placeholder="e.g. Product research" autoFocus aria-invalid={!!errors.name} aria-describedby={errors.name ? 'workspace-name-error' : undefined} {...register('name')} />{errors.name && <small id="workspace-name-error" role="alert">{errors.name.message}</small>}</div>
      {error && <div className="notice error" role="alert">{error}</div>}
      <div className="form-actions"><button className="secondary-button" type="button" onClick={onCancel} disabled={isSubmitting}>Cancel</button><button className="primary-button" type="submit" disabled={isSubmitting}>{isSubmitting ? 'Creating…' : 'Create workspace'}</button></div>
    </form>
  </section>
}
