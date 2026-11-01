import { spawn } from 'node:child_process'
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { basename, dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parseDataUrl } from '../utils/storage.js'

const CDP_ENDPOINT = (process.env.CHROME_CDP_ENDPOINT || 'http://127.0.0.1:9222').replace(/\/$/, '')
const PYTHON_BIN = process.env.PYTHON_BIN || (process.platform === 'win32' ? 'python' : 'python3')
const SOURCE_SCRIPTS_DIR = resolve(dirname(fileURLToPath(import.meta.url)), '../../scripts/google-flow')
const SCRIPTS_DIR = process.env.GOOGLE_FLOW_SCRIPTS_DIR || SOURCE_SCRIPTS_DIR
const SESSION_PATH = process.env.GOOGLE_FLOW_SESSION_PATH || join(SCRIPTS_DIR, 'session.json')

export const GOOGLE_FLOW_IMAGE_MODELS = [
  'Nano Banana 2.1',
  'Nano Banana Pro',
  'Nano Banana 2 Lite',
] as const

export const GOOGLE_FLOW_VIDEO_MODELS = [
  'Veo 3.1 - Lite',
  'Veo 3.1 - Fast',
  'Veo 3.1 - Quality',
  'Omni 1.1 Flash',
] as const

export interface GoogleFlowAccount {
  authuser: string
  email: string
  label: string
}

export interface GoogleFlowStatus {
  available: boolean
  connected: boolean
  detail: string
  accounts: GoogleFlowAccount[]
  cdpEndpoint: string
}

export interface GoogleFlowCredits {
  total: number
  subscription: number
  topUp: number
  refreshAt: string | null
  observedAt: string
  source: 'google-flow-rpc'
}

interface CdpTab {
  id: string
  url?: string
  webSocketDebuggerUrl?: string
}

interface PythonResult {
  ok: boolean
  stdout: string
  stderr: string
}

interface FlowProgress {
  percent: number
  stage?: string
}

const FLOW_CREDITS_EXPRESSION = String.raw`(async () => {
  const rpcId = 'nzlxg'
  const globals = window.WIZ_global_data || {}
  const at = globals.SNlM0e
  const bl = globals.cfb2h
  const sid = globals.FdrFJe
  if (!at || !bl) return { ok: false, detail: 'Google Flow credit RPC is not ready.' }
  const accountMatch = location.pathname.match(/^\/u\/(\d+)\//)
  const accountPrefix = accountMatch ? '/u/' + accountMatch[1] : ''
  const body = 'f.req=' + encodeURIComponent(JSON.stringify([[[rpcId, '[]', null, 'generic']]]))
    + '&at=' + encodeURIComponent(at) + '&'
  const url = location.origin + accountPrefix
    + '/_/AiSandboxAngularFrontend/data/batchexecute'
    + '?rpcids=' + rpcId
    + '&source-path=' + encodeURIComponent(location.pathname)
    + '&bl=' + encodeURIComponent(bl)
    + '&f.sid=' + encodeURIComponent(sid || '')
    + '&hl=en&_reqid=' + (1000000 + Math.floor(8000000 * Math.random())) + '&rt=c'
  const response = await fetch(url, {
    method: 'POST',
    credentials: 'include',
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
      'X-Same-Domain': '1',
    },
    body,
  })
  if (!response.ok) return { ok: false, detail: 'Google Flow credit RPC returned HTTP ' + response.status }
  const text = await response.text()
  for (const line of text.replace(")]}'", '').split(/\r?\n/)) {
    if (!line.trimStart().startsWith('[')) continue
    let rows
    try { rows = JSON.parse(line) } catch { continue }
    for (const row of rows) {
      if (!Array.isArray(row) || row[0] !== 'wrb.fr' || row[1] !== rpcId) continue
      let payload = row[2]
      try { payload = typeof payload === 'string' ? JSON.parse(payload) : payload } catch { continue }
      if (!Array.isArray(payload) || !Number.isFinite(Number(payload[0]))) continue
      const refresh = Array.isArray(payload[6]) ? payload[6] : null
      const refreshMs = refresh
        ? Number(refresh[0] || 0) * 1000 + Number(refresh[1] || 0) / 1000000
        : 0
      return {
        ok: true,
        total: Number(payload[0]),
        subscription: Number(payload[5] || 0),
        topUp: Number(payload[4] || 0),
        refreshAt: refreshMs > 0 ? new Date(refreshMs).toISOString() : null,
      }
    }
  }
  return { ok: false, detail: 'Google Flow did not return a credit balance.' }
})()`

