# Imaginv4 API — Quick Reference

Base URL: `http://localhost:8000`

Auth (all endpoints except Health/Auth-Login/Register) — two options:
- `X-Session-Token: <token>` (get token from Login/Register response) — human/FE auth
- `Authorization: Bearer imgn_...` — machine-to-machine API key (see `POST /v1/agent/message` below), minted via `scripts/create_api_key.py`, not tied to any login

---

## POST /v1/auth/register
**Auth:** none
**Body:**
```json
{
  "email": "tester1111@example.com",
  "password": "correct-horse-battery-staple",
  "display_name": "tester1111"
}
```

## POST /v1/auth/login
**Auth:** none
**Body:**
```json
{
  "email": "tester1111@example.com",
  "password": "correct-horse-battery-staple"
}
```

## GET /v1/auth/me
**Auth:** `X-Session-Token`
**Body:** none

## POST /v1/auth/logout
**Auth:** `X-Session-Token`
**Body:** none

---

## POST /v1/conversations
**Auth:** `X-Session-Token`
**Body:**
```json
{ "title": "New conversation" }
```

## GET /v1/conversations/{conversation_id}/messages
**Auth:** `X-Session-Token`
**Body:** none

---

## POST /v1/conversations/{conversation_id}/smart-message
Main agentic endpoint — AI classifies intent (chat / general image / poster / infographic) and enqueues accordingly. No confirmation step; paid jobs enqueue immediately.

**Auth:** `X-Session-Token`
**Body:**
```json
{
  "text": "ทำโปสเตอร์ Open House มหาวิทยาลัยหอการค้าไทย วันที่ 20 สิงหาคม ที่หอประชุม",
  "client_message_id": "optional-string"
}
```
**Response `type` values:** `"chat"` (assistant_message set), `"image_job"` (job set — poll via GET /v1/jobs/{id})

---

## POST /v1/agent/message
Machine-to-machine entry point into the same routing pipeline as smart-message, for
external systems (e.g. a university chatbot workflow) that just forward raw text and
don't want to manage this system's own conversation_id.

**Auth:** `Authorization: Bearer imgn_...` (or `X-Session-Token`)
**Body:**
```json
{
  "external_conversation_id": "utcc-student-2142",
  "text": "ทำโปสเตอร์ Open House มหาวิทยาลัยหอการค้าไทย วันที่ 20 สิงหาคม",
  "client_message_id": "optional-string"
}
```
`external_conversation_id` is whatever the caller already uses to mean "this end
user/thread" — required, non-blank. The first message for a given
`(api key's user, external_conversation_id)` pair creates a new conversation; every
later message with the same id reuses it, keeping different real end users' history
separate even though they all authenticate as the same service account. Response shape
is identical to smart-message's (`type`: `"chat"` or `"image_job"`).

---

## GET /v1/jobs/{job_id}
**Auth:** `X-Session-Token`
**Body:** none

## GET /v1/jobs/{job_id}/asset
**Auth:** `X-Session-Token`
**Body:** none — returns image bytes once job state is `succeeded`

## POST /v1/jobs/{job_id}/cancel
**Auth:** `X-Session-Token`
**Body:** none

---

## POST /v1/generations
Manual generation (bypasses AI routing).

**Auth:** `X-Session-Token` + `Idempotency-Key: <any-unique-string>` header
**Body:**
```json
{
  "workflow_name": "image_basic",
  "workflow_version": "v1",
  "conversation_id": "optional-uuid",
  "inputs": {
    "prompt": "a cat sitting in a spaceship",
    "aspect_ratio": "1:1",
    "resolution": "1k"
  }
}
```
`workflow_name` is either `"image_basic"` (free, ComfyUI) or `"poster_infographic"` (paid, Gemini — inputs should include `"exact_text": [...]` and `"action_type": "poster"|"infographic"`).
