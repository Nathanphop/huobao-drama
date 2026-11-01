import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8')
const ai = read('../src/services/ai.ts')
const service = read('../src/services/google-flow.ts')
const generation = read('../src/services/generation.ts')
const route = read('../src/routes/googleFlow.ts')

test('Google Flow is limited to image and video providers', () => {
  assert.doesNotMatch(ai, /text: \[[^\]]*google_flow/)
  assert.match(ai, /image: \[[^\]]*'google_flow'/)
  assert.match(ai, /video: \[[^\]]*'google_flow'/)
})

test('Google Flow uses Chrome 9222 cookies locally without an API key', () => {
  assert.match(service, /127\.0\.0\.1:9222/)
  assert.match(service, /Storage\.getCookies/)
  assert.match(service, /session\.json/)
  assert.match(route, /getConnInfo\(c\)\.remote\.address/)
  assert.match(route, /GOOGLE_FLOW_LOCAL_ALLOW_REMOTE/)
})

test('Google Flow generation is wired into the Huobao image and video task pipeline', () => {
  assert.match(generation, /generateGoogleFlowImage/)
  assert.match(generation, /generateGoogleFlowVideo/)
  assert.match(generation, /handleVideoCompleteBuffer/)
  assert.match(service, /google_flow_image_auto\.py/)
  assert.match(service, /google_flow_video_auto\.py/)
})
