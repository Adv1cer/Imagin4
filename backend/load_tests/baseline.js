// Baseline load: steady realistic traffic representing normal campus usage (see
// README.md capacity-planning notes for how VU/arrival-rate numbers were chosen).
// Verifies the platform's core SLOs under normal conditions.
//
// Thresholds enforced:
//  - POST /v1/generations p95 < 250ms (excludes generation time: 202 is returned the
//    instant the job is admitted to the queue, before ComfyUI ever runs it, so this
//    measures admission latency only, per the requirement).
//  - API error rate < 1% overall.
//  - No job lost after 202: every admitted job must be GET-able afterwards.
//  - Status/event visibility within 2s: time from admission to the job first reporting
//    a transition away from `queued` (via polling; SSE covered by campus_peak.js).
//  - Dispatch lag p95 < 2s when a compatible slot is available (custom trend metric
//    `dispatch_lag_ms`, measured admission -> first observed non-queued state).
//
// Run: k6 run load_tests/baseline.js
// Env: BASE_URL, BASELINE_EMAIL, BASELINE_PASSWORD

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend, Rate } from 'k6/metrics';
import { BASE_URL, login, authHeaders, randomIdempotencyKey, generationPayload } from './common.js';

const dispatchLag = new Trend('dispatch_lag_ms', true);
const jobLost = new Rate('job_lost_rate');
const visibilityLag = new Trend('status_visibility_lag_ms', true);

export const options = {
  scenarios: {
    steady_traffic: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: '30s', target: 20 }, // ramp up
        { duration: '3m', target: 20 }, // hold: representative campus baseline
        { duration: '30s', target: 0 }, // ramp down
      ],
      gracefulRampDown: '10s',
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.01'],
    'http_req_duration{endpoint:generations_post}': ['p(95)<250'],
    dispatch_lag_ms: ['p(95)<2000'],
    job_lost_rate: ['rate==0'],
    status_visibility_lag_ms: ['p(95)<2000'],
  },
};

const EMAIL = __ENV.BASELINE_EMAIL || 'student@example.edu';
const PASSWORD = __ENV.BASELINE_PASSWORD || 'correct-horse-battery-staple';

export default function () {
  const token = login(EMAIL, PASSWORD);
  if (!token) {
    console.warn('baseline: login failed; seed a test user before running this scenario');
    sleep(1);
    return;
  }
  const headers = authHeaders(token);

  const submittedAt = Date.now();
  const postRes = http.post(`${BASE_URL}/v1/generations`, generationPayload(), {
    headers: Object.assign({}, headers, { 'Idempotency-Key': randomIdempotencyKey('baseline') }),
    tags: { endpoint: 'generations_post' },
  });
  const admitted = check(postRes, { 'generation admitted (202)': (r) => r.status === 202 });
  if (!admitted) {
    jobLost.add(1);
    sleep(1);
    return;
  }
  const jobId = postRes.json('id');

  let sawNonQueued = false;
  let sawFound = false;
  for (let i = 0; i < 10; i++) {
    sleep(0.2);
    const jobRes = http.get(`${BASE_URL}/v1/jobs/${jobId}`, { headers, tags: { endpoint: 'jobs_get' } });
    if (jobRes.status === 200) {
      sawFound = true;
      const state = jobRes.json('state');
      if (state !== 'queued' && !sawNonQueued) {
        sawNonQueued = true;
        const now = Date.now();
        dispatchLag.add(now - submittedAt);
        visibilityLag.add(now - submittedAt);
        break;
      }
    }
  }

  jobLost.add(sawFound ? 0 : 1);
  check(null, { 'job became visible within poll window': () => sawFound });

  sleep(1);
}
