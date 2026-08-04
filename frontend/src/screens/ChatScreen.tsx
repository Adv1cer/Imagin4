import { useCallback, useEffect, useRef, useState } from 'react'
import { Composer } from '../components/Composer'
import { MessageBubble } from '../components/MessageBubble'
import { Toasts } from '../components/Toasts'
import { useToasts } from '../hooks/useToasts'
import { createConversation, createGeneration, getJob, listMessages } from '../api/endpoints'
import { ApiError } from '../api/client'
import type { MeResponse } from '../api/types'
import type { UiMessage } from '../types/chat'

function uuidv4(): string {
  if ('randomUUID' in crypto) return crypto.randomUUID()
  // Fallback for environments without crypto.randomUUID (shouldn't be needed in modern
  // browsers, but keeps this robust).
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0
    const v = c === 'x' ? r : (r & 0x3) | 0x8
    return v.toString(16)
  })
}

let seq = 0
function nextId(): string {
  seq += 1
  return `local-${Date.now()}-${seq}`
}

export function ChatScreen({ user, onLogout }: { user: MeResponse; onLogout: () => void }) {
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [messages, setMessages] = useState<UiMessage[]>([])
  const [sending, setSending] = useState(false)
  const [imageMode, setImageMode] = useState(false)
  const [banner, setBanner] = useState<string | null>(null)
  const { toasts, showToast, dismissToast } = useToasts()
  const scrollRef = useRef<HTMLDivElement>(null)
  const pollTimers = useRef<Record<string, ReturnType<typeof setTimeout>>>({})

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight })
  }, [messages])

  useEffect(() => {
    const timers = pollTimers.current
    return () => {
      Object.values(timers).forEach(clearTimeout)
    }
  }, [])

  async function ensureConversation(): Promise<string> {
    if (conversationId) return conversationId
    const conv = await createConversation({ title: 'New conversation' })
    setConversationId(conv.id)
    // Attempt to load any existing history for this conversation (cursor-paginated).
    // For a brand-new conversation this will simply come back empty.
    try {
      const page = await listMessages(conv.id)
      if (page.items.length > 0) {
        setMessages(
          page.items.map((m) => ({
            id: m.id,
            role: m.role === 'tool' ? 'assistant' : m.role,
            text: typeof m.content?.text === 'string' ? (m.content.text as string) : JSON.stringify(m.content),
            createdAt: m.created_at,
          })),
        )
      }
    } catch {
      // Non-fatal: history load failure shouldn't block sending a new message.
    }
    return conv.id
  }

  const pollJob = useCallback((jobId: string, messageId: string) => {
    const tick = async () => {
      try {
        const job = await getJob(jobId)
        setMessages((prev) =>
          prev.map((m) =>
            m.id === messageId
              ? {
                  ...m,
                  imageJob: {
                    jobId: job.id,
                    state: job.state as 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled',
                    errorCode: job.error_code,
                    objectKey:
                      (job.result as { outputs?: { object_key: string }[] } | null)?.outputs?.[0]
                        ?.object_key ?? null,
                  },
                }
              : m,
          ),
        )
        if (!['succeeded', 'failed', 'cancelled'].includes(job.state)) {
          pollTimers.current[jobId] = setTimeout(tick, 1500)
        } else {
          delete pollTimers.current[jobId]
        }
      } catch {
        showToast('Lost connection while checking image generation status.', 'error')
      }
    }
    pollTimers.current[jobId] = setTimeout(tick, 1500)
  }, [showToast])

  async function handleSend(text: string) {
    setBanner(null)
    const userMessage: UiMessage = {
      id: nextId(),
      role: 'user',
      text,
      createdAt: new Date().toISOString(),
    }
    setMessages((prev) => [...prev, userMessage])
    setSending(true)

    try {
      const convId = await ensureConversation()

      if (imageMode) {
        setImageMode(false)
        const assistantMsgId = nextId()
        setMessages((prev) => [
          ...prev,
          {
            id: assistantMsgId,
            role: 'assistant',
            text: `Generating image for: "${text}"`,
            createdAt: new Date().toISOString(),
            imageJob: { jobId: '', state: 'queued' },
          },
        ])
        try {
          const generation = await createGeneration(
            {
              workflow_name: 'txt2img_basic',
              workflow_version: 'v1',
              conversation_id: convId,
              inputs: { prompt: text },
            },
            uuidv4(),
          )
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantMsgId
                ? { ...m, imageJob: { jobId: generation.id, state: 'queued' } }
                : m,
            ),
          )
          pollJob(generation.id, assistantMsgId)
        } catch (err) {
          const msg =
            err instanceof ApiError ? err.message : 'Failed to start image generation.'
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantMsgId
                ? { ...m, text: msg, imageJob: { jobId: '', state: 'failed' } }
                : m,
            ),
          )
          showToast(msg, 'error')
        }
      } else {
        // NOTE (backend gap, worked around client-side): there is no chat-completion /
        // message-creation endpoint on the backend, so this is a local mock reply rather
        // than a real model response.
        setTimeout(() => {
          setMessages((prev) => [
            ...prev,
            {
              id: nextId(),
              role: 'assistant',
              text: "This is a mock reply — the backend doesn't yet expose a chat/message endpoint, only image generation is wired to the real API.",
              createdAt: new Date().toISOString(),
            },
          ])
        }, 400)
      }
    } catch (err) {
      const msg =
        err instanceof ApiError ? err.message : 'Something went wrong sending your message.'
      setBanner(msg)
      showToast(msg, 'error')
    } finally {
      setSending(false)
    }
  }

  const isEmpty = messages.length === 0

  return (
    <div className="flex h-screen flex-col bg-gray-50">
      <header className="flex items-center justify-between border-b border-gray-200 bg-white px-4 py-3">
        <div className="text-sm font-semibold text-gray-900">Imaginv4</div>
        <div className="flex items-center gap-3">
          <span className="text-sm text-gray-600">{user.display_name}</span>
          <button
            type="button"
            onClick={onLogout}
            className="rounded-full border border-gray-200 px-3 py-1 text-xs font-medium text-gray-600 hover:bg-gray-100"
          >
            Log out
          </button>
        </div>
      </header>

      {banner && (
        <div className="border-b border-red-100 bg-red-50 px-4 py-2 text-sm text-red-700">
          {banner}
        </div>
      )}

      <main ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-6">
        {isEmpty ? (
          <div className="flex h-full items-center justify-center">
            <h1 className="text-center text-2xl font-medium text-gray-800">
              Hello, {user.display_name}. What would you like to ask about?
            </h1>
          </div>
        ) : (
          <div className="mx-auto flex max-w-3xl flex-col gap-3">
            {messages.map((m) => (
              <MessageBubble key={m.id} message={m} />
            ))}
          </div>
        )}
      </main>

      <Composer
        onSend={handleSend}
        onToast={showToast}
        imageMode={imageMode}
        onEnterImageMode={() => setImageMode(true)}
        onExitImageMode={() => setImageMode(false)}
        disabled={sending}
      />

      <Toasts toasts={toasts} onDismiss={dismissToast} />
    </div>
  )
}
