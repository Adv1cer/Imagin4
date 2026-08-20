import { useMemo } from 'react'
import type { RequestResult } from '../types'

interface Props {
  results: (RequestResult | undefined)[]
  startedAtMs: number | null
  nowMs: number
  buckets?: number
}

// Lightweight inline-SVG concurrency-over-time chart. No chart library is installed in
// this project (package.json has no recharts/chart.js/etc), so this hand-rolls a bar
// chart from scratch rather than adding a dependency for one graph.
export function Timeline({ results, startedAtMs, nowMs, buckets = 60 }: Props) {
  const counts = useMemo(() => {
    if (startedAtMs === null) return []
    const span = Math.max(1, nowMs - startedAtMs)
    const bucketMs = span / buckets
    const out = new Array(buckets).fill(0)
    for (const r of results) {
      if (!r || r.dispatchedAtMs === null) continue
      const start = r.dispatchedAtMs
      const end = r.completedAtMs ?? nowMs
      const startBucket = Math.max(0, Math.floor((start - startedAtMs) / bucketMs))
      const endBucket = Math.min(buckets - 1, Math.floor((end - startedAtMs) / bucketMs))
      for (let b = startBucket; b <= endBucket; b++) out[b] += 1
    }
    return out
  }, [results, startedAtMs, nowMs, buckets])

  if (startedAtMs === null || counts.length === 0) {
    return <div className="flex h-24 items-center justify-center text-xs text-gray-400">ยังไม่มีข้อมูล</div>
  }

  const max = Math.max(1, ...counts)
  const width = 600
  const height = 96
  const barWidth = width / counts.length

  return (
    <div>
      <svg viewBox={`0 0 ${width} ${height}`} className="h-24 w-full" preserveAspectRatio="none">
        {counts.map((c, i) => {
          const h = (c / max) * (height - 4)
          return (
            <rect
              key={i}
              x={i * barWidth}
              y={height - h}
              width={Math.max(1, barWidth - 1)}
              height={h}
              fill="#3b82f6"
              opacity={0.85}
            />
          )
        })}
      </svg>
      <p className="mt-1 text-xs text-gray-500">
        in-flight concurrency สูงสุดในกราฟ: {max} คำขอพร้อมกัน (นับตั้งแต่ dispatch จนถึง terminal state)
      </p>
    </div>
  )
}
