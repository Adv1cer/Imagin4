import { useEffect, useMemo, useRef, useState } from 'react'
import { getJobAssetBlob } from '../adminApi'
import { getPromptById } from '../promptBank'
import type { RequestResult } from '../types'
import { fmtMs } from '../stats'

interface Props {
  results: (RequestResult | undefined)[]
  baseUrl: string
  /** virtualUser id -> bearer token, so each job's image is fetched with the same
   *  credential that submitted it (a different virtual user's token might not own it). */
  tokensById: Record<string, string>
}

// Lazily fetches each generated image only once its card scrolls into view (via
// IntersectionObserver), rather than firing every request the instant the gallery mounts
// -- a 100-request burst test would otherwise hit GET /v1/jobs/{id}/asset 100 times at
// once just to render thumbnails. `<img src>` can't carry the Authorization header this
// backend requires (see adminApi.ts's getJobAssetBlob comment), so images are fetched as
// blobs and shown via object URLs instead.
function useInView<T extends HTMLElement>(rootMargin = '200px') {
  const ref = useRef<T | null>(null)
  const [inView, setInView] = useState(false)
  useEffect(() => {
    if (!ref.current || inView) return
    const el = ref.current
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting) {
          setInView(true)
          observer.disconnect()
        }
      },
      { rootMargin },
    )
    observer.observe(el)
    return () => observer.disconnect()
  }, [inView, rootMargin])
  return { ref, inView }
}

function useAuthedImageBlob(baseUrl: string, token: string, jobId: string | null, enabled: boolean) {
  const [url, setUrl] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    if (!enabled || !jobId || !token) return
    let cancelled = false
    let objectUrl: string | null = null
    getJobAssetBlob(baseUrl, token, jobId, 0)
      .then((blob) => {
        if (cancelled) return
        objectUrl = URL.createObjectURL(blob)
        setUrl(objectUrl)
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err))
      })
    return () => {
      cancelled = true
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [baseUrl, token, jobId, enabled])
  return { url, error }
}

function GalleryCard({
  result,
  baseUrl,
  token,
  onOpen,
}: {
  result: RequestResult
  baseUrl: string
  token: string
  onOpen: (url: string) => void
}) {
  const { ref, inView } = useInView<HTMLButtonElement>()
  const { url, error } = useAuthedImageBlob(baseUrl, token, result.jobId, inView)
  const prompt = getPromptById(result.promptId)

  return (
    <button
      ref={ref}
      type="button"
      onClick={() => url && onOpen(url)}
      disabled={!url}
      className="group flex flex-col overflow-hidden rounded-lg border border-gray-200 bg-white text-left hover:border-blue-300 disabled:cursor-default"
    >
      <div className="flex aspect-square items-center justify-center bg-gray-50">
        {url ? (
          <img src={url} alt={result.promptCategory} className="h-full w-full object-cover" />
        ) : error ? (
          <span className="px-2 text-center text-[11px] text-red-500">โหลดรูปไม่สำเร็จ</span>
        ) : (
          <div className="h-5 w-5 animate-spin rounded-full border-2 border-gray-300 border-t-gray-500" />
        )}
      </div>
      <div className="space-y-0.5 p-2 text-[11px]">
        <p className="truncate font-medium text-gray-800">
          #{result.seq} · {result.promptCategory}
        </p>
        <p className="text-gray-500">
          {result.workerName ?? 'worker: —'} · {fmtMs(result.totalGenerationMs)}
        </p>
        {prompt && (
          <p className="text-gray-400">
            {prompt.aspectRatio} · {prompt.resolution} · {prompt.modelProfile}
          </p>
        )}
      </div>
    </button>
  )
}

function Lightbox({ url, result, onClose }: { url: string; result: RequestResult; onClose: () => void }) {
  const prompt = getPromptById(result.promptId)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-6"
      onClick={onClose}
      role="presentation"
    >
      <div
        className="flex max-h-full max-w-4xl gap-4 overflow-hidden rounded-lg bg-white"
        onClick={(e) => e.stopPropagation()}
      >
        <img src={url} alt={result.promptCategory} className="max-h-[85vh] max-w-[60vw] object-contain" />
        <div className="w-72 space-y-2 overflow-y-auto p-4 text-xs">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-semibold text-gray-900">#{result.seq}</h3>
            <button type="button" onClick={onClose} className="text-gray-400 hover:text-gray-700">
              ✕
            </button>
          </div>
          <Field label="prompt id" value={result.promptId} />
          <Field label="category" value={result.promptCategory} />
          <Field label="virtual user" value={result.virtualUser} />
          <Field label="worker" value={result.workerName ?? 'ไม่ทราบ'} />
          <Field label="job id" value={result.jobId ?? '—'} mono />
          <Field label="submit latency" value={fmtMs(result.submitLatencyMs)} />
          <Field label="เวลาเจนทั้งหมด" value={fmtMs(result.totalGenerationMs)} />
          <Field label="poll count" value={String(result.pollCount)} />
          {prompt && (
            <>
              <Field label="aspect_ratio" value={prompt.aspectRatio} />
              <Field label="resolution" value={prompt.resolution} />
              <Field label="model_profile" value={prompt.modelProfile} />
              <div>
                <p className="mb-0.5 text-gray-400">prompt text</p>
                <p className="text-gray-700">{prompt.text}</p>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

function Field({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex justify-between gap-2">
      <span className="text-gray-400">{label}</span>
      <span className={`text-right text-gray-800 ${mono ? 'font-mono' : ''}`}>{value}</span>
    </div>
  )
}

export function Gallery({ results, baseUrl, tokensById }: Props) {
  const [lightbox, setLightbox] = useState<{ url: string; result: RequestResult } | null>(null)

  const withImages = useMemo(
    () =>
      results
        .filter((r): r is RequestResult => r !== undefined)
        .filter((r) => r.outcome === 'succeeded' && r.outputCount > 0)
        .sort((a, b) => a.seq - b.seq),
    [results],
  )

  if (withImages.length === 0) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-6 text-center text-sm text-gray-400">
        ยังไม่มีภาพที่เจนสำเร็จให้แสดงค่ะ (การ์ดจะขึ้นเองเมื่อมีคำขอที่ succeeded)
      </div>
    )
  }

  return (
    <div>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-6">
        {withImages.map((r) => (
          <GalleryCard
            key={r.seq}
            result={r}
            baseUrl={baseUrl}
            token={tokensById[r.virtualUser] ?? ''}
            onOpen={(url) => setLightbox({ url, result: r })}
          />
        ))}
      </div>
      {lightbox && <Lightbox url={lightbox.url} result={lightbox.result} onClose={() => setLightbox(null)} />}
    </div>
  )
}
