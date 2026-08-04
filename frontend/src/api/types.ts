// Types mirror the Pydantic response/request models found in the backend, verified
// directly against source:
//   - backend/app/api/v1/auth.py            (LoginRequest, LoginResponse, MeResponse)
//   - backend/app/api/v1/conversations.py   (ConversationCreate/Out, MessageOut, MessagePage)
//   - backend/app/api/v1/generations.py     (GenerationCreate, GenerationOut)
//   - backend/app/api/v1/jobs.py            (JobOut)
//
// NOTE (frontend-side workaround, no backend code was changed): the backend has no
// endpoint to POST a new chat message (only GET .../messages exists) and no text-chat
// / LLM completion endpoint at all. Only image generation (POST /v1/generations) is a
// real, working backend capability. Plain text chat messages are therefore held in
// local React state only (never persisted) -- see src/hooks/useConversation.ts.

export interface LoginRequest {
  email: string
  password: string
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

export interface JobOut {
  id: string
  state: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | string
  kind: string
  current_attempt: number
  error_code: string | null
  result: { outputs?: { object_key: string; mime_type: string }[] } | Record<string, unknown> | null
}
