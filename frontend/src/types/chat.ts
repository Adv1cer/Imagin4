// Local-only chat message shape used by the UI. The backend has no endpoint to create
// chat messages or to run a text/LLM completion (only GET .../messages exists, and there
// is no chat-completion endpoint at all) -- see src/api/types.ts for the note on this
// backend gap. So plain text turns exist only in this local state, never sent to or
// fetched from the server. Image-generation turns are real: they carry a `job` that is
// backed by POST /v1/generations + GET /v1/jobs/{id} polling.

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
