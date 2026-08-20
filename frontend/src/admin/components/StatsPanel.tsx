import { useMemo } from 'react'
import type { RequestResult } from '../types'
import { computeLatencyStats, fmtMinSec, fmtMs } from '../stats'
import { Timeline } from './Timeline'

interface Props {
  results: (RequestResult | undefined)[]
  startedAtMs: number | null
  finishedAtMs: number | null
  nowMs: number
  running: boolean
}

const OUTCOME_LABEL: Record<string, string> = {
  pending: 'รอคิว',
  submitting: 'กำลังส่ง',
  polling: 'กำลังเจน',
  succeeded: 'สำเร็จ',
  failed: 'ล้มเหลว',
  network_error: 'network error',
  submit_timeout: 'submit timeout',
  poll_timeout: 'poll timeout',
  non_image: 'ไม่ใช่ image_job',
}

export function StatsPanel({ results, startedAtMs, finishedAtMs, nowMs, running }: Props) {
  const defined = useMemo(() => results.filter((r): r is RequestResult => r !== undefined), [results])

  const counts = useMemo(() => {
    const c: Record<string, number> = {}
    for (const r of defined) c[r.outcome] = (c[r.outcome] ?? 0) + 1
    return c
  }, [defined])

  const submitStats = useMemo(
    () => computeLatencyStats(defined.map((r) => r.submitLatencyMs).filter((v): v is number => v !== null)),
    [defined],
  )
  const genStats = useMemo(
    () =>
      computeLatencyStats(
        defined
          .filter((r) => r.outcome === 'succeeded' || r.outcome === 'failed')
          .map((r) => r.totalGenerationMs)
          .filter((v): v is number => v !== null),
      ),
    [defined],
  )

  const elapsedMs = startedAtMs !== null ? (finishedAtMs ?? nowMs) - startedAtMs : 0
  const dispatchedCount = defined.filter((r) => r.dispatchedAtMs !== null).length
  const inFlight = defined.filter((r) => r.outcome === 'submitting' || r.outcome === 'polling').length
  const throughput = elapsedMs > 0 ? dispatchedCount / (elapsedMs / 1000) : 0

  const total = results.length
  const succeeded = counts.succeeded ?? 0
  const failedLike =
    (counts.failed ?? 0) + (counts.network_error ?? 0) + (counts.submit_timeout ?? 0) + (counts.poll_timeout ?? 0)

  return (
    <div className="space-y-4 rounded-lg border border-gray-200 bg-white p-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-900">Live stats</h2>
        <span className={`text-xs font-medium ${running ? 'text-blue-600' : 'text-gray-500'}`}>
          {running ? '● กำลังรัน' : finishedAtMs ? '✓ เสร็จแล้ว' : 'ยังไม่เริ่ม'}
        </span>
      </div>

      <div className="grid grid-cols-4 gap-2 text-center sm:grid-cols-7">
        <Stat label="ทั้งหมด" value={total} />
        <Stat label="ส่งแล้ว" value={dispatchedCount} />
        <Stat label="กำลังทำงาน" value={inFlight} tone="blue" />
        <Stat label="สำเร็จ" value={succeeded} tone="green" />
        <Stat label="ล้มเหลว" value={failedLike} tone="red" />
        <Stat label="ผ่านไป" value={fmtMinSec(elapsedMs)} />
        <Stat label="req/s" value={throughput.toFixed(2)} />
      </div>

      <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-xs">
        {Object.entries(OUTCOME_LABEL).map(([k, label]) =>
          counts[k] ? (
            <div key={k} className="flex justify-between text-gray-600">
              <span>{label}</span>
              <span className="font-mono">{counts[k]}</span>
            </div>
          ) : null,
        )}
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <LatencyTable title="เวลาตอบสนอง POST (submit latency — จนกว่า API รับงาน 202)" stats={submitStats} />
        <LatencyTable
          title="เวลาเจนภาพทั้งหมด (dispatch → job terminal state)"
          stats={genStats}
          highlight
        />
      </div>

      <div>
        <h3 className="mb-1 text-xs font-medium text-gray-600">Concurrency ตามเวลา</h3>
        <Timeline results={results} startedAtMs={startedAtMs} nowMs={nowMs} />
      </div>
    </div>
  )
}

function Stat({ label, value, tone }: { label: string; value: string | number; tone?: 'blue' | 'green' | 'red' }) {
  const color =
    tone === 'blue' ? 'text-blue-600' : tone === 'green' ? 'text-green-600' : tone === 'red' ? 'text-red-600' : 'text-gray-900'
  return (
    <div className="rounded border border-gray-100 bg-gray-50 py-2">
      <div className={`text-lg font-semibold ${color}`}>{value}</div>
      <div className="text-[11px] text-gray-500">{label}</div>
    </div>
  )
}

function LatencyTable({
  title,
  stats,
  highlight,
}: {
  title: string
  stats: ReturnType<typeof computeLatencyStats>
  highlight?: boolean
}) {
  return (
    <div className={`rounded border p-3 ${highlight ? 'border-blue-200 bg-blue-50/40' : 'border-gray-100'}`}>
      <p className="mb-2 text-xs font-medium text-gray-700">{title}</p>
      <div className="grid grid-cols-3 gap-x-3 gap-y-1 text-xs">
        <Metric label="min" value={fmtMs(stats.min)} />
        <Metric label="mean" value={fmtMs(stats.mean)} />
        <Metric label="p50" value={fmtMs(stats.p50)} />
        <Metric label="p95" value={fmtMs(stats.p95)} />
        <Metric label="p99" value={fmtMs(stats.p99)} />
        <Metric label="max" value={fmtMs(stats.max)} />
      </div>
      <p className="mt-1 text-[11px] text-gray-400">n = {stats.count}</p>
    </div>
  )
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between">
      <span className="text-gray-500">{label}</span>
      <span className="font-mono text-gray-800">{value}</span>
    </div>
  )
}
