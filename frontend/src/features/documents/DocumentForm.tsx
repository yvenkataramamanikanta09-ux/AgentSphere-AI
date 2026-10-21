import { zodResolver } from '@hookform/resolvers/zod'
import { useEffect, useRef, useState } from 'react'
import { useForm } from 'react-hook-form'
import { ApiError } from '../../api/client'
import { documentApi } from '../../api/documents'
import type { DocumentRecord } from '../../api/documents'
import { documentError } from './errors'
import { inferredContentType, metadataSchemaFor } from './validation'
import type { MetadataValues } from './validation'

interface Props {
  workspaceId: string
  record?: DocumentRecord
  fileBacked?: boolean
  disabled: boolean
  onSaved: (record: DocumentRecord) => void
  onCancel: () => void
  onPermissionFailure: () => void
}

export function DocumentForm({ workspaceId, record, fileBacked = false, disabled, onSaved, onCancel, onPermissionFailure }: Props) {
  const [error, setErrorMessage] = useState<string | null>(null)
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const { register, setValue, setError, handleSubmit, formState: { errors, isSubmitting } } = useForm<MetadataValues>({
    resolver: zodResolver(metadataSchemaFor(record?.filename)),
    defaultValues: { filename: record?.filename ?? '', content_type: record?.content_type ?? 'application/pdf', size_bytes: record?.size_bytes },
  })

  async function submit(values: MetadataValues) {
    if (disabled) return
    setErrorMessage(null)
    try {
      const saved = record
        ? await documentApi.update(workspaceId, record.id, fileBacked ? { filename: values.filename } : values)
        : await documentApi.create(workspaceId, values)
      if (mounted.current) onSaved(saved)
    } catch (cause) {
      if (!mounted.current) return
      if (cause instanceof ApiError) {
        for (const field of ['filename', 'content_type', 'size_bytes'] as const) {
          if (cause.fieldErrors[field]) setError(field, { message: cause.fieldErrors[field] })
        }
        if (cause.status === 403) onPermissionFailure()
      }
      setErrorMessage(documentError(cause))
    }
  }

  return <section className="document-form panel" aria-labelledby="document-form-title">
    <div className="document-panel-heading"><div><span className="section-label">DOCUMENT METADATA</span><h3 id="document-form-title">{record ? 'Edit record' : 'Add a document record'}</h3></div><button type="button" className="refresh-button" onClick={onCancel} disabled={isSubmitting}>Cancel</button></div>
    <p className="document-help">{fileBacked ? 'Rename this uploaded record within the same file format. Its content type and measured size are controlled by the server.' : 'Enter metadata only. No file is uploaded or inspected.'}</p>
    <form onSubmit={(event) => { void handleSubmit(submit)(event) }} noValidate>
      <fieldset disabled={disabled || isSubmitting}>
        <div className="field"><label htmlFor="document-filename">Filename</label><input id="document-filename" autoFocus placeholder="research.pdf" aria-invalid={!!errors.filename} aria-describedby={errors.filename ? 'filename-error' : undefined} {...register('filename', { onChange: (event) => { const type = inferredContentType(event.target.value); if (type && !fileBacked) setValue('content_type', type) } })} />{errors.filename && <small id="filename-error" role="alert">{errors.filename.message}</small>}</div>
        <div className="document-form-row"><div className="field"><label htmlFor="document-type">Content type</label><select id="document-type" aria-invalid={!!errors.content_type} aria-describedby={errors.content_type ? 'content-type-error' : undefined} {...(fileBacked ? { value: record?.content_type, disabled: true } : register('content_type'))}><option value="application/pdf">PDF — application/pdf</option><option value="application/vnd.openxmlformats-officedocument.wordprocessingml.document">Word — DOCX</option><option value="text/plain">Plain text — text/plain</option><option value="text/markdown">Markdown — text/markdown</option></select>{fileBacked && <input type="hidden" {...register('content_type')} />}{errors.content_type && <small id="content-type-error" role="alert">{errors.content_type.message}</small>}</div><div className="field"><label htmlFor="document-size">{fileBacked ? 'Measured size (bytes)' : 'Declared size (bytes)'}</label><input id="document-size" type="number" readOnly={fileBacked} min="1" step="1" placeholder="2048" aria-invalid={!!errors.size_bytes} aria-describedby={errors.size_bytes ? 'size-error' : 'size-hint'} {...register('size_bytes', { valueAsNumber: true })} />{errors.size_bytes ? <small id="size-error" role="alert">{errors.size_bytes.message}</small> : <small id="size-hint" className="hint">{fileBacked ? 'Measured from the uploaded original; this value cannot be edited.' : 'The server enforces its configured size limit.'}</small>}</div></div>
      </fieldset>
      {error && <div className="notice error" role="alert">{error}</div>}
      <div className="form-actions"><button className="primary-button" type="submit" disabled={disabled || isSubmitting}>{isSubmitting ? 'Saving…' : record ? 'Save changes' : 'Create record'}</button></div>
    </form>
  </section>
}
