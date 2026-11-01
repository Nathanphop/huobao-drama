import { Hono } from 'hono'
import { getConnInfo } from '@hono/node-server/conninfo'
import { getCodexStatus, listCodexModels, loginWithCodex } from '../services/codex-companion.js'
import { CODEX_IMAGE_MODELS } from '../services/codex-image.js'
import { success } from '../utils/response.js'

const app = new Hono()

function isLoopbackAddress(address?: string | null): boolean {
  if (!address) return false
  const normalized = address.toLowerCase().replace(/^::ffff:/, '')
  return normalized === '127.0.0.1' || normalized === '::1'
}

app.use('*', async (c, next) => {
  if (process.env.CODEX_LOCAL_ALLOW_REMOTE === 'true') return next()
  const remoteAddress = getConnInfo(c).remote.address
  if (!isLoopbackAddress(remoteAddress)) {
    return c.json({ code: 403, message: 'Codex local companion is available only from this computer.' }, 403)
  }
  return next()
})

app.get('/status', async c => success(c, await getCodexStatus()))

app.get('/models', async c => {
  const status = await getCodexStatus()
  if (!status.connected) {
    return c.json({ code: 401, message: 'Connect ChatGPT before loading Codex models.' }, 401)
  }
  try {
    const text = await listCodexModels()
    return success(c, {
      text,
      image: CODEX_IMAGE_MODELS.map((id, index) => ({
        id,
        displayName: id === 'gpt-image-2' ? 'GPT Image 2' : id,
        description: 'Codex native image generation',
        isDefault: index === 0,
        inputModalities: ['text'],
      })),
      textSource: 'codex-app-server',
      imageSource: 'codex-native-image',
    })
  } catch (error: any) {
    return c.json({ code: 503, message: error?.message || 'Could not load Codex models.' }, 503)
  }
})

app.post('/login', async c => {
  const status = await loginWithCodex()
  return status.connected
    ? success(c, status)
    : c.json({ code: 503, message: status.detail, data: status }, 503)
})

export default app
