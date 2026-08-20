import type { ScenarioConfig, ScenarioMode } from '../types'
import { totalForMode } from '../scenarios'

interface Props {
  cfg: ScenarioConfig
  onChange: (cfg: ScenarioConfig) => void
  disabled: boolean
  virtualUserCount: number
}

const MODE_LABEL: Record<ScenarioMode, string> = {
  burst: 'Burst — ยิงพร้อมกันในช่วงเวลาสั้นๆ',
  'batched-rate': 'Batched rate — มาเป็นชุด ทุกๆ N นาที/วินาที',
  sequential: 'Sequential — ทีละคำขอ รอจบก่อนยิงต่อ',
  'concurrency-pool': 'Concurrency pool — คงระดับ concurrency ต่อเนื่อง',
}

const MODE_HELP: Record<ScenarioMode, string> = {
  burst: 'จำลอง "100 คนพร้อมกัน" เหมาะสำหรับเทส Redis admission gate (max_inflight) และ per-user rate limit',
  'batched-rate': 'จำลองผู้ใช้ทยอยเข้ามา เช่น 10-20 คนต่อนาที ต่อเนื่องหลายรอบ',
  sequential: 'ใช้เป็น baseline เวลาเจนภาพแบบไม่มี concurrency กวนเลย เทียบกับโหมดอื่น',
  'concurrency-pool': 'รักษาจำนวนงานพร้อมกันคงที่ต่อเนื่อง (soak test) ต่างจาก burst ที่เป็น spike ทันที',
}

const PRESETS: { label: string; cfg: Partial<ScenarioConfig> }[] = [
  { label: '100 พร้อมกัน (burst)', cfg: { mode: 'burst', totalRequests: 100, burstWindowMs: 100 } },
  {
    label: '10/นาที ต่อเนื่อง 5 รอบ',
    cfg: { mode: 'batched-rate', batchSize: 10, batchIntervalMs: 60_000, batchCount: 5, spreadWithinBatch: true },
  },
  {
    label: '20/นาที ต่อเนื่อง 5 รอบ',
    cfg: { mode: 'batched-rate', batchSize: 20, batchIntervalMs: 60_000, batchCount: 5, spreadWithinBatch: true },
  },
  { label: 'Sequential baseline (20 คำขอ)', cfg: { mode: 'sequential', totalRequests: 20 } },
  { label: 'Concurrency pool (10 workers x 50)', cfg: { mode: 'concurrency-pool', totalRequests: 50, poolConcurrency: 10 } },
  { label: 'Rate-limit probe (70 burst, >60/min ต่อ user)', cfg: { mode: 'burst', totalRequests: 70, burstWindowMs: 500 } },
]

