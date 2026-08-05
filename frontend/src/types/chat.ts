// Local UI message shape. Both text turns (persisted via POST .../messages and
// .../assistant-reply) and image-generation turns (backed by POST /v1/generations +
// GET /v1/jobs/{id} polling) are real -- see src/screens/ChatScreen.tsx.

export interface ImageJobState {
  jobId: string
  state: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
  errorCode?: string | null
  // The actual backend-specific failure reason (e.g. "gemini_not_configured",
  // "gemini_error:ClientError") -- errorCode alone is a generic bucket shared by every
  // backend and won't tell you whether ComfyUI or Gemini handled the job. See
  // backend/app/api/v1/jobs.py:JobOut.error_detail.
  errorDetail?: string | null
  objectKey?: string | null
}

export interface UiMessage {
  id: string
  role: 'user' | 'assistant' | 'system'
  text: string
  createdAt: string
  imageJob?: ImageJobState
}
