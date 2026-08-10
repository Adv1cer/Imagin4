import { useEffect, useState } from 'react'

// Rotating phrases standing in for the pipeline's actual stages (classify intent -> best
// -effort web research when needed -> design the image prompt -> enqueue generation --
// see backend/app/api/v1/chat_router.py:smart_message). There's no streaming/progress
// channel from the backend for this request today (it's one request/response, not SSE),
// so this can't reflect the model's literal live reasoning -- but cycling through the
// real stages the pipeline goes through beats a single static "…" the whole time.
const THINKING_PHRASES = [
  'กำลังทำความเข้าใจคำขอ',
  'กำลังเลือกเครื่องมือที่เหมาะสม',
  'กำลังตรวจสอบรายละเอียดที่มี',
  'กำลังค้นหาข้อมูลเพิ่มเติม',
  'กำลังออกแบบภาพให้เหมาะสม',
  'กำลังเตรียมส่งไปสร้างภาพ',
]

export function ThinkingIndicator() {
  const [phraseIndex, setPhraseIndex] = useState(0)

  useEffect(() => {
    const interval = setInterval(() => {
      setPhraseIndex((i) => (i + 1) % THINKING_PHRASES.length)
    }, 2200)
    return () => clearInterval(interval)
  }, [])

  return (
    <div className="flex items-center gap-2 py-1 text-sm text-gray-500">
      <span>{THINKING_PHRASES[phraseIndex]}</span>
      <span className="flex items-end gap-0.5">
        <span
          className="h-1.5 w-1.5 animate-bounce rounded-full bg-gray-400"
          style={{ animationDelay: '0ms' }}
        />
        <span
          className="h-1.5 w-1.5 animate-bounce rounded-full bg-gray-400"
          style={{ animationDelay: '150ms' }}
        />
        <span
          className="h-1.5 w-1.5 animate-bounce rounded-full bg-gray-400"
          style={{ animationDelay: '300ms' }}
        />
      </span>
    </div>
  )
}
