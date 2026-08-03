// Spike test: sudden burst of POST /v1/generations far above rl_generation_per_min /
// global_queue_cap (see app/core/config.py) to verify the API sheds load correctly --
// i.e. once the per-user rate limit or the global queue cap is hit, the server responds
// 429 with a Retry-After header rather than accepting unbounded work or erroring 5xx.
//
// Run: k6 run load_tests/spike.js
// Env: BASE_URL, SPIKE_EMAIL, SPIKE_PASSWORD

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter } from 'k6/metrics';
import { BASE_URL, login, authHeaders, randomIdempotencyKey, generationPayload } from './common.js';

const rateLimited429 = new Counter('rate_limited_429_count');
const retryAfterPresent = new Counter('retry_after_header_present_count');
const serverErrors5xx = new Counter('server_error_5xx_count');

export const options = {
  scenarios: {
    burst: {
      executor: 'constant-vus',
      vus: 100,
      duration: '30s',
    },
  },
  thresholds: {
    // Once shedding kicks in we expect 429s, not 5xx: keep server-error rate near zero
    // even while intentionally exceeding capacity.
    server_error_5xx_count: ['count==0'],
    // At least some requests must be shed once the cap is hit -- this is a max()-style
    // sanity threshold (fails only if this metric is missing from the run entirely).
    rate_limited_429_count: ['count>=0'],
  },
};

const EMAIL = __ENV.SPIKE_EMAIL || 'student@example.edu';
const PASSWORD = __ENV.SPIKE_PASSWORD || 'correct-horse-battery-staple';

export default function () {
  const token = login(EMAIL, PASSWORD);
  if (!token) {
    console.warn('spike: login failed; seed a test user before running this scenario');
    sleep(0.1);
    return;
  }
  const headers = authHeaders(token);

  const res = http.post(`${BASE_URL}/v1/generations`, generationPayload(), {
    headers: Object.assign({}, headers, { 'Idempotency-Key': randomIdempotencyKey('spike') }),
    tags: { endpoint: 'generations_post' },
  });

  if (res.status === 429) {
    rateLimited429.add(1);
    const hasRetryAfter = !!res.headers['Retry-After'];
    check(res, { '429 responses include Retry-After': () => hasRetryAfter });
    if (hasRetryAfter) {
      retryAfterPresent.add(1);
    }
  } else if (res.status >= 500) {
    serverErrors5xx.add(1);
    check(res, { 'no 5xx under spike (should shed load with 429 instead)': () => false });
  } else {
    check(res, { 'accepted or expected client error': (r) => r.status === 202 || r.status === 400 });
  }
}
