// Spike test of the webhook endpoint (requirements §10, decision D10, docs/testing.md "Spike scenario").
//
//   k6 run -e WEBHOOK_SECRET=... [-e CDMS_URL=http://localhost:8100] [-e PRODUCTS=1000] [-e PEAK=500] load/k6/spike.js
//
// Open model (ramping-arrival-rate): requests keep arriving at the target rate even when CDMS slows down, like a real
// sender. Mix per request:
//   60 % a new state of a load-test product (new event id, newer timestamp)
//   25 % an exact redelivery of an event this VU already sent (same id, same body) → expect 200 duplicate
//   15 % an old state again under a new event id (a cross-mechanism style duplicate) → stored, then UNCHANGED/STALE
// Products are "LT<run>-00001"… (run = a tag made in setup) so runs never overlap each other or the emulator's
// catalogue. Every event carries one item; the
// timestamp is base + global iteration number, so for each product a later iteration is always a newer state.
// After the run: `python scripts/verify_invariants.py --wait --prefix LT<run>-` checks that CDMS ended with exactly the
// newest state of every product.

import crypto from 'k6/crypto';
import exec from 'k6/execution';
import http from 'k6/http';
import { check } from 'k6';
import { Counter } from 'k6/metrics';

const CDMS_URL = __ENV.CDMS_URL || 'http://localhost:8100';
const SECRET = __ENV.WEBHOOK_SECRET;
const PRODUCTS = Number(__ENV.PRODUCTS || 1000);
const PEAK = Number(__ENV.PEAK || 500);
const BASE = Number(__ENV.BASE || 1_800_000_000); // Unix seconds of iteration 0

const accepted = new Counter('webhook_accepted_202');
const duplicates = new Counter('webhook_duplicate_200');
const rejected = new Counter('webhook_other_status');

export const options = {
  scenarios: {
    spike: {
      executor: 'ramping-arrival-rate',
      startRate: 10,
      timeUnit: '1s',
      preAllocatedVUs: 100,
      maxVUs: 1000,
      stages: [
        { duration: '30s', target: 10 }, // baseline
        { duration: '10s', target: PEAK }, // spike up
        { duration: '60s', target: PEAK }, // hold the spike
        { duration: '10s', target: 10 }, // back down
        { duration: '60s', target: 10 }, // baseline again
      ],
    },
  },
  thresholds: {
    // D10: accept latency p95 < 200 ms at the spike, < 1 % errors (a duplicate answer 200 is a success)
    http_req_duration: ['p(95)<200'],
    http_req_failed: ['rate<0.01'],
  },
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

export function setup() {
  if (!SECRET) throw new Error('set -e WEBHOOK_SECRET=<the value in .env>');
  const run = __ENV.RUN || Math.floor(Date.now() / 1000).toString(36);
  console.log(`run tag: ${run} — verify with: python scripts/verify_invariants.py --wait --prefix LT${run}-`);
  return { run };
}

// Per-VU memory of events already sent, for redeliveries and "old state under a new id".
const sent = [];

function product(sku, iteration) {
  return {
    partnerSKU: sku,
    sku: sku,
    productName: `Load test ${sku} #${iteration}`,
    assetType: 'Single',
    hasSerial: iteration % 2 === 0,
    hasExpiration: false,
    color: ['Red', 'Blue', 'Green'][iteration % 3],
    size: 'M',
    description: `state ${iteration}`,
    isActive: true,
    units: ['PCS'],
    categories: [{ categoryCode: 'LT', categoryName: 'Load test' }],
  };
}

function newEvent(run, iteration) {
  const sku = `LT${run}-${String(1 + Math.floor(Math.random() * PRODUCTS)).padStart(5, '0')}`;
  return JSON.stringify({
    id: `lt-${run}-${iteration}`,
    timestamp: BASE + iteration,
    event: 'PRODUCT_UPSERTED',
    items: [product(sku, iteration)],
  });
}

function post(body, kind) {
  const signature = crypto.hmac('sha256', SECRET, body, 'base64');
  const res = http.post(`${CDMS_URL}/api/v1/webhooks/vietful`, body, {
    headers: { 'content-type': 'application/json', 'x-vf-hmacsha256': signature },
    tags: { kind },
  });
  if (res.status === 202) accepted.add(1, { kind });
  else if (res.status === 200) duplicates.add(1, { kind });
  else rejected.add(1, { kind, status: String(res.status) });
  check(res, {
    'new event → 202': (r) => kind !== 'new' || r.status === 202,
    'redelivery → 200 duplicate': (r) => kind !== 'redelivery' || (r.status === 200 && r.json('status') === 'duplicate'),
    'old state, new id → 202': (r) => kind !== 'old_state_new_id' || r.status === 202,
  });
}

export default function (data) {
  const iteration = exec.scenario.iterationInTest;
  const roll = Math.random();
  if (sent.length > 0 && roll < 0.25) {
    post(sent[Math.floor(Math.random() * sent.length)], 'redelivery');
  } else if (sent.length > 0 && roll < 0.4) {
    const old = JSON.parse(sent[Math.floor(Math.random() * sent.length)]);
    old.id = `lt-${data.run}-dup-${iteration}`;
    post(JSON.stringify(old), 'old_state_new_id');
  } else {
    const body = newEvent(data.run, iteration);
    post(body, 'new');
    sent.push(body);
    if (sent.length > 50) sent.shift();
  }
}

export function handleSummary(data) {
  const file = __ENV.SUMMARY || 'load/results/spike-summary.json';
  return { [file]: JSON.stringify(data, null, 2), stdout: textSummary(data) };
}

function textSummary(data) {
  const m = data.metrics;
  const d = m.http_req_duration.values;
  const count = (name) => (m[name] ? m[name].values.count : 0);
  return [
    '',
    `requests            ${m.http_reqs.values.count} (${m.http_reqs.values.rate.toFixed(1)}/s average)`,
    `latency ms          avg ${d.avg.toFixed(1)}  p50 ${d.med.toFixed(1)}  p95 ${d['p(95)'].toFixed(1)}  p99 ${d['p(99)'].toFixed(1)}  max ${d.max.toFixed(1)}`,
    `failed (non-2xx)    ${(m.http_req_failed.values.rate * 100).toFixed(2)} %`,
    `202 accepted        ${count('webhook_accepted_202')}`,
    `200 duplicate       ${count('webhook_duplicate_200')}`,
    `other status        ${count('webhook_other_status')}`,
    `dropped iterations  ${count('dropped_iterations')} (arrivals k6 could not start: VUs exhausted)`,
    `thresholds          ${Object.entries(data.metrics).filter(([, v]) => v.thresholds).map(([k, v]) => `${k}: ${Object.values(v.thresholds).every((t) => t.ok) ? 'ok' : 'FAILED'}`).join(', ')}`,
    '',
  ].join('\n');
}
