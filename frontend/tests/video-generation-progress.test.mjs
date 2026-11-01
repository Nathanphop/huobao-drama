import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const read = path => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8')

test('video workbench displays model-aware generation progress', () => {
  const episode = read('app/views/drama/episode.vue')
  const english = read('app/locales/en.json')
  const thai = read('app/locales/th.ts')

  assert.match(episode, /function videoTaskProgress\(sb\)/)
  assert.match(episode, /provider === 'google_flow'/)
  assert.match(episode, /reported > 0 \|\| provider === 'google_flow'/)
  assert.match(episode, /model\.includes\('quality'\)/)
  assert.match(episode, /role="progressbar"/)
  assert.match(episode, /videoTaskProgress\(selectedSb\)/)
  assert.match(episode, /generatingProgress/)
  assert.match(english, /"generatingProgress": "Generating video… \{p\}%"/)
  assert.match(thai, /generatingProgress: 'กำลังสร้างวิดีโอ… \{p\}%'/)
})

test('video workbench displays the live Google Flow credit balance and observed use', () => {
  const episode = read('app/views/drama/episode.vue')
  const api = read('app/composables/useApi.ts')
  const english = read('app/locales/en.json')
  const thai = read('app/locales/th.ts')

  assert.match(api, /googleFlowAPI/)
  assert.match(api, /\/google-flow\/credits/)
  assert.match(episode, /flow-credit-pill/)
  assert.match(episode, /flowCreditsObservedUsed\.value \+= previous - total/)
  assert.match(episode, /loadFlowCredits\(true\)/)
  assert.match(english, /"usedObserved": "\{n\} credits used since this page was opened"/)
  assert.match(thai, /usedObserved: 'ใช้จริง \{n\} เครดิตนับตั้งแต่เปิดหน้านี้'/)
})