const flowCreditsCache = new Map<string, { expiresAt: number; value: GoogleFlowCredits }>()

const FLOW_ACCOUNT_DISCOVERY_EXPRESSION = String.raw`(async () => {
  const accounts = []
  const seenEmails = new Set()
  const marker = '"oPEP7c":"'
  for (let index = 0; index < 10; index += 1) {
    const response = await fetch('/?authuser=' + index, {
      credentials: 'include', redirect: 'follow', cache: 'no-store'
    })
    if (!response.ok) break
    const html = await response.text()
    const markerIndex = html.indexOf(marker)
    if (markerIndex < 0) break
    const emailStart = markerIndex + marker.length
    const emailEnd = html.indexOf('"', emailStart)
    if (emailEnd < 0) break
    const email = html.slice(emailStart, emailEnd).trim()
    const resolvedUrl = new URL(response.url)
    const pathMatch = resolvedUrl.pathname.match(/^\/u\/(\d+)(?:\/|$)/)
    const resolvedAuthuser = pathMatch ? pathMatch[1] : '0'
    if (index > 0 && resolvedAuthuser !== String(index)) break
    if (!email || seenEmails.has(email)) continue
    seenEmails.add(email)
    accounts.push({ authuser: String(index), email })
  }
  return accounts
})()`

async function readCdpJson(path: string): Promise<any> {
  const response = await fetch(`${CDP_ENDPOINT}${path}`, { signal: AbortSignal.timeout(5_000) })
  if (!response.ok) throw new Error(`Chrome remote debugging returned HTTP ${response.status}.`)
  return response.json()
}

function cdpCall(webSocketUrl: string, method: string, params: Record<string, unknown> = {}, timeoutMs = 10_000): Promise<any> {
  return new Promise((resolveCall, rejectCall) => {
    const socket = new WebSocket(webSocketUrl)
    const id = Math.floor(Math.random() * 1_000_000_000)
    let settled = false
    const finish = (error?: Error, value?: any) => {
      if (settled) return
      settled = true
      clearTimeout(timeout)
      socket.close()
      if (error) rejectCall(error)
      else resolveCall(value)
    }
    const timeout = setTimeout(() => finish(new Error(`Chrome CDP ${method} timed out.`)), timeoutMs)
    socket.addEventListener('open', () => socket.send(JSON.stringify({ id, method, params })))
    socket.addEventListener('message', event => {
      try {
        const message = JSON.parse(String(event.data))
        if (message.id !== id) return
        if (message.error) finish(new Error(message.error.message || `Chrome CDP ${method} failed.`))
        else finish(undefined, message.result)
      } catch {
        // Ignore unrelated protocol messages.
      }
    })
    socket.addEventListener('error', () => finish(new Error('Could not connect to Chrome remote debugging.')))
  })
}

async function browserWebSocketUrl(): Promise<string> {
  const version = await readCdpJson('/json/version')
  if (typeof version?.webSocketDebuggerUrl !== 'string') {
    throw new Error('Chrome remote debugging did not expose a browser websocket.')
  }
  return version.webSocketDebuggerUrl
}

async function readTabs(): Promise<CdpTab[]> {
  const tabs = await readCdpJson('/json')
  return Array.isArray(tabs) ? tabs : []
}

