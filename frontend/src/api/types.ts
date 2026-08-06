// Types mirror the Pydantic response/request models found in the backend, verified
// directly against source:
//   - backend/app/api/v1/auth.py            (LoginRequest, LoginResponse, MeResponse)
//   - backend/app/api/v1/conversations.py   (ConversationCreate/Out, MessageOut, MessagePage)
//   - backend/app/api/v1/generations.py     (GenerationCreate, GenerationOut)
//   - backend/app/api/v1/jobs.py            (JobOut)
//
// UPDATE: POST /v1/conversations/{id}/messages now exists on the backend (added after
// this frontend was first built) and persists messages for real. There is still no
// chat-completion / LLM endpoint, so the "assistant" reply to a plain text message is a
// client-side echo that we also persist via that same endpoint -- see ChatScreen.tsx.

export interface LoginRequest {
  email: string
  password: string
}

export interface RegisterRequest {
  email: string
  password: string
  display_name: string
}

export interface LoginResponse {
  session_token: string
  expires_at: string
}

export interface MeResponse {
  id: string
  email: string
  display_name: string
}

export interface ConversationCreate {
  title?: string | null
}

export interface ConversationOut {
  id: string
  title: string
  status: string
  created_at: string
  updated_at: string
}

export interface MessageOut {
  id: string
  role: 'user' | 'assistant' | 'system' | 'tool'
  sequence_no: number
  content: Record<string, unknown>
  status: string
  created_at: string
}

export interface MessagePage {
  items: MessageOut[]
  next_cursor: string | null
}

export interface MessageCreate {
  role?: 'user' | 'assistant' | 'system' | 'tool'
  content: Record<string, unknown>
  client_message_id?: string | null
}

export interface GenerationCreate {
  workflow_name: string
  workflow_version: string
  conversation_id?: string | null
  inputs: Record<string, unknown>
}

export interface GenerationOut {
  id: string
  state: string
  kind: string
}

export interface SmartMessageCreate {
  text: string
  client_message_id?: string | null
}

export interface PendingActionOut {
  id: string
  action_type: string
  billing_category: string
  normalized_prompt: string
  exact_text: string[]
  status: 'pending' | 'confirmed' | 'cancelled' | 'expired' | string
  expires_at: string
}

// Discriminated by `type`: "chat" carries assistant_message, "image_job" carries job,
// "confirmation_required" carries pending_action. Mirrors backend
// app/api/v1/chat_router.py:SmartMessageOut exactly (including the flat-not-a-union
// shape, kept consistent with GenerationOut/JobOut/MessageOut elsewhere in this file).
export interface SmartMessageOut {
  type: 'chat' | 'image_job' | 'confirmation_required' | string
  user_message: MessageOut
  assistant_message?: MessageOut | null
  job?: GenerationOut | null
  pending_action?: PendingActionOut | null
}

export interface JobOut {
  id: string
  state: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | string
  kind: string
  current_attempt: number
  error_code: string | null
  // The underlying adapter's own sanitized error (e.g. "gemini_error:ClientError",
  // "gemini_not_configured"). error_code alone is a generic retry-classification bucket
  // ("comfy_transient") shared by every backend, so this is what actually tells you
  // which backend handled the job and why it failed -- see backend/app/api/v1/jobs.py.
  error_detail: string | null
  result: { outputs?: { object_key: string; mime_type: string }[] } | Record<string, unknown> | null
}
