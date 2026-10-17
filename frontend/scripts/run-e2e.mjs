import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const python = fileURLToPath(new URL(process.platform === 'win32'
  ? '../../backend/.venv/Scripts/python.exe' : '../../backend/.venv/bin/python', import.meta.url))
if (!existsSync(python)) throw new Error('The existing backend Poetry environment is required.')
const result = spawnSync(python, [fileURLToPath(new URL('./run_e2e.py', import.meta.url)), ...process.argv.slice(2)], {
  stdio: 'inherit',
})
if (result.error) throw result.error
process.exit(result.status ?? 1)
