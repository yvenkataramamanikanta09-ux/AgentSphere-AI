import { ApiError } from '../../api/client'

export function documentError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403) return 'Action denied. Your permissions or security token may have changed. Check workspace access, then try again.'
    if (error.status === 404) return 'This record is unavailable in the selected workspace. It may have been deleted or your access may have changed.'
    if (error.status === 413) return 'The declared size exceeds the server’s configured limit. Reduce the size and try again.'
    if (error.status === 422) return 'Check the document metadata and try again.'
    if (error.status === 409) return 'This update conflicts with the current record. Uploaded file sizes and content types are managed by the server. Refresh the record before trying again.'
    return error.message
  }
  return 'Could not complete this document request. Please try again.'
}
