import { getModelProfiles, type ModelProfilesResponse } from '../adminApi'
import type { RequestBuilderOptions } from '../scenarios'

interface Props {
  baseUrl: string
  onBaseUrlChange: (v: string) => void
  tokensRaw: string
  onTokensRawChange: (v: string) => void
  virtualUserCount: number
  reqOpts: RequestBuilderOptions
  onReqOptsChange: (opts: RequestBuilderOptions) => void
  disabled: boolean
  profiles: ModelProfilesResponse | null
  onProfilesLoaded: (p: ModelProfilesResponse | null, error?: string) => void
}

export function ConfigPanel({
  baseUrl,
  onBaseUrlChange,
  tokensRaw,
  onTokensRawChange,
  virtualUserCount,
  reqOpts,
  onReqOptsChange,
  disabled,
  profiles,
  onProfilesLoaded,
}: Props) {
  const patch = (p: Partial<RequestBuilderOptions>) => onReqOptsChange({ ...reqOpts, ...p })

  const loadProfiles = async () => {
    const token = tokensRaw.split(/[\n,]+/).map((t) => t.trim()).find(Boolean)
    if (!token) {
      onProfilesLoaded(null, 'ใส่ API key อย่างน้อย 1 ตัวก่อนค่ะ')
      return
    }
    try {
      const { data } = await getModelProfiles(baseUrl, token)
      onProfilesLoaded(data)
    } catch (err) {
      onProfilesLoaded(null, err instanceof Error ? err.message : String(err))
    }
  }

  return (
    <div className="space-y-4 rounded-lg border border-gray-200 bg-white p-4">
      <h2 className="text-sm font-semibold text-gray-900">Connection</h2>

      <div>
        <label className="block text-xs font-medium text-gray-600">Backend base URL</label>
        <input
          className="mt-1 w-full rounded border border-gray-300 px-2 py-1.5 text-sm font-mono"
          value={baseUrl}
          disabled={disabled}
          onChange={(e) => onBaseUrlChange(e.target.value)}
          placeholder="http://10.7.2.63:8000"
        />
      </div>

      <div>
        <label className="block text-xs font-medium text-gray-600">
          Bearer API key(s) — หนึ่งบรรทัดต่อหนึ่ง virtual user (imgn_...)
        </label>
        <textarea
          className="mt-1 h-20 w-full rounded border border-gray-300 px-2 py-1.5 text-sm font-mono"
          value={tokensRaw}
          disabled={disabled}
          onChange={(e) => onTokensRawChange(e.target.value)}
          placeholder={'imgn_xxxxxxxxxxxxxxxx\nimgn_yyyyyyyyyyyyyyyy (optional, ใส่หลายตัวเพื่อแยก rate limit ต่อ user)'}
        />
        <p className="mt-1 text-xs text-gray-500">
          รู้จัก {virtualUserCount} virtual user{virtualUserCount === 1 ? '' : 's'} — คีย์แต่ละบรรทัดเปิดจาก{' '}
          <code>python -m scripts.create_api_key</code> ในฝั่ง backend
        </p>
      </div>

      <div className="flex items-center gap-2">
        <button
          type="button"
          className="rounded border border-gray-300 px-2 py-1 text-xs text-gray-700 hover:bg-gray-50 disabled:opacity-50"
          disabled={disabled}
          onClick={loadProfiles}
        >
          Load model profiles
        </button>
        {profiles && (
          <span className="text-xs text-gray-500">
            profiles: {profiles.profiles.map((p) => p.key).join(', ')} · steps{' '}
            {profiles.override_range.min_steps}-{profiles.override_range.max_steps} · cfg{' '}
            {profiles.override_range.min_cfg_scale}-{profiles.override_range.max_cfg_scale}
          </span>
        )}
      </div>

      <h2 className="pt-2 text-sm font-semibold text-gray-900">Request overrides (ทางเลือก)</h2>

      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="block text-xs font-medium text-gray-600">model_profile override</label>
          <input
            className="mt-1 w-full rounded border border-gray-300 px-2 py-1.5 text-sm"
            value={reqOpts.modelProfileOverride}
            disabled={disabled}
            onChange={(e) => patch({ modelProfileOverride: e.target.value })}
            placeholder="ว่าง = ใช้ค่าของแต่ละ prompt"
          />
        </div>
        <div>
          <label className="block text-xs font-medium text-gray-600">steps override</label>
          <input
            type="number"
            className="mt-1 w-full rounded border border-gray-300 px-2 py-1.5 text-sm"
            value={reqOpts.stepsOverride ?? ''}
            disabled={disabled}
            onChange={(e) => patch({ stepsOverride: e.target.value === '' ? null : Number(e.target.value) })}
            placeholder="ว่าง = ไม่ override"
          />
        </div>
        <div>
          <label className="block text-xs font-medium text-gray-600">cfg_scale override</label>
          <input
            type="number"
            step="0.1"
            className="mt-1 w-full rounded border border-gray-300 px-2 py-1.5 text-sm"
            value={reqOpts.cfgScaleOverride ?? ''}
            disabled={disabled}
            onChange={(e) => patch({ cfgScaleOverride: e.target.value === '' ? null : Number(e.target.value) })}
            placeholder="ว่าง = ไม่ override"
          />
        </div>
        <div>
          <label className="block text-xs font-medium text-gray-600">negative_prompt override</label>
          <input
            className="mt-1 w-full rounded border border-gray-300 px-2 py-1.5 text-sm"
            value={reqOpts.negativePromptOverride}
            disabled={disabled}
            onChange={(e) => patch({ negativePromptOverride: e.target.value })}
            placeholder="ว่าง = ใช้ค่าของแต่ละ prompt"
          />
        </div>
      </div>

      <div className="flex flex-wrap gap-4 pt-1">
        <label className="flex items-center gap-1.5 text-xs text-gray-700">
          <input
            type="checkbox"
            checked={reqOpts.skipPromptDesign}
            disabled={disabled}
            onChange={(e) => patch({ skipPromptDesign: e.target.checked })}
          />
          skip_prompt_design
        </label>
        <label className="flex items-center gap-1.5 text-xs text-gray-700">
          <input
            type="checkbox"
            checked={reqOpts.assumeImage}
            disabled={disabled}
            onChange={(e) => patch({ assumeImage: e.target.checked })}
          />
          assume_image
        </label>
        <label className="flex items-center gap-1.5 text-xs text-gray-700">
          <input
            type="checkbox"
            checked={reqOpts.useExactText}
            disabled={disabled}
            onChange={(e) => patch({ useExactText: e.target.checked })}
          />
          ส่ง exact_text (เฉพาะ prompt ที่มี)
        </label>
      </div>
    </div>
  )
}
