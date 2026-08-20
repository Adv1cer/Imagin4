import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react'
import { ConfigPanel } from '../admin/components/ConfigPanel'
import { ScenarioPanel } from '../admin/components/ScenarioPanel'
import { StatsPanel } from '../admin/components/StatsPanel'
import { ResultsTable } from '../admin/components/ResultsTable'
import { Gallery } from '../admin/components/Gallery'
import type { ModelProfilesResponse } from '../admin/adminApi'
import { DEFAULT_REQUEST_OPTIONS, runScenario, totalForMode, type RunHandle, type VirtualUser } from '../admin/scenarios'
import { DEFAULT_SCENARIO, type RequestResult } from '../admin/types'
import { downloadText, toCsv } from '../admin/stats'

// /admin -- load-test console for POST /v1/agent/message. Not part of the normal
// authed app flow: this talks directly to an operator-chosen backend host using bearer
// API keys (see admin/adminApi.ts's header comment for why it can't reuse ../api/client
// or the cookie session). Everything here runs client-side in the browser tab that has
// this page open; closing the tab or navigating away cancels any run in progress.

function parseTokens(raw: string): VirtualUser[] {
  return raw
    .split(/[\n,]+/)
    .map((t) => t.trim())
    .filter(Boolean)
    .map((token, i) => ({ id: `vu${i + 1}`, token }))
}

