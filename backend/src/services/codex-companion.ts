import { spawn } from 'node:child_process'
import { mkdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createInterface } from 'node:readline'
import { z } from 'zod'
import type { RequestContext } from '@mastra/core/request-context'
import { getCodexSpawn, resolveCodexCommand } from './codex-command.js'

const CODEX_TIMEOUT_MS = Number(process.env.CODEX_TIMEOUT_MS || 10 * 60_000)
const CODEX_COMMAND = resolveCodexCommand()
const CODEX_CWD = join(tmpdir(), 'huobao-codex-companion')
mkdirSync(CODEX_CWD, { recursive: true })

interface CommandResult {
  ok: boolean
  stdout: string
  stderr: string
  reason?: string
}

export interface CodexStatus {
  available: boolean
  connected: boolean
  detail: string
}

export interface CodexModelInfo {
  id: string
  displayName: string
  description: string
  isDefault: boolean
  inputModalities: string[]
}

export interface CodexAgentResult {
  text: string
  toolCalls: Array<{ toolName: string; args: unknown }>
  toolResults: Array<{ toolName: string; result: string }>
}

interface RunCodexAgentOptions {
  message: string
  instructions: string
  model?: string
  tools: Record<string, any>
  requestContext: RequestContext<any>
  signal?: AbortSignal
}

