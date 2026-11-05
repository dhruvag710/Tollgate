// B5 (SPEC §9): bench/workloads/make_workload.py's 20,000-request stream,
// replayed through one gateway at RATE requests a second. bench/run_v2.sh runs
// it twice, cache layer off and on, from an empty cache each time.
//
// Every replayed request is cacheable (temperature 0) and asks the mock for an
// answer it takes MOCK_LATENCY_MS to give, OUTPUT_TOKENS long, seeded: the
// mock's answer is a function of the seed and the question, so each answer
// served names the question it was first given for, and bench/b5_report.py
// can tell a correct hit from a false one.
//
// Two probes at PROBE_RATE, B1's request, which isn't cacheable (no
// temperature), sent through the gateway and straight to the mock at once; the
// mock answers it at once. Their difference at each percentile is the
// gateway's overhead for requests the cache can't serve: first for IDLE_S on
// their own (the layer loaded but idle), then alongside the replay (the layer
// working on the replay's requests).
//
// A record per replayed request goes to the log (one JSON line, starting
// {"i":): its index, status, X-Tollgate-Cache, latency, the gateway's
// Server-Timing stages, and a hash of the answer. Never the question's text,
// whose licences don't allow redistributing it, nor any key.

import http from 'k6/http';
import { check } from 'k6';
import crypto from 'k6/crypto';
import { SharedArray } from 'k6/data';
import exec from 'k6/execution';

const TARGET = __ENV.TARGET || 'http://gateway:8000';
const MOCK_URL = __ENV.MOCK_URL || 'http://mock_provider:8001';
const ADMIN_TOKEN = __ENV.ADMIN_TOKEN || 'dev-only-admin-token';
const RATE = Number(__ENV.RATE || 20);
const PROBE_RATE = Number(__ENV.PROBE_RATE || 20);
const WARMUP_S = Number(__ENV.WARMUP_S || 10);
const IDLE_S = Number(__ENV.IDLE_S || 60);
const MOCK_LATENCY_MS = Number(__ENV.MOCK_LATENCY_MS || 1000);
const OUTPUT_TOKENS = Number(__ENV.OUTPUT_TOKENS || 128);
const MAX_TOKENS = Number(__ENV.MAX_TOKENS || 256);
const MOCK_SEED = __ENV.MOCK_SEED || '20261105';
const LIMIT = Number(__ENV.LIMIT || 0); // replay only the first LIMIT requests (smoke runs)
const TENANT = __ENV.TENANT || `b5-${Date.now()}`;
const OUT = __ENV.OUT || '/results/b5';

const requests = new SharedArray('requests', () => JSON.parse(open('/data/b5_requests.json')));
const N = LIMIT ? Math.min(LIMIT, requests.length) : requests.length;
const DURATION_S = Math.ceil(N / RATE);
const JSON_HEADERS = { 'Content-Type': 'application/json' };
const PROBE = JSON.stringify({
  model: 'gpt-4o-mini',
  messages: [{ role: 'user', content: 'Explain a token bucket rate limiter in one sentence.' }],
  max_tokens: 32,
});

function rate(exec, r, startS, durationS, vus) {
  return {
    executor: 'constant-arrival-rate',
    rate: r,
    timeUnit: '1s',
    duration: `${durationS}s`,
    startTime: `${startS}s`,
    preAllocatedVUs: vus,
    maxVUs: 400,
    gracefulStop: '30s',
    exec,
  };
}

const measured = ['replay', 'probe_gateway', 'probe_mock', 'idle_gateway', 'idle_mock'];
const REPLAY_START_S = WARMUP_S + IDLE_S;

