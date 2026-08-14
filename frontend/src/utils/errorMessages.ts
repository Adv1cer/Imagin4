// Translates the backend's sanitized error codes (job.error_detail / job.error_code from
// GET /v1/jobs/{id}, and the `detail` field of a 502/503 ApiError from
// /assistant-reply, /smart-message, /agent/message) into short, human-readable text.
//
// WHY THIS EXISTS: the backend deliberately never sends raw exception text (see
// backend/app/adapters/gemini.py:_sanitized_error's docstring) -- only a small, stable
// vocabulary of codes like "gemini_overloaded" or "openrouter_rate_limited". Before this
// file existed, the UI just printed those codes verbatim ("Reason: gemini_overloaded"),
// which is accurate but reads like a bug report, not something a non-technical customer
// can act on. This maps each known code to a plain-language explanation -- most
// importantly, anything meaning "the external AI provider is temporarily overloaded"
// becomes "Google is experiencing high demand right now -- please try again in a
// moment", not something that sounds like our own system is broken.
//
// Keep this in sync with the sanitized codes the backend can actually produce:
//   - backend/app/adapters/gemini.py:_sanitized_error / GEMINI_OVERLOAD_ERROR_CODES
//   - backend/app/adapters/openrouter.py:_sanitized_error
//   - backend/app/adapters/routing_comfyui.py (gemini_not_configured / openrouter_not_configured)
//   - backend/app/domain/jobs/retry.py (RETRYABLE_ERROR_CODES / NON_RETRYABLE_ERROR_CODES,
//     the coarse error_code bucket -- only used here as a fallback when error_detail is
//     missing, since error_detail is almost always the more specific/useful one)

export interface ErrorDescription {
  message: string
  // Whether the underlying cause is expected to resolve on its own shortly (provider
  // overload/rate-limit) -- callers can use this to phrase a retry hint, show a retry
  // button sooner, etc. False means "retrying right now probably won't help" (bad
  // config, invalid input, unknown failure).
  retryableSoon: boolean
}

const KNOWN_CODES: Record<string, ErrorDescription> = {
  gemini_overloaded: {
    message:
      'Google is experiencing high demand right now for this model -- please try again in a moment. (ระบบ AI ของ Google มีผู้ใช้งานหนาแน่นชั่วคราว กรุณาลองใหม่อีกครั้งค่ะ)',
    retryableSoon: true,
  },
  gemini_rate_limited: {
    message:
      'The image/chat model has hit a rate or quota limit -- please try again shortly. (ระบบใช้งานเกินโควต้าชั่วคราว กรุณาลองใหม่อีกครั้งค่ะ)',
    retryableSoon: true,
  },
  gemini_not_configured: {
    message: "This feature isn't set up on the server yet (missing Gemini API key). Please contact support.",
    retryableSoon: false,
  },
  gemini_no_image_in_response: {
    message: 'Google returned a response with no image this time -- please try again.',
    retryableSoon: true,
  },
  openrouter_overloaded: {
    message:
      'The image model is experiencing high demand right now via OpenRouter -- please try again in a moment. (ผู้ให้บริการโมเดลมีผู้ใช้งานหนาแน่นชั่วคราว กรุณาลองใหม่อีกครั้งค่ะ)',
    retryableSoon: true,
  },
  openrouter_rate_limited: {
    message: 'The image model has hit a rate limit on OpenRouter -- please try again shortly.',
    retryableSoon: true,
  },
  openrouter_insufficient_credits: {
    message: "The OpenRouter account has run out of credits. Please contact support.",
    retryableSoon: false,
  },
  openrouter_auth_error: {
    message: "There's a configuration problem with the image provider. Please contact support.",
    retryableSoon: false,
  },
  openrouter_upstream_error: {
    message: 'The image provider had an unexpected error -- please try again in a moment.',
    retryableSoon: true,
  },
  openrouter_not_configured: {
    message: "This feature isn't set up on the server yet (missing OpenRouter API key). Please contact support.",
    retryableSoon: false,
  },
  openrouter_no_image_in_response: {
    message: 'The image provider returned a response with no image this time -- please try again.',
    retryableSoon: true,
  },
  empty_prompt: {
    message: 'No prompt text was provided for this generation.',
    retryableSoon: false,
  },
  unknown_prompt_id: {
    message: 'Lost track of this generation on the server -- please try again.',
    retryableSoon: false,
  },
  comfy_transient: {
    message: 'The image generator had a temporary hiccup -- please try again.',
    retryableSoon: true,
  },
  comfy_timeout: {
    message: 'The image generator took too long to respond -- please try again.',
    retryableSoon: true,
  },
  comfy_disconnect: {
    message: 'Lost connection to the image generator -- please try again.',
    retryableSoon: true,
  },
  worker_lease_expired: {
    message: 'The image generation worker took too long -- please try again.',
    retryableSoon: true,
  },
  worker_unreachable: {
    message: 'No image generation worker is currently reachable. Please contact support if this persists.',
    retryableSoon: true,
  },
  comfy_rejected: {
    message: 'The image generator rejected this request. Please contact support.',
    retryableSoon: false,
  },
  workflow_invalid: {
    message: 'This generation request is invalid. Please contact support.',
    retryableSoon: false,
  },
  quota_exceeded: {
    message: "You've hit your generation quota. Please try again later.",
    retryableSoon: false,
  },
  malformed_response: {
    message: 'The image generator returned an unexpected response. Please try again.',
    retryableSoon: true,
  },
  comfy_permanent: {
    message: 'The image generator could not complete this request. Please contact support.',
    retryableSoon: false,
  },
}

const DEFAULT_DESCRIPTION: ErrorDescription = {
  message: 'Something went wrong. Please try again in a moment.',
  retryableSoon: true,
}

/** `code` is normally job.error_detail (preferred, more specific) or job.error_code
 * (coarse fallback) from GET /v1/jobs/{id}, or the `detail` field of a 502/503
 * ApiError. Handles the "gemini_error:ClassName" / "openrouter_error:ClassName" generic
 * fallback shapes from each adapter's _sanitized_error too (see backend
 * app/adapters/gemini.py / app/adapters/openrouter.py) by prefix, since those carry an
 * unbounded suffix (the exception class name) that can't be a fixed map key. */
export function describeErrorCode(code: string | null | undefined): ErrorDescription {
  if (!code) return DEFAULT_DESCRIPTION
  const known = KNOWN_CODES[code]
  if (known) return known
  if (code.startsWith('gemini_error:') || code.startsWith('openrouter_error:')) {
    return {
      message: 'The AI provider had an unexpected error -- please try again in a moment.',
      retryableSoon: true,
    }
  }
  return DEFAULT_DESCRIPTION
}

/** Convenience for a job's paired (error_detail, error_code) fields -- prefers
 * error_detail (the specific, per-backend reason) and falls back to error_code (the
 * coarse retry-classification bucket) only if error_detail is missing. */
export function describeJobError(
  errorDetail: string | null | undefined,
  errorCode: string | null | undefined,
): ErrorDescription {
  return describeErrorCode(errorDetail || errorCode)
}
