import { expect, it } from 'vitest'
import { ApiError } from '../../api/client'
import { fileRequestError, processingFailure } from './fileErrors'
import { MAX_DOCUMENT_FILE_BYTES, safeDownloadFilename, uploadFileError } from './fileValidation'

it('accepts the inclusive actual-byte limit and rejects one byte more and empty files', () => {
  const exact = new File([new Uint8Array(MAX_DOCUMENT_FILE_BYTES)], 'notes.txt', { type: 'text/plain' })
  expect(uploadFileError(exact)).toBeNull()
  expect(uploadFileError(new File([new Uint8Array(MAX_DOCUMENT_FILE_BYTES + 1)], 'notes.txt'))).toContain('10,000,000')
  expect(uploadFileError(new File([], 'notes.txt'))).toContain('not empty')
})

it.each(['../notes.txt', 'CON.txt', '.notes.txt', 'notes.txt ', 'notes\u0000.txt', 'notes.exe', 'notes\u202etxt.pdf'])('rejects unsafe or unsupported filename %s', (filename) => {
  expect(uploadFileError(new File(['hello'], filename))).not.toBeNull()
  expect(safeDownloadFilename(filename)).toBe('document')
})

it.each(['notes.PDF', 'notes.docx', 'notes.txt', 'notes.md', 'notes.markdown'])('accepts supported extension %s without browser MIME', (filename) => {
  expect(uploadFileError(new File(['content'], filename))).toBeNull()
  expect(safeDownloadFilename(filename)).toBe(filename)
})

it('accepts browser MIME hints while leaving actual format verification to the server', () => {
  expect(uploadFileError(new File(['# Heading'], 'notes.md', { type: 'text/plain' }))).toBeNull()
  expect(uploadFileError(new File(['content'], 'notes.docx', { type: 'application/zip' }))).toBeNull()
  expect(uploadFileError(new File(['content'], 'notes.txt', { type: 'application/octet-stream' }))).toBeNull()
})

it('maps status and processing failures without rendering raw error text', () => {
  expect(fileRequestError(new ApiError(503, 'private-storage-path'), 'file')).toContain('schema')
  expect(fileRequestError(new Error('private-storage-path'), 'download')).not.toContain('private-storage-path')
  expect(fileRequestError(new ApiError(0, 'offline'), 'upload')).toContain('may have received')
  expect(processingFailure('encrypted_pdf')).toContain('password protected')
  expect(processingFailure('private-storage-path')).not.toContain('private-storage-path')
})

it.each(['__proto__', 'constructor', 'toString'])('uses a safe message for unknown processing code %s', (code) => {
  expect(processingFailure(code)).toBe('The file could not be processed. Upload a valid copy or contact your workspace administrator.')
})