async function createBackgroundTab(url = 'https://flow.google.com/'): Promise<string> {
  const result = await cdpCall(await browserWebSocketUrl(), 'Target.createTarget', {
    url,
    background: true,
  })
  if (!result?.targetId) throw new Error('Chrome could not open a background Google Flow tab.')
  return result.targetId
}

async function closeTab(targetId?: string | null): Promise<void> {
  if (!targetId) return
  try {
    await cdpCall(await browserWebSocketUrl(), 'Target.closeTarget', { targetId }, 5_000)
  } catch {
    // Temporary tabs may already be closed by Chrome.
  }
}

async function discoverAccounts(): Promise<GoogleFlowAccount[]> {
  let temporaryTabId: string | null = null
  try {
    let tab = (await readTabs()).find(item => item.url?.startsWith('https://flow.google.com/'))
    if (!tab) {
      temporaryTabId = await createBackgroundTab()
      for (let attempt = 0; attempt < 20; attempt += 1) {
        await new Promise(resolveWait => setTimeout(resolveWait, 250))
        tab = (await readTabs()).find(item => item.id === temporaryTabId)
        if (tab?.url?.startsWith('https://flow.google.com/')) break
      }
    }
    if (!tab?.webSocketDebuggerUrl) return discoverFallbackAccounts()
    const evaluated = await cdpCall(tab.webSocketDebuggerUrl, 'Runtime.evaluate', {
      expression: FLOW_ACCOUNT_DISCOVERY_EXPRESSION,
      awaitPromise: true,
      returnByValue: true,
    }, 20_000)
    const value = evaluated?.result?.value
    if (!Array.isArray(value) || !value.length) return discoverFallbackAccounts()
    return value
      .filter(item => /^\d+$/.test(String(item?.authuser || '')) && String(item?.email || '').includes('@'))
      .map(item => ({
        authuser: String(item.authuser),
        email: String(item.email),
        label: String(item.email),
      }))
  } finally {
    await closeTab(temporaryTabId)
  }
}

async function discoverFallbackAccounts(): Promise<GoogleFlowAccount[]> {
  const chooserAccounts = await discoverAccountsFromChooser()
  return chooserAccounts.length ? chooserAccounts : discoverAccountsFromGoogleTabs()
}

