// One benchmark step (SPEC §9): the same open-model load, RATE requests a
// second, first straight to the mock provider, then through the gateway.
// Each phase warms up for WARMUP_S and is then measured for DURATION_S; only
// the measured scenarios ("mock", "gateway") are reported. Overhead is the
// gateway's latency minus the mock's at the same percentile.
//
// Non-streamed requests are timed by http_req_duration. Streamed ones by
// http_req_waiting, k6's time to first byte: the gateway sends its response
// headers with the first SSE chunk, and the mock (no TTFT delay) right before
// its first chunk, so this is time to first chunk.
//
// Writes OUT.json (k6's raw end-of-test summary data) and OUT.verdict.json
// (the B2 stopping rule's inputs and result), both under /results.

import http from 'k6/http';
import { check } from 'k6';

const MOCK_URL = __ENV.MOCK_URL || 'http://mock_provider:8001';
const TARGET = __ENV.TARGET || 'http://gateway:8000';
const RATE = Number(__ENV.RATE || 500);
const WARMUP_S = Number(__ENV.WARMUP_S || 10);
const DURATION_S = Number(__ENV.DURATION_S || 60);
const STREAM = __ENV.STREAM === 'true';
const OUT = __ENV.OUT || '/results/step';
const PRE_VUS = Number(__ENV.PRE_VUS || 50);
const MAX_VUS = Number(__ENV.MAX_VUS || 400);
const ADMIN_TOKEN = __ENV.ADMIN_TOKEN || 'dev-only-admin-token';
const GAP_S = 3; // lets the mock phase's last requests finish before the gateway's start

// B2's stopping rule: a step is sustained while the p99 overhead stays within
// 50 ms, under 0.1% of requests fail, and k6 starts every request on time.
const MAX_P99_OVERHEAD_MS = 50;
const MAX_ERROR_RATE = 0.001;

const BODY = JSON.stringify({
  model: 'gpt-4o-mini',
  messages: [{ role: 'user', content: 'Explain a token bucket rate limiter in one sentence.' }],
  max_tokens: 32,
  stream: STREAM,
  ...(STREAM ? { stream_options: { include_usage: true } } : {}),
});
const JSON_HEADERS = { 'Content-Type': 'application/json' };

function phase(exec, startS, durationS) {
  return {
    executor: 'constant-arrival-rate',
    rate: RATE,
    timeUnit: '1s',
    duration: `${durationS}s`,
    startTime: `${startS}s`,
    preAllocatedVUs: PRE_VUS,
    maxVUs: MAX_VUS,
    gracefulStop: `${GAP_S}s`,
    exec,
  };
}

const gatewayStart = WARMUP_S + DURATION_S + GAP_S;
const measured = ['mock', 'gateway'];

export const options = {
  discardResponseBodies: true,
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
  scenarios: {
    mock_warmup: phase('mock', 0, WARMUP_S),
    mock: phase('mock', WARMUP_S, DURATION_S),
    gateway_warmup: phase('gateway', gatewayStart, WARMUP_S),
    gateway: phase('gateway', gatewayStart + WARMUP_S, DURATION_S),
  },
  // Always-true thresholds: k6 only reports a per-scenario sub-metric that a
  // threshold names, and these keep the warm-up out of the measured numbers.
  thresholds: Object.fromEntries(
    measured.flatMap((s) => [
      [`http_req_duration{scenario:${s}}`, ['max>=0']],
      [`http_req_waiting{scenario:${s}}`, ['max>=0']],
      [`http_req_failed{scenario:${s}}`, ['rate>=0']],
      [`http_reqs{scenario:${s}}`, ['count>=0']],
      [`dropped_iterations{scenario:${s}}`, ['count>=0']],
      // The warm-up's request count too, to spread the gateway's CPU over every request.
      [`http_reqs{scenario:${s}_warmup}`, ['count>=0']],
    ]),
  ),
};

