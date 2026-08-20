import type { RequestResult } from './types'

export function percentile(values: number[], p: number): number | null {
  if (values.length === 0) return null
  const sorted = [...values].sort((a, b) => a - b)
  const idx = Math.min(sorted.length - 1, Math.max(0, Math.ceil((p / 100) * sorted.length) - 1))
  return sorted[idx]
}

export function mean(values: number[]): number | null {
  if (values.length === 0) return null
  return values.reduce((a, b) => a + b, 0) / values.length
}

export interface LatencyStats {
  count: number
  min: number | null
  mean: number | null
  p50: number | null
  p95: number | null
  p99: number | null
  max: number | null
}

export function computeLatencyStats(values: number[]): LatencyStats {
  const sorted = [...values].sort((a, b) => a - b)
  return {
    count: values.length,
    min: sorted[0] ?? null,
    mean: mean(values),
    p50: percentile(values, 50),
    p95: percentile(values, 95),
    p99: percentile(values, 99),
    max: sorted[sorted.length - 1] ?? null,
  }
}

export function fmtMs(ms: number | null): string {
  if (ms === null || Number.isNaN(ms)) return '—'
  if (ms < 1000) return `${Math.round(ms)} ms`
  return `${(ms / 1000).toFixed(2)} s`
}

export function fmtMinSec(ms: number | null): string {
  if (ms === null || Number.isNaN(ms)) return '—'
  const totalSec = Math.round(ms / 1000)
  const min = Math.floor(totalSec / 60)
  const sec = totalSec % 60
  if (min === 0) return `${sec}s`
  return `${min}m ${sec}s`
}

export function toCsv(results: RequestResult[]): string {
  const headers = [
    'seq',
    'virtual_user',
    'prompt_id',
    'prompt_category',
    'scheduled_at_ms',
    'dispatched_at_ms',
    'submit_latency_ms',
    'http_status',
    'job_id',
    'job_type',
    'final_state',
    'total_generation_ms',
    'poll_count',
    'outcome',
    'error_code',
    'error_detail',
  ]
  const rows = results.map((r) =>
    [
      r.seq,
      r.virtualUser,
      r.promptId,
      r.promptCategory,
      Math.round(r.scheduledAtMs),
      r.dispatchedAtMs !== null ? Math.round(r.dispatchedAtMs) : '',
      r.submitLatencyMs !== null ? Math.round(r.submitLatencyMs) : '',
      r.httpStatus ?? '',
      r.jobId ?? '',
      r.jobType ?? '',
      r.finalState ?? '',
      r.totalGenerationMs !== null ? Math.round(r.totalGenerationMs) : '',
      r.pollCount,
      r.outcome,
      r.errorCode ?? '',
      (r.errorDetail ?? '').replace(/[\n,]/g, ' '),
    ].join(','),
  )
  return [headers.join(','), ...rows].join('\n')
}

export function downloadText(filename: string, content: string, mime = 'text/plain') {
  const blob = new Blob([content], { type: mime })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}
