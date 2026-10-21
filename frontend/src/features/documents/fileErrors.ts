import { ApiError } from '../../api/client'

export type FileRequestKind = 'upload' | 'file' | 'sections' | 'download'

export function fileRequestError(error: unknown, kind: FileRequestKind): string {
  if (error instanceof ApiError) {
    switch (error.status) {
      case 0: return kind === 'upload'
        ? 'Connection lost. The server may have received the file. Retry this same upload to check its result safely.'
        : 'Cannot reach the server. Check your connection, then retry.'
      case 400: return 'The upload could not be read. Choose a supported file and try again.'
      case 401: return 'Your session has expired. Sign in again to continue.'
      case 403: return 'Action denied. Your permissions or security token may have changed. Check workspace access, then try again.'
      case 404: return 'This document is unavailable in the selected workspace. It may have been deleted or your access may have changed.'
      case 408: return 'The upload timed out. The server may have received the file. Retry this same upload to check its result safely.'
      case 409: return kind === 'upload'
        ? 'This upload intent cannot be reused for different or deleted content. Choose a file again to start a new upload.'
        : 'The file is no longer available for this action. Refresh file status to check its current availability.'
      case 413: return 'The file exceeds the server’s upload limit. The maximum is 10,000,000 bytes; the server may enforce a lower limit.'
      case 415: return 'The file format, content, or encoding is unsupported. Choose a valid PDF, DOCX, UTF-8 text, or Markdown file.'
      case 422: return 'The file or upload details were rejected. Check its filename, format, and contents before trying again.'
      case 429: return 'Too many requests. Wait before retrying.'
      case 503: return 'File services are temporarily unavailable or the required schema is not ready. Retry when the service is available.'
    }
  }
  return kind === 'upload'
    ? 'The upload result could not be confirmed. The server may have received the file. Retry this same upload to check its result safely.'
    : 'Could not complete this file request. Retry when the service is available.'
}

const processingErrors: Record<string, string> = {
  encrypted_pdf: 'This PDF is password protected. Upload an unencrypted copy.',
  malformed_pdf: 'This PDF could not be read. Upload a valid copy.',
  invalid_pdf: 'This PDF could not be read. Upload a valid copy.',
  malformed_docx: 'This Word document could not be read. Upload a valid DOCX copy.',
  invalid_docx: 'This Word document could not be read. Upload a valid DOCX copy.',
  invalid_utf8: 'This text file is not valid UTF-8. Save a UTF-8 copy and upload it.',
  input_unavailable: 'The original file could not be read for processing.',
  resource_limit: 'The document exceeded the processing limits.',
  extraction_limit: 'The document exceeded the processing limits.',
  extraction_timeout: 'The document took too long to process.',
  text_limit_exceeded: 'The extracted text exceeded the processing limits.',
  section_limit_exceeded: 'The document contains more sections than can be processed.',
  structure_limit_exceeded: 'The document structure exceeded the processing limits.',
  pdf_page_limit: 'The PDF contains more pages than can be processed.',
  docx_expansion_limit: 'The expanded Word document exceeded the processing limits.',
  unsafe_pdf: 'This PDF contains unsupported or unsafe features. Upload a simpler copy.',
  unsafe_docx: 'This Word document contains unsupported or unsafe features. Upload a simpler copy.',
  unsafe_text: 'This text file contains unsupported characters. Save a UTF-8 copy and upload it.',
  unsupported_type: 'The document format is unsupported for text extraction.',
}

export function processingFailure(code: string | null): string {
  return code && Object.hasOwn(processingErrors, code)
    ? processingErrors[code]
    : 'The file could not be processed. Upload a valid copy or contact your workspace administrator.'
}
