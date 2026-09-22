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
const CACHE_KEY = `local-ci-health-cache:${CONFIG.worker}`;
const MARKER = `<!-- ${KEY} -->`;
const ISSUE_SEARCH_PAGE_SIZE = 100;
const ISSUE_SEARCH_MAX_PAGES = 3;
const LABELS = Object.freeze({
  source_unreadable: '连续两次无法读取健康数据（不能据此判断服务器宕机）',
  snapshot_stale: '健康快照已超过20分钟未更新（当前服务状态未知）',
  poller_unavailable: 'Worker未运行或心跳过期',
  runtime_unavailable: 'Docker运行环境不可用',
  relay_poll_failed: 'Gitee任务轮询失败',
  codex_connection_error: 'Codex连接失败',
  codex_auth_error: 'Codex认证失败',
  codex_session_invalid: 'Codex会话失效',
  task_state_unavailable: '任务状态或上传队列采集失败（当前任务及交付状态未知）',
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
  container_failed: '任务容器意外停止或发生 OOM',
  task_no_progress: '任务超过30分钟未记录有效进展（不据此终止任务）',
  task_stalled: '任务超过60分钟无有效进展，等待 Worker 复查',
  task_recovering: '任务正在重试或等待依赖恢复',
  task_recovery_exhausted: '任务恢复预算已耗尽',
});
const CODEX_ERRORS = new Set(['connection_error', 'auth_error', 'session_invalid', 'rate_limited', 'timeout', 'failed']);
const CODEX_OK = new Set(['running', 'succeeded']);
const READ_ERRORS = {timeout: '请求超时', network_error: '网络请求失败', http_error: 'HTTP 请求失败',
  rate_limited: '访问被限流', invalid_document: '健康数据格式无效', identity_mismatch: '快照身份或时间无效',
  authentication_error: '读取凭据无效', configuration_error: '读取凭据未配置'};

const rows = value => Array.isArray(value) ? value : [];
const identity = row => row?.task_id && row?.run_id ? `${row.task_id}:${row.run_id}` : '';
const recovering = new Set(['retry_wait', 'waiting_dependency', 'recovering']);
const publicWord = value => typeof value === 'string' && /^[A-Za-z0-9_.:-]{1,160}$/.test(value) ? value : undefined;
const publicSha = value => typeof value === 'string' && /^[0-9a-f]{40}$/.test(value) ? value : undefined;
const publicRepository = value => typeof value === 'string' && /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(value) ? value : undefined;
const instant = value => Number.isFinite(Date.parse(value));
const connectionFailures = new Set(['connection', 'connection_error']);
const connectionRecovery = row => row?.stage === 'running'
  && ['retry_wait', 'recovering'].includes(row.recovery?.state)
  && connectionFailures.has(row.recovery?.failure_code);
function automaticConnectionRetry(row, now) {
  if (!connectionRecovery(row)) return false;
  const budget = row.budget || {}, used = budget.codex_attempts_used, limit = budget.codex_attempts_limit;
  if (!Number.isInteger(used) || !Number.isInteger(limit) || used > limit
    || used === limit && row.recovery.state !== 'recovering') return false;
  return ['codex_deadline_at', 'recovery_deadline_at'].every(key =>
    !instant(budget[key]) || Date.parse(budget[key]) > now);
}
const connectionRetryEvent = event => event.kind === 'recovery'
  && ['retry_wait', 'recovering'].includes(event.detail?.state)
  && connectionFailures.has(event.detail?.failure_code);
const incidentEvents = (state, suppressConnection, firstSeen = state.first_seen) => rows(state.events)
  .filter(event => Date.parse(event.at) >= Date.parse(firstSeen)
    && !(suppressConnection && connectionRetryEvent(event)));