export function AdminTestScreen() {
  const [baseUrl, setBaseUrl] = useState('http://10.7.2.63:8000')
  const [tokensRaw, setTokensRaw] = useState('')
  const [reqOpts, setReqOpts] = useState(DEFAULT_REQUEST_OPTIONS)
  const [scenario, setScenario] = useState(DEFAULT_SCENARIO)
  const [profiles, setProfiles] = useState<ModelProfilesResponse | null>(null)
  const [profileError, setProfileError] = useState<string | null>(null)

  const [running, setRunning] = useState(false)
  const [startedAtMs, setStartedAtMs] = useState<number | null>(null)
  const [finishedAtMs, setFinishedAtMs] = useState<number | null>(null)
  const [nowMs, setNowMs] = useState(0)
  const [view, setView] = useState<'table' | 'gallery'>('table')

  const resultsRef = useRef<(RequestResult | undefined)[]>([])
  const [, forceTick] = useReducer((x: number) => x + 1, 0)
  const pendingRerender = useRef(false)
  const runHandleRef = useRef<RunHandle | null>(null)

  const virtualUsers = useMemo(() => parseTokens(tokensRaw), [tokensRaw])
  const tokensById = useMemo(
    () => Object.fromEntries(virtualUsers.map((vu) => [vu.id, vu.token])),
    [virtualUsers],
  )

  const scheduleRerender = useCallback(() => {
    if (pendingRerender.current) return
    pendingRerender.current = true
    requestAnimationFrame(() => {
      pendingRerender.current = false
      forceTick()
    })
  }, [])

  // Live clock while a run is active, so elapsed time / the concurrency timeline keep
  // moving even between result updates (e.g. during a long batched-rate gap).
  useEffect(() => {
    if (!running) return
    const id = setInterval(() => setNowMs(performance.now()), 250)
    return () => clearInterval(id)
  }, [running])

  const handleProfilesLoaded = (p: ModelProfilesResponse | null, error?: string) => {
    setProfiles(p)
    setProfileError(error ?? null)
  }

  const handleRun = () => {
    if (virtualUsers.length === 0) return
    const total = totalForMode(scenario)
    resultsRef.current = new Array(total)
    setStartedAtMs(performance.now())
    setFinishedAtMs(null)
    setNowMs(performance.now())
    setRunning(true)

    const handle = runScenario(baseUrl, virtualUsers, scenario, reqOpts, (r) => {
      resultsRef.current[r.seq] = r
      scheduleRerender()
    })
    runHandleRef.current = handle
    handle.done.then(() => {
      setRunning(false)
      setFinishedAtMs(performance.now())
      forceTick()
    })
  }

  const handleCancel = () => {
    runHandleRef.current?.cancel()
  }

  const handleExportCsv = () => {
    const rows = resultsRef.current.filter((r): r is RequestResult => r !== undefined)
    downloadText(`imagin-admin-test-${Date.now()}.csv`, toCsv(rows), 'text/csv')
  }

  const handleExportJson = () => {
    const rows = resultsRef.current.filter((r): r is RequestResult => r !== undefined)
    downloadText(`imagin-admin-test-${Date.now()}.json`, JSON.stringify(rows, null, 2), 'application/json')
  }

  const canRun = !running && virtualUsers.length > 0 && baseUrl.trim().length > 0

  return (
    <div className="min-h-screen bg-gray-50 pb-16">
      <header className="border-b border-gray-200 bg-white px-6 py-4">
        <h1 className="text-base font-semibold text-gray-900">Imagin — Admin load-test console</h1>
        <p className="text-xs text-gray-500">
          ยิงคำขอทดสอบไปยัง <code>POST /v1/agent/message</code> หลายรูปแบบ พร้อมวัดเวลา submit และเวลาเจนภาพจริง
        </p>
      </header>

      <div className="mx-auto max-w-6xl space-y-4 px-4 py-4">
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <ConfigPanel
            baseUrl={baseUrl}
            onBaseUrlChange={setBaseUrl}
            tokensRaw={tokensRaw}
            onTokensRawChange={setTokensRaw}
            virtualUserCount={virtualUsers.length}
            reqOpts={reqOpts}
            onReqOptsChange={setReqOpts}
            disabled={running}
            profiles={profiles}
            onProfilesLoaded={handleProfilesLoaded}
          />
          <ScenarioPanel cfg={scenario} onChange={setScenario} disabled={running} virtualUserCount={virtualUsers.length} />
        </div>

        {profileError && (
          <p className="rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
            โหลด model profiles ไม่สำเร็จ: {profileError}
          </p>
        )}

        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            disabled={!canRun}
            onClick={handleRun}
            className="rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-40"
          >
            ▶ Run
          </button>
          <button
            type="button"
            disabled={!running}
            onClick={handleCancel}
            className="rounded border border-gray-300 px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40"
          >
            ■ Cancel
          </button>
          <button
            type="button"
            disabled={resultsRef.current.length === 0}
            onClick={handleExportCsv}
            className="rounded border border-gray-300 px-3 py-2 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-40"
          >
            Export CSV
          </button>
          <button
            type="button"
            disabled={resultsRef.current.length === 0}
            onClick={handleExportJson}
            className="rounded border border-gray-300 px-3 py-2 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-40"
          >
            Export JSON
          </button>
          {virtualUsers.length === 0 && (
            <span className="text-xs text-red-500">ใส่ API key อย่างน้อย 1 ตัวก่อนถึงจะกด Run ได้ค่ะ</span>
          )}
        </div>

        <StatsPanel
          results={resultsRef.current}
          startedAtMs={startedAtMs}
          finishedAtMs={finishedAtMs}
          nowMs={nowMs || performance.now()}
          running={running}
        />

        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => setView('table')}
            className={`rounded px-3 py-1.5 text-xs font-medium ${
              view === 'table' ? 'bg-gray-900 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'
            } border border-gray-300`}
          >
            ตาราง
          </button>
          <button
            type="button"
            onClick={() => setView('gallery')}
            className={`rounded px-3 py-1.5 text-xs font-medium ${
              view === 'gallery' ? 'bg-gray-900 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'
            } border border-gray-300`}
          >
            🖼 Gallery
          </button>
        </div>

        {view === 'table' ? (
          <ResultsTable results={resultsRef.current} />
        ) : (
          <Gallery results={resultsRef.current} baseUrl={baseUrl} tokensById={tokensById} />
        )}

        <footer className="pt-2 text-[11px] leading-relaxed text-gray-400">
          หมายเหตุ: หน้านี้เรียก backend ตรงๆ ด้วย bearer API key จาก browser tab นี้ — ถ้าเจอ "Network error" ให้เช็ค
          CORS allowlist (<code>APP_CORS_ALLOW_ORIGINS_CSV</code> ฝั่ง backend ต้องมี origin ของหน้านี้) และความถูกต้องของ
          API key ก่อน. เวลาเจนภาพวัดจาก client (dispatch → job terminal state) เพราะ{' '}
          <code>GET /v1/jobs/&#123;id&#125;</code> ยังไม่ส่ง timestamp กลับมาให้จากฝั่ง server.
        </footer>
      </div>
    </div>
  )
}
