import { z } from 'zod'

export const contentTypes = {
  '.pdf': 'application/pdf',
  '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  '.txt': 'text/plain',
  '.md': 'text/markdown',
  '.markdown': 'text/markdown',
} as const

export function inferredContentType(filename: string): string | undefined {
  const extension = filename.slice(filename.lastIndexOf('.')).toLowerCase()
  return contentTypes[extension as keyof typeof contentTypes]
}

export const metadataSchema = z.object({
  filename: z.string().min(1, 'Enter a filename.').max(255, 'Use 255 characters or fewer.')
    .refine((name) => name === name.trim() && !name.startsWith('.') && !name.endsWith('.'), 'Remove surrounding whitespace and leading or trailing dots.')
    .refine((name) => !/[<>:"/\\|?*]/u.test(name) && [...name].every((char) => char === ' ' || !/[\p{C}\p{Z}]/u.test(char)), 'Use a filename without paths, reserved characters, or control characters.')
    .refine((name) => !/^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$/i.test(name.split('.')[0]), 'Reserved device names are not allowed.')
    .refine((name) => !!inferredContentType(name), 'Use .pdf, .docx, .txt, .md, or .markdown.'),
  content_type: z.string().min(1, 'Choose a content type.'),
  size_bytes: z.number('Enter a positive whole number of bytes.').int('Use whole bytes.').positive('Size must be greater than zero.').max(Number.MAX_SAFE_INTEGER, 'Size is too large to represent safely.'),
}).refine((metadata) => metadata.content_type === inferredContentType(metadata.filename), {
  path: ['content_type'], message: 'Content type must match the filename extension.',
})

export type MetadataValues = z.infer<typeof metadataSchema>
