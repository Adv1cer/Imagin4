// hundred_concurrent_burst.js -- "100 people generate an image at the same moment"
// scenario (Chet, 2026-08-17). Distinct from spike.js: spike.js hammers ONE login at
// 100 VUs to test rate-limit shedding; this scenario uses 100 DISTINCT accounts (seeded
// via `python -m scripts.seed_load_test_users`) each submitting exactly ONE
// general-image job, because app/core/config.py's max_active_jobs_per_user=1 and
// rl_generation_per_min are PER-USER -- one shared login can't model "100 different
// people" hitting admission/fairness/GPU-dispatch at once.
//
// Only exercises the ComfyUI (general_image / txt2img_basic) path via
// common.js's generationPayload() -- does NOT touch poster/infographic (Gemini),
// per explicit instruction not to involve that path in this test.
//
// Unlike baseline.js (which stops timing at "first non-queued state"), this scenario
// polls each job all the way to a TERMINAL state (succeeded/failed/cancelled) so you
// can read off wall-clock time for the whole 100-image batch to finish -- the actual
// question being answered ("100 people generate at once, how long until everyone has
// their image").
//
// Prereqs:
//   docker compose run --rm api python -m scripts.seed_load_test_users --count 100
//
// Run: k6 run load_tests/hundred_concurrent_burst.js
// Env: BASE_URL, LOADTEST_PASSWORD, LOADTEST_USER_COUNT (must match --count above),
//      LOADTEST_POLL_TIMEOUT_S (default 600 = 10min ceiling per job)

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend, Rate, Counter } from 'k6/metrics';
import { BASE_URL, authHeaders, randomIdempotencyKey, generationPayload } from './common.js';

const dispatchLag = new Trend('dispatch_lag_ms', true); // submit -> first non-queued
const completionLag = new Trend('completion_lag_ms', true); // submit -> terminal state
const jobLost = new Rate('job_lost_rate'); // 202 admitted but never GET-able / never terminal
const jobFailed = new Rate('job_failed_rate'); // reached a terminal state, but it was 'failed'
const jobTimedOut = new Counter('job_poll_timeout_count'); // never reached terminal state before ceiling

const USER_COUNT = Number(__ENV.LOADTEST_USER_COUNT || 100);
const PASSWORD = __ENV.LOADTEST_PASSWORD || 'correct-horse-battery-staple';
const POLL_TIMEOUT_S = Number(__ENV.LOADTEST_POLL_TIMEOUT_S || 600);
const POLL_INTERVAL_S = 2;

export const options = {
  scenarios: {
    // per-vu-iterations with 1 iteration/VU: every VU starts at t=0 and fires its
    // single POST /v1/generations essentially simultaneously -- this IS the burst.
    hundred_at_once: {
      executor: 'per-vu-iterations',
      vus: USER_COUNT,
      iterations: 1,
      maxDuration: `${POLL_TIMEOUT_S + 60}s`,
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.05'],
    job_lost_rate: ['rate==0'],
    // Informational, not a hard gate: read completion_lag_ms p95/max from the summary
    // to answer "how long did the batch take" -- no SLO exists yet for real GPU
    // generation time (see README.md's capacity-planning section).
  },
};

function emailForVU(vu) {
  // Matches scripts/seed_load_test_users.py's EMAIL_TEMPLATE exactly.
  const idx = ((vu - 1) % USER_COUNT) + 1;
  return `loadtest-user-${String(idx).padStart(4, '0')}@example.edu`;
}

export default function () {
  const email = emailForVU(__VU);

  const loginRes = http.post(
    `${BASE_URL}/v1/auth/login`,
    JSON.stringify({ email, password: PASSWORD }),
    { headers: { 'Content-Type': 'application/json' }, tags: { endpoint: 'login' } },
  );
  if (loginRes.status !== 200) {
    console.error(
      `hundred_concurrent_burst: login failed for ${email} (status=${loginRes.status}) -- ` +
        'did you run `python -m scripts.seed_load_test_users` first?',
    );
    jobLost.add(1);
    return;
  }
  const token = loginRes.json('session_token');
  const headers = authHeaders(token);

  const submittedAt = Date.now();
  const postRes = http.post(`${BASE_URL}/v1/generations`, generationPayload(), {
    headers: Object.assign({}, headers, { 'Idempotency-Key': randomIdempotencyKey(`hundred-${email}`) }),
    tags: { endpoint: 'generations_post' },
  });
  const admitted = check(postRes, { 'generation admitted (202)': (r) => r.status === 202 });
  if (!admitted) {
    console.error(`hundred_concurrent_burst: ${email} not admitted, status=${postRes.status} body=${postRes.body}`);
    jobLost.add(1);
    return;
  }
  const jobId = postRes.json('id');

  let sawNonQueued = false;
  let terminalState = null;
  const deadline = submittedAt + POLL_TIMEOUT_S * 1000;

  while (Date.now() < deadline) {
    sleep(POLL_INTERVAL_S);
    const jobRes = http.get(`${BASE_URL}/v1/jobs/${jobId}`, { headers, tags: { endpoint: 'jobs_get' } });
    if (jobRes.status !== 200) {
      continue;
    }
    const state = jobRes.json('state');

    if (!sawNonQueued && state !== 'queued') {
      sawNonQueued = true;
      dispatchLag.add(Date.now() - submittedAt);
    }

    if (state === 'succeeded' || state === 'failed' || state === 'cancelled') {
      terminalState = state;
      break;
    }
  }

  if (terminalState === null) {
    jobTimedOut.add(1);
    jobLost.add(1);
    check(null, {
      [`${email} job reached terminal state within ${POLL_TIMEOUT_S}s`]: () => false,
    });
    return;
  }

  jobLost.add(0);
  completionLag.add(Date.now() - submittedAt);
  jobFailed.add(terminalState === 'failed' ? 1 : 0);
  check(null, { 'job reached succeeded (not failed/cancelled)': () => terminalState === 'succeeded' });
}
