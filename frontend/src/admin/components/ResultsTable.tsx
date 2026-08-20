import type { RequestResult, RequestOutcome } from '../types'
import { fmtMs } from '../stats'

interface Props {
  results: (RequestResult | undefined)[]
}

const OUTCOME_STYLE: Record<RequestOutcome, string> = {
  pending: 'bg-gray-100 text-gray-500',
  submitting: 'bg-blue-100 text-blue-700',
  polling: 'bg-indigo-100 text-indigo-700',
  succeeded: 'bg-green-100 text-green-700',
  failed: 'bg-red-100 text-red-700',
  network_error: 'bg-orange-100 text-orange-700',
  submit_timeout: 'bg-orange-100 text-orange-700',
  poll_timeout: 'bg-amber-100 text-amber-700',
  non_image: 'bg-purple-100 text-purple-700',
}

export function ResultsTable({ results }: Props) {
  const rows = results.filter((r): r is RequestResult => r !== undefined).sort((a, b) => a.seq - b.seq)

  if (rows.length === 0) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-6 text-center text-sm text-gray-400">
        ยังไม่มีคำขอ — ตั้งค่าแล้วกด Run ได้เลยค่ะ
      </div>
    )
  }

  return (
    <div className="max-h-[480px] overflow-auto rounded-lg border border-gray-200 bg-white">
      <table className="w-full min-w-[900px] text-left text-xs">
        <thead className="sticky top-0 bg-gray-50 text-gray-500">
          <tr>
            <th className="px-2 py-1.5 font-medium">#</th>
            <th className="px-2 py-1.5 font-medium">user</th>
            <th className="px-2 py-1.5 font-medium">prompt</th>
            <th className="px-2 py-1.5 font-medium">status</th>
            <th className="px-2 py-1.5 font-medium">http</th>
            <th className="px-2 py-1.5 font-medium">submit</th>
            <th className="px-2 py-1.5 font-medium">gen time</th>
            <th className="px-2 py-1.5 font-medium">polls</th>
            <th className="px-2 py-1.5 font-medium">job id</th>
            <th className="px-2 py-1.5 font-medium">error</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.seq} className="border-t border-gray-100 hover:bg-gray-50">
              <td className="px-2 py-1 font-mono text-gray-500">{r.seq}</td>
              <td className="px-2 py-1 text-gray-600">{r.virtualUser}</td>
              <td className="px-2 py-1 text-gray-600" title={r.promptId}>
                {r.promptCategory}
              </td>
              <td className="px-2 py-1">
                <span className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${OUTCOME_STYLE[r.outcome]}`}>
                  {r.outcome}
                  {r.finalState && r.finalState !== r.outcome ? ` (${r.finalState})` : ''}
                </span>
              </td>
              <td className="px-2 py-1 font-mono text-gray-600">{r.httpStatus ?? '—'}</td>
              <td className="px-2 py-1 font-mono text-gray-600">{fmtMs(r.submitLatencyMs)}</td>
              <td className="px-2 py-1 font-mono font-medium text-gray-800">{fmtMs(r.totalGenerationMs)}</td>
              <td className="px-2 py-1 font-mono text-gray-500">{r.pollCount}</td>
              <td className="px-2 py-1 font-mono text-gray-400" title={r.jobId ?? ''}>
                {r.jobId ? `${r.jobId.slice(0, 8)}…` : '—'}
              </td>
              <td className="max-w-[220px] truncate px-2 py-1 text-red-600" title={r.errorDetail ?? ''}>
                {r.errorCode ? `[${r.errorCode}] ` : ''}
                {r.errorDetail ?? ''}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
