// Soak test: moderate, sustained load over a long duration to catch slow leaks/decay
// (connection pool exhaustion, unbounded queue growth, memory growth in scheduler/
// reconciler, lease/backoff logic drifting under continuous churn) that short tests
// won't surface. Not run in CI by default (duration is long); intended for scheduled
// manual/nightly runs against a disposable environment.
//
// Run: k6 run load_tests/soak.js
// Env: BASE_URL, SOAK_EMAIL, SOAK_PASSWORD, SOAK_DURATION (default 2h)

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend, Rate } from 'k6/metrics';
import { BASE_URL, login, authHeaders, randomIdempotencyKey, generationPayload } from './common.js';

const dispatchLag = new Trend('dispatch_lag_ms', true);
const jobLost = new Rate('job_lost_rate');

const DURATION = __ENV.SOAK_DURATION || '2h';

export const options = {
  scenarios: {
    soak: {
      executor: 'constant-vus',
      vus: 15, // moderate, sustained -- see README.md concurrency assumptions
      duration: DURATION,
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.01'],
    'http_req_duration{endpoint:generations_post}': ['p(95)<250'],
    dispatch_lag_ms: ['p(95)<2000'],
    job_lost_rate: ['rate==0'],
  },
};

const EMAIL = __ENV.SOAK_EMAIL || 'student@example.edu';
const PASSWORD = __ENV.SOAK_PASSWORD || 'correct-horse-battery-staple';

export default function () {
  const token = login(EMAIL, PASSWORD);
  if (!token) {
    console.warn('soak: login failed; seed a test user before running this scenario');
    sleep(5);
    return;
  }
  const headers = authHeaders(token);

  const submittedAt = Date.now();
  const postRes = http.post(`${BASE_URL}/v1/generations`, generationPayload(), {
    headers: Object.assign({}, headers, { 'Idempotency-Key': randomIdempotencyKey('soak') }),
    tags: { endpoint: 'generations_post' },
  });
  const admitted = check(postRes, { 'generation admitted (202)': (r) => r.status === 202 });
  if (!admitted) {
    jobLost.add(1);
    sleep(2);
    return;
  }
  const jobId = postRes.json('id');

  let found = false;
  for (let i = 0; i < 10; i++) {
    sleep(0.5);
    const jobRes = http.get(`${BASE_URL}/v1/jobs/${jobId}`, { headers, tags: { endpoint: 'jobs_get' } });
    if (jobRes.status === 200) {
      found = true;
      if (jobRes.json('state') !== 'queued') {
        dispatchLag.add(Date.now() - submittedAt);
        break;
      }
    }
  }
  jobLost.add(found ? 0 : 1);

  sleep(3);
}
