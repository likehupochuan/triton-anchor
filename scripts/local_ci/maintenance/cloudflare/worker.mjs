// Standalone Cloudflare module Worker. Bind ALERT_STATE (KV) and GITEE_TOKEN (Secret).
const CONFIG = Object.freeze({
  owner: 'likehupochuan',
  repository: 'triton-anchor-worker-health',
  worker: 'jiwang-ci-race-1',
  staleMs: 20 * 60 * 1000,
  uploadMs: 20 * 60 * 1000,
  diskFreeBytes: 5 * 1024 ** 3,
});

const API = 'https://gitee.com/api/v5';
const KEY = `local-ci-alert:${CONFIG.worker}`;
const MARKER = `<!-- ${KEY} -->`;
const LABELS = Object.freeze({
  source_unreadable: '连续两次无法读取健康数据（不能据此判断服务器宕机）',
  snapshot_stale: '健康快照已超过20分钟未更新（当前服务状态未知）',
  poller_unavailable: 'Worker未运行或心跳过期',
  runtime_unavailable: 'Docker运行环境不可用',
  relay_poll_failed: 'Gitee任务轮询失败',
  codex_connection_error: 'Codex连接失败',
  codex_auth_error: 'Codex认证失败',
  codex_rate_limited: 'Codex受到限流',
  codex_timeout: 'Codex执行超时',
  codex_failed: 'Codex执行失败（原因需查看服务器日志）',
  delivery_pending: '结果等待上传超过20分钟',
  environment_unavailable: '环境不可用',
  control_update_failed: '控制代码更新执行失败',
  control_update_invalid: '控制代码更新请求无效',
  control_update_blocked: '控制代码更新请求受阻（尚未执行更新）；具体原因请查看 Worker 日志',
  service_failed: '服务器侧维护服务失败',
  disk_space_low: '服务器可用磁盘不足5GiB',
});
const CODEX_ERRORS = new Set(['connection_error', 'auth_error', 'rate_limited', 'timeout', 'failed']);
const CODEX_OK = new Set(['running', 'succeeded']);

function faults(snapshot, previous, now) {
  if (now - Date.parse(snapshot.collected_at) > CONFIG.staleMs) {
    return [...new Set([...previous.filter(code => code !== 'source_unreadable'), 'snapshot_stale'])].sort();
  }
  const result = new Set();
  const check = (code, bad, known) => {
    if (bad || (!known && previous.includes(code))) result.add(code);
  };
  const poller = snapshot.poller || {};
  const runtime = snapshot.runtime || {};
  check('poller_unavailable', poller.alive === false || poller.heartbeat_stale === true,
    typeof poller.alive === 'boolean' && typeof poller.heartbeat_stale === 'boolean');
  check('runtime_unavailable', runtime.available === false, typeof runtime.available === 'boolean');
  check('relay_poll_failed', poller.last_poll_status === 'error', ['error', 'success'].includes(poller.last_poll_status));
  const codex = snapshot.active_task?.codex_status;
  if (CODEX_ERRORS.has(codex)) result.add(`codex_${codex}`);
  else if (!CODEX_OK.has(codex)) {
    for (const code of previous) if (code.startsWith('codex_')) result.add(code);
  }
  const uploads = snapshot.uploads;
  check('delivery_pending', Array.isArray(uploads) && uploads.some(row =>
    Number.isFinite(Date.parse(row.queued_at)) && now - Date.parse(row.queued_at) > CONFIG.uploadMs),
  Array.isArray(uploads) && uploads.every(row => Number.isFinite(Date.parse(row.queued_at))));
  const environment = snapshot.environments || {};
  check('environment_unavailable', environment.unavailable === true, typeof environment.unavailable === 'boolean');
  const update = snapshot.control_update?.state;
  const updateKnown = ['idle', 'pending', 'updating', 'failed', 'invalid', 'blocked'].includes(update);
  for (const state of ['failed', 'invalid', 'blocked']) {
    check(`control_update_${state}`, update === state, updateKnown);
  }
  const services = snapshot.services;
  check('service_failed', Array.isArray(services) && services.some(row => row.active_state === 'failed'
    || (row.name?.endsWith('.timer') && row.active_state === 'inactive')),
  Array.isArray(services) && services.length > 0 && services.every(row => row.available === true
    && ['active', 'inactive', 'failed', 'activating'].includes(row.active_state)));
  const storage = snapshot.storage;
  check('disk_space_low', Array.isArray(storage) && storage.some(row =>
    typeof row.filesystem_free_bytes === 'number' && row.filesystem_free_bytes < CONFIG.diskFreeBytes),
  Array.isArray(storage) && storage.length > 0 && storage.every(row => typeof row.filesystem_free_bytes === 'number'));
  return [...result].sort();
}

async function snapshot(fetcher, now) {
  const ref = encodeURIComponent(`snapshot/${CONFIG.worker}`);
  const response = await fetcher(`${API}/repos/${CONFIG.owner}/${CONFIG.repository}/contents/worker-health.json?ref=${ref}`, {
    headers: { Accept: 'application/json' }, signal: AbortSignal.timeout(15000),
  });
  if (!response.ok) throw new Error('Health data unavailable');
  const envelope = await response.json();
  if (envelope.encoding !== 'base64' || typeof envelope.content !== 'string') throw new Error('Invalid health document');
  const bytes = Uint8Array.from(atob(envelope.content.replace(/\s/g, '')), character => character.charCodeAt(0));
  const value = JSON.parse(new TextDecoder().decode(bytes));
  if (value.schema !== 'triton-anchor-worker-health' || value.worker_id !== CONFIG.worker
      || !Number.isFinite(Date.parse(value.collected_at))
      || Date.parse(value.collected_at) > now + 60000) throw new Error('Invalid health identity');
  return value;
}

