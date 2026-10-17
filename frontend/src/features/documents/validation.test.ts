import { expect, it } from 'vitest'
import { metadataSchema } from './validation'

it.each(['../report.pdf', '.hidden.pdf', ' report.pdf', 'report.pdf ', 'CON.pdf', 'LPT1.txt', 'report.exe', 'a\nb.pdf', 'report:2026.pdf', 'a\u00a0b.pdf'])('rejects unsafe or unsupported filename %j', (filename) => {
  expect(metadataSchema.safeParse({ filename, content_type: 'application/pdf', size_bytes: 10 }).success).toBe(false)
})
it.each([0, -1, 1.5, NaN, Number.MAX_SAFE_INTEGER + 1])('rejects invalid declared size %s', (size_bytes) => {
  expect(metadataSchema.safeParse({ filename: 'report.pdf', content_type: 'application/pdf', size_bytes }).success).toBe(false)
})
it.each([
  ['report.PDF', 'application/pdf'], ['report.docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'],
  ['notes.txt', 'text/plain'], ['notes.md', 'text/markdown'], ['notes.markdown', 'text/markdown'],
])('accepts supported metadata for %s', (filename, content_type) => {
  expect(metadataSchema.safeParse({ filename, content_type, size_bytes: 2048 }).success).toBe(true)
})
it('rejects a mismatched content type', () => {
  expect(metadataSchema.safeParse({ filename: 'report.pdf', content_type: 'text/plain', size_bytes: 1 }).success).toBe(false)
})
