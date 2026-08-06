import { apiFetch } from './client'
import type {
  ConversationCreate,
  ConversationOut,
  GenerationCreate,
  GenerationOut,
  JobOut,
  LoginRequest,
  LoginResponse,
  MeResponse,
  MessageCreate,
  MessageOut,
  MessagePage,
  PendingActionOut,
  RegisterRequest,
  SmartMessageCreate,
  SmartMessageOut,
} from './types'

export function login(payload: LoginRequest): Promise<LoginResponse> {
  return apiFetch<LoginResponse>('/v1/auth/login', { method: 'POST', body: payload })
}

export function register(payload: RegisterRequest): Promise<LoginResponse> {
  return apiFetch<LoginResponse>('/v1/auth/register', { method: 'POST', body: payload })
}

export function me(): Promise<MeResponse> {
  return apiFetch<MeResponse>('/v1/auth/me')
}

export function logout(): Promise<void> {
  return apiFetch<void>('/v1/auth/logout', { method: 'POST' })
}

export function createConversation(payload: ConversationCreate = {}): Promise<ConversationOut> {
  return apiFetch<ConversationOut>('/v1/conversations', { method: 'POST', body: payload })
}

export function listMessages(
  conversationId: string,
  cursor?: string | null,
): Promise<MessagePage> {
  const params = new URLSearchParams()
  if (cursor) params.set('cursor', cursor)
  const qs = params.toString()
  return apiFetch<MessagePage>(
    `/v1/conversations/${conversationId}/messages${qs ? `?${qs}` : ''}`,
  )
}

export function createGeneration(
  payload: GenerationCreate,
  idempotencyKey: string,
): Promise<GenerationOut> {
  return apiFetch<GenerationOut>('/v1/generations', {
    method: 'POST',
    body: payload,
    headers: { 'Idempotency-Key': idempotencyKey },
  })
}

export function getJob(jobId: string): Promise<JobOut> {
  return apiFetch<JobOut>(`/v1/jobs/${jobId}`)
}

export function createMessage(
  conversationId: string,
  payload: MessageCreate,
): Promise<MessageOut> {
  return apiFetch<MessageOut>(`/v1/conversations/${conversationId}/messages`, {
    method: 'POST',
    body: payload,
  })
}

export function createAssistantReply(conversationId: string): Promise<MessageOut> {
  return apiFetch<MessageOut>(`/v1/conversations/${conversationId}/assistant-reply`, {
    method: 'POST',
  })
}

// Agentic intent-routing endpoints (backend/app/api/v1/chat_router.py). smart-message
// persists the user's message itself (unlike createMessage, which callers use alongside
// the manual image-gen flow) and classifies it into chat / an immediate local image job /
// a pending paid action requiring explicit confirmation.
// Longer than apiFetch's 12s default: a POSTER/INFOGRAPHIC classification with missing
// fields can trigger up to 3 sequential Gemini calls on the backend (classify -> grounded
// research -> re-classify -- see app/api/v1/chat_router.py:_research_augment), whose
// worst-case budget is APP_GEMINI_REQUEST_TIMEOUT_S*2 + APP_GEMINI_RESEARCH_TIMEOUT_S
// (default 30+30+20=80s server-side). 100s gives comfortable headroom above that so the
// client doesn't abort a request the backend is still legitimately working on -- this was
// observed in practice as a spurious "server took too long to respond" even though the
// backend went on to finish and create the pending action successfully.
const SMART_MESSAGE_TIMEOUT_MS = 100_000

export function createSmartMessage(
  conversationId: string,
  payload: SmartMessageCreate,
): Promise<SmartMessageOut> {
  return apiFetch<SmartMessageOut>(`/v1/conversations/${conversationId}/smart-message`, {
    method: 'POST',
    body: payload,
    timeoutMs: SMART_MESSAGE_TIMEOUT_MS,
  })
}

export function confirmPendingAction(pendingActionId: string): Promise<SmartMessageOut> {
  return apiFetch<SmartMessageOut>(`/v1/pending-actions/${pendingActionId}/confirm`, {
    method: 'POST',
  })
}

export function cancelPendingAction(pendingActionId: string): Promise<PendingActionOut> {
  return apiFetch<PendingActionOut>(`/v1/pending-actions/${pendingActionId}/cancel`, {
    method: 'POST',
  })
}