function mergeEvents(previous, incoming, now) {
  const unique = new Map();
  for (const event of [...rows(previous), ...rows(incoming)]) {
    if (!publicWord(event.id) || !instant(event.at) || Date.parse(event.at) > now
        || now - Date.parse(event.at) > 7 * 86400000) continue;
    const detail = {};
    for (const key of ['state', 'failure_code', 'action', 'outcome', 'phase']) {
      if (publicWord(event.detail?.[key])) detail[key] = event.detail[key];
    }
    if (Number.isInteger(event.detail?.attempt) && event.detail.attempt >= 0) detail.attempt = event.detail.attempt;
    const known = unique.get(event.id);
    unique.set(event.id, { id: event.id, at: event.at, kind: publicWord(event.kind) || 'recovery',
      ...(publicWord(event.task_id) ? { task_id: event.task_id } : {}),
      ...(publicWord(event.run_id) ? { run_id: event.run_id } : {}), detail,
      head_sha: publicSha(event.head_sha) || known?.head_sha,
      repository: publicRepository(event.repository) || known?.repository,
      ...(Array.isArray(event.codes) ? { codes: event.codes.filter(code => LABELS[code]) } : {}),
    });
  }
  const counts = new Map();
  return [...unique.values()].sort((a, b) => Date.parse(b.at) - Date.parse(a.at)).filter(event => {
    if (!event.task_id) return true;
    const count = (counts.get(event.task_id) || 0) + 1;
    counts.set(event.task_id, count);
    return count <= 20;
  }).slice(0, 100);
}