export const options = {
  discardResponseBodies: false,
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
  scenarios: {
    probe_gateway_warmup: rate('probeGateway', PROBE_RATE, 0, WARMUP_S, 5),
    probe_mock_warmup: rate('probeMock', PROBE_RATE, 0, WARMUP_S, 5),
    idle_gateway: rate('probeGateway', PROBE_RATE, WARMUP_S, IDLE_S, 5),
    idle_mock: rate('probeMock', PROBE_RATE, WARMUP_S, IDLE_S, 5),
    // In flight at once: about RATE x the mock's latency.
    replay: rate('replay', RATE, REPLAY_START_S, DURATION_S, Math.max(20, 3 * RATE)),
    probe_gateway: rate('probeGateway', PROBE_RATE, REPLAY_START_S, DURATION_S, 10),
    probe_mock: rate('probeMock', PROBE_RATE, REPLAY_START_S, DURATION_S, 10),
  },
  thresholds: Object.fromEntries(
    measured.flatMap((s) => [
      [`http_req_duration{scenario:${s}}`, ['max>=0']],
      [`http_req_failed{scenario:${s}}`, ['rate>=0']],
      [`http_reqs{scenario:${s}}`, ['count>=0']],
      [`dropped_iterations{scenario:${s}}`, ['count>=0']],
    ]),
  ),
};

function tenant(admin, name) {
  const t = http.post(
    `${TARGET}/admin/tenants`,
    JSON.stringify({ name, rpm_limit: 2147483647, tpm_limit: 2147483647 }),
    admin,
  );
  if (t.status !== 201) throw new Error(`creating tenant ${name}: ${t.status} ${t.body}`);
  const key = http.post(`${TARGET}/admin/tenants/${t.json('id')}/keys`, '{}', admin);
  if (key.status !== 201) throw new Error(`creating a key: ${key.status} ${key.body}`);
  return key.json('key');
}

// Two tenants with limits nothing reaches: the replay's, whose ledger rows
// bench/run_v2.sh sums for the cost saved, and the probes'.
export function setup() {
  const admin = {
    headers: { ...JSON_HEADERS, Authorization: `Bearer ${ADMIN_TOKEN}` },
    responseType: 'text',
  };
  return { replay: tenant(admin, TENANT), probe: tenant(admin, `${TENANT}-probe`) };
}

// "semantic-embed;dur=10.1, ..., semantic;desc=hit" -> {stage: ms}, notes.
function serverTiming(header) {
  const out = {};
  if (!header) return out;
  for (const item of header.split(',')) {
    const [name, value] = item.trim().split(';');
    if (!value) continue;
    if (value.startsWith('dur=')) out[name] = Number(value.slice(4));
    else if (value.startsWith('desc=')) out[`${name}:note`] = value.slice(5);
  }
  return out;
}

const STAGES = {
  'semantic-embed-wait': 'ew',
  'semantic-embed': 'e',
  'semantic-lookup': 'l',
  'semantic-verify-wait': 'vw',
  'semantic-verify': 'v',
  gateway: 'g',
  total: 'gt',
};

export function replay(data) {
  const i = exec.scenario.iterationInTest;
  if (i >= N) return;
  const item = requests[i];
  const body = JSON.stringify({
    model: 'gpt-4o-mini',
    messages: [{ role: 'user', content: item.text }],
    temperature: 0,
    max_tokens: MAX_TOKENS,
  });
  const at = Date.now();
  const r = http.post(`${TARGET}/v1/chat/completions`, body, {
    headers: {
      ...JSON_HEADERS,
      Authorization: `Bearer ${data.replay}`,
      'X-Mock-Latency-Ms': String(MOCK_LATENCY_MS),
      'X-Mock-Output-Tokens': String(OUTPUT_TOKENS),
      'X-Mock-Seed': MOCK_SEED,
    },
    tags: { kind: item.kind },
  });
  check(r, { 'status 200': (res) => res.status === 200 });
  let answer = null;
  if (r.status === 200) {
    try {
      answer = crypto.sha256(r.json('choices.0.message.content'), 'hex').slice(0, 16);
    } catch (e) {
      answer = 'unparsed';
    }
  } else {
    const excerpt = (typeof r.body === 'string' ? r.body : '').slice(0, 200).replace(/\s+/g, ' ');
    console.warn(`failure in replay at ${i}: status ${r.status}; k6 error_code ${r.error_code}; ${excerpt}`);
  }
  const record = {
    i,
    at,
    s: r.status,
    c: r.headers['X-Tollgate-Cache'] || null,
    t: Math.round(r.timings.duration * 1000) / 1000,
    h: answer,
  };
  const timing = serverTiming(r.headers['Server-Timing']);
  for (const [name, key] of Object.entries(STAGES)) {
    if (timing[name] !== undefined) record[key] = timing[name];
  }
  if (timing['semantic:note']) record.o = timing['semantic:note'];
  console.log(JSON.stringify(record));
}

