import assert from 'node:assert/strict';
import test from 'node:test';
import worker from './worker.mjs';

const ID = 'jiwang-ci-race-1';
const MARKER = `<!-- local-ci-alert:${ID} -->`;

function fixture() {
  const h = {
    now: Date.parse('2026-09-20T00:00:00Z'), stored: null, puts: 0, issues: [], writes: [],
    readFailure: false, failPatch: false, loseCreateResponse: false,
    health: {
      schema: 'triton-anchor-worker-health', worker_id: ID, state: 'healthy',
      poller: { alive: true, heartbeat_stale: false, last_poll_status: 'success' },
      runtime: { available: true }, active_task: null, uploads: [], services: [],
      environments: { unavailable: false }, control_update: { state: 'idle' },
      storage: [{ filesystem_free_bytes: 50 * 1024 ** 3 }],
    },
  };
  h.health.collected_at = new Date(h.now).toISOString();
  h.env = {
    GITEE_TOKEN: 'PRIVATE_TOKEN',
    ALERT_STATE: {
      async get() { return h.stored ? JSON.parse(h.stored) : null; },
      async put(_key, value) { h.puts++; h.stored = value; },
    },
  };
  h.advance = () => {
    h.now += 5 * 60 * 1000;
    h.health.collected_at = new Date(h.now).toISOString();
  };
  const json = value => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } });
  h.fetch = async (url, options) => {
    assert.ok(!url.includes('PRIVATE_TOKEN'));
    const parsed = new URL(url);
    if (parsed.pathname.endsWith('/contents/worker-health.json')) {
      assert.equal(parsed.searchParams.get('ref'), `snapshot/${ID}`);
      assert.equal(options.headers.Authorization, undefined);
      if (h.readFailure) throw new Error('PRIVATE network failure');
      return json({ encoding: 'base64', content: Buffer.from(JSON.stringify(h.health)).toString('base64') });
    }
    assert.equal(options.headers.Authorization, 'Bearer PRIVATE_TOKEN');
    if (options.method === 'GET') return json(h.issues);
    const body = JSON.parse(options.body);
    assert.equal(body.repo, 'triton-anchor-worker-health');
    assert.ok(!options.body.includes('PRIVATE'));
    h.writes.push({ method: options.method, body });
    if (options.method === 'POST') {
      const issue = { ...body, number: `I${h.issues.length + 1}`, state: 'open' };
      h.issues.push(issue);
      if (h.loseCreateResponse) {
        h.loseCreateResponse = false;
        throw new Error('Response lost');
      }
      return json(issue);
    }
    assert.equal(options.method, 'PATCH');
    if (h.failPatch) { h.failPatch = false; return new Response('', { status: 503 }); }
    const issue = h.issues.find(row => parsed.pathname.endsWith(`/${row.number}`));
    assert.ok(issue);
    Object.assign(issue, body);
    return json(issue);
  };
  h.run = async () => {
    const original = globalThis.fetch;
    globalThis.fetch = h.fetch;
    let pending;
    const before = h.puts;
    try {
      worker.scheduled({ scheduledTime: h.now }, h.env, { waitUntil(value) { pending = value; } });
      await pending;
    } finally {
      globalThis.fetch = original;
      assert.equal(h.puts, before + 1, 'one KV write per execution');
    }
  };
  return h;
}

test('one Issue per incident, unchanged faults are quiet, changed faults update, recovery closes', async () => {
  const h = fixture();
  await h.run();
  assert.equal(h.writes.length, 0);
  h.health.poller.alive = false;
  h.health.private_error = 'PRIVATE diagnostic';
  await h.run();
  assert.equal(h.issues.length, 1);
  assert.ok(h.issues[0].body.includes(MARKER));
  h.advance();
  await h.run();
  assert.equal(h.writes.length, 1);
  h.health.runtime.available = false;
  await h.run();
  assert.equal(h.writes.length, 2);
  assert.ok(h.issues[0].body.includes('Docker'));
  h.advance();
  h.health.poller.alive = true;
  h.health.runtime.available = true;
  await h.run();
  assert.equal(h.issues[0].state, 'closed');
  assert.ok(h.issues[0].body.includes('恢复时间：'));
  assert.ok(h.issues[0].body.includes('10 分钟'));
  await h.run();
  assert.equal(h.writes.length, 3);
  h.health.runtime.available = false;
  await h.run();
  assert.equal(h.issues.length, 2);
});

