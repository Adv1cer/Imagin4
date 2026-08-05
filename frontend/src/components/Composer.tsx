import { useState } from 'react'
import type { FormEvent } from 'react'
import { ToolsMenu } from './ToolsMenu'
import type { ToolItem } from './ToolsMenu'
import { ImageGenPanel } from './ImageGenPanel'
import type { ImageGenConfig } from '../types/imageGen'

export function Composer({
  onSend,
  onToast,
  imageMode,
  imageGenConfig,
  onEnterImageMode,
  onExitImageMode,
  onImageGenConfigChange,
  disabled,
}: {
  onSend: (text: string) => void
  onToast: (message: string) => void
  imageMode: boolean
  imageGenConfig: ImageGenConfig
  onEnterImageMode: () => void
  onExitImageMode: () => void
  onImageGenConfigChange: (config: ImageGenConfig) => void
  disabled: boolean
}) {
  const [text, setText] = useState('')
  const [agentSkills, setAgentSkills] = useState(false)
  const [toolsOpen, setToolsOpen] = useState(false)
  const [panelOpen, setPanelOpen] = useState(false)

  function handleSubmit(e: FormEvent) {
    e.preventDefault()
    const trimmed = text.trim()
    if (!trimmed || disabled) return
    onSend(trimmed)
    setText('')
  }

  function handleToolSelect(tool: ToolItem) {
    setToolsOpen(false)
    if (tool.key === 'image_generation') {
      onEnterImageMode()
      setPanelOpen(true)
      onToast('Image generation mode enabled — describe the image to create.')
      return
    }
    onToast(`${tool.label} — coming soon`)
  }

  return (
    <div className="border-t border-gray-200 bg-white px-4 py-3">
      {imageMode && (
        <div className="mx-auto mb-2 flex max-w-3xl items-center justify-between rounded-lg bg-purple-50 px-3 py-2 text-sm text-purple-800">
          <span>🖼️ Image generation mode — your next message will generate an image.</span>
          <button
            type="button"
            onClick={onExitImageMode}
            className="ml-3 rounded-full px-2 py-0.5 text-xs font-medium text-purple-700 hover:bg-purple-100"
          >
            Cancel
          </button>
        </div>
      )}
      <form
        onSubmit={handleSubmit}
        className="relative mx-auto flex max-w-3xl flex-col gap-2 rounded-2xl border border-gray-300 bg-white p-2 shadow-sm"
      >
        <input
          type="text"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={
            imageMode ? 'Describe the image you want to create…' : 'How can I help you today?'
          }
          className="w-full resize-none border-none px-2 py-1.5 text-sm text-gray-900 outline-none placeholder:text-gray-400"
        />

        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <label className="flex items-center gap-2">
              <button
                type="button"
                role="switch"
                aria-checked={agentSkills}
                onClick={() => setAgentSkills((v) => !v)}
                className={`relative h-5 w-9 rounded-full transition ${
                  agentSkills ? 'bg-gray-900' : 'bg-gray-300'
                }`}
              >
                <span
                  className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition ${
                    agentSkills ? 'left-4.5 translate-x-0.5' : 'left-0.5'
                  }`}
                />
              </button>
              <span className="text-xs text-gray-500">Agent Skills</span>
            </label>
          </div>

          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={() => onToast('Attachments — coming soon')}
              title="Attach a file"
              className="rounded-full p-2 text-gray-500 hover:bg-gray-100"
            >
              📎
            </button>

            {imageMode && (
              <div className="relative">
                <button
                  type="button"
                  onClick={() => setPanelOpen((v) => !v)}
                  title="Image generation options"
                  className={`flex items-center gap-1 rounded-full border px-3 py-1 text-xs font-medium transition ${
                    panelOpen
                      ? 'border-purple-300 bg-purple-100 text-purple-800'
                      : 'border-gray-200 bg-gray-50 text-gray-700 hover:bg-gray-100'
                  }`}
                >
                  {imageGenConfig.kind === 'poster' ? 'Poster / Infographic' : 'Image'} · {imageGenConfig.aspectRatio}
                  <span className="text-[10px]">▾</span>
                </button>
                <ImageGenPanel
                  open={panelOpen}
                  config={imageGenConfig}
                  onChange={onImageGenConfigChange}
                  onClose={() => setPanelOpen(false)}
                />
              </div>
            )}

            <div className="relative">
              <button
                type="button"
                onClick={() => setToolsOpen((v) => !v)}
                title="Tools"
                className="rounded-full p-2 text-gray-500 hover:bg-gray-100"
              >
                🔧
              </button>
              <ToolsMenu
                open={toolsOpen}
                onClose={() => setToolsOpen(false)}
                onSelect={handleToolSelect}
              />
            </div>

            <button
              type="submit"
              disabled={disabled || !text.trim()}
              title="Send"
              className="flex h-9 w-9 items-center justify-center rounded-full bg-gray-900 text-white transition hover:bg-gray-800 disabled:cursor-not-allowed disabled:opacity-40"
            >
              ↑
            </button>
          </div>
        </div>
      </form>
    </div>
  )
}