async function issueRequest(fetcher, token, path, method = 'GET', body) {
  if (!token) throw new Error('GITEE_TOKEN is not configured');
  const response = await fetcher(`${API}${path}`, {
    method, headers: { Accept: 'application/json', 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    ...(body ? { body: JSON.stringify(body) } : {}), signal: AbortSignal.timeout(15000),
  });
  if (!response.ok) throw new Error(`Gitee Issue request failed (HTTP ${response.status})`);
  return response.json();
}

async function findOpenIssue(fetcher, token) {
  for (let page = 1; ; page++) {
    const rows = await issueRequest(fetcher, token,
      `/repos/${CONFIG.owner}/${CONFIG.repository}/issues?state=all&sort=updated&direction=desc&per_page=100&page=${page}`);
    if (!Array.isArray(rows)) throw new Error('Invalid Issue list');
    const found = rows.find(row => ['open', 'progressing'].includes(row.state)
      && typeof row.body === 'string' && row.body.includes(MARKER));
    if (found) return found;
    if (rows.length < 100) return null;
  }
}

function issueBody(state, at, recovered = false) {
  const lines = [MARKER, `服务器：${CONFIG.worker}`, '', `首次发现：${state.first_seen}`, `本次观测：${at}`, ''];
  if (recovered) {
    const minutes = Math.max(0, Math.round((Date.parse(at) - Date.parse(state.first_seen)) / 60000));
    lines.push(`恢复时间：${at}`, `本次异常持续约 ${minutes} 分钟。`, '新鲜健康快照确认此前异常已结束。', '', '此前异常：');
  } else lines.push('当前异常（无法确认恢复的原有异常继续保留）：');
  for (const code of state.codes) lines.push(`- ${LABELS[code]}`);
  lines.push('', '此告警仅基于公开健康快照；原始错误、认证信息和服务器日志不在此发布。');
  return lines.join('\n');
}

function resetIncident(state) {
  state.first_seen = null;
  state.issue_number = null;
  state.signature = '';
  state.codes = [];
  state.pending_close = null;
}

async function runMonitor(env, { fetcher = fetch, now = new Date() } = {}) {
  const state = await env.ALERT_STATE.get(KEY, 'json') || {
    first_seen: null, issue_number: null, signature: '', codes: [], read_failures: 0,
  };
  const at = now.toISOString();
  const patch = (body, extra = {}) => issueRequest(fetcher, env.GITEE_TOKEN,
    `/repos/${CONFIG.owner}/issues/${encodeURIComponent(state.issue_number)}`, 'PATCH',
    { repo: CONFIG.repository, body, ...extra });
  try {
    // A previously observed recovery stays retryable even if its API response was lost.
    if (state.pending_close) {
      await patch(issueBody(state, state.pending_close, true), { state: 'closed' });
      resetIncident(state);
    }
    let current;
    try {
      current = await snapshot(fetcher, now.getTime());
      state.read_failures = 0;
    } catch {
      state.read_failures += 1;
    }
    // Reconcile before deciding recovery as well: POST or KV acknowledgement may have been lost.
    if (!state.issue_number) {
      const existing = await findOpenIssue(fetcher, env.GITEE_TOKEN);
      if (existing) {
        state.issue_number = String(existing.number);
        const first = existing.body.match(/^首次发现：(.+)$/m)?.[1] || existing.created_at;
        state.first_seen ||= Number.isFinite(Date.parse(first)) ? new Date(first).toISOString() : at;
        if (!state.codes.length) {
          const lines = existing.body.split('\n');
          state.codes = Object.keys(LABELS).filter(code => lines.includes(`- ${LABELS[code]}`)).sort();
        }
      }
    }
    const codes = current ? faults(current, state.codes, now.getTime()) : [...state.codes];
    if (!current && state.read_failures >= 2 && !codes.includes('source_unreadable')) codes.push('source_unreadable');
    codes.sort();
    if (codes.length) {
      state.first_seen ||= at;
      state.codes = codes;
      const signature = codes.join(',');
      const title = `[Local CI 告警] ${CONFIG.worker} ${codes.length}项异常`;
      if (!state.issue_number) {
        const created = await issueRequest(fetcher, env.GITEE_TOKEN, `/repos/${CONFIG.owner}/issues`, 'POST', {
          repo: CONFIG.repository, title, body: issueBody(state, at),
        });
        if (!created.number) throw new Error('Issue creation returned no number');
        state.issue_number = String(created.number);
        state.signature = signature;
      }
      if (state.signature !== signature) {
        await patch(issueBody(state, at), { title });
        state.signature = signature;
      }
    } else if (state.issue_number) {
      state.pending_close = at;
      await patch(issueBody(state, at, true), { state: 'closed' });
      resetIncident(state);
    } else resetIncident(state);
  } finally {
    // KV permits one write per key per second; persist once per five-minute execution.
    await env.ALERT_STATE.put(KEY, JSON.stringify(state));
  }
}

export default {
  scheduled(event, env, ctx) { ctx.waitUntil(runMonitor(env, { now: new Date(event.scheduledTime) })); },
  fetch() { return new Response('Not found', { status: 404 }); },
};
