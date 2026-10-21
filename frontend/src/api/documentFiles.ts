import { ApiError, obtainCsrf, request, requestBlob, uploadRequest } from './client'
import { canonicalDocumentContentType } from './documentFormats'
import type { DocumentRecord } from './documents'

export type ProcessingStatus = 'queued' | 'processing' | 'ready' | 'failed'

export interface DocumentFileMetadata {
  id: string
  document_id: string
  original_filename: string
  sha256: string
  uploaded_by: string
  uploaded_at: string
  processing_status: ProcessingStatus
  processed_at: string | null
  error_code: string | null
  warning_code: string | null
  download_available: boolean
}

export interface UploadResult {
  document: DocumentRecord
  file: DocumentFileMetadata
  replayed: boolean
}

export interface DocumentSection {
  id: string
  ordinal: number
  text: string
  page_number: number | null
  heading: string | null
  line_start: number | null
  line_end: number | null
}

export interface DocumentSectionPage {
  document_id: string
  items: DocumentSection[]
  limit: number
  offset: number
  has_more: boolean
}

const collection = (workspaceId: string) => `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/documents`
const item = (workspaceId: string, id: string) => `${collection(workspaceId)}/${encodeURIComponent(id)}`

export const fileApi = {
  async upload(workspaceId: string, file: File, key: string, options: {
    signal?: AbortSignal
    onProgress?: (percent: number | null) => void
  } = {}): Promise<UploadResult> {
    const type = canonicalDocumentContentType(file.name)
    if (!type) throw new ApiError(422, 'Choose a PDF, DOCX, TXT, or Markdown file.', { file: 'Supported extensions are .pdf, .docx, .txt, .md, and .markdown.' })
    await obtainCsrf(options.signal)
    const body = new FormData()
    body.append('file', file.slice(0, file.size, type), file.name)
    return uploadRequest<UploadResult>(`${collection(workspaceId)}/uploads`, body, {
      ...options, headers: { 'Idempotency-Key': key },
    })
  },
  get: (workspaceId: string, id: string, signal?: AbortSignal) =>
    request<DocumentFileMetadata>(`${item(workspaceId, id)}/file`, { signal }),
  sections(workspaceId: string, id: string, limit = 20, offset = 0, signal?: AbortSignal) {
    const query = new URLSearchParams({ limit: String(limit), offset: String(offset) })
    return request<DocumentSectionPage>(`${item(workspaceId, id)}/sections?${query}`, { signal })
  },
  download: (workspaceId: string, id: string, signal?: AbortSignal) =>
    requestBlob(`${item(workspaceId, id)}/download`, { signal }),
}
