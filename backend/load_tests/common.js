// Shared helpers for the k6 scripts in this directory. Self-contained: only imports
// standard k6 modules (no npm deps), matching backend/app/api/v1 routes as of this
// commit:
//   POST /v1/auth/login          -> { session_token, expires_at }
//   POST /v1/generations         -> 202 { id, state, kind }  (Idempotency-Key header)
//   GET  /v1/jobs/{id}           -> { id, state, kind, current_attempt, error_code, result }
//   GET  /v1/jobs/{id}/events    -> text/event-stream (SSE) of state transitions
//   GET  /v1/health/live | /v1/health/ready
//
// Auth uses an opaque session token via the `X-Session-Token` header (see
// app/api/deps.py:get_current_user), not JWT/OAuth.

import http from 'k6/http';

export const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';

export function login(email, password) {
  const res = http.post(
    `${BASE_URL}/v1/auth/login`,
    JSON.stringify({ email, password }),
    { headers: { 'Content-Type': 'application/json' } },
  );
  if (res.status !== 200) {
    return null;
  }
  return res.json('session_token');
}

export function authHeaders(token) {
  return { 'Content-Type': 'application/json', 'X-Session-Token': token };
}

export function randomIdempotencyKey(prefix) {
  return `${prefix}-${Date.now()}-${Math.floor(Math.random() * 1e9)}`;
}

export function generationPayload() {
  return JSON.stringify({
    workflow_name: 'txt2img_basic',
    workflow_version: 'v1',
    inputs: { prompt: 'a friendly robot in a campus courtyard, watercolor style' },
  });
}
