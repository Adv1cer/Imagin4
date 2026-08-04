import type { UiMessage } from '../types/chat'

function ImageJobStatus({ job }: { job: NonNullable<UiMessage['imageJob']> }) {
  if (job.state === 'queued' || job.state === 'running') {
    return (
      <div className="mt-2 flex items-center gap-2 text-xs text-gray-500">
        <span className="h-3 w-3 animate-spin rounded-full border-2 border-gray-300 border-t-gray-600" />
        Generating image… ({job.state}, job {job.jobId.slice(0, 8)})
      </div>
    )
  }
  if (job.state === 'succeeded') {
    return (
      <div className="mt-2 rounded-lg bg-emerald-50 px-3 py-2 text-xs text-emerald-800">
        Image ready. Backend asset key:{' '}
        <code className="rounded bg-emerald-100 px-1 py-0.5">
          {job.objectKey ?? 'unknown'}
        </code>
        <div className="mt-1 text-[11px] text-emerald-700">
          Note: the backend does not expose a signed-URL/asset endpoint yet, so the raw
          storage key is shown instead of a preview image.
        </div>
      </div>
    )
  }
  if (job.state === 'failed' || job.state === 'cancelled') {
    return (
      <div className="mt-2 rounded-lg bg-red-50 px-3 py-2 text-xs text-red-700">
        Image generation {job.state}
        {job.errorCode ? ` (${job.errorCode})` : ''}.
      </div>
    )
  }
  return null
}

export function MessageBubble({ message }: { message: UiMessage }) {
  const isUser = message.role === 'user'
  return (
    <div className={`flex w-full ${isUser ? 'justify-end' : 'justify-start'}`}>
      <div
        className={`max-w-[75%] rounded-2xl px-4 py-2.5 text-sm leading-relaxed ${
          isUser
            ? 'bg-gray-900 text-white'
            : message.role === 'system'
              ? 'bg-amber-50 text-amber-900'
              : 'bg-gray-100 text-gray-900'
        }`}
      >
        <div className="whitespace-pre-wrap">{message.text}</div>
        {message.imageJob && <ImageJobStatus job={message.imageJob} />}
      </div>
    </div>
  )
}
