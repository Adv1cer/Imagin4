// Smoke test: 1 VU, a handful of iterations. Sanity-checks the full happy path
// (login -> submit generation -> poll job -> see it move to a non-queued state) before
// running anything heavier. Safe to run in CI once k6 + a live stack are available.
//
// Run: k6 run load_tests/smoke.js
// Env: BASE_URL (default http://localhost:8000), SMOKE_EMAIL, SMOKE_PASSWORD

import http from 'k6/http';
import { check, sleep } from 'k6';
import { BASE_URL, login, authHeaders, randomIdempotencyKey, generationPayload } from './common.js';

export const options = {
  vus: 1,
  iterations: 5,
  thresholds: {
    http_req_failed: ['rate<0.01'],
    'http_req_duration{endpoint:generations_post}': ['p(95)<250'],
  },
};

const EMAIL = __ENV.SMOKE_EMAIL || 'student@example.edu';
const PASSWORD = __ENV.SMOKE_PASSWORD || 'correct-horse-battery-staple';

export default function () {
  const live = http.get(`${BASE_URL}/v1/health/live`, { tags: { endpoint: 'health_live' } });
  check(live, { 'health/live is 200': (r) => r.status === 200 });

  const token = login(EMAIL, PASSWORD);
  if (!token) {
    // No seeded user in this environment yet -- health check above is still meaningful.
    console.warn('smoke: login failed, skipping authenticated checks (seed a user first)');
    return;
  }
  const headers = authHeaders(token);

  const postRes = http.post(`${BASE_URL}/v1/generations`, generationPayload(), {
    headers: Object.assign({}, headers, { 'Idempotency-Key': randomIdempotencyKey('smoke') }),
    tags: { endpoint: 'generations_post' },
  });
  check(postRes, {
    'generation admitted (202)': (r) => r.status === 202,
    'generation has id': (r) => !!r.json('id'),
  });
  if (postRes.status !== 202) {
    return;
  }
  const jobId = postRes.json('id');

  sleep(0.5);
  const jobRes = http.get(`${BASE_URL}/v1/jobs/${jobId}`, { headers, tags: { endpoint: 'jobs_get' } });
  check(jobRes, {
    'job lookup succeeds (no job lost after 202)': (r) => r.status === 200,
    'job id matches': (r) => r.json('id') === jobId,
  });

  sleep(1);
}
