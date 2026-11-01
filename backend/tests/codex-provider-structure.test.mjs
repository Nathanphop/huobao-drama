import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8')
const ai = read('../src/services/ai.ts')
const companion = read('../src/services/codex-companion.ts')
const route = read('../src/routes/codex.ts')
const generator = read('../src/agents/generate.ts')
const generation = read('../src/services/generation.ts')
const codexImage = read('../src/services/codex-image.ts')

test('codex supports text through app-server and image through the local OAuth bridge', () => {
  assert.match(ai, /text: \['openai', 'gemini', 'volcengine', 'codex'\]/)
  assert.match(ai, /image: \['openai', 'gemini', 'volcengine', 'codex', 'google_flow'\]/)
  assert.match(generator, /provider\.toLowerCase\(\) === 'codex'/)
  assert.match(generator, /runCodexAgent/)
  assert.match(generation, /generateCodexImage/)
  assert.match(codexImage, /chatgpt\.com\/backend-api\/codex/)
  assert.match(codexImage, /CODEX_IMAGE_MODELS = \['gpt-image-2'\]/)
})

test('Codex models come from app-server while image models match the native image route', () => {
  assert.match(companion, /call\('model\/list'/)
  assert.match(companion, /includeHidden: false/)
  assert.match(route, /app\.get\('\/models'/)
  assert.match(route, /CODEX_IMAGE_MODELS/)
})

test('Codex app-server turns are isolated and expose only Huobao dynamic tools', () => {
  assert.match(companion, /approvalPolicy: 'never'/)
  assert.match(companion, /sandbox: 'read-only'/)
  assert.match(companion, /ephemeral: true/)
  assert.match(companion, /runtimeWorkspaceRoots: \[\]/)
  assert.match(companion, /dynamicTools: dynamicToolSpecs/)
  assert.match(companion, /message\.method === 'item\/tool\/call'/)
  assert.match(companion, /Only allowlisted Huobao tools are available/)
})

test('Codex local endpoints reject non-loopback clients by default', () => {
  assert.match(route, /getConnInfo\(c\)\.remote\.address/)
  assert.match(route, /isLoopbackAddress/)
  assert.match(route, /CODEX_LOCAL_ALLOW_REMOTE/)
})
