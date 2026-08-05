// Local UI message shape. Both text turns (persisted via POST .../messages and
// .../assistant-reply) and image-generation turns (backed by POST /v1/generations +
// GET /v1/jobs/{id} polling) are real -- see src/screens/ChatScreen.tsx.

export interface ImageJobState {
  jobId: string
  state: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
  errorCode?: string | null
  objectKey?: string | null
}

export interface UiMessage {
  id: string
  role: 'user' | 'assistant' | 'system'
  text: string
  createdAt: string
  imageJob?: ImageJobState
}