function runCodexCommand(args: string[], timeoutMs = 30_000): Promise<CommandResult> {
  return new Promise(resolve => {
    const invocation = getCodexSpawn(CODEX_COMMAND, args)
    const child = spawn(invocation.command, invocation.args, {
      cwd: CODEX_CWD,
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    let stdout = ''
    let stderr = ''
    let settled = false

    const finish = (result: CommandResult) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      resolve(result)
    }
    const timer = setTimeout(() => {
      child.kill()
      finish({ ok: false, stdout, stderr, reason: 'Codex command timed out.' })
    }, timeoutMs)

    child.stdout.on('data', chunk => { stdout += chunk.toString() })
    child.stderr.on('data', chunk => { stderr += chunk.toString() })
    child.once('error', error => finish({ ok: false, stdout, stderr, reason: error.message }))
    child.once('exit', code => finish({ ok: code === 0, stdout, stderr }))
  })
}

export async function getCodexStatus(): Promise<CodexStatus> {
  const result = await runCodexCommand(['login', 'status'])
  if (!result.ok) {
    return {
      available: !/ENOENT|not recognized|not found/i.test(result.reason || result.stderr),
      connected: false,
      detail: 'Codex CLI is unavailable or not signed in.',
    }
  }
  const output = `${result.stdout}\n${result.stderr}`
  const connected = /logged in/i.test(output) && !/not logged in/i.test(output)
  return {
    available: true,
    connected,
    detail: connected
      ? 'Connected with the local Codex OAuth session.'
      : 'Codex CLI is installed but not signed in.',
  }
}

export async function loginWithCodex(): Promise<CodexStatus> {
  const before = await getCodexStatus()
  if (before.connected) return before
  const result = await runCodexCommand(['login'], 10 * 60_000)
  if (!result.ok) return getCodexStatus()
  return getCodexStatus()
}

/**
 * Read the picker catalog exposed by the installed Codex app-server.
 * The catalog reflects the local Codex client but is not an entitlement guarantee;
 * a successful turn remains the definitive access check for a selected model.
 */
export function listCodexModels(): Promise<CodexModelInfo[]> {
  return new Promise((resolve, reject) => {
    const invocation = getCodexSpawn(CODEX_COMMAND, ['app-server', '--listen', 'stdio://'])
    const child = spawn(invocation.command, invocation.args, {
      cwd: CODEX_CWD,
      windowsHide: true,
      stdio: ['pipe', 'pipe', 'pipe'],
    })
    const lines = createInterface({ input: child.stdout })
    const pending = new Map<number, { resolve: (value: any) => void; reject: (error: Error) => void }>()
    let nextId = 1
    let settled = false

    const stop = () => {
      lines.close()
      child.kill()
    }
    const finish = (error?: Error, models: CodexModelInfo[] = []) => {
      if (settled) return
      settled = true
      clearTimeout(timeout)
      for (const waiter of pending.values()) waiter.reject(error || new Error('Codex stopped.'))
      pending.clear()
      stop()
      if (error) reject(error)
      else resolve(models)
    }
    const timeout = setTimeout(() => finish(new Error('Codex model catalog timed out.')), 30_000)
    const write = (message: unknown) => {
      if (!child.stdin.destroyed) child.stdin.write(`${JSON.stringify(message)}\n`)
    }
    const call = (method: string, params: unknown) => new Promise<any>((resolveCall, rejectCall) => {
      const id = nextId++
      pending.set(id, { resolve: resolveCall, reject: rejectCall })
      write({ id, method, params })
    })
    const notify = (method: string, params: unknown) => write({ method, params })

    child.stderr.resume()
    lines.on('line', line => {
      let message: any
      try {
        message = JSON.parse(line)
      } catch {
        return
      }
      if (message.id !== undefined && !message.method) {
        const waiter = pending.get(message.id)
        if (!waiter) return
        pending.delete(message.id)
        if (message.error) waiter.reject(new Error(message.error.message || 'Codex RPC failed.'))
        else waiter.resolve(message.result)
        return
      }
      if (message.id !== undefined && message.method) {
        write({ id: message.id, error: { code: -32_601, message: 'Host requests are disabled.' } })
      }
    })
    child.once('error', error => finish(new Error(`Could not start Codex: ${error.message}`)))
    child.once('exit', code => {
      if (!settled) finish(new Error(`Codex app-server exited unexpectedly (${code ?? 'unknown'}).`))
    })

    void (async () => {
      try {
        await call('initialize', {
          clientInfo: { name: 'huobao-drama', title: 'Huobao Drama', version: '4.0.10' },
          capabilities: { experimentalApi: true, requestAttestation: false },
        })
        notify('initialized', {})

        const models: CodexModelInfo[] = []
        let cursor: string | null = null
        do {
          const page = await call('model/list', { cursor, limit: 100, includeHidden: false })
          for (const model of page?.data || []) {
            const id = typeof model.model === 'string' && model.model ? model.model : model.id
            if (!id || model.hidden || models.some(item => item.id === id)) continue
            models.push({
              id,
              displayName: model.displayName || id,
              description: model.description || '',
              isDefault: model.isDefault === true,
              inputModalities: Array.isArray(model.inputModalities) ? model.inputModalities : [],
            })
          }
          cursor = page?.nextCursor || null
        } while (cursor)
        models.sort((a, b) => Number(b.isDefault) - Number(a.isDefault))
        finish(undefined, models)
      } catch (error: any) {
        finish(error instanceof Error ? error : new Error('Could not load Codex models.'))
      }
    })()
  })
}

function jsonText(value: unknown): string {
  if (typeof value === 'string') return value
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

function dynamicToolSpecs(tools: Record<string, any>) {
  return Object.values(tools).map((tool: any) => {
    const schema = z.toJSONSchema(tool.inputSchema) as Record<string, unknown>
    delete schema.$schema
    return {
      type: 'function',
      name: tool.id,
      description: tool.description || tool.id,
      inputSchema: schema,
    }
  })
}

/**
 * Run one isolated Huobao Agent turn through the user's local Codex app-server.
 * OAuth remains owned by Codex CLI. Only the explicitly supplied Huobao tools are executable.
 */
export function runCodexAgent(options: RunCodexAgentOptions): Promise<CodexAgentResult> {
  return new Promise((resolve, reject) => {
    const invocation = getCodexSpawn(CODEX_COMMAND, ['app-server', '--listen', 'stdio://'])
    const child = spawn(invocation.command, invocation.args, {
      cwd: CODEX_CWD,
      windowsHide: true,
      stdio: ['pipe', 'pipe', 'pipe'],
    })
    const lines = createInterface({ input: child.stdout })
    const pending = new Map<number, { resolve: (value: any) => void; reject: (error: Error) => void }>()
    const toolsById = new Map(Object.values(options.tools).map((tool: any) => [tool.id, tool]))
    const toolCalls: CodexAgentResult['toolCalls'] = []
    const toolResults: CodexAgentResult['toolResults'] = []
    let nextId = 1
    let threadId: string | null = null
    let turnId: string | null = null
    let fullText = ''
    let settled = false

    const stop = () => {
      lines.close()
      child.kill()
    }
    const finish = (error?: Error) => {
      if (settled) return
      settled = true
      clearTimeout(timeout)
      options.signal?.removeEventListener('abort', abort)
      for (const waiter of pending.values()) waiter.reject(error || new Error('Codex stopped.'))
      pending.clear()
      stop()
      if (error) reject(error)
      else resolve({ text: fullText, toolCalls, toolResults })
    }
    const timeout = setTimeout(() => finish(new Error('Codex request timed out.')), CODEX_TIMEOUT_MS)
    const abort = () => finish(new Error('Codex request was cancelled.'))
    const write = (message: unknown) => {
      if (!child.stdin.destroyed) child.stdin.write(`${JSON.stringify(message)}\n`)
    }
    const call = (method: string, params: unknown) => new Promise<any>((resolveCall, rejectCall) => {
      const id = nextId++
      pending.set(id, { resolve: resolveCall, reject: rejectCall })
      write({ id, method, params })
    })
    const notify = (method: string, params: unknown) => write({ method, params })

    async function handleToolCall(message: any) {
      const params = message.params || {}
      const tool = toolsById.get(params.tool)
      if (!tool) {
        write({
          id: message.id,
          result: {
            contentItems: [{ type: 'inputText', text: 'Tool is not allowed by Huobao.' }],
            success: false,
          },
        })
        return
      }

      const args = params.arguments && typeof params.arguments === 'object' ? params.arguments : {}
      toolCalls.push({ toolName: params.tool, args })
      try {
        const result = await tool.execute(args, { requestContext: options.requestContext })
        const text = jsonText(result)
        toolResults.push({ toolName: params.tool, result: text })
        write({
          id: message.id,
          result: { contentItems: [{ type: 'inputText', text }], success: true },
        })
      } catch (error: any) {
        const text = error?.message || 'Huobao tool failed.'
        toolResults.push({ toolName: params.tool, result: text })
        write({
          id: message.id,
          result: { contentItems: [{ type: 'inputText', text }], success: false },
        })
      }
    }

    child.stderr.resume()
    options.signal?.addEventListener('abort', abort, { once: true })
    lines.on('line', line => {
      let message: any
      try {
        message = JSON.parse(line)
      } catch {
        return
      }

      if (message.id !== undefined && !message.method) {
        const waiter = pending.get(message.id)
        if (!waiter) return
        pending.delete(message.id)
        if (message.error) waiter.reject(new Error(message.error.message || 'Codex RPC failed.'))
        else waiter.resolve(message.result)
        return
      }

      if (message.id !== undefined && message.method) {
        if (message.method === 'item/tool/call') {
          void handleToolCall(message)
        } else {
          write({
            id: message.id,
            error: { code: -32_601, message: 'Only allowlisted Huobao tools are available.' },
          })
        }
        return
      }

      if (message.method === 'item/agentMessage/delta') {
        const params = message.params || {}
        if (params.threadId === threadId && params.turnId === turnId && typeof params.delta === 'string') {
          fullText += params.delta
        }
        return
      }

      if (message.method === 'turn/completed') {
        const turn = message.params?.turn
        if (!turn || turn.id !== turnId) return
        if (turn.status === 'completed') finish()
        else finish(new Error(turn.error?.message || `Codex turn ended with status ${turn.status}.`))
      }
    })

    child.once('error', error => finish(new Error(`Could not start Codex: ${error.message}`)))
    child.once('exit', code => {
      if (!settled) finish(new Error(`Codex app-server exited unexpectedly (${code ?? 'unknown'}).`))
    })

    void (async () => {
      try {
        await call('initialize', {
          clientInfo: { name: 'huobao-drama', title: 'Huobao Drama', version: '4.0.10' },
          capabilities: { experimentalApi: true, requestAttestation: false },
        })
        notify('initialized', {})
        const thread = await call('thread/start', {
          cwd: CODEX_CWD,
          runtimeWorkspaceRoots: [],
          approvalPolicy: 'never',
          sandbox: 'read-only',
          ephemeral: true,
          environments: [],
          baseInstructions: options.instructions,
          developerInstructions: [
            'You are the isolated text Agent inside Huobao Drama.',
            'Use only the Huobao dynamic tools supplied by the host when the task requires reading or saving application data.',
            'Never run shell commands, inspect files, use MCP, browse the web, delegate, or request additional permissions.',
            'Do not reveal local paths, credentials, OAuth data, or implementation details.',
          ].join(' '),
          dynamicTools: dynamicToolSpecs(options.tools),
          ...(options.model && options.model !== 'default' ? { model: options.model } : {}),
        })
        threadId = thread.thread.id
        const turn = await call('turn/start', {
          threadId,
          input: [{ type: 'text', text: options.message, text_elements: [] }],
        })
        turnId = turn.turn.id
      } catch (error: any) {
        finish(error instanceof Error ? error : new Error('Could not start Codex turn.'))
      }
    })()
  })
}