export function ScenarioPanel({ cfg, onChange, disabled, virtualUserCount }: Props) {
  const patch = (p: Partial<ScenarioConfig>) => onChange({ ...cfg, ...p })
  const total = totalForMode(cfg)
  const rlWarning =
    cfg.mode !== 'sequential' && virtualUserCount > 0 && total / virtualUserCount > 60

  return (
    <div className="space-y-4 rounded-lg border border-gray-200 bg-white p-4">
      <h2 className="text-sm font-semibold text-gray-900">Scenario</h2>

      <div className="flex flex-wrap gap-2">
        {PRESETS.map((preset) => (
          <button
            key={preset.label}
            type="button"
            disabled={disabled}
            className="rounded-full border border-blue-200 bg-blue-50 px-2.5 py-1 text-xs text-blue-700 hover:bg-blue-100 disabled:opacity-50"
            onClick={() => onChange({ ...cfg, ...preset.cfg })}
          >
            {preset.label}
          </button>
        ))}
      </div>

      <div>
        <label className="block text-xs font-medium text-gray-600">Mode</label>
        <select
          className="mt-1 w-full rounded border border-gray-300 px-2 py-1.5 text-sm"
          value={cfg.mode}
          disabled={disabled}
          onChange={(e) => patch({ mode: e.target.value as ScenarioMode })}
        >
          {(Object.keys(MODE_LABEL) as ScenarioMode[]).map((m) => (
            <option key={m} value={m}>
              {MODE_LABEL[m]}
            </option>
          ))}
        </select>
        <p className="mt-1 text-xs text-gray-500">{MODE_HELP[cfg.mode]}</p>
      </div>

      {(cfg.mode === 'burst' || cfg.mode === 'sequential' || cfg.mode === 'concurrency-pool') && (
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="block text-xs font-medium text-gray-600">จำนวนคำขอทั้งหมด</label>
            <input
              type="number"
              min={1}
              className="mt-1 w-full rounded border border-gray-300 px-2 py-1.5 text-sm"
              value={cfg.totalRequests}
              disabled={disabled}
              onChange={(e) => patch({ totalRequests: Math.max(1, Number(e.target.value)) })}
            />
          </div>
          {cfg.mode === 'burst' && (
            <div>
              <label className="block text-xs font-medium text-gray-600">กระจายภายใน (ms)</label>
              <input
                type="number"
                min={0}
                className="mt-1 w-full rounded border border-gray-300 px-2 py-1.5 text-sm"
                value={cfg.burstWindowMs}
                disabled={disabled}
                onChange={(e) => patch({ burstWindowMs: Math.max(0, Number(e.target.value)) })}
              />
            </div>
          )}
          {cfg.mode === 'concurrency-pool' && (
            <div>
              <label className="block text-xs font-medium text-gray-600">Concurrency (workers)</label>
              <input
                type="number"
                min={1}
                className="mt-1 w-full rounded border border-gray-300 px-2 py-1.5 text-sm"
                value={cfg.poolConcurrency}
                disabled={disabled}
                onChange={(e) => patch({ poolConcurrency: Math.max(1, Number(e.target.value)) })}
              />
            </div>
          )}
        </div>
      )}

      {cfg.mode === 'batched-rate' && (
        <div className="grid grid-cols-3 gap-3">
          <div>
            <label className="block text-xs font-medium text-gray-600">ต่อชุด (batch size)</label>
            <input
              type="number"
              min={1}
              className="mt-1 w-full rounded border border-gray-300 px-2 py-1.5 text-sm"
              value={cfg.batchSize}
              disabled={disabled}
              onChange={(e) => patch({ batchSize: Math.max(1, Number(e.target.value)) })}
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-600">ทุกๆ (วินาที)</label>
            <input
              type="number"
              min={1}
              className="mt-1 w-full rounded border border-gray-300 px-2 py-1.5 text-sm"
              value={cfg.batchIntervalMs / 1000}
              disabled={disabled}
              onChange={(e) => patch({ batchIntervalMs: Math.max(1, Number(e.target.value)) * 1000 })}
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-600">จำนวนรอบ</label>
            <input
              type="number"
              min={1}
              className="mt-1 w-full rounded border border-gray-300 px-2 py-1.5 text-sm"
              value={cfg.batchCount}
              disabled={disabled}
              onChange={(e) => patch({ batchCount: Math.max(1, Number(e.target.value)) })}
            />
          </div>
          <label className="col-span-3 flex items-center gap-1.5 text-xs text-gray-700">
            <input
              type="checkbox"
              checked={cfg.spreadWithinBatch}
              disabled={disabled}
              onChange={(e) => patch({ spreadWithinBatch: e.target.checked })}
            />
            กระจายคำขอในแต่ละชุดให้เท่าๆ กันตลอดช่วงเวลา (ไม่ยิงพร้อมกันเป๊ะ)
          </label>
        </div>
      )}

      <div className="grid grid-cols-3 gap-3 border-t border-gray-100 pt-3">
        <div>
          <label className="block text-xs font-medium text-gray-600">Poll interval (ms)</label>
          <input
            type="number"
            min={200}
            className="mt-1 w-full rounded border border-gray-300 px-2 py-1.5 text-sm"
            value={cfg.pollIntervalMs}
            disabled={disabled}
            onChange={(e) => patch({ pollIntervalMs: Math.max(200, Number(e.target.value)) })}
          />
        </div>
        <div>
          <label className="block text-xs font-medium text-gray-600">Max wait (ms)</label>
          <input
            type="number"
            min={1000}
            className="mt-1 w-full rounded border border-gray-300 px-2 py-1.5 text-sm"
            value={cfg.maxWaitMs}
            disabled={disabled}
            onChange={(e) => patch({ maxWaitMs: Math.max(1000, Number(e.target.value)) })}
          />
        </div>
        <div>
          <label className="block text-xs font-medium text-gray-600">Submit timeout (ms)</label>
          <input
            type="number"
            min={1000}
            className="mt-1 w-full rounded border border-gray-300 px-2 py-1.5 text-sm"
            value={cfg.submitTimeoutMs}
            disabled={disabled}
            onChange={(e) => patch({ submitTimeoutMs: Math.max(1000, Number(e.target.value)) })}
          />
        </div>
      </div>

      <p className="text-xs text-gray-500">รวม {total} คำขอ ต่อ virtual user {virtualUserCount || 1} คน</p>

      {rlWarning && (
        <p className="rounded border border-amber-200 bg-amber-50 px-2 py-1.5 text-xs text-amber-800">
          นายท่านคะ ⚠️ จำนวนคำขอต่อ virtual user เกิน 60/นาที (rl_message_per_min) โอปอลคาดว่าจะเจอ 429
          บางส่วน — ถ้าตั้งใจเทส rate limit อยู่แล้วก็ปล่อยผ่านได้เลยค่ะ แต่ถ้าอยากวัด throughput ล้วนๆ
          ลองเพิ่มจำนวน API key (virtual user) ให้มากขึ้นนะคะ
        </p>
      )}
      {total > 150 && cfg.mode === 'burst' && (
        <p className="rounded border border-amber-200 bg-amber-50 px-2 py-1.5 text-xs text-amber-800">
          นายท่านคะ ⚠️ burst {total} คำขอ เกิน admission_max_inflight (150) ของระบบ คาดว่าจะเห็น 503 บางส่วน
          — เป็นพฤติกรรมที่ถูกต้องของ admission gate ค่ะ ไม่ใช่บั๊ก
        </p>
      )}
    </div>
  )
}
