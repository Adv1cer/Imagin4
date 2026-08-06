import { useCallback, useEffect, useRef, useState } from 'react'
import { Composer } from '../components/Composer'
import { MessageBubble } from '../components/MessageBubble'
import { Toasts } from '../components/Toasts'
import { useToasts } from '../hooks/useToasts'
import {
  cancelPendingAction,
  confirmPendingAction,
  createConversation,
  createGeneration,
  createMessage,
  createSmartMessage,
  getJob,
  listMessages,
} from '../api/endpoints'
import { ApiError } from '../api/client'
import type { MeResponse } from '../api/types'
import type { PendingActionState, UiMessage } from '../types/chat'
import { DEFAULT_IMAGE_GEN_CONFIG, workflowNameFor } from '../types/imageGen'
import type { ImageGenConfig } from '../types/imageGen'

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
  const [imageGenConfig, setImageGenConfig] = useState<ImageGenConfig>(DEFAULT_IMAGE_GEN_CONFIG)
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
                    errorDetail: job.error_detail,
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

  const handleConfirmPendingAction = useCallback(
    async (messageId: string, pendingActionId: string) => {
      setMessages((prev) =>
        prev.map((m) =>
          m.id === messageId && m.pendingAction
            ? { ...m, pendingAction: { ...m.pendingAction, busy: true, errorMessage: null } }
            : m,
        ),
      )
      try {
        const res = await confirmPendingAction(pendingActionId)
        if (res.type === 'image_job' && res.job) {
          const job = res.job
          setMessages((prev) =>
            prev.map((m) =>
              m.id === messageId
                ? {
                    ...m,
                    text: 'กำลังสร้างภาพ…',
                    pendingAction: m.pendingAction
                      ? { ...m.pendingAction, status: 'confirmed', busy: false }
                      : m.pendingAction,
                    imageJob: { jobId: job.id, state: 'queued' },
                  }
                : m,
            ),
          )
          pollJob(job.id, messageId)
        } else {
          setMessages((prev) =>
            prev.map((m) =>
              m.id === messageId && m.pendingAction
                ? {
                    ...m,
                    pendingAction: {
                      ...m.pendingAction,
                      busy: false,
                      errorMessage: 'Unexpected response from server.',
                    },
                  }
                : m,
            ),
          )
        }
      } catch (err) {
        const msg =
          err instanceof ApiError ? err.message : 'Failed to confirm — please try again.'
        setMessages((prev) =>
          prev.map((m) =>
            m.id === messageId && m.pendingAction
              ? { ...m, pendingAction: { ...m.pendingAction, busy: false, errorMessage: msg } }
              : m,
          ),
        )
        showToast(msg, 'error')
      }
    },
    [pollJob, showToast],
  )

  const handleCancelPendingAction = useCallback(
    async (messageId: string, pendingActionId: string) => {
      setMessages((prev) =>
        prev.map((m) =>
          m.id === messageId && m.pendingAction
            ? { ...m, pendingAction: { ...m.pendingAction, busy: true, errorMessage: null } }
            : m,
        ),
      )
      try {
        const pa = await cancelPendingAction(pendingActionId)
        setMessages((prev) =>
          prev.map((m) =>
            m.id === messageId && m.pendingAction
              ? {
                  ...m,
                  pendingAction: {
                    ...m.pendingAction,
                    status: pa.status as PendingActionState['status'],
                    busy: false,
                  },
                }
              : m,
          ),
        )
      } catch (err) {
        const msg = err instanceof ApiError ? err.message : 'Failed to cancel — please try again.'
        setMessages((prev) =>
          prev.map((m) =>
            m.id === messageId && m.pendingAction
              ? { ...m, pendingAction: { ...m.pendingAction, busy: false, errorMessage: msg } }
              : m,
          ),
        )
        showToast(msg, 'error')
      }
    },
    [showToast],
  )

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
        // Manual "Tools > Image generation" flow: persist the user's message for real
        // and WAIT for it to land before doing anything else (POST /v1/generations
        // doesn't touch chat history, but keeping this ordering avoids surprises if the
        // conversation is reloaded mid-flight). Still non-fatal on failure -- the
        // message still shows locally, it just won't survive a reload.
        try {
          await createMessage(convId, {
            role: 'user',
            content: { text },
            client_message_id: userMessage.id,
          })
        } catch {
          showToast('Message sent, but failed to save to history.', 'error')
        }

        setImageMode(false)
        const config = imageGenConfig
        const workflowName = workflowNameFor(config.kind)
        // config.variations spawns that many independent generation jobs (each with its
        // own idempotency key), one bubble per job -- Leonardo-style "batch of N".
        const count = config.variations
        const jobEntries = Array.from({ length: count }, () => ({ msgId: nextId() }))

        setMessages((prev) => [
          ...prev,
          ...jobEntries.map(({ msgId }) => ({
            id: msgId,
            role: 'assistant' as const,
            text:
              count > 1
                ? `Generating image ${jobEntries.findIndex((e) => e.msgId === msgId) + 1}/${count} for: "${text}"`
                : `Generating image for: "${text}"`,
            createdAt: new Date().toISOString(),
            imageJob: { jobId: '', state: 'queued' as const },
          })),
        ])

        await Promise.all(
          jobEntries.map(async ({ msgId }) => {
            try {
              const generation = await createGeneration(
                {
                  workflow_name: workflowName,
                  workflow_version: 'v1',
                  conversation_id: convId,
                  inputs: {
                    prompt: text,
                    aspect_ratio: config.aspectRatio,
                    resolution: config.resolution,
                    prompt_enhancer: config.promptEnhancer,
                  },
                },
                uuidv4(),
              )
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === msgId
                    ? { ...m, imageJob: { jobId: generation.id, state: 'queued' } }
                    : m,
                ),
              )
              pollJob(generation.id, msgId)
            } catch (err) {
              const msg =
                err instanceof ApiError ? err.message : 'Failed to start image generation.'
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === msgId
                    ? { ...m, text: msg, imageJob: { jobId: '', state: 'failed' } }
                    : m,
                ),
              )
              showToast(msg, 'error')
            }
          }),
        )
      } else {
        // Agentic routing (backend/app/api/v1/chat_router.py): the backend persists the
        // user's message itself, classifies intent via the same Gemini model, and
        // returns exactly one of a chat reply, an immediately-enqueued local image job
        // (GENERAL_IMAGE), or a paid PendingAction awaiting explicit confirmation
        // (POSTER/INFOGRAPHIC). We must NOT also call createMessage() here -- the
        // backend already persisted it (that would double-save it).
        const pendingId = nextId()
        setMessages((prev) => [
          ...prev,
          { id: pendingId, role: 'assistant', text: '…', createdAt: new Date().toISOString() },
        ])
        try {
          const res = await createSmartMessage(convId, {
            text,
            client_message_id: userMessage.id,
          })
          if (res.type === 'chat') {
            const replyText =
              res.assistant_message && typeof res.assistant_message.content?.text === 'string'
                ? (res.assistant_message.content.text as string)
                : '(empty response)'
            const replyId = res.assistant_message?.id ?? pendingId
            setMessages((prev) =>
              prev.map((m) =>
                m.id === pendingId ? { ...m, id: replyId, text: replyText } : m,
              ),
            )
          } else if (res.type === 'image_job' && res.job) {
            const job = res.job
            setMessages((prev) =>
              prev.map((m) =>
                m.id === pendingId
                  ? {
                      ...m,
                      text: `Generating image for: "${text}"`,
                      imageJob: { jobId: job.id, state: 'queued' },
                    }
                  : m,
              ),
            )
            pollJob(job.id, pendingId)
          } else if (res.type === 'confirmation_required' && res.pending_action) {
            const pa = res.pending_action
            setMessages((prev) =>
              prev.map((m) =>
                m.id === pendingId
                  ? {
                      ...m,
                      text:
                        pa.action_type === 'poster'
                          ? 'ต้องการยืนยันก่อนสร้างโปสเตอร์นี้ค่ะ (มีค่าใช้จ่าย)'
                          : 'ต้องการยืนยันก่อนสร้างอินโฟกราฟิกนี้ค่ะ (มีค่าใช้จ่าย)',
                      pendingAction: {
                        id: pa.id,
                        actionType: pa.action_type,
                        billingCategory: pa.billing_category,
                        normalizedPrompt: pa.normalized_prompt,
                        exactText: pa.exact_text,
                        status: pa.status,
                        expiresAt: pa.expires_at,
                      },
                    }
                  : m,
              ),
            )
          } else {
            setMessages((prev) =>
              prev.map((m) =>
                m.id === pendingId ? { ...m, text: 'Unexpected response from server.' } : m,
              ),
            )
          }
        } catch (err) {
          const msg =
            err instanceof ApiError && err.status === 503
              ? "Chat isn't configured on the backend yet (no Gemini API key set)."
              : err instanceof ApiError
                ? err.message
                : 'Failed to get a reply.'
          setMessages((prev) =>
            prev.map((m) => (m.id === pendingId ? { ...m, text: msg } : m)),
          )
          showToast(msg, 'error')
        }
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
              <MessageBubble
                key={m.id}
                message={m}
                onConfirmPendingAction={handleConfirmPendingAction}
                onCancelPendingAction={handleCancelPendingAction}
              />
            ))}
          </div>
        )}
      </main>

      <Composer
        onSend={handleSend}
        onToast={showToast}
        imageMode={imageMode}
        imageGenConfig={imageGenConfig}
        onEnterImageMode={() => setImageMode(true)}
        onExitImageMode={() => setImageMode(false)}
        onImageGenConfigChange={setImageGenConfig}
        disabled={sending}
      />

      <Toasts toasts={toasts} onDismiss={dismissToast} />
    </div>
  )
}
