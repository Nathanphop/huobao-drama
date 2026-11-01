import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const root = new URL('..', import.meta.url)
const read = path => readFileSync(new URL(path, root), 'utf8')

test('source update checks use the release version instead of the backend npm version', () => {
  const service = read('src/services/server-update.ts')

  assert.match(service, /process\.env\.HUOBAO_VERSION/)
  assert.match(service, /\.\.\/\.\.\/\.\.\/desktop\/package\.json/)
  assert.match(service, /\.\.\/\.\.\/package\.json/)
  assert.ok(
    service.indexOf("../../../desktop/package.json") < service.indexOf("../../package.json"),
    'the release package must be checked before the backend npm package',
  )
})
