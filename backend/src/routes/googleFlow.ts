import { Hono } from 'hono'
import { getConnInfo } from '@hono/node-server/conninfo'
import {
  getGoogleFlowCredits,
  getGoogleFlowStatus,
  GOOGLE_FLOW_IMAGE_MODELS,
  GOOGLE_FLOW_VIDEO_MODELS,
  refreshGoogleFlowSession,
} from '../services/google-flow.js'
import { success } from '../utils/response.js'

const app = new Hono()

function isLoopbackAddress(address?: string | null): boolean {
  if (!address) return false
  const normalized = address.toLowerCase().replace(/^::ffff:/, '')
  return normalized === '127.0.0.1' || normalized === '::1'
}

app.use('*', async (c, next) => {
  if (process.env.GOOGLE_FLOW_LOCAL_ALLOW_REMOTE === 'true') return next()
  if (!isLoopbackAddress(getConnInfo(c).remote.address)) {
    return c.json({ code: 403, message: 'Google Flow local companion is available only from this computer.' }, 403)
  }
  return next()
})

app.get('/status', async c => success(c, await getGoogleFlowStatus()))

app.get('/credits', async c => {
  const authuser = /^\d+$/.test(String(c.req.query('authuser') || ''))
    ? String(c.req.query('authuser'))
    : '0'
  const projectUrl = String(c.req.query('project_url') || '')
  const force = c.req.query('fresh') === '1'
  return success(c, await getGoogleFlowCredits(authuser, projectUrl, force))
})

app.get('/models', c => success(c, {
  image: GOOGLE_FLOW_IMAGE_MODELS.map((id, index) => ({ id, displayName: id, isDefault: index === 0 })),
  video: GOOGLE_FLOW_VIDEO_MODELS.map((id, index) => ({ id, displayName: id, isDefault: index === 0 })),
}))

app.post('/refresh', async c => {
  const body = await c.req.json().catch(() => ({}))
  await refreshGoogleFlowSession(/^\d+$/.test(String(body.authuser || '')) ? String(body.authuser) : '0')
  return success(c, await getGoogleFlowStatus())
})

export default app
