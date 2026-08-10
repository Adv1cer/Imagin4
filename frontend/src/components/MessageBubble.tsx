import { API_BASE_URL } from '../api/client'
import { PendingActionCard } from './PendingActionCard'
import { ThinkingIndicator } from './ThinkingIndicator'
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
    // Streamed via GET /v1/jobs/{id}/asset (ownership-checked on the backend); the
    // browser sends the session cookie automatically for a same-origin/same-site
    // <img> request the same way it does for fetch(), since credentials aren't
    // opt-in for image loads the way they are for CORS fetch/XHR.
    return (
      <div className="mt-2">
        <img
          src={`${API_BASE_URL}/v1/jobs/${job.jobId}/asset`}
          alt="Generated"
          className="max-h-80 max-w-full rounded-lg border border-gray-200 object-contain"
        />
      </div>
    )
  }
  if (job.state === 'failed' || job.state === 'cancelled') {
    return (
      <div className="mt-2 rounded-lg bg-red-50 px-3 py-2 text-xs text-red-700">
        <div>
          Image generation {job.state}
          {job.errorCode ? ` (${job.errorCode})` : ''}.
        </div>
        {job.errorDetail && (
          <div className="mt-0.5 text-red-500">Reason: {job.errorDetail}</div>
        )}
      </div>
    )
  }
  return null
}

export function MessageBubble({
  message,
  onConfirmPendingAction,
  onCancelPendingAction,
}: {
  message: UiMessage
  onConfirmPendingAction?: (messageId: string, pendingActionId: string) => void
  onCancelPendingAction?: (messageId: string, pendingActionId: string) => void
}) {
  const isUser = message.role === 'user'

  // Thinking placeholder: no bubble chrome at all, just the rotating status text +
  // animated dots, left-aligned like a normal assistant turn would be.
  if (message.thinking) {
    return (
      <div className="flex w-full justify-start">
        <div className="max-w-[75%] px-1">
          <ThinkingIndicator />
        </div>
      </div>
    )
  }

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
        {message.pendingAction && !message.imageJob && (
          <PendingActionCard
            pendingAction={message.pendingAction}
            onConfirm={() => onConfirmPendingAction?.(message.id, message.pendingAction!.id)}
            onCancel={() => onCancelPendingAction?.(message.id, message.pendingAction!.id)}
          />
        )}
      </div>
    </div>
  )
}