// Missing telemetry never proves recovery. Remember which task/service caused each fault.
function faults(snapshot, previous, references, now) {
  const result = new Set(), detected = new Set(), nextReferences = { ...references };
  const check = (code, bad, known, affected = []) => {
    if (bad) { result.add(code); detected.add(code); }
    else if (!known && previous.includes(code)) result.add(code);
    if (bad && affected.length) nextReferences[code] = [...new Set([...(references[code] || []), ...affected])];
    if (!result.has(code)) delete nextReferences[code];
  };
  const poller = snapshot.poller || {}, runtime = snapshot.runtime || {};
  check('poller_unavailable', poller.alive === false || poller.heartbeat_stale === true,
    typeof poller.alive === 'boolean' && typeof poller.heartbeat_stale === 'boolean');
  check('runtime_unavailable', runtime.available === false, typeof runtime.available === 'boolean');
  check('relay_poll_failed', poller.last_poll_status === 'error', ['error', 'success'].includes(poller.last_poll_status));
  const active = rows(snapshot.tasks).length ? rows(snapshot.tasks) : snapshot.active_task ? [snapshot.active_task] : [];
  const knownTasks = [...active, ...rows(snapshot.recent_tasks)];
  const taskKnown = (code, predicate) => snapshot.tasks_available === false ? false : references[code]?.length
    ? references[code].every(id => knownTasks.some(row => identity(row) === id && predicate(row)))
    : knownTasks.some(predicate);
  const endedFailed = row => row.stage === 'published' && row.result_status === 'infra_error';
  const completed = row => row.recovery?.state === 'recovered'
    || ['published', 'publish_pending', 'sealing'].includes(row.stage) && ['pass', 'fail', 'cancelled'].includes(row.result_status);
  for (const error of CODEX_ERRORS) {
    const code = `codex_${error}`;
    const managed = row => error === 'connection_error' && (automaticConnectionRetry(row, now)
      || row.codex_status === 'connection_error' && row.recovery?.state === 'exhausted');
    const bad = active.filter(row => row.codex_status === error && !managed(row));
    check(code, bad.length > 0, taskKnown(code, row => CODEX_OK.has(row.codex_status) || row.recovery?.state === 'recovered'
      || managed(row) || ['sealing', 'publish_pending', 'published'].includes(row.stage)
        && ['pass', 'fail'].includes(row.result_status)), bad.map(identity).filter(Boolean));
  }
  const taskChecks = [
    ['task_recovering', row => recovering.has(row.recovery?.state)
      && !automaticConnectionRetry(row, now) && row.codex_status !== 'connection_error',
    row => automaticConnectionRetry(row, now) || row.codex_status === 'connection_error'
      || ['normal', 'recovered', 'exhausted'].includes(row.recovery?.state) || completed(row) || endedFailed(row)],
    ['task_recovery_exhausted', row => row.recovery?.state === 'exhausted', row => row.recovery?.state === 'recovered' || endedFailed(row)],
    ['task_no_progress', row => row.stage === 'running' && instant(row.last_progress_at)
      && now - Date.parse(row.last_progress_at) > (snapshot.thresholds?.progress_warning_seconds || 1800) * 1000,
    row => instant(row.last_progress_at) && now - Date.parse(row.last_progress_at) <= (snapshot.thresholds?.progress_warning_seconds || 1800) * 1000 || completed(row) || endedFailed(row)],
    ['task_stalled', row => row.stage === 'running' && instant(row.last_progress_at)
      && now - Date.parse(row.last_progress_at) > (snapshot.thresholds?.progress_stalled_seconds || 3600) * 1000,
    row => instant(row.last_progress_at) && now - Date.parse(row.last_progress_at) <= (snapshot.thresholds?.progress_stalled_seconds || 3600) * 1000 || completed(row) || endedFailed(row)],
  ];
  for (const [code, bad, healthy] of taskChecks) {
    const affected = active.filter(bad);
    check(code, affected.length > 0, taskKnown(code, healthy), affected.map(identity).filter(Boolean));
  }
  const containers = rows(snapshot.task_containers);
  const badContainers = containers.filter(row => row.oom_killed === true || row.expected_running === true
    && (row.status === 'missing' || row.available === true && row.running === false));
  const containerKnown = snapshot.tasks_available !== false && (references.container_failed || []).every(id => containers.some(row => identity(row) === id
    && row.available === true && row.running === true && row.oom_killed === false) || knownTasks.some(row => identity(row) === id && (completed(row) || endedFailed(row))));
  check('container_failed', badContainers.length > 0, containerKnown, badContainers.map(identity).filter(Boolean));
  check('task_state_unavailable', snapshot.tasks_available === false || snapshot.uploads_available === false,
    snapshot.tasks_available === true && snapshot.uploads_available === true);
  const uploads = snapshot.uploads;
  check('delivery_pending', Array.isArray(uploads) && uploads.some(row => instant(row.queued_at)
    && now - Date.parse(row.queued_at) > (snapshot.thresholds?.upload_seconds || 1200) * 1000),
  snapshot.uploads_available !== false && Array.isArray(uploads) && uploads.every(row => instant(row.queued_at)));
  const environment = snapshot.environments || {};
  check('environment_unavailable', environment.unavailable === true, typeof environment.unavailable === 'boolean');
  const update = snapshot.control_update?.state;
  for (const state of ['failed', 'invalid', 'blocked']) {
    check(`control_update_${state}`, update === state, ['idle', 'pending', 'updating', 'failed', 'invalid', 'blocked'].includes(update));
  }
  const services = rows(snapshot.services);
  const knownService = row => row.available === true && ['active', 'inactive', 'failed', 'activating'].includes(row.active_state);
  const badService = row => row.available === true && (row.active_state === 'failed'
    || row.active_state === 'inactive' && (row.name?.endsWith('.timer') || row.type && row.type !== 'oneshot'
      || row.name === 'triton-anchor-local-ci.service')
    || row.result && row.result !== 'success');
  const badServices = services.filter(badService);
  check('service_failed', badServices.length > 0, services.length > 0 && services.every(knownService)
    && (references.service_failed || []).every(name => services.some(row => row.name === name && knownService(row))), badServices.map(row => row.name));
  const storage = snapshot.storage;
  check('disk_space_low', Array.isArray(storage) && storage.some(row =>
    typeof row.filesystem_free_bytes === 'number' && row.filesystem_free_bytes < CONFIG.diskFreeBytes),
  Array.isArray(storage) && storage.length > 0 && storage.every(row => typeof row.filesystem_free_bytes === 'number'));
  const finishedFailed = knownTasks.some(row => endedFailed(row) && Object.entries(references)
    .some(([code, ids]) => (code.startsWith('task_') || code === 'container_failed') && ids.includes(identity(row))));
  return { codes: [...result].sort(), detected: [...detected], references: nextReferences, finishedFailed };
}

