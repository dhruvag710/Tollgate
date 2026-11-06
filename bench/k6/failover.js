// B3 (SPEC §9): failover under load. RATE requests a second through nginx to
// the gateway replicas for WARMUP_S, then DURATION_S measured; bench/run_v2.sh
// kills the primary mock provider (docker kill) partway through the measured
// part, and every gpt-4o-mini request must then fail over to the secondary
// (config/providers.test.yaml: mock, then mock-secondary).
//
// The request is B2's: non-streamed, the mock answering at once. A failure
// before the gateway has sent the client anything is retried on the next
// provider (SPEC Phase 2.1), so the client should see no error; one that
// strikes a stream after its first byte can't be, which X3 covers.
//
// A record per request goes to the log, one JSON line starting {"at":: when it
// started (ms since the epoch, the Docker VM's clock, which the kill's time is
// read from too), its latency, status, k6's error code and the provider that
// answered. A poller reads the breakers (GET /admin/providers) twice a second
// and logs their states ({"breakers":...}). bench/v2_report.py builds the
// timeline from both.

import http from 'k6/http';
import { check } from 'k6';
import exec from 'k6/execution';

const TARGET = __ENV.TARGET || 'http://nginx:8000';
const ADMIN_TOKEN = __ENV.ADMIN_TOKEN || 'dev-only-admin-token';
const RATE = Number(__ENV.RATE || 300);
const WARMUP_S = Number(__ENV.WARMUP_S || 10);
const DURATION_S = Number(__ENV.DURATION_S || 120);
// k6 starts more VUs only as it needs them, and until they are ready it doesn't
// start requests on time: enough here, and none go missing when a kill stalls them.
const PRE_VUS = Number(__ENV.PRE_VUS || 50);
const OUT = __ENV.OUT || '/results/b3';

const JSON_HEADERS = { 'Content-Type': 'application/json' };
const BODY = JSON.stringify({
  model: 'gpt-4o-mini',
  messages: [{ role: 'user', content: 'Explain a token bucket rate limiter in one sentence.' }],
  max_tokens: 32,
});

export const options = {
  discardResponseBodies: false,
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
  scenarios: {
    warmup: {
      executor: 'constant-arrival-rate', rate: RATE, timeUnit: '1s', duration: `${WARMUP_S}s`,
      preAllocatedVUs: 50, maxVUs: 600, exec: 'chat', gracefulStop: '5s',
    },
    load: {
      executor: 'constant-arrival-rate', rate: RATE, timeUnit: '1s', duration: `${DURATION_S}s`,
      startTime: `${WARMUP_S}s`, preAllocatedVUs: PRE_VUS, maxVUs: 600, exec: 'chat',
      gracefulStop: '30s',
    },
    breakers: {
      executor: 'constant-arrival-rate', rate: 2, timeUnit: '1s',
      duration: `${WARMUP_S + DURATION_S}s`, preAllocatedVUs: 2, maxVUs: 4, exec: 'poll',
    },
  },
  thresholds: {
    'http_req_duration{scenario:load}': ['max>=0'],
    'http_req_failed{scenario:load}': ['rate>=0'],
    'http_reqs{scenario:load}': ['count>=0'],
    'dropped_iterations{scenario:load}': ['count>=0'],
  },
};

export function setup() {
  const admin = {
    headers: { ...JSON_HEADERS, Authorization: `Bearer ${ADMIN_TOKEN}` },
    responseType: 'text',
  };
  const name = `b3-${Date.now()}-${Math.floor(Math.random() * 1e9)}`;
  const tenant = http.post(
    `${TARGET}/admin/tenants`,
    JSON.stringify({ name, rpm_limit: 2147483647, tpm_limit: 2147483647 }),
    admin,
  );
  if (tenant.status !== 201) throw new Error(`creating the tenant: ${tenant.status} ${tenant.body}`);
  const key = http.post(`${TARGET}/admin/tenants/${tenant.json('id')}/keys`, '{}', admin);
  if (key.status !== 201) throw new Error(`creating a key: ${key.status} ${key.body}`);
  return { key: key.json('key'), tenant: tenant.json('id') };
}

export function chat(data) {
  const at = Date.now();
  const r = http.post(`${TARGET}/v1/chat/completions`, BODY, {
    headers: { ...JSON_HEADERS, Authorization: `Bearer ${data.key}` },
  });
  check(r, { 'status 200': (res) => res.status === 200 });
  if (exec.scenario.name !== 'load') return;
  const record = {
    at,
    t: Math.round(r.timings.duration * 1000) / 1000,
    s: r.status,
    p: r.headers['X-Tollgate-Provider'] || null,
  };
  if (r.error_code) record.e = r.error_code;
  if (r.status !== 200) {
    const body = typeof r.body === 'string' ? r.body : '';
    record.b = body.slice(0, 160).replace(/\s+/g, ' ');
  }
  console.log(JSON.stringify(record));
}

export function poll() {
  const at = Date.now();
  const r = http.get(`${TARGET}/admin/providers`, {
    headers: { Authorization: `Bearer ${ADMIN_TOKEN}` },
  });
  let states = null;
  try {
    states = r.json();
  } catch (e) {
    states = { status: r.status };
  }
  console.log(JSON.stringify({ breakers: states, at }));
}

export function handleSummary(data) {
  const v = (metric, stat) => {
    const m = data.metrics[`${metric}{scenario:load}`];
    return m && m.values[stat] !== undefined ? m.values[stat] : 0;
  };
  const summary = {
    rate: RATE,
    warmup_s: WARMUP_S,
    duration_s: DURATION_S,
    tenant: data.setup_data ? data.setup_data.tenant : null,
    requests: v('http_reqs', 'count'),
    error_rate: v('http_req_failed', 'rate'),
    dropped: v('dropped_iterations', 'count'),
    p50_ms: v('http_req_duration', 'med'),
    p99_ms: v('http_req_duration', 'p(99)'),
    max_ms: v('http_req_duration', 'max'),
  };
  const raw = { ...data };
  delete raw.setup_data; // the tenant's API key never goes into results
  return {
    [`${OUT}.json`]: JSON.stringify(raw, null, 1),
    [`${OUT}.summary.json`]: JSON.stringify(summary, null, 1),
    stdout:
      `B3: ${RATE}/s for ${DURATION_S}s: ${summary.requests} requests, ` +
      `${(100 * summary.error_rate).toFixed(3)}% failed, ${summary.dropped} not started on time, ` +
      `p50 ${summary.p50_ms.toFixed(1)} ms, p99 ${summary.p99_ms.toFixed(1)} ms, max ${summary.max_ms.toFixed(0)} ms\n`,
  };
}