test('two read failures trigger an observation alert; unreadable or stale data never clears service faults', async () => {
  const h = fixture();
  h.readFailure = true;
  await h.run();
  assert.equal(h.issues.length, 0);
  h.advance();
  await h.run();
  assert.ok(h.issues[0].body.includes('不能据此判断服务器宕机'));
  h.readFailure = false;
  h.health.runtime.available = false;
  await h.run();
  assert.ok(!h.issues[0].body.includes('连续两次'));
  h.health.runtime.available = true;
  h.now += 25 * 60 * 1000;
  await h.run();
  assert.equal(h.issues[0].state, 'open');
  assert.ok(h.issues[0].body.includes('Docker'));
  assert.ok(h.issues[0].body.includes('20分钟未更新'));
  h.readFailure = true;
  await h.run();
  assert.equal(h.issues[0].state, 'open');
  h.readFailure = false;
  h.advance();
  await h.run();
  assert.equal(h.issues[0].state, 'closed');
});

test('missing or idle Codex telemetry cannot clear a known connection fault', async () => {
  const h = fixture();
  h.health.active_task = { codex_status: 'connection_error' };
  await h.run();
  for (const active of [{}, null, { codex_status: 'starting' }]) {
    h.advance();
    h.health.active_task = active;
    await h.run();
    assert.equal(h.issues[0].state, 'open');
  }
  assert.equal(h.writes.length, 1);
  h.health.active_task = { codex_status: 'running' };
  await h.run();
  assert.equal(h.issues[0].state, 'closed');
});

test('a lost create response is reconciled before immediate recovery, without a duplicate Issue', async () => {
  const h = fixture();
  h.health.runtime.available = false;
  h.loseCreateResponse = true;
  await assert.rejects(h.run(), /Response lost/);
  assert.equal(h.issues.length, 1);
  h.health.runtime.available = true;
  h.advance();
  await h.run();
  assert.equal(h.issues.length, 1);
  assert.equal(h.issues[0].state, 'closed');
  assert.equal(h.writes.filter(row => row.method === 'POST').length, 1);
});

test('failed Issue updates and recovery are retried without losing the incident', async () => {
  const h = fixture();
  h.health.runtime.available = false;
  await h.run();
  h.health.poller.alive = false;
  h.failPatch = true;
  await assert.rejects(h.run(), /HTTP 503/);
  h.advance();
  await h.run();
  assert.ok(h.issues[0].body.includes('Worker未运行'));
  h.health.runtime.available = true;
  h.health.poller.alive = true;
  h.failPatch = true;
  await assert.rejects(h.run(), /HTTP 503/);
  h.advance();
  await h.run();
  assert.equal(h.issues[0].state, 'closed');
  assert.equal(JSON.parse(h.stored).issue_number, null);
  assert.equal(h.issues.length, 1);
});

test('KV loss adopts a progressing Issue and preserves unknown Codex faults until explicit recovery', async () => {
  const h = fixture();
  h.health.active_task = { codex_status: 'auth_error' };
  await h.run();
  h.issues[0].state = 'progressing';
  h.stored = null;
  h.health.active_task = null;
  h.advance();
  await h.run();
  assert.equal(h.issues.length, 1);
  assert.equal(h.issues[0].state, 'progressing');
  assert.ok(h.issues[0].body.includes('Codex认证失败'));
  h.health.active_task = { codex_status: 'running' };
  h.stored = null;
  await h.run();
  assert.equal(h.issues[0].state, 'closed');
});

test('future-dated snapshots are unreadable; unavailable service telemetry does not imply recovery', async () => {
  const h = fixture();
  h.health.services = [{ name: 'health.timer', available: true, active_state: 'inactive' }];
  await h.run();
  assert.ok(h.issues[0].body.includes('维护服务失败'));
  h.health.services[0] = { name: 'health.timer', available: false, active_state: 'unknown' };
  await h.run();
  assert.equal(h.issues[0].state, 'open');
  h.health.services = [];
  await h.run();
  assert.equal(h.issues[0].state, 'open');
  h.health.collected_at = new Date(h.now + 120000).toISOString();
  await h.run();
  await h.run();
  assert.ok(h.issues[0].body.includes('连续两次无法读取'));
  h.advance();
  h.health.services = [{ name: 'health.service', available: true, active_state: 'inactive' }];
  await h.run();
  assert.equal(h.issues[0].state, 'closed');
});