// A tenant with limits no benchmark reaches, and a key for it.
export function setup() {
  const admin = {
    headers: { ...JSON_HEADERS, Authorization: `Bearer ${ADMIN_TOKEN}` },
    responseType: 'text',
  };
  const name = `bench-${Date.now()}-${Math.floor(Math.random() * 1e9)}`;
  const tenant = http.post(
    `${TARGET}/admin/tenants`,
    JSON.stringify({ name, rpm_limit: 2147483647, tpm_limit: 2147483647 }),
    admin,
  );
  if (tenant.status !== 201) throw new Error(`creating the tenant: ${tenant.status} ${tenant.body}`);
  const key = http.post(`${TARGET}/admin/tenants/${tenant.json('id')}/keys`, '{}', admin);
  if (key.status !== 201) throw new Error(`creating a key: ${key.status} ${key.body}`);
  return { key: key.json('key') };
}

export function mock() {
  const r = http.post(`${MOCK_URL}/v1/chat/completions`, BODY, { headers: JSON_HEADERS });
  check(r, { 'status 200': (res) => res.status === 200 });
}

export function gateway(data) {
  const r = http.post(`${TARGET}/v1/chat/completions`, BODY, {
    headers: { ...JSON_HEADERS, Authorization: `Bearer ${data.key}` },
  });
  check(r, { 'status 200': (res) => res.status === 200 });
}

function value(data, metric, scenario, stat) {
  const m = data.metrics[`${metric}{scenario:${scenario}}`];
  return m && m.values[stat] !== undefined ? m.values[stat] : 0;
}

export function handleSummary(data) {
  const latency = STREAM ? 'http_req_waiting' : 'http_req_duration';
  const side = (s) => ({
    requests: value(data, 'http_reqs', s, 'count'),
    error_rate: value(data, 'http_req_failed', s, 'rate'),
    dropped: value(data, 'dropped_iterations', s, 'count'),
    p50_ms: value(data, latency, s, 'med'),
    p95_ms: value(data, latency, s, 'p(95)'),
    p99_ms: value(data, latency, s, 'p(99)'),
  });
  const mock = side('mock');
  const gw = side('gateway');
  const overheadP99 = gw.p99_ms - mock.p99_ms;
  const mockHeld = mock.error_rate <= MAX_ERROR_RATE && mock.dropped === 0;
  const verdict = {
    rate: RATE,
    stream: STREAM,
    latency_metric: latency,
    mock,
    gateway: gw,
    p99_overhead_ms: overheadP99,
    // Not the gateway's limit if the mock itself couldn't keep up.
    limited_by: !mockHeld ? 'mock' : null,
    pass:
      mockHeld &&
      overheadP99 <= MAX_P99_OVERHEAD_MS &&
      gw.error_rate <= MAX_ERROR_RATE &&
      gw.dropped === 0,
  };
  const ms = (x) => x.toFixed(2).padStart(8);
  const line = (n, s) =>
    `${n.padEnd(8)} requests ${String(s.requests).padStart(7)}  errors ${(100 * s.error_rate).toFixed(3)}%  ` +
    `dropped ${s.dropped}  p50 ${ms(s.p50_ms)} ms  p95 ${ms(s.p95_ms)} ms  p99 ${ms(s.p99_ms)} ms\n`;
  const text =
    `rate ${RATE}/s, ${STREAM ? 'streamed (time to first byte)' : 'non-streamed (duration)'}, ` +
    `warm-up ${WARMUP_S}s, measured ${DURATION_S}s\n` +
    line('mock', mock) +
    line('gateway', gw) +
    `p99 overhead ${overheadP99.toFixed(2)} ms -> ${verdict.pass ? 'PASS' : 'FAIL'}` +
    `${verdict.limited_by ? ` (limited by the ${verdict.limited_by})` : ''}\n`;
  // setup() returns the benchmark tenant's API key, and k6 would put it in the
  // summary as setup_data: a raw key never goes into results.
  const summary = { ...data };
  delete summary.setup_data;
  return {
    [`${OUT}.json`]: JSON.stringify(summary, null, 1),
    [`${OUT}.verdict.json`]: JSON.stringify(verdict, null, 1),
    stdout: text,
  };
}
