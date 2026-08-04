import { useEffect, useRef } from 'react'

export interface ToolItem {
  key: string
  label: string
  icon: string
  real?: boolean
}

const TOOLS: ToolItem[] = [
  { key: 'graph', label: 'Create graph or dashboard', icon: '📊' },
  { key: 'web_search', label: 'Web search', icon: '🔎' },
  { key: 'word', label: 'Create Word', icon: '📄' },
  { key: 'excel', label: 'Create Excel', icon: '📈' },
  { key: 'ppt', label: 'Create PowerPoint', icon: '📽️' },
  { key: 'image_generation', label: 'Image generation', icon: '🖼️', real: true },
]

export function ToolsMenu({
  open,
  onClose,
  onSelect,
}: {
  open: boolean
  onClose: () => void
  onSelect: (tool: ToolItem) => void
}) {
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        onClose()
      }
    }
    document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [open, onClose])

  if (!open) return null

  return (
    <div
      ref={ref}
      className="absolute bottom-12 left-0 z-20 w-64 overflow-hidden rounded-xl border border-gray-200 bg-white py-1 shadow-lg"
    >
      {TOOLS.map((tool) => (
        <button
          key={tool.key}
          type="button"
          onClick={() => onSelect(tool)}
          className="flex w-full items-center gap-3 px-3 py-2 text-left text-sm text-gray-800 hover:bg-gray-50"
        >
          <span className="text-base">{tool.icon}</span>
          <span>{tool.label}</span>
          {tool.real && (
            <span className="ml-auto rounded-full bg-emerald-50 px-2 py-0.5 text-[10px] font-medium text-emerald-700">
              live
            </span>
          )}
        </button>
      ))}
    </div>
  )
}
