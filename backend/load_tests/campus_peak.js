// campus_peak.js -- WARNING: DO NOT RUN IN CI. DO NOT RUN AUTOMATICALLY.
//
// This scenario ramps toward 1000 concurrent VUs, each holding a long-lived SSE
// connection to GET /v1/jobs/{id}/events against the mock ComfyUI backend, to model a
// worst-case "everyone refreshes right before a deadline" campus peak. It is expensive
// (memory, file descriptors, network) and is meant to be run deliberately by an
// operator against a disposable environment, not as part of any automated pipeline.
//
// Guardrails:
//  - Not referenced by any CI workflow in this repo.
//  - Requires an explicit opt-in env var (RUN_CAMPUS_PEAK=1) or it exits immediately.
//  - Only wired into docker-compose under the 'load-test' profile (never started by
//    `docker compose up`); see backend/docker-compose.yml.
//
// Run (deliberately, against a disposable environment):
//   RUN_CAMPUS_PEAK=1 k6 run load_tests/campus_peak.js
//
// Thresholds mirror baseline.js (see that file for rationale) plus SSE-specific ones.

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend, Rate } from 'k6/metrics';
import { BASE_URL, login, authHeaders, randomIdempotencyKey, generationPayload } from './common.js';

const dispatchLag = new Trend('dispatch_lag_ms', true);
const jobLost = new Rate('job_lost_rate');
const sseFirstEventLag = new Trend('sse_first_event_lag_ms', true);

export const options = __ENV.RUN_CAMPUS_PEAK
  ? {
      scenarios: {
        campus_peak: {
          executor: 'ramping-vus',
          startVUs: 0,
          stages: [
            { duration: '2m', target: 200 },
            { duration: '3m', target: 1000 }, // deliberate worst-case ramp toward 1000 VUs
            { duration: '5m', target: 1000 }, // hold at peak
            { duration: '2m', target: 0 },
          ],
          gracefulRampDown: '30s',
        },
      },
      thresholds: {
        http_req_failed: ['rate<0.01'],
        'http_req_duration{endpoint:generations_post}': ['p(95)<250'],
        dispatch_lag_ms: ['p(95)<2000'],
        job_lost_rate: ['rate==0'],
        sse_first_event_lag_ms: ['p(95)<2000'],
      },
    }
  : { vus: 1, iterations: 1 }; // opt-in guard below turns this into a no-op

const EMAIL = __ENV.CAMPUS_PEAK_EMAIL || 'student@example.edu';
const PASSWORD = __ENV.CAMPUS_PEAK_PASSWORD || 'correct-horse-battery-staple';

export default function () {
  if (!__ENV.RUN_CAMPUS_PEAK) {
    console.error(
      'campus_peak.js: refusing to run without RUN_CAMPUS_PEAK=1 -- this is a deliberate, ' +
        'expensive, manually-triggered scenario. See the header comment in this file.',
    );
    return;
  }

  const token = login(EMAIL, PASSWORD);
  if (!token) {
    console.warn('campus_peak: login failed; seed a test user before running this scenario');
    sleep(1);
    return;
  }
  const headers = authHeaders(token);

  const submittedAt = Date.now();
  const postRes = http.post(`${BASE_URL}/v1/generations`, generationPayload(), {
    headers: Object.assign({}, headers, { 'Idempotency-Key': randomIdempotencyKey('peak') }),
    tags: { endpoint: 'generations_post' },
  });
  const admitted = check(postRes, { 'generation admitted (202)': (r) => r.status === 202 });
  if (!admitted) {
    jobLost.add(1);
    sleep(1);
    return;
  }
  const jobId = postRes.json('id');

  // Long-lived SSE connection: GET /v1/jobs/{id}/events streams `event: state` frames
  // until a terminal state. k6's http module treats this as a streaming response; we
  // measure time-to-first-byte as a proxy for "first event visible".
  const sseRes = http.get(`${BASE_URL}/v1/jobs/${jobId}/events`, {
    headers,
    tags: { endpoint: 'jobs_events_sse' },
    timeout: '30s',
  });
  const gotEvent = check(sseRes, { 'sse stream responded': (r) => r.status === 200 });
  if (gotEvent) {
    sseFirstEventLag.add(Date.now() - submittedAt);
    dispatchLag.add(Date.now() - submittedAt);
  }
  jobLost.add(gotEvent ? 0 : 1);

  sleep(1);
}
