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
  MessagePage,
} from './types'

export function login(payload: LoginRequest): Promise<LoginResponse> {
  return apiFetch<LoginResponse>('/v1/auth/login', { method: 'POST', body: payload })
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