async function readDocument(fetcher, token, filename, branch, schema, read) {
  if (!token) {
    read.error_code = 'configuration_error';
    throw new Error('GITEE_TOKEN is not configured');
  }
  const ref = encodeURIComponent(branch);
  const response = await fetcher(`${API}/repos/${CONFIG.owner}/${CONFIG.repository}/contents/${filename}?ref=${ref}`, {
    headers: { Accept: 'application/json', Authorization: `Bearer ${token}` }, signal: AbortSignal.timeout(15000),
  });
  read.http_status = response.status;
  if (!response.ok) {
    read.error_code = 'http_error';
    if (response.status === 401) read.error_code = 'authentication_error';
    // Only a specific rate-limit response proves that a 403 is throttling.
    else if (response.status === 429 || response.status === 403
        && /rate limit exceeded/i.test((await response.text()).slice(0, 4096))) read.error_code = 'rate_limited';
    throw new Error('Health data unavailable');
  }
  const body = await response.text();
  read.error_code = 'invalid_document';
  const envelope = JSON.parse(body);
  if (envelope.encoding !== 'base64' || typeof envelope.content !== 'string') throw new Error('Invalid health document');
  const bytes = Uint8Array.from(atob(envelope.content.replace(/\s/g, '')), character => character.charCodeAt(0));
  const value = JSON.parse(new TextDecoder().decode(bytes));
  if (value.schema !== schema) throw new Error('Invalid health document');
  return value;
}

