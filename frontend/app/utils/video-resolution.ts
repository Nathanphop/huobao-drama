export type VideoResolutionQuality = 'smooth' | 'hd' | 'uhd'

export interface VideoResolutionOption {
  value: '360p' | '480p' | '720p' | '1080p'
  nativeLabel: string
  quality: VideoResolutionQuality
}

const GOOGLE_FLOW: VideoResolutionOption[] = [
  { value: '360p', nativeLabel: '360p', quality: 'smooth' },
  { value: '720p', nativeLabel: '720p', quality: 'hd' },
]

const SEEDANCE: VideoResolutionOption[] = [
  { value: '480p', nativeLabel: '480p', quality: 'smooth' },
  { value: '720p', nativeLabel: '720p', quality: 'hd' },
]

const MINIMAX: VideoResolutionOption[] = [
  { value: '720p', nativeLabel: '768P', quality: 'hd' },
  { value: '1080p', nativeLabel: '2K', quality: 'uhd' },
]

const WAN: VideoResolutionOption[] = [
  { value: '480p', nativeLabel: '480P', quality: 'smooth' },
  { value: '720p', nativeLabel: '720P', quality: 'hd' },
  { value: '1080p', nativeLabel: '1080P', quality: 'uhd' },
]

const CONSERVATIVE: VideoResolutionOption[] = [
  { value: '720p', nativeLabel: '720p', quality: 'hd' },
]

export const OFFICIAL_VIDEO_PROVIDERS = new Set(['google_flow', 'volcengine', 'minimax', 'aliyun'])

/**
 * Return the native resolution tiers for the selected video model.
 * Model signatures are checked as well as provider IDs so imported configs
 * keep the correct capabilities even when their provider label is customized.
 */
export function videoResolutionOptions(provider?: string | null, model?: string | null): VideoResolutionOption[] {
  const providerId = String(provider || '').trim().toLowerCase()
  const modelId = String(model || '').trim().toLowerCase()

  if (providerId === 'google_flow' || /\b(veo|omni)\b/.test(modelId)) return GOOGLE_FLOW
  if (providerId === 'aliyun' || modelId.startsWith('wan3.0-video')) return WAN
  if (providerId === 'minimax' || modelId.startsWith('minimax-')) return MINIMAX
  if (providerId === 'volcengine' || modelId.startsWith('doubao-seedance-2-0')) return SEEDANCE
  return CONSERVATIVE
}

export function preferredVideoResolution(provider?: string | null, model?: string | null): VideoResolutionOption['value'] {
  const options = videoResolutionOptions(provider, model)
  return options.find(option => option.value === '720p')?.value || options[0].value
}

export function normalizeVideoResolution(
  resolution: string | null | undefined,
  provider?: string | null,
  model?: string | null,
): VideoResolutionOption['value'] {
  const options = videoResolutionOptions(provider, model)
  const value = String(resolution || '').trim().toLowerCase()
  return options.find(option => option.value === value)?.value || preferredVideoResolution(provider, model)
}
