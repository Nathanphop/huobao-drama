import { Hono } from 'hono'
import { bodyLimit } from 'hono/body-limit'
import { eq, and } from 'drizzle-orm'
import { db, getInsertId, schema } from '../db/index.js'
import { success, created, badRequest, notFound, now } from '../utils/response.js'
import { toSnakeCase } from '../utils/transform.js'
import { generateImage } from '../services/generation.js'
import { generateImageThumb, saveUploadedFile } from '../utils/storage.js'

const app = new Hono()

const VALUE_PATTERN = /^[a-z0-9][a-z0-9-]*$/
const PREVIEW_MAX_BYTES = 12 * 1024 * 1024
const PREVIEW_MIME_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp'])
const SAMPLE_SUBJECT = 'a cinematic medium-wide character portrait of a young traveler in a detailed city street, balanced character and environment composition, expressive face, natural pose, no text, no logo, no watermark'

// GET /style-presets — 默认只返回启用项，?all=1 返回全部
app.get('/', async (c) => {
  const all = c.req.query('all') === '1'
  const rows = await db.select().from(schema.stylePresets)
  const filtered = all ? rows : rows.filter(r => r.isActive)
  filtered.sort((a, b) => (a.sortOrder || 0) - (b.sortOrder || 0) || a.id - b.id)
  return success(c, filtered.map(r => toSnakeCase(r)))
})

// POST /style-presets — 新增风格预设
app.post('/', async (c) => {
  const body = await c.req.json()
  if (!body.name?.trim()) return badRequest(c, 'Style name is required')
  if (!body.value?.trim()) return badRequest(c, 'Style key is required')
  if (!VALUE_PATTERN.test(body.value.trim())) return badRequest(c, 'Style key may contain lowercase letters, numbers, and hyphens only')
  if (!body.prompt?.trim()) return badRequest(c, 'English prompt fragment is required')

  const value = body.value.trim()
  const [dup] = await db.select().from(schema.stylePresets)
    .where(eq(schema.stylePresets.value, value))
  if (dup) return badRequest(c, 'Style key already exists')

  const ts = now()
  const res = await db.insert(schema.stylePresets).values({
    name: body.name.trim(),
    value,
    prompt: body.prompt.trim(),
    description: body.description || null,
    sortOrder: Number(body.sort_order ?? body.sortOrder ?? 0),
    isActive: body.is_active === false || body.is_active === 0 ? false : true,
    createdAt: ts,
    updatedAt: ts,
  })
  const [row] = await db.select().from(schema.stylePresets)
    .where(eq(schema.stylePresets.id, getInsertId(res)))
  return created(c, toSnakeCase(row))
})

// PUT /style-presets/:id — 更新（value 创建后不可修改）
app.put('/:id', async (c) => {
  const id = Number(c.req.param('id'))
  const body = await c.req.json()
  const [row] = await db.select().from(schema.stylePresets)
    .where(eq(schema.stylePresets.id, id))
  if (!row) return notFound(c, 'Style preset not found')

  const updates: Record<string, any> = { updatedAt: now() }
  if (body.name !== undefined) updates.name = String(body.name).trim()
  if (body.prompt !== undefined) updates.prompt = String(body.prompt).trim()
  if (body.description !== undefined) updates.description = body.description || null
  if (body.preview_image_url !== undefined || body.previewImageUrl !== undefined) {
    updates.previewImageUrl = body.preview_image_url ?? body.previewImageUrl ?? null
  }
  if (body.sort_order !== undefined || body.sortOrder !== undefined) {
    updates.sortOrder = Number(body.sort_order ?? body.sortOrder)
  }
  if (body.is_active !== undefined || body.isActive !== undefined) {
    const v = body.is_active ?? body.isActive
    updates.isActive = !(v === false || v === 0)
  }
  await db.update(schema.stylePresets).set(updates)
    .where(eq(schema.stylePresets.id, id))
  return success(c)
})

// POST /style-presets/:id/generate-preview — create one comparable sample with the active image provider.
app.post('/:id/generate-preview', async (c) => {
  const id = Number(c.req.param('id'))
  const body = await c.req.json().catch(() => ({}))
  const [preset] = await db.select().from(schema.stylePresets).where(eq(schema.stylePresets.id, id))
  if (!preset) return notFound(c, 'Style preset not found')

  const subject = typeof body.subject_prompt === 'string' && body.subject_prompt.trim()
    ? body.subject_prompt.trim()
    : SAMPLE_SUBJECT
  try {
    const taskId = await generateImage({
      stylePresetId: id,
      prompt: `${preset.prompt}, ${subject}`,
      model: typeof body.model === 'string' ? body.model : undefined,
      configId: Number.isSafeInteger(Number(body.config_id)) ? Number(body.config_id) : undefined,
      size: '1024x1024',
    })
    return created(c, { task_id: taskId })
  } catch (error: any) {
    return badRequest(c, error?.message || 'Could not start style preview generation')
  }
})

// POST /style-presets/:id/preview — upload and associate a local sample image.
app.post('/:id/preview', bodyLimit({
  maxSize: PREVIEW_MAX_BYTES + 64 * 1024,
  onError: c => badRequest(c, 'Preview image must be 12MB or smaller'),
}), async (c) => {
  const id = Number(c.req.param('id'))
  const [preset] = await db.select().from(schema.stylePresets).where(eq(schema.stylePresets.id, id))
  if (!preset) return notFound(c, 'Style preset not found')
  const body = await c.req.parseBody()
  const file = body.file
  if (!(file instanceof File)) return badRequest(c, 'Preview image is required')
  if (!PREVIEW_MIME_TYPES.has(file.type)) return badRequest(c, 'Preview image must be PNG, JPEG, or WebP')
  const data = await file.arrayBuffer()
  if (data.byteLength > PREVIEW_MAX_BYTES) return badRequest(c, 'Preview image must be 12MB or smaller')

  const localPath = await saveUploadedFile(data, 'style-previews', file.name)
  await generateImageThumb(localPath)
  await db.update(schema.stylePresets)
    .set({ previewImageUrl: localPath, updatedAt: now() })
    .where(eq(schema.stylePresets.id, id))
  return success(c, { preview_image_url: localPath })
})

// DELETE /style-presets/:id/preview — keep the media file recoverable, only remove the association.
app.delete('/:id/preview', async (c) => {
  const id = Number(c.req.param('id'))
  const [preset] = await db.select().from(schema.stylePresets).where(eq(schema.stylePresets.id, id))
  if (!preset) return notFound(c, 'Style preset not found')
  await db.update(schema.stylePresets).set({ previewImageUrl: null, updatedAt: now() })
    .where(eq(schema.stylePresets.id, id))
  return success(c)
})

// DELETE /style-presets/:id — 硬删除（已使用此风格的项目保留 key，无害）
app.delete('/:id', async (c) => {
  const id = Number(c.req.param('id'))
  await db.delete(schema.stylePresets).where(eq(schema.stylePresets.id, id))
  return success(c)
})

export default app
