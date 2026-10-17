import { obtainCsrf, request } from './client'
import type { Workspace } from './client'

export interface WorkspacePage {
  items: Workspace[]
  limit: number
  offset: number
  has_more: boolean
}

export const workspaceApi = {
  list: (offset = 0, signal?: AbortSignal) => request<WorkspacePage>(
    `/api/v1/workspaces?limit=20&offset=${offset}`, { signal },
  ),
  get: (id: string, signal?: AbortSignal) => request<Workspace>(
    `/api/v1/workspaces/${encodeURIComponent(id)}`, { signal },
  ),
  async create(name: string): Promise<Workspace> {
    await obtainCsrf()
    return request<Workspace>('/api/v1/workspaces', {
      method: 'POST', body: JSON.stringify({ name }),
    })
  },
}
