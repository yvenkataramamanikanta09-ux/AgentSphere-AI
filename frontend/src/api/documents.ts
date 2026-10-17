import { obtainCsrf, request } from './client'

export type DocumentStatus = 'registered' | 'archived'
export interface DocumentMetadata {
  filename: string
  content_type: string
  size_bytes: number
}
export interface DocumentRecord extends DocumentMetadata {
  id: string
  workspace_id: string
  status: DocumentStatus
  created_at: string
  updated_at: string
}
export interface DocumentPage {
  items: DocumentRecord[]
  limit: number
  offset: number
  has_more: boolean
}
const collection = (workspaceId: string) => `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/documents`
const item = (workspaceId: string, id: string) => `${collection(workspaceId)}/${encodeURIComponent(id)}`

export const documentApi = {
  list(workspaceId: string, offset = 0, status: DocumentStatus | '' = '', signal?: AbortSignal) {
    const query = new URLSearchParams({ limit: '10', offset: String(offset) })
    if (status) query.set('status', status)
    return request<DocumentPage>(`${collection(workspaceId)}?${query}`, { signal })
  },
  get: (workspaceId: string, id: string, signal?: AbortSignal) => request<DocumentRecord>(item(workspaceId, id), { signal }),
  async create(workspaceId: string, metadata: DocumentMetadata) {
    await obtainCsrf()
    return request<DocumentRecord>(collection(workspaceId), { method: 'POST', body: JSON.stringify(metadata) })
  },
  async update(workspaceId: string, id: string, patch: Partial<DocumentMetadata> & { status?: DocumentStatus }) {
    await obtainCsrf()
    return request<DocumentRecord>(item(workspaceId, id), { method: 'PATCH', body: JSON.stringify(patch) })
  },
  async remove(workspaceId: string, id: string) {
    await obtainCsrf()
    return request<void>(item(workspaceId, id), { method: 'DELETE' })
  },
}