async function discoverAccountsFromGoogleTabs(): Promise<GoogleFlowAccount[]> {
  const tabs = (await readTabs()).filter(tab => /https:\/\/(?:aistudio\.google\.com|studio\.youtube\.com)\//.test(tab.url || ''))
  const accounts = new Map<string, GoogleFlowAccount>()
  const emailExpression = String.raw`(() => {
    const values = [document.body?.innerText || '', document.documentElement?.innerHTML || '']
    const emails = values.join('\n').match(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g) || []
    return emails.find(email => !email.endsWith('@example.com')) || ''
  })()`
  for (const tab of tabs) {
    const match = String(tab.url || '').match(/\.google\.com\/u\/(\d+)(?:\/|$)/)
    if (!match || accounts.has(match[1])) continue
    let email = ''
    if (tab.webSocketDebuggerUrl) {
      try {
        const evaluated = await cdpCall(tab.webSocketDebuggerUrl, 'Runtime.evaluate', {
          expression: emailExpression,
          returnByValue: true,
        }, 3_000)
        email = typeof evaluated?.result?.value === 'string' ? evaluated.result.value : ''
      } catch {
        // The authuser still identifies the signed-in Google session.
      }
    }
    accounts.set(match[1], {
      authuser: match[1],
      email: email || `Google account u/${match[1]}`,
      label: email || `Google account u/${match[1]}`,
    })
  }
  return [...accounts.values()].sort((a, b) => Number(a.authuser) - Number(b.authuser))
}

async function discoverAccountsFromChooser(): Promise<GoogleFlowAccount[]> {
  const chooserUrl = 'https://accounts.google.com/AccountChooser?continue=https%3A%2F%2Fflow.google.com%2F'
  let targetId: string | null = null
  try {
    targetId = await createBackgroundTab(chooserUrl)
    let tab: CdpTab | undefined
    for (let attempt = 0; attempt < 24; attempt += 1) {
      await new Promise(resolveWait => setTimeout(resolveWait, 250))
      tab = (await readTabs()).find(item => item.id === targetId)
      if (tab?.webSocketDebuggerUrl && /accounts\.google\.com/.test(tab.url || '')) break
    }
    if (!tab?.webSocketDebuggerUrl) return []
    const expression = String.raw`(() => {
      const values = []
      const push = value => { if (typeof value === 'string' && value) values.push(value) }
      push(document.body && document.body.innerText)
      for (const element of document.querySelectorAll('[aria-label],[title],[alt],[data-email],[data-identifier]')) {
        push(element.getAttribute('aria-label'))
        push(element.getAttribute('title'))
        push(element.getAttribute('alt'))
        push(element.getAttribute('data-email'))
        push(element.getAttribute('data-identifier'))
      }
      return Array.from(new Set(values.join('\n').match(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g) || []))
        .filter(email => !email.endsWith('@example.com'))
        .slice(0, 10)
    })()`
    const evaluated = await cdpCall(tab.webSocketDebuggerUrl, 'Runtime.evaluate', {
      expression,
      returnByValue: true,
    }, 5_000)
    const emails = evaluated?.result?.value
    return Array.isArray(emails)
      ? emails.map((email, index) => ({ authuser: String(index), email: String(email), label: String(email) }))
      : []
  } finally {
    await closeTab(targetId)
  }
}

async function readGoogleCookies(): Promise<any[]> {
  const result = await cdpCall(await browserWebSocketUrl(), 'Storage.getCookies', {}, 10_000)
  const cookies = Array.isArray(result?.cookies) ? result.cookies : []
  return cookies.filter((cookie: any) => {
    const domain = String(cookie?.domain || '').toLowerCase()
    return domain === 'google.com' || domain.endsWith('.google.com')
  })
}

export async function refreshGoogleFlowSession(authuser = '0'): Promise<void> {
  const cookies = await readGoogleCookies()
  const sapisid = cookies.find(cookie => cookie.name === 'SAPISID')?.value
  if (!cookies.length || !sapisid) {
    throw new Error('Chrome has no usable Google session. Sign in to Google in the Chrome 9222 profile first.')
  }
  const allowed = ['name', 'value', 'domain', 'path', 'expires', 'httpOnly', 'secure', 'sameSite']
  const safeCookies = cookies.map(cookie => Object.fromEntries(
    allowed.filter(key => cookie[key] !== undefined).map(key => [key, cookie[key]]),
  ))
  await mkdir(dirname(SESSION_PATH), { recursive: true })
  await writeFile(SESSION_PATH, JSON.stringify({
    cookies: safeCookies,
    sapisid,
    saved_at: Date.now() / 1000,
    source: 'chrome-cdp-session',
    authuser,
  }, null, 2), 'utf8')
}

export async function getGoogleFlowStatus(): Promise<GoogleFlowStatus> {
  try {
    await browserWebSocketUrl()
    const [accounts, cookies] = await Promise.all([discoverAccounts(), readGoogleCookies()])
    const connected = accounts.length > 0 && cookies.some(cookie => cookie.name === 'SAPISID')
    return {
      available: true,
      connected,
      detail: connected
        ? 'Connected to Google Flow through the signed-in Chrome 9222 session.'
        : 'Chrome remote debugging is available, but no signed-in Google Flow account was found.',
      accounts,
      cdpEndpoint: CDP_ENDPOINT,
    }
  } catch (error: any) {
    return {
      available: false,
      connected: false,
      detail: error?.message || 'Chrome remote debugging is unavailable.',
      accounts: [],
      cdpEndpoint: CDP_ENDPOINT,
    }
  }
}

function googleFlowUrlForAccount(projectUrl: string | null | undefined, authuser: string): string {
  let url: URL
  try {
    url = new URL(projectUrl || 'https://flow.google.com/')
  } catch {
    url = new URL('https://flow.google.com/')
  }
  if (url.origin !== 'https://flow.google.com') url = new URL('https://flow.google.com/')
  url.searchParams.set('authuser', authuser)
  return url.toString()
}

/** Read the signed-in account's live Flow balance from Flow's own credits RPC. */
export async function getGoogleFlowCredits(
  authuser = '0',
  projectUrl?: string | null,
  force = false,
): Promise<GoogleFlowCredits> {
  const normalizedAuthuser = /^\d+$/.test(authuser) ? authuser : '0'
  const cacheKey = `${normalizedAuthuser}:${projectUrl || ''}`
  const cached = flowCreditsCache.get(cacheKey)
  if (!force && cached && cached.expiresAt > Date.now()) return cached.value

  let temporaryTabId: string | null = null
  try {
    temporaryTabId = await createBackgroundTab(googleFlowUrlForAccount(projectUrl, normalizedAuthuser))
    let tab: CdpTab | undefined
    let lastDetail = 'Google Flow credit RPC is not ready.'
    for (let attempt = 0; attempt < 40; attempt += 1) {
      await new Promise(resolveWait => setTimeout(resolveWait, 250))
      tab = (await readTabs()).find(item => item.id === temporaryTabId)
      if (!tab?.webSocketDebuggerUrl || !tab.url?.startsWith('https://flow.google.com/')) continue
      try {
        const evaluated = await cdpCall(tab.webSocketDebuggerUrl, 'Runtime.evaluate', {
          expression: FLOW_CREDITS_EXPRESSION,
          awaitPromise: true,
          returnByValue: true,
        }, 15_000)
        const result = evaluated?.result?.value
        if (!result?.ok) {
          lastDetail = String(result?.detail || lastDetail)
          continue
        }
        const value: GoogleFlowCredits = {
          total: Number(result.total),
          subscription: Number(result.subscription || 0),
          topUp: Number(result.topUp || 0),
          refreshAt: typeof result.refreshAt === 'string' ? result.refreshAt : null,
          observedAt: new Date().toISOString(),
          source: 'google-flow-rpc',
        }
        if (!Number.isFinite(value.total)) throw new Error('Google Flow returned an invalid credit balance.')
        flowCreditsCache.set(cacheKey, { expiresAt: Date.now() + 15_000, value })
        return value
      } catch (error: any) {
        lastDetail = error?.message || lastDetail
      }
    }
    throw new Error(lastDetail)
  } finally {
    await closeTab(temporaryTabId)
  }
}

function runPython(
  args: string[],
  timeoutMs: number,
  onProgress?: (progress: FlowProgress) => void | Promise<void>,
): Promise<PythonResult> {
  return new Promise(resolveRun => {
    const child = spawn(PYTHON_BIN, args, {
      cwd: SCRIPTS_DIR,
      env: { ...process.env, GOOGLE_FLOW_SESSION_PATH: SESSION_PATH },
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    let stdout = ''
    let stderr = ''
    let progressLineBuffer = ''
    let progressQueue = Promise.resolve()
    let settled = false
    const finish = (result: PythonResult) => {
      if (settled) return
      settled = true
      clearTimeout(timeout)
      resolveRun(result)
    }
    const timeout = setTimeout(() => {
      child.kill()
      finish({ ok: false, stdout, stderr: `${stderr}\nGoogle Flow automation timed out.` })
    }, timeoutMs)
    child.stdout.on('data', chunk => {
      const text = String(chunk)
      stdout = `${stdout}${text}`.slice(-20_000)
      progressLineBuffer += text
      const lines = progressLineBuffer.split(/\r?\n/)
      progressLineBuffer = lines.pop() || ''
      for (const line of lines) {
        const marker = line.match(/^FLOW_PROGRESS:\s*(\{.*\})\s*$/)
        if (!marker || !onProgress) continue
        try {
          const parsed = JSON.parse(marker[1])
          const percent = Number(parsed?.percent)
          if (!Number.isFinite(percent)) continue
          const update = {
            percent: Math.max(0, Math.min(100, percent)),
            stage: typeof parsed?.stage === 'string' ? parsed.stage : undefined,
          }
          progressQueue = progressQueue.then(() => onProgress(update)).then(() => undefined).catch(() => undefined)
        } catch {
          // Ignore malformed progress telemetry while preserving generation.
        }
      }
    })
    child.stderr.on('data', chunk => { stderr = `${stderr}${chunk}`.slice(-20_000) })
    child.once('error', error => finish({ ok: false, stdout, stderr: error.message }))
    child.once('exit', code => {
      void progressQueue.finally(() => finish({ ok: code === 0, stdout, stderr }))
    })
  })
}

function safePythonError(result: PythonResult): string {
  const lines = result.stderr.split(/\r?\n/).map(line => line.trim()).filter(Boolean)
  const marker = [...lines].reverse().find(line => /FLOW_(?:VIDEO_)?ERROR:/i.test(line))
  return (marker || lines.at(-1) || 'Google Flow automation failed.').slice(0, 800)
}

async function writeReferenceManifest(tempDir: string, references: string[]): Promise<string> {
  const entries: Array<{ path?: string; assetUrl?: string }> = []
  for (const [index, reference] of references.slice(0, 10).entries()) {
    if (reference.startsWith('https://flow-content.google/image/')) {
      entries.push({ assetUrl: reference })
      continue
    }
    let parsed = parseDataUrl(reference)
    if (!parsed && /^https?:\/\//.test(reference)) {
      const response = await fetch(reference, { signal: AbortSignal.timeout(30_000) })
      if (response.ok) {
        parsed = {
          mimeType: response.headers.get('content-type') || 'image/jpeg',
          data: Buffer.from(await response.arrayBuffer()).toString('base64'),
        }
      }
    }
    if (!parsed) continue
    const ext = parsed.mimeType === 'image/png' ? '.png' : parsed.mimeType === 'image/webp' ? '.webp' : '.jpg'
    const path = join(tempDir, `reference-${index + 1}${ext}`)
    await writeFile(path, Buffer.from(parsed.data, 'base64'))
    entries.push({ path })
  }
  const manifest = join(tempDir, 'references.json')
  await writeFile(manifest, JSON.stringify(entries), 'utf8')
  return manifest
}

function normalizeImageAspect(size?: string | null): string {
  const [width, height] = String(size || '').split('x').map(Number)
  if (!width || !height) return '16:9'
  const ratio = width / height
  const candidates: Array<[string, number]> = [['16:9', 16 / 9], ['4:3', 4 / 3], ['1:1', 1], ['3:4', 3 / 4], ['9:16', 9 / 16]]
  return candidates.reduce((best, current) => Math.abs(current[1] - ratio) < Math.abs(best[1] - ratio) ? current : best)[0]
}

function normalizeVideoAspect(value?: string | null): string {
  if (value === '9:16') return '9:16'
  const [width, height] = String(value || '').split('x').map(Number)
  return width && height && height > width ? '9:16' : '16:9'
}

export async function generateGoogleFlowImage(options: {
  prompt: string
  model?: string | null
  size?: string | null
  references?: string[]
  authuser?: string | null
  projectUrl?: string | null
}): Promise<{ buffer: Buffer; mimeType: string; metadata: any }> {
  const authuser = /^\d+$/.test(String(options.authuser || '')) ? String(options.authuser) : '0'
  const model = GOOGLE_FLOW_IMAGE_MODELS.includes(options.model as any)
    ? String(options.model)
    : GOOGLE_FLOW_IMAGE_MODELS[0]
  await refreshGoogleFlowSession(authuser)
  const tempDir = await mkdtemp(join(tmpdir(), 'huobao-flow-image-'))
  try {
    const promptPath = join(tempDir, 'prompt.txt')
    const outputPath = join(tempDir, 'flow-image.png')
    const metadataPath = join(tempDir, 'result.json')
    const manifestPath = await writeReferenceManifest(tempDir, options.references || [])
    await writeFile(promptPath, options.prompt, 'utf8')
    const args = [
      join(SCRIPTS_DIR, 'google_flow_image_auto.py'),
      '--prompt-file', promptPath,
      '--out', outputPath,
      '--meta', metadataPath,
      '--aspect', normalizeImageAspect(options.size),
      '--model', model,
      '--resolution', '1K',
      '--authuser', authuser,
      '--expected-assets', '1',
    ]
    if (options.projectUrl) args.push('--project-url', options.projectUrl)
    if ((options.references || []).length) args.push('--references-file', manifestPath)
    const result = await runPython(args, 12 * 60_000)
    if (!result.ok) throw new Error(safePythonError(result))
    const [buffer, metadata] = await Promise.all([
      readFile(outputPath),
      readFile(metadataPath, 'utf8').then(raw => JSON.parse(raw)),
    ])
    if (!buffer.length) throw new Error('Google Flow returned an empty image.')
    return { buffer, mimeType: 'image/png', metadata }
  } finally {
    await rm(tempDir, { recursive: true, force: true }).catch(() => {})
  }
}

export async function generateGoogleFlowVideo(options: {
  prompt: string
  model?: string | null
  aspectRatio?: string | null
  resolution?: string | null
  duration?: number | null
  references?: string[]
  authuser?: string | null
  projectUrl?: string | null
  onProgress?: (progress: FlowProgress) => void | Promise<void>
}): Promise<{ buffer: Buffer; mimeType: string; duration: number; metadata: any }> {
  const authuser = /^\d+$/.test(String(options.authuser || '')) ? String(options.authuser) : '0'
  const model = GOOGLE_FLOW_VIDEO_MODELS.includes(options.model as any)
    ? String(options.model)
    : GOOGLE_FLOW_VIDEO_MODELS[0]
  await refreshGoogleFlowSession(authuser)
  const tempDir = await mkdtemp(join(tmpdir(), 'huobao-flow-video-'))
  try {
    const promptPath = join(tempDir, 'prompt.txt')
    const outputPath = join(tempDir, 'flow-video.mp4')
    const metadataPath = join(tempDir, 'result.json')
    const references = options.references || []
    const manifestPath = await writeReferenceManifest(tempDir, references)
    await writeFile(promptPath, options.prompt, 'utf8')
    const duration = [4, 6, 8, 10].includes(Number(options.duration)) ? Number(options.duration) : 8
    const args = [
      join(SCRIPTS_DIR, 'google_flow_video_auto.py'),
      '--prompt-file', promptPath,
      '--out', outputPath,
      '--meta', metadataPath,
      '--aspect', normalizeVideoAspect(options.aspectRatio),
      '--model', model,
      '--authuser', authuser,
      '--video-type', references.length > 2 ? 'Ingredients' : 'Frames',
      '--resolution', options.resolution === '360p' ? '360p' : '720p',
      '--duration', String(duration),
    ]
    if (options.projectUrl) args.push('--project-url', options.projectUrl)
    if (references.length) args.push('--references-file', manifestPath)
    const result = await runPython(args, 22 * 60_000, options.onProgress)
    if (!result.ok) throw new Error(safePythonError(result))
    const [buffer, metadata] = await Promise.all([
      readFile(outputPath),
      readFile(metadataPath, 'utf8').then(raw => JSON.parse(raw)),
    ])
    if (!buffer.length) throw new Error('Google Flow returned an empty video.')
    return {
      buffer,
      mimeType: String(metadata.contentType || 'video/mp4'),
      duration: Number(metadata.duration || duration),
      metadata,
    }
  } finally {
    await rm(tempDir, { recursive: true, force: true }).catch(() => {})
  }
}
