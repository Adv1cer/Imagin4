// Config collected by ImageGenPanel and consumed by ChatScreen.handleSend. `kind`
// selects which backend workflow gets used (see backend/app/domain/jobs/workflow_registry.py):
// "image" -> workflow_name "image_basic" (routes to ComfyUI), "poster" -> workflow_name
// "poster_infographic" (routes to Gemini -- better at in-image text/layout).
export type ImageGenKind = 'image' | 'poster'
export type ImageGenResolution = '1K' | '2K' | '4K'

export const ASPECT_RATIOS = [
  '9:16',
  '2:3',
  '3:4',
  '4:5',
  '1:1',
  '5:4',
  '4:3',
  '3:2',
  '16:9',
  '21:9',
] as const
export type AspectRatio = (typeof ASPECT_RATIOS)[number]

export interface ImageGenConfig {
  kind: ImageGenKind
  aspectRatio: AspectRatio
  resolution: ImageGenResolution
  variations: 1 | 2 | 3 | 4
  promptEnhancer: boolean
}

export const DEFAULT_IMAGE_GEN_CONFIG: ImageGenConfig = {
  kind: 'image',
  aspectRatio: '1:1',
  resolution: '1K',
  variations: 1,
  promptEnhancer: false,
}

export function workflowNameFor(kind: ImageGenKind): string {
  return kind === 'poster' ? 'poster_infographic' : 'image_basic'
}
