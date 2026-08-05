import { useEffect, useRef, useState } from 'react'
import {
  ASPECT_RATIOS,
  type AspectRatio,
  type ImageGenConfig,
  type ImageGenKind,
  type ImageGenResolution,
} from '../types/imageGen'

// Rough width:height used only to draw the little rectangle icon per ratio -- purely
// visual, has no bearing on what's actually sent to the backend (that's the "W:H"
// string itself, in config.aspectRatio).
function ratioBoxStyle(ratio: AspectRatio): { width: number; height: number } {
  const [w, h] = ratio.split(':').map(Number)
  const maxDim = 18
  if (w >= h) return { width: maxDim, height: Math.round((maxDim * h) / w) }
  return { width: Math.round((maxDim * w) / h), height: maxDim }
}

const VARIATIONS = [1, 2, 3, 4] as const
const RESOLUTIONS: { value: ImageGenResolution; enabled: boolean }[] = [
  { value: '1K', enabled: true },
  { value: '2K', enabled: false },
  { value: '4K', enabled: false },
]

export function ImageGenPanel({
  open,
  config,
  onChange,
  onClose,
}: {
  open: boolean
  config: ImageGenConfig
  onChange: (config: ImageGenConfig) => void
  onClose: () => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const [moreOpen, setMoreOpen] = useState(true)

  useEffect(() => {
    if (!open) return
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [open, onClose])

  if (!open) return null

  function patch(partial: Partial<ImageGenConfig>) {
    onChange({ ...config, ...partial })
  }

  return (
    <div
      ref={ref}
      className="absolute bottom-14 right-0 z-30 w-80 overflow-hidden rounded-2xl border border-gray-800 bg-gray-950 text-gray-100 shadow-2xl"
    >
      {/* Image vs Poster/Infographic tabs */}
      <div className="flex border-b border-gray-800 p-1.5">
        {(
          [
            { key: 'image', label: 'Image' },
            { key: 'poster', label: 'Poster / Infographic' },
          ] as { key: ImageGenKind; label: string }[]
        ).map((tab) => (
          <button
            key={tab.key}
            type="button"
            onClick={() => patch({ kind: tab.key })}
            className={`flex-1 rounded-lg px-3 py-1.5 text-xs font-medium transition ${
              config.kind === tab.key
                ? 'bg-gray-800 text-white'
                : 'text-gray-400 hover:text-gray-200'
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <div className="max-h-[26rem] overflow-y-auto p-4">
        <div className="mb-1 flex items-center justify-between">
          <span className="text-xs font-medium text-gray-300">Select Aspect Ratio</span>
          <button
            type="button"
            onClick={() => patch({ aspectRatio: '1:1' })}
            className="text-[11px] text-gray-500 hover:text-gray-300"
          >
            Reset
          </button>
        </div>

        <div className="mb-4 grid grid-cols-4 gap-2">
          {ASPECT_RATIOS.map((ratio) => {
            const box = ratioBoxStyle(ratio)
            const active = config.aspectRatio === ratio
            return (
              <button
                key={ratio}
                type="button"
                onClick={() => patch({ aspectRatio: ratio })}
                className={`flex flex-col items-center justify-center gap-1 rounded-xl border px-1 py-2 text-[10px] transition ${
                  active
                    ? 'border-white bg-gray-800 text-white'
                    : 'border-gray-800 text-gray-400 hover:border-gray-600 hover:text-gray-200'
                }`}
              >
                <span
                  className={`rounded-sm border ${active ? 'border-white' : 'border-gray-500'}`}
                  style={{ width: box.width, height: box.height }}
                />
                {ratio}
              </button>
            )
          })}
        </div>

        <button
          type="button"
          onClick={() => setMoreOpen((v) => !v)}
          className="mb-2 flex w-full items-center justify-between text-xs font-medium text-gray-300"
        >
          More options
          <span className="text-[10px] text-gray-500">{moreOpen ? '▾' : '▸'}</span>
        </button>

        {moreOpen && (
          <div className="flex flex-col gap-4">
            <label className="flex items-center justify-between">
              <span className="text-xs text-gray-300">Prompt Enhancer</span>
              <button
                type="button"
                role="switch"
                aria-checked={config.promptEnhancer}
                onClick={() => patch({ promptEnhancer: !config.promptEnhancer })}
                className={`relative h-5 w-9 rounded-full transition ${
                  config.promptEnhancer ? 'bg-emerald-500' : 'bg-gray-700'
                }`}
              >
                <span
                  className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition ${
                    config.promptEnhancer ? 'left-4.5 translate-x-0.5' : 'left-0.5'
                  }`}
                />
              </button>
            </label>

            <div>
              <div className="mb-1.5 text-xs text-gray-300">Variations</div>
              <div className="grid grid-cols-4 gap-2">
                {VARIATIONS.map((n) => (
                  <button
                    key={n}
                    type="button"
                    onClick={() => patch({ variations: n })}
                    className={`rounded-lg border px-2 py-1.5 text-xs transition ${
                      config.variations === n
                        ? 'border-white bg-gray-800 text-white'
                        : 'border-gray-800 text-gray-400 hover:border-gray-600 hover:text-gray-200'
                    }`}
                  >
                    {n}
                  </button>
                ))}
              </div>
            </div>

            <div>
              <div className="mb-1.5 text-xs text-gray-300">Resolution</div>
              <div className="flex gap-2">
                {RESOLUTIONS.map(({ value, enabled }) => (
                  <button
                    key={value}
                    type="button"
                    disabled={!enabled}
                    title={enabled ? undefined : 'Not supported by the current backend yet'}
                    onClick={() => enabled && patch({ resolution: value })}
                    className={`rounded-lg border px-3 py-1.5 text-xs transition ${
                      config.resolution === value
                        ? 'border-white bg-gray-800 text-white'
                        : enabled
                          ? 'border-gray-800 text-gray-400 hover:border-gray-600 hover:text-gray-200'
                          : 'cursor-not-allowed border-gray-900 text-gray-700'
                    }`}
                  >
                    {value}
                  </button>
                ))}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
