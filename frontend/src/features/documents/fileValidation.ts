import { canonicalDocumentContentType } from '../../api/documentFormats'
import { metadataSchema } from './validation'

export const MAX_DOCUMENT_FILE_BYTES = 10_000_000
export const DOCUMENT_FILE_ACCEPT = '.pdf,.docx,.txt,.md,.markdown'

export function uploadFileError(file: File): string | null {
  const contentType = canonicalDocumentContentType(file.name)
  const parsed = metadataSchema.safeParse({ filename: file.name, content_type: contentType ?? '', size_bytes: file.size })
  const filenameIssue = parsed.success ? undefined : parsed.error.issues.find((issue) => issue.path[0] === 'filename')
  if (filenameIssue) return filenameIssue.message
  if (!contentType) return 'Use .pdf, .docx, .txt, .md, or .markdown.'
  if (!Number.isSafeInteger(file.size) || file.size < 1) return 'Choose a file that is not empty.'
  if (file.size > MAX_DOCUMENT_FILE_BYTES) return 'Choose a file of 10,000,000 bytes or fewer (10 MB).'
  // OS/browser MIME hints vary (for example, DOCX can be application/zip).
  // The API sends the canonical type and verifies the actual uploaded bytes.
  return null
}

export function safeDownloadFilename(filename: string): string {
  const result = metadataSchema.safeParse({ filename, content_type: canonicalDocumentContentType(filename) ?? '', size_bytes: 1 })
  return result.success ? filename : 'document'
}
