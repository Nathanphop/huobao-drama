import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8')
const ai = read('../src/services/ai.ts')
const service = read('../src/services/google-flow.ts')
const generation = read('../src/services/generation.ts')
const route = read('../src/routes/googleFlow.ts')
const schema = read('../src/db/schema.ts')
const sqliteSchema = read('../src/db/sqlite-schema.ts')

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

test('Google Flow video automation tolerates dynamic settings UI and local frame uploads', () => {
  const video = read('../scripts/google-flow/google_flow_video_auto.py')
  assert.match(video, /def find_video_settings_trigger/)
  assert.match(video, /Settings\\s\*trigger/)
  assert.match(video, /page\.reload\(wait_until="domcontentloaded"/)
  assert.match(video, /page\.expect_file_chooser/)
  assert.match(video, /\^Upload media\$/)
  assert.match(video, /chooser_info\.value\.set_files\(local_path\)/)
})

test('Google Flow forwards its real render percentage into persistent tasks', () => {
  const video = read('../scripts/google-flow/google_flow_video_auto.py')
  assert.match(video, /FLOW_PROGRESS:/)
  assert.match(video, /flow-grid-tile-container/)
  assert.match(video, /emit_flow_progress\(progress_values\[-1\], "rendering"\)/)
  assert.match(service, /line\.match\(\/\^FLOW_PROGRESS:/)
  assert.match(service, /onProgress\?: \(progress: FlowProgress\)/)
  assert.match(generation, /onProgress: async update/)
  assert.match(generation, /progress: flowProgress/)
  assert.match(schema, /progress: integer\('progress'\)/)
  assert.match(sqliteSchema, /progress: 'INTEGER DEFAULT 0'/)
})

test('Google Flow exposes the signed-in account credit balance from the live RPC', () => {
  assert.match(service, /const rpcId = 'nzlxg'/)
  assert.match(service, /source: 'google-flow-rpc'/)
  assert.match(service, /subscription: Number\(result\.subscription/)
  assert.match(service, /topUp: Number\(result\.topUp/)
  assert.match(route, /app\.get\('\/credits'/)
  assert.match(route, /getGoogleFlowCredits\(authuser, projectUrl, force\)/)
})
