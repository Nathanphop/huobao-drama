import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const root = new URL('..', import.meta.url)
const read = (path) => readFileSync(new URL(path, root), 'utf8')

test('episode creation derives resolution choices from the active video model', () => {
  const page = read('app/views/drama/detail.vue')

  assert.match(page, /v-model="newEpisodeTitle"/)
  assert.match(page, /v-model="newEpisodeResolution"/)
  assert.match(page, /:options="createResolutionOptions"/)
  assert.match(page, /activeVideoConfig/)
  assert.match(page, /activeVideoModel/)
  assert.match(page, /videoResolutionOptions\(config\?\.provider, model\)/)
  assert.match(page, /detail\.epCreate\.resolutionModel/)
})

test('video resolution profiles expose native tiers for every official integration', () => {
  const profiles = read('app/utils/video-resolution.ts')

  assert.match(profiles, /GOOGLE_FLOW[\s\S]*'360p'[\s\S]*'720p'/)
  assert.match(profiles, /SEEDANCE[\s\S]*'480p'[\s\S]*'720p'/)
  assert.match(profiles, /MINIMAX[\s\S]*nativeLabel: '768P'[\s\S]*nativeLabel: '2K'/)
  assert.match(profiles, /WAN[\s\S]*'480p'[\s\S]*'720p'[\s\S]*'1080p'/)
  assert.match(profiles, /providerId === 'google_flow'/)
  assert.match(profiles, /modelId\.startsWith\('wan3\.0-video'\)/)
})

test('addEpisode locks the same video config used to calculate resolutions', () => {
  const page = read('app/views/drama/detail.vue')
  const addEpisodeBody = page.slice(page.indexOf('async function addEpisode'), page.indexOf('async function confirmDelEpisode'))

  assert.match(addEpisodeBody, /drama_id: dramaId/)
  assert.match(addEpisodeBody, /title: newEpisodeTitle\.value/)
  assert.match(addEpisodeBody, /video_config_id: activeVideoConfig\.value\?\.id/)
  assert.match(addEpisodeBody, /resolution: newEpisodeResolution\.value/)
})

test('episode cards use their locked provider resolution profile', () => {
  const page = read('app/views/drama/detail.vue')

  assert.match(page, /resolutionOptionsForEpisode\(ep\)/)
  assert.match(page, /videoConfigForEpisode\(ep\)/)
  assert.match(page, /normalizeVideoResolution\(ep\.resolution, config\?\.provider/)
  assert.match(page, /episodeAPI\.update\(ep\.id, \{ resolution \}\)/)
  assert.match(page, /epResolutionLabel\(ep\)/)
})

test('active config selection mirrors backend priority rules', () => {
  const page = read('app/views/drama/detail.vue')

  assert.match(page, /aiConfigAPI\.list\('video'\)/)
  assert.match(page, /config\.is_active && OFFICIAL_VIDEO_PROVIDERS\.has\(config\.provider\)/)
  assert.match(page, /sort\(\(a, b\) => \(b\.priority \|\| 0\) - \(a\.priority \|\| 0\)\)/)
  assert.match(page, /preferredVideoResolution\(activeVideoConfig\.value\?\.provider, activeVideoModel\.value\)/)
})
