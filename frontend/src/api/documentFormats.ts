const contentTypes: Readonly<Record<string, string>> = {
  '.pdf': 'application/pdf',
  '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  '.txt': 'text/plain',
  '.md': 'text/markdown',
  '.markdown': 'text/markdown',
}

/** Browser MIME labels vary. The server validates the file's bytes independently. */
export function canonicalDocumentContentType(filename: string): string | null {
  const dot = filename.lastIndexOf('.')
  return dot < 0 ? null : contentTypes[filename.slice(dot).toLowerCase()] ?? null
}