async function snapshot(fetcher, token, now, read) {
  const value = await readDocument(fetcher, token, 'worker-health.json', `snapshot/${CONFIG.worker}`, 'triton-anchor-worker-health', read);
  read.error_code = 'identity_mismatch';
  if (value.worker_id !== CONFIG.worker
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
  let incomplete = false;
  for (const state of ['open', 'progressing']) {
    for (let page = 1; page <= ISSUE_SEARCH_MAX_PAGES; page++) {
      const rows = await issueRequest(fetcher, token,
        `/repos/${CONFIG.owner}/${CONFIG.repository}/issues?state=${state}&sort=updated&direction=desc&per_page=${ISSUE_SEARCH_PAGE_SIZE}&page=${page}`);
      if (!Array.isArray(rows)) throw new Error('Invalid Issue list');
      const found = rows.find(row => ['open', 'progressing'].includes(row.state)
        && typeof row.body === 'string' && row.body.includes(MARKER));
      if (found) return found;
      if (rows.length < ISSUE_SEARCH_PAGE_SIZE) break;
      if (page === ISSUE_SEARCH_MAX_PAGES) incomplete = true;
    }
  }
  // A truncated search cannot prove absence after a lost Issue creation response.
  if (incomplete) throw new Error('Active Issue search reached pagination limit; refusing duplicate creation');
  return null;
}

const EVENT_LABELS = {
  normal: '正常执行', retry_wait: '等待重试', waiting_dependency: '等待依赖恢复', recovering: '恢复中',
  recovered: '恢复成功', exhausted: '恢复预算耗尽', pending: '进行中', success: '恢复成功', failed: '恢复失败',
  resume: '复用原 session', resume_codex: '复用原 session', new_session: '创建新 session', new_codex_session: '创建新 session',
  rebuild: '重建隔离环境', rebuild_execution: '重建隔离环境', wait_dependency: '等待依赖',
  wait_credentials: '等待凭据更新', wait_runtime: '等待 Docker 恢复', retry_sealing: '重新封存',
  retry_publish: '重传已封存结果', publish_infra_error: '发布基础设施失败结果', continue_sealing: '继续封存', configuration_invalid: '任务配置无效', delivery_failed: '结果上传失败', container_oom: '任务容器内存不足（OOM）',
  preparing: '准备环境', running: '执行中', sealing: '封存结果', publish_pending: '等待上传', published: '已发布',
};
function eventText(event) {
  if (event.codes?.length) return (event.kind === 'recovered' ? '确认恢复：' : event.kind === 'finished_failed' ? '恢复失败，任务已结束：' : '异常：')
    + event.codes.map(code => LABELS[code]).join('；');
  const detail = event.detail || {};
  const label = value => value === 'unknown' ? '' : EVENT_LABELS[value] || LABELS[value] || LABELS[`codex_${value}`] || value;
  return [...new Set([event.task_id && `任务 ${event.task_id}`, label(detail.phase), label(detail.state), label(detail.failure_code),
    label(detail.action), Number.isInteger(detail.attempt) && `第 ${detail.attempt} 次`, label(detail.outcome)].filter(Boolean))].join(' · ');
}

function beijingTime(value) {
  if (!instant(value)) return '尚未取得';
  // Keep the offset parseable when recovering an incident from its Issue body.
  return new Date(Date.parse(value) + 8 * 3600000).toISOString().replace('Z', '+08:00');
}

function issueBody(state, at, recovered = false, suppressConnection = false) {
  const lines = [MARKER, `服务器：${CONFIG.worker}`, '以下时间均为北京时间（UTC+8）。', '',
    `首次发现：${beijingTime(state.first_seen)}`, `本次观测：${beijingTime(at)}`,
    `故障证据快照：${beijingTime(state.fault_snapshot_at)}`, ''];
  const minutes = Math.max(0, Math.round((Date.parse(at) - Date.parse(state.first_seen)) / 60000));
  lines.push(`持续时间：约 ${minutes} 分钟。`);
  if (recovered) lines.push(`恢复时间：${beijingTime(at)}`, `恢复证据快照：${beijingTime(state.last_source_at)}`,
    state.finished_failed ? '任务恢复失败，已发布基础设施失败结果并结束；这不表示该任务恢复成功。'
      : '更新且有效的健康快照确认此前异常已结束。', '', '此前异常：');
  else lines.push('当前异常（缺少明确恢复证据的原有异常继续保留）：');
  for (const code of state.codes) lines.push(`- ${LABELS[code]}`);
  if (!recovered && state.codes.includes('source_unreadable') && state.health_read?.status === 'error') {
    const read = state.health_read;
    lines.push('', `Cloudflare → Gitee 健康快照读取：${READ_ERRORS[read.error_code] || '错误详情未上报'}`
      + (read.http_status ? `（HTTP ${read.http_status}）` : '') + `；耗时 ${read.duration_ms} ms。`,
    `连续失败：${read.consecutive_failures} 次；最近成功读取：${beijingTime(read.last_success_at)}。`);
  }
  const events = incidentEvents(state, suppressConnection).slice(0, 20).reverse();
  if (events.length) lines.push('', '近期异常与恢复过程：', ...events.map(event => `- ${beijingTime(event.at)} · ${eventText(event)}`));
  lines.push('', '仅依据公开健康快照；Cloudflare 不执行服务器恢复。原始错误、认证信息和服务器日志不在此发布。');
  return lines.join('\n');
}

function resetIncident(state) {
  state.first_seen = null;
  state.issue_number = null;
  state.signature = '';
  state.codes = [];
  state.references = {};
  state.pending_close = null;
  state.needs_reopen = false;
  state.fault_snapshot_at = null;
  state.finished_failed = false;
}

async function runMonitor(env, { fetcher, now, current, read }) {
  const state = await env.ALERT_STATE.get(KEY, 'json') || {
    first_seen: null, issue_number: null, signature: '', codes: [], read_failures: 0,
  };
  state.references ||= {};
  const at = now.toISOString(), timestamp = now.getTime();
  const patch = (body, extra = {}) => issueRequest(fetcher, env.GITEE_TOKEN,
    `/repos/${CONFIG.owner}/issues/${encodeURIComponent(state.issue_number)}`, 'PATCH',
    { repo: CONFIG.repository, body, ...extra });
  const record = (kind, codes) => {
    const id = `cf:${at}:${kind}`;
    state.events = mergeEvents(state.events, [{ id, at, kind, codes }], timestamp);
  };
  try {
    state.read_failures = current ? 0 : state.read_failures + 1;
    state.health_read = { ...read, consecutive_failures: state.read_failures,
      last_success_at: current ? at : state.health_read?.last_success_at || null };
    if (!state.issue_number) {
      const existing = await findOpenIssue(fetcher, env.GITEE_TOKEN);
      if (existing) {
        state.issue_number = String(existing.number);
        const first = existing.body.match(/^首次发现：(.+)$/m)?.[1] || existing.created_at;
        state.first_seen ||= instant(first) ? new Date(first).toISOString() : at;
        const evidence = existing.body.match(/^故障证据快照：(.+)$/m)?.[1];
        state.fault_snapshot_at ||= instant(evidence) ? evidence : state.first_seen;
        if (!state.codes.length) {
          state.codes = Object.keys(LABELS).filter(code => existing.body.split('\n').includes(`- ${LABELS[code]}`)).sort();
        }
      }
    }
    const sourceAt = Date.parse(current?.collected_at);
    const accepted = current && (!instant(state.last_source_at) || sourceAt >= Date.parse(state.last_source_at));
    const fresh = accepted && timestamp - sourceAt <= CONFIG.staleMs;
    const prior = [...state.codes];
    let codes = [...prior];
    if (accepted) {
      state.last_source_at = current.collected_at;
      state.events = mergeEvents(state.events, current.events, timestamp);
      if (fresh) {
        const observed = faults(current, prior, state.references, timestamp);
        codes = observed.codes;
        const faultAt = Date.parse(state.fault_snapshot_at || state.first_seen);
        if (Number.isFinite(faultAt) && sourceAt <= faultAt) {
          codes = [...new Set([...codes, ...prior])];
          for (const code of prior) if (state.references[code]) observed.references[code] = state.references[code];
        }
        if (observed.detected.length) state.fault_snapshot_at = current.collected_at;
        state.references = observed.references;
        state.finished_failed ||= observed.finishedFailed;
      } else codes = [...new Set([...prior.filter(code => code !== 'source_unreadable'), 'snapshot_stale'])];
    }
    if (!current && state.read_failures >= 2 && !codes.includes('source_unreadable')) codes.push('source_unreadable');
    codes.sort();
    state.events = mergeEvents(state.events, [], timestamp);
    // Never retry a remembered close before observing this round's evidence.
    const wasClosing = !!state.pending_close || state.needs_reopen;
    state.pending_close = null;
    if (codes.length) {
      if (wasClosing) state.needs_reopen = true;
      state.first_seen ||= at;
      state.codes = codes;
      if (codes.join(',') !== prior.join(',')) record('fault', codes);
      const suppressConnection = !codes.includes('codex_connection_error') && !codes.includes('task_recovering');
      const recoveryEvents = incidentEvents(state, suppressConnection)
        .filter(event => event.kind !== 'phase' && event.kind !== 'progress');
      const signature = JSON.stringify([codes, recoveryEvents.map(event => event.id),
        ...(codes.includes('source_unreadable') ? [[read.error_code, read.http_status]] : [])]);
      const title = `[Local CI 告警] ${CONFIG.worker} ${codes.length}项异常`;
      if (!state.issue_number) {
        const created = await issueRequest(fetcher, env.GITEE_TOKEN, `/repos/${CONFIG.owner}/issues`, 'POST', {
          repo: CONFIG.repository, title, body: issueBody(state, at, false, suppressConnection),
        });
        if (!created.number) throw new Error('Issue creation returned no number');
        state.issue_number = String(created.number);
        state.signature = signature;
      }
      if (state.signature !== signature || wasClosing) {
        await patch(issueBody(state, at, false, suppressConnection), { title, ...(wasClosing ? {state: 'open'} : {}) });
        state.signature = signature;
        state.needs_reopen = false;
      }
    } else if (state.issue_number && fresh) {
      state.pending_close = current.collected_at;
      await patch(issueBody(state, at, true, true), { state: 'closed' });
      record(state.finished_failed ? 'finished_failed' : 'recovered', prior);
      resetIncident(state);
    } else if (!state.issue_number) resetIncident(state);
  } finally {
    // Single writer / single cron: one private-state write and one cache write per run.
    await env.ALERT_STATE.put(KEY, JSON.stringify(state));
  }
}

async function refreshCache(env, { fetcher, now, current }) {
  const previous = await env.ALERT_STATE.get(CACHE_KEY, 'json');
  const state = await env.ALERT_STATE.get(KEY, 'json');
  const older = current && Date.parse(current.collected_at) < Date.parse(previous?.worker?.collected_at);
  const cache = {
    schema: 'triton-anchor-worker-health-cache', worker_id: CONFIG.worker,
    updated_at: now.toISOString(), worker: current && !older ? current : previous?.worker || null,
    events: mergeEvents(state?.events, [], now.getTime()), alerts: previous?.alerts || [],
    health_read: state?.health_read,
    errors: { worker: !current ? 'Cloudflare 未能读取 Gitee 健康快照' : older ? 'Gitee 返回较旧快照，保留最后已知数据' : '', alerts: '' },
  };
  try {
    const alerts = await issueRequest(fetcher, env.GITEE_TOKEN,
      `/repos/${CONFIG.owner}/${CONFIG.repository}/issues?state=all&sort=updated&direction=desc&per_page=50`);
    if (!Array.isArray(alerts)) throw new Error('Invalid Issue list');
    cache.alerts = alerts.filter(row => typeof row.body === 'string' && row.body.includes(MARKER)
      && /^[A-Za-z0-9]+$/.test(String(row.number))).slice(0, 10).map(row => ({
      title: String(row.title || 'Local CI 告警'), state: row.state, updated_at: row.updated_at,
      url: `https://gitee.com/${CONFIG.owner}/${CONFIG.repository}/issues/${row.number}`,
    }));
  } catch { cache.errors.alerts = 'Cloudflare 未能读取 Gitee 告警记录'; }
  await env.ALERT_STATE.put(CACHE_KEY, JSON.stringify(cache));
}

async function runScheduled(env, now) {
  const fetcher = fetch;
  const started = Date.now();
  const read = { status: 'error', error_code: 'network_error', http_status: null,
    duration_ms: 0, checked_at: now.toISOString() };
  let current = null;
  try {
    current = await snapshot(fetcher, env.GITEE_TOKEN, now.getTime(), read);
    read.status = 'ok';
    read.error_code = null;
  } catch (error) {
    if (['TimeoutError', 'AbortError'].includes(error?.name)) read.error_code = 'timeout';
  }
  read.duration_ms = Math.max(0, Date.now() - started);
  if (!current) console.warn(JSON.stringify({ event: 'health_read_failed', ...read }));
  try {
    await runMonitor(env, { fetcher, now, current, read });
  } finally {
    // Preserve display updates when Issue delivery fails, and cache the latest Issue state.
    await refreshCache(env, { fetcher, now, current });
  }
}

export default {
  scheduled(event, env, ctx) { ctx.waitUntil(runScheduled(env, new Date(event.scheduledTime))); },
  async fetch(request, env) {
    if (new URL(request.url).pathname !== '/health') return new Response('Not found', { status: 404 });
    const headers = { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Methods': 'GET, OPTIONS' };
    if (request.method === 'OPTIONS') return new Response(null, { status: 204, headers });
    if (request.method !== 'GET') return new Response('Method not allowed', { status: 405, headers });
    try {
      const cache = await env.ALERT_STATE.get(CACHE_KEY, 'json');
      if (cache) return Response.json(cache, { headers: { ...headers, 'Cache-Control': 'public, max-age=60' } });
    } catch { /* A cache outage does not imply a server outage. */ }
    return Response.json({ error: 'Health cache unavailable' }, {
      status: 503, headers: { ...headers, 'Cache-Control': 'no-store' },
    });
  },
};