function probe(url, auth, label) {
  const headers = auth ? { ...JSON_HEADERS, Authorization: `Bearer ${auth}` } : JSON_HEADERS;
  const at = Date.now();
  const r = http.post(`${url}/v1/chat/completions`, PROBE, { headers });
  check(r, { 'status 200': (res) => res.status === 200 });
  const scenario = exec.scenario.name;
  if (scenario.endsWith('_warmup')) return;
  if (r.status !== 200) {
    console.warn(`failure in ${scenario}: status ${r.status}; k6 error_code ${r.error_code}`);
  }
  const phase = scenario.startsWith('idle_') ? 'idle' : 'replay';
  const t = Math.round(r.timings.duration * 1000) / 1000;
  console.log(JSON.stringify({ p: label, phase, at, s: r.status, t }));
}

export function probeGateway(data) {
  probe(TARGET, data.probe, 'g');
}

export function probeMock() {
  probe(MOCK_URL, null, 'm');
}

function value(data, metric, scenario, stat) {
  const m = data.metrics[`${metric}{scenario:${scenario}}`];
  return m && m.values[stat] !== undefined ? m.values[stat] : 0;
}

export function handleSummary(data) {
  const side = (s) => ({
    requests: value(data, 'http_reqs', s, 'count'),
    error_rate: value(data, 'http_req_failed', s, 'rate'),
    dropped: value(data, 'dropped_iterations', s, 'count'),
    p50_ms: value(data, 'http_req_duration', s, 'med'),
    p95_ms: value(data, 'http_req_duration', s, 'p(95)'),
    p99_ms: value(data, 'http_req_duration', s, 'p(99)'),
  });
  const summary = {
    rate: RATE,
    probe_rate: PROBE_RATE,
    requests: N,
    duration_s: DURATION_S,
    mock_latency_ms: MOCK_LATENCY_MS,
    output_tokens: OUTPUT_TOKENS,
    tenant: TENANT,
    idle_s: IDLE_S,
    replay: side('replay'),
    probe_gateway: side('probe_gateway'),
    probe_mock: side('probe_mock'),
    idle_gateway: side('idle_gateway'),
    idle_mock: side('idle_mock'),
  };
  const raw = { ...data };
  delete raw.setup_data; // the tenants' API keys never go into results
  const line = (n, s) =>
    `${n.padEnd(14)} requests ${String(s.requests).padStart(6)}  errors ${(100 * s.error_rate).toFixed(3)}%  ` +
    `dropped ${s.dropped}  p50 ${s.p50_ms.toFixed(2)} ms  p99 ${s.p99_ms.toFixed(2)} ms\n`;
  return {
    [`${OUT}.json`]: JSON.stringify(raw, null, 1),
    [`${OUT}.summary.json`]: JSON.stringify(summary, null, 1),
    stdout:
      `B5: ${N} requests at ${RATE}/s, probes at ${PROBE_RATE}/s\n` +
      line('idle gateway', summary.idle_gateway) +
      line('idle mock', summary.idle_mock) +
      line('replay', summary.replay) +
      line('probe gateway', summary.probe_gateway) +
      line('probe mock', summary.probe_mock),
  };
}
