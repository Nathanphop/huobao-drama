import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const root = new URL('..', import.meta.url)
const read = (path) => readFileSync(new URL(path, root), 'utf8')

test('project creation dialog only asks for title and visual style', () => {
  const page = read('app/pages/index.vue')

  assert.match(page, /index\.createDialog\.title/)
  assert.match(page, /index\.createDialog\.style/)
  assert.doesNotMatch(page, /total_episodes/)
  // 硬编码风格列表已移除，预设来自 API
  assert.doesNotMatch(page, /\['realistic', 'anime'/)
  assert.match(page, /stylePresetAPI/)
  assert.match(page, /stylePresetAPI\.list\(\)/)
  assert.match(page, /styleLabel\(d\.style\)/)
  assert.match(page, /styleCount', \{ n: stylePresets\.length \}/)
})

test('useApi exposes style preset endpoints', () => {
  const useApi = read('app/composables/useApi.ts')

  assert.match(useApi, /stylePresetAPI/)
  assert.match(useApi, /\/style-presets/)
  assert.match(useApi, /\?all=1/)
})

test('settings page manages style presets in a base tab', () => {
  const settings = read('app/pages/settings.vue')

  assert.match(settings, /settings\.styles\.title/)
  assert.match(settings, /Palette/)
  assert.match(settings, /stylePresetAPI/)
  assert.match(settings, /\{ id: 'styles', label: t\('settings\.tabs\.styles'\)/)
  assert.match(settings, /startAddStyle/)
  assert.match(settings, /startEditStyle/)
  assert.match(settings, /toggleStyle/)
  assert.match(settings, /confirmDelStyle/)
  assert.match(settings, /styleToDelete/)
  assert.match(settings, /<ConfirmDialog/)
  assert.match(settings, /loadStylePresets/)
  assert.match(settings, /generateStylePreview/)
  assert.match(settings, /uploadStylePreview/)
  assert.match(settings, /removeStylePreview/)
  assert.match(settings, /openStylePreview/)
  assert.match(settings, /stylePreviewLightbox/)
  assert.match(settings, /@keydown\.esc\.prevent\.stop="closeStylePreview"/)
  assert.match(settings, /role="dialog"/)
  assert.match(settings, /p\.preview_image_url/)
  // 风格 key 编辑时不可修改
  assert.match(settings, /:disabled="!!styleEditId"/)
  // image_prompt_generator 默认提示词副本同步更新
  assert.doesNotMatch(settings, /必须包含 "consistent art style"/)
})

test('project creation shows the selected style preview', () => {
  const page = read('app/pages/index.vue')

  assert.match(page, /selectedStylePreview/)
  assert.match(page, /style-choice-preview/)
  assert.match(page, /preview_image_url/)
  assert.match(page, /thumbOf\(selectedStylePreview\)/)
})
