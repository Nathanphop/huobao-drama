import { randomUUID } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import { homedir } from 'node:os'
import { join } from 'node:path'

const CODEX_IMAGE_BASE_URL = 'https://chatgpt.com/backend-api/codex'
export const CODEX_IMAGE_MODELS = ['gpt-image-2'] as const
const CODEX_IMAGE_QUALITIES = new Set(['low', 'medium', 'high'])

interface CodexImageCredential {
  token: string
  accountId?: string
}

function decodeJwtPayload(token: string): Record<string, any> | null {
  try {
    const payload = token.split('.')[1]
    return payload ? JSON.parse(Buffer.from(payload, 'base64url').toString('utf8')) : null
  } catch {
    return null
  }
}

async function readCodexImageCredential(): Promise<CodexImageCredential> {
  const raw = await readFile(join(homedir(), '.codex', 'auth.json'), 'utf8')
  const auth = JSON.parse(raw)
  const token = auth?.tokens?.access_token
  if (typeof token !== 'string' || !token.trim()) {
    throw new Error('No Codex OAuth access token is available. Connect ChatGPT first.')
  }
  return {
    token: token.trim(),
    accountId: typeof auth?.tokens?.account_id === 'string'
      ? auth.tokens.account_id.trim()
      : undefined,
  }
}

function buildHeaders({ token, accountId }: CodexImageCredential): Record<string, string> {
  const headers: Record<string, string> = {
    Authorization: `Bearer ${token}`,
    'Content-Type': 'application/json',
    'User-Agent': 'HuobaoDrama/4.0.10',
    originator: 'huobao-drama',
    'x-codex-image-turn-id': randomUUID(),
  }
  const claims = decodeJwtPayload(token)?.['https://api.openai.com/auth']
  const resolvedAccountId = claims?.chatgpt_account_id || accountId
  const residency = claims?.chatgpt_data_residency || claims?.chatgpt_compute_residency
  if (typeof resolvedAccountId === 'string' && resolvedAccountId) {
    headers['ChatGPT-Account-ID'] = resolvedAccountId
  }
  if (typeof residency === 'string' && residency.trim()) {
    headers['x-openai-internal-codex-residency'] = residency.trim()
  }
  return headers
}

function normalizeSize(value?: string | null): string {
  const [width, height] = String(value || '').toLowerCase().split('x').map(Number)
  if (!width || !height || width === height) return '1024x1024'
  return width > height ? '1536x1024' : '1024x1536'
}

function summarizeError(raw: string): string {
  try {
    const message = JSON.parse(raw)?.error?.message
    if (typeof message === 'string' && message.trim()) return message.trim().slice(0, 500)
  } catch {
    // Fall through to a bounded response body.
  }
  return String(raw || 'Unknown Codex image error.').slice(0, 500)
}

/**
 * Generate an image through Codex Desktop's local OAuth credential.
 * This native Codex image route is intentionally isolated because it is not a stable public API.
 * The OAuth token is read and used only inside the backend process and is never returned or logged.
 */
export async function generateCodexImage(options: {
  prompt: string
  model?: string | null
  size?: string | null
  quality?: string | null
}): Promise<{ data: string; mimeType: string; model: string }> {
  const prompt = options.prompt?.trim()
  if (!prompt) throw new Error('Image prompt is required.')
  if (prompt.length > 10_000) throw new Error('Image prompt must be 10,000 characters or fewer.')

  const model = options.model && CODEX_IMAGE_MODELS.includes(options.model as any)
    ? options.model
    : CODEX_IMAGE_MODELS[0]
  const quality = options.quality && CODEX_IMAGE_QUALITIES.has(options.quality)
    ? options.quality
    : 'medium'
  const credential = await readCodexImageCredential()
  const response = await fetch(`${CODEX_IMAGE_BASE_URL}/images/generations`, {
    method: 'POST',
    headers: buildHeaders(credential),
    body: JSON.stringify({
      prompt,
      model,
      n: 1,
      quality,
      size: normalizeSize(options.size),
      background: 'opaque',
    }),
    signal: AbortSignal.timeout(300_000),
  })
  const raw = await response.text()
  if (!response.ok) throw new Error(`Codex image error ${response.status}: ${summarizeError(raw)}`)

  const base64 = JSON.parse(raw)?.data?.[0]?.b64_json
  if (typeof base64 !== 'string' || !base64) throw new Error('Codex returned no image data.')
  const bytes = Buffer.from(base64, 'base64')
  if (!bytes.length || bytes.length > 30 * 1024 * 1024) {
    throw new Error('Codex returned an invalid image payload.')
  }
  return { data: base64, mimeType: 'image/png', model }
}
