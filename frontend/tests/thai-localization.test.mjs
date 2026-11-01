import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const root = new URL('..', import.meta.url)
const read = (path) => readFileSync(new URL(path, root), 'utf8')

test('Thai is registered as a UI and content language', () => {
  const i18n = read('app/composables/i18n.ts')
  const unified = read('app/composables/useUnifiedLanguage.ts')
  const settings = read('app/pages/settings.vue')

  assert.match(i18n, /UiLocale = 'zh' \| 'en' \| 'ja' \| 'ko' \| 'th'/)
  assert.match(i18n, /\{ value: 'th', label: 'ไทย' \}/)
  assert.match(i18n, /messages: \{ zh, en, ja, ko, th \}/)
  assert.match(unified, /th: 'langNameTh'/)
  assert.match(settings, /\{ value: 'th', label: 'ไทย', shortLabel: 'ไทย' \}/)
})

test('Thai catalog covers the main project, settings and production surfaces', () => {
  const thai = read('app/locales/th.ts')

  assert.match(thai, /โปรเจกต์ใหม่/)
  assert.match(thai, /การตั้งค่าบริการ AI/)
  assert.match(thai, /เนื้อหาต้นฉบับ/)
  assert.match(thai, /แบ่งสตอรีบอร์ด/)
  assert.match(thai, /ผลิตวิดีโอ/)
  assert.match(thai, /fallbackLocale:|complete English catalog as a safe fallback/)
  assert.ok((thai.match(/[ก-๙]/g) || []).length > 1000, 'Thai catalog should contain substantial translated coverage')
})

test('other locales can name Thai in the unified-language confirmation', () => {
  for (const locale of ['en', 'zh', 'ja', 'ko']) {
    assert.match(read(`app/locales/${locale}.json`), /"langNameTh"/)
  }
})

test('unified language switching persists first and keeps browser surfaces in sync', () => {
  const unified = read('app/composables/useUnifiedLanguage.ts')
  const plugin = read('app/plugins/i18n.client.ts')
  const settings = read('app/pages/settings.vue')

  assert.match(unified, /onConfirm: \(\) => finish\(true\)/)
  assert.match(unified, /onCancel: \(\) => finish\(false\)/)
  assert.match(unified, /saved\?\.language !== lang/)
  assert.match(unified, /failed to persist unified language/)
  assert.doesNotMatch(unified, /setInterval/)
  assert.match(plugin, /settingsAPI\.contentLanguage\(\)/)
  assert.match(plugin, /setUiLocale\(language as UiLocale\)/)
  assert.match(settings, /contentLanguage\.value === lang && locale\.value === lang/)
})
