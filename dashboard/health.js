/* Public Gitee health snapshots with a Cloudflare cache fallback. */
(function () {
  const source = {
    repository: 'likehupochuan/triton-anchor-worker-health',
    worker: 'jiwang-ci-race-1',
    staleSeconds: 1200,
    refreshMs: 300000,
    cacheUrl: 'https://local-ci-alert.2272640910.workers.dev/health',
  };
  const incidents = {
    poller_unavailable: ['服务异常', 'Worker 已停止或心跳异常'],
    runtime_unavailable: ['服务异常', 'Docker 容器运行环境不可用'],
    relay_poll_failed: ['中转访问异常', '服务器访问 Gitee 失败；可能涉及网络、认证或仓库访问'],
    environment_unavailable: ['环境异常', '任务环境不可用'],
    environment_update_failed: ['环境异常', '环境更新或验证失败'],
    disk_space_low: ['资源异常', '可用磁盘空间不足 5 GiB'],
    delivery_pending: ['结果交付异常', '结果已等待上传超过 20 分钟；具体原因需查看服务器日志'],
    task_no_progress: ['任务进展提示', '超过 30 分钟未记录新进展；不能据此认定进程卡死'],
    task_stalled: ['任务进展提示', '超过 60 分钟无新进展，等待 Worker 复查；不自动终止静默任务'],
    container_failed: ['容器异常', '任务容器意外停止、丢失或发生 OOM'],
    task_recovering: ['恢复中', '任务正在重试或等待依赖'],
    task_recovery_exhausted: ['恢复结束', '恢复预算已耗尽，请查看任务最终结果'],
    task_state_unavailable: ['状态采集异常', '任务状态或上传队列读取失败；当前任务及交付状态未知'],
    snapshot_stale: ['心跳过期', '服务器健康快照已过期；宕机、断网或采集服务故障均可能导致'],
  };
  const codexStates = {
    starting: ['启动中', 'info'], running: ['执行中', 'good'], retrying: ['恢复重试中', 'warn'],
    connection_error: ['模型连接异常', 'bad'], auth_error: ['模型认证失败', 'bad'], session_invalid: ['会话失效', 'warn'],
    rate_limited: ['模型服务限流', 'warn'], failed: ['执行失败，原因待确认', 'bad'],
    timeout: ['执行超时', 'bad'], succeeded: ['已完成', 'good'], cancelled: ['已取消', 'info'],
  };
  const serviceNames = {
    'triton-anchor-local-ci.service': 'Worker',
    'triton-anchor-local-ci-health.timer': '健康采集定时器',
    'triton-anchor-local-ci-health.service': '健康采集服务',
    'triton-anchor-local-ci-control-update.service': '控制代码更新',
    'docker.service': 'Rootless Docker',
  };
  const serviceStates = {active: '运行中', inactive: '未运行', failed: '失败', activating: '启动中'};
  const controlUpdateStates = {
    failed: '控制代码更新执行失败；具体原因请查看更新服务日志',
    invalid: '控制代码更新请求无效；具体原因请查看 Worker 日志',
    blocked: '控制代码更新请求受阻（尚未执行更新）；具体原因请查看 Worker 日志',
  };
  const rows = value => Array.isArray(value) ? value : [];
  const age = (value, now) => (now - Date.parse(value)) / 1000;
  const fresh = (value, now) => age(value, now) >= -60 && age(value, now) <= source.staleSeconds;
  const date = value => Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('zh-CN', {hour12: false}) : '未上报';

  function assess(worker, events = [], {now = Date.now(), workerError = ''} = {}) {
    const issues = [], seen = new Set();
    const add = (code, category, text, tone = 'bad') => {
      if (!seen.has(code)) { issues.push({code, category, text, tone}); seen.add(code); }
    };
    const incident = code => { if (incidents[code]) add(code, ...incidents[code]); };
    const current = !!worker && !workerError && fresh(worker.collected_at, now);
    const poller = worker?.poller || {}, active = worker?.active_task;
    if (workerError || !worker) add('health_read', '数据读取异常', workerError || '尚未取得健康快照', 'warn');
    else if (!current) add('snapshot_stale', ...incidents.snapshot_stale, 'warn');
    const cards = [
      {name: 'Worker 服务', text: '状态未知', tone: 'muted'},
      {name: 'Docker 服务', text: '状态未知', tone: 'muted'},
      {name: 'Gitee 中转', text: '状态未知', tone: 'muted'},
      {name: 'Codex', text: '状态未知', tone: 'muted'},
    ];
    if (current) {
      // The five-minute collector already measures the three-minute Worker heartbeat.
      const workerOk = poller.alive === true && poller.heartbeat_stale === false && age(poller.heartbeat_at, Date.parse(worker.collected_at)) <= 180;
      const workerBad = poller.alive === false || poller.heartbeat_stale === true;
      cards[0] = {name: 'Worker 服务', text: workerOk ? (active ? '处理任务中' : '运行中') : workerBad ? '已停止 / 心跳异常' : '未上报', tone: workerOk ? 'good' : workerBad ? 'bad' : 'muted'};
      if (workerBad) incident('poller_unavailable');
      const runtimeOk = worker.runtime?.available;
      cards[1] = {name: 'Docker 服务', text: runtimeOk === true ? '可用' : runtimeOk === false ? '不可用' : '未上报', tone: runtimeOk === true ? 'good' : runtimeOk === false ? 'bad' : 'muted'};
      if (runtimeOk === false) incident('runtime_unavailable');
      const pollStatus = poller.last_poll_status;
      cards[2] = {name: 'Gitee 中转', text: pollStatus === 'error' ? '访问失败' : pollStatus === 'success' ? '最近轮询成功' : '未上报', tone: pollStatus === 'error' ? 'bad' : pollStatus === 'success' ? 'good' : 'muted'};
      if (pollStatus === 'error') incident('relay_poll_failed');
      const codex = codexStates[active?.codex_status];
      const codexIdle = !active ? '空闲' : active.stage === 'preparing' ? '准备任务环境' : ['sealing', 'publish_pending', 'published'].includes(active.stage) ? '任务已结束' : '连接状态未上报';
      cards[3] = {name: 'Codex', text: codex?.[0] || codexIdle, tone: codex?.[1] || 'muted'};
      if (codex && ['bad', 'warn'].includes(codex[1]) && active.codex_status !== 'retrying')
        add('codex_' + active.codex_status, 'Codex 异常', codex[0], codex[1]);
      else if (active?.codex_alive === false && active.codex_status === 'running')
        add('codex_process', 'Codex 异常', 'Codex 进程已退出，等待 Worker 处理');
      if (worker.environments?.unavailable) incident('environment_unavailable');
      if (rows(worker.images).some(row => row.state === 'failed')) incident('environment_update_failed');
      if (rows(worker.storage).some(row => typeof row.filesystem_free_bytes === 'number' && row.filesystem_free_bytes < 5 * 1024 ** 3)) incident('disk_space_low');
      if (worker.workspaces?.pause_intake) add('pause_intake', '资源异常', '服务器已暂停接收新任务，请检查磁盘和结果存储预算', 'warn');
      if (worker.workspaces?.status === 'error') add('workspace_error', '清理异常', '任务空间或结果保留清理失败');
      if (worker.tasks_available === false || worker.uploads_available === false)
        add('task_state_unavailable', ...incidents.task_state_unavailable, 'warn');
      if (rows(worker.uploads).some(row => age(row.queued_at, now) > (worker.thresholds?.upload_seconds || 1200))) incident('delivery_pending');
      for (const task of rows(worker.tasks).length ? rows(worker.tasks) : active ? [active] : []) {
        if (task.stage === 'running' && age(task.last_progress_at, now) > (worker.thresholds?.progress_warning_seconds || 1800))
          add('task_no_progress', ...incidents.task_no_progress, 'warn');
        if (task.stage === 'running' && age(task.last_progress_at, now) > (worker.thresholds?.progress_stalled_seconds || 3600))
          add('task_stalled', ...incidents.task_stalled, 'warn');
        if (['recovering', 'retry_wait', 'waiting_dependency'].includes(task.recovery?.state))
          add('task_recovering', ...incidents.task_recovering, 'warn');
        if (task.recovery?.state === 'exhausted') incident('task_recovery_exhausted');
      }
      if (rows(worker.task_containers).some(row => row.oom_killed === true || row.expected_running === true
        && (row.status === 'missing' || row.available === true && row.running === false))) incident('container_failed');
      for (const service of rows(worker.services)) {
        const persistent = service.name?.endsWith('.timer') || service.name === 'triton-anchor-local-ci.service'
          || service.type && service.type !== 'oneshot';
        if (service.available === true && (service.active_state === 'failed' || persistent && service.active_state === 'inactive'
          || service.result && service.result !== 'success'))
          add('service_' + service.name, '服务异常', (serviceNames[service.name] || service.name) + '：' + serviceStates[service.active_state]);
      }
      const controlUpdate = controlUpdateStates[worker.control_update?.state];
      if (controlUpdate) add('control_update', '控制更新异常', controlUpdate);
    }
    const history = historyEvents([...rows(worker?.events), ...rows(events)], now);
    return {current, cards, issues, history,
      title: !worker || workerError ? '健康数据不可用' : !current ? '心跳快照已过期' : issues.length ? '发现异常或待确认项' : '已上报状态正常',
      tone: issues.some(row => row.tone === 'bad') ? 'bad' : issues.length ? 'warn' : 'good'};
  }

  async function readGitee(url, message) {
    const response = await fetch(url, {credentials: 'omit', cache: 'no-store', signal: AbortSignal.timeout(15000)});
    if (!response.ok) {
      const limited = response.status === 429 || (response.status === 403 && /Rate Limit Exceeded/i.test(await response.text()));
      const error = new Error(limited ? 'Gitee 访问被限流，暂停请求至少 15 分钟后自动重试' : message + '（HTTP ' + response.status + '）');
      error.rateLimited = limited;
      throw error;
    }
    return response.json();
  }

  async function readSnapshot(filename, branch, schema) {
    const url = 'https://gitee.com/api/v5/repos/' + source.repository + '/contents/' + filename + '?ref=' + encodeURIComponent(branch);
    const envelope = await readGitee(url, 'Gitee 健康数据读取失败');
    const value = envelope.encoding === 'base64'
      ? JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(envelope.content.replace(/\s/g, '')), char => char.charCodeAt(0))))
      : envelope;
    if (value.schema !== schema || (filename === 'worker-health.json' && value.worker_id !== source.worker))
      throw new Error('健康数据格式或服务器身份不匹配');
    return value;
  }

  async function readAlerts() {
    const values = await readGitee('https://gitee.com/api/v5/repos/' + source.repository + '/issues?state=all&sort=updated&direction=desc&per_page=50',
      'Gitee 告警列表读取失败');
    if (!Array.isArray(values)) throw new Error('Gitee 告警列表格式不正确');
    return values.filter(row => typeof row.body === 'string' && row.body.includes('<!-- local-ci-alert:' + source.worker + ' -->')
      && /^[A-Za-z0-9]+$/.test(String(row.number))).slice(0, 10).map(row => ({
        title: String(row.title || 'Local CI 告警'), state: row.state, updated_at: row.updated_at,
        url: 'https://gitee.com/' + source.repository + '/issues/' + row.number,
      }));
  }

  async function readCache() {
    const response = await fetch(source.cacheUrl, {credentials: 'omit', signal: AbortSignal.timeout(15000)});
    if (!response.ok) throw new Error('Cloudflare 备用数据读取失败（HTTP ' + response.status + '）');
    const cache = await response.json();
    if (cache.schema !== 'triton-anchor-worker-health-cache' || cache.worker_id !== source.worker
      || !Number.isFinite(Date.parse(cache.updated_at))) throw new Error('Cloudflare 缓存格式或服务器身份不匹配');
    return cache;
  }

  async function readHealth(retryAt = 0, previous = {}) {
    const cooling = Date.now() < retryAt;
    // Gitee remains the primary snapshot source. The read-only cache also supplies external incidents.
    const [gitee, cached] = await Promise.all([
      cooling ? Promise.resolve(Array.from({length: 2}, () => ({status: 'rejected', reason: new Error('Gitee 限流冷却中')})))
        : Promise.allSettled([
          readSnapshot('worker-health.json', 'snapshot/' + source.worker, 'triton-anchor-worker-health'), readAlerts(),
        ]),
      readCache().then(value => ({value}), error => ({error})),
    ]);
    let results = gitee, notice = '';
    if (results.some(result => result.status === 'rejected' && result.reason?.rateLimited))
      retryAt = Date.now() + 15 * 60 * 1000;
    const cache = cached.value;
    if (results.some(result => result.status === 'rejected')) {
      if (cache) {
        const stale = !fresh(cache.updated_at, Date.now()), names = ['worker', 'alerts'], used = [];
        results = results.map((result, index) => {
          const name = names[index], value = cache[name];
          if (result.status === 'fulfilled' || value == null) return result;
          used.push(index === 0 ? '健康快照' : '告警记录');
          return {status: 'fulfilled', value,
            error: cache.errors?.[name] || (stale ? 'Cloudflare 缓存已过期，当前状态无法确认' : '')};
        });
        notice = used.length ? 'Gitee 读取未成功，已使用 Cloudflare 备用缓存：' + used.join('、')
          + '。缓存更新：' + date(cache.updated_at) : 'Cloudflare 尚无可用的备用数据。';
      } else notice = 'Cloudflare 备用数据也无法读取；保留最后读取的数据，当前状态待确认。';
    }
    if (results[0].status === 'fulfilled' && Date.parse(results[0].value?.collected_at) < Date.parse(previous.worker?.collected_at))
      results[0] = {status: 'fulfilled', value: previous.worker, error: '来源返回较旧快照，保留较新数据，当前状态待确认'};
    return {results, retryAt, notice, monitor: cache ? {updated_at: cache.updated_at, events: rows(cache.events),
      error: fresh(cache.updated_at, Date.now()) ? '' : '外部监测缓存已过期'} : {error: '外部监测记录暂不可用'}};
  }

  const stages = {preparing:'准备环境', running:'执行中', sealing:'汇总结果', publish_pending:'等待上传', published:'已发布'};
  const recoveryStates = {normal:'正常执行', retry_wait:'等待重试', waiting_dependency:'等待依赖恢复', recovering:'恢复中', recovered:'恢复成功', exhausted:'恢复预算耗尽', unknown:'未上报'};
  const recoveryActions = {resume_codex:'复用原 session', new_codex_session:'创建新 session', rebuild_execution:'重建隔离环境',
    resume:'复用原 session', new_session:'创建新 session', rebuild:'重建隔离环境',
    defer_wait_dependency:'等待依赖', wait_credentials:'等待凭据更新', wait_runtime:'等待 Docker 恢复',
    publish_infra_error:'发布基础设施失败结果', continue_sealing:'继续封存结果', published:'结果已发布',
    wait_dependency:'等待依赖', retry_sealing:'重新封存', retry_publish:'重传已封存结果', none:'无需恢复', no_retry:'不重试'};
  const failureNames = {connection:'Codex 连接中断', authentication:'Codex 认证失败', rate_limit:'Codex 限流',
    connection_error:'Codex 连接中断', auth_error:'Codex 认证失败', rate_limited:'Codex 限流',
    cli_failed:'Codex 执行异常（未分类）', result_missing:'未生成有效执行报告', recovery_exhausted:'恢复预算耗尽', sealing_failed:'结果封存失败', disk_budget:'磁盘空间不足',
    configuration_invalid:'任务配置无效', delivery_failed:'结果上传失败', container_oom:'任务容器内存不足（OOM）', timeout:'执行超时', session_invalid:'session 无效', container_failed:'任务容器异常', runtime_unavailable:'Docker 不可用',
    budget_exhausted:'恢复预算耗尽', publication_failed:'结果上传失败', interrupted:'执行中断', no_progress:'长时间无进展'};
  const describe = (value, labels) => value ? labels[value] || value : '未上报';
  function taskFacts(task) {
    const budget = task.budget || {}, recovery = task.recovery || {};
    const attempts = (used, limit) => Number.isInteger(used) ? used + ' / ' + (Number.isInteger(limit) ? limit : '未上报') : '未上报';
    return [
      ['任务 ID', task.task_id || '未上报'], ['运行 ID', task.run_id || '未上报'],
      ['执行阶段', describe(task.stage, stages)], ['恢复状态', describe(recovery.state, recoveryStates)],
      ['异常原因', describe(recovery.failure_code, failureNames)], ['恢复动作', describe(recovery.action, recoveryActions)],
      ['Codex 尝试', attempts(budget.codex_attempts_used, budget.codex_attempts_limit)],
      ['环境创建', attempts(budget.execution_attempts_used, budget.execution_attempts_limit)],
      ['session 切换', attempts(budget.session_switches, budget.session_switches_limit)],
      ['最近有效进展', date(task.last_progress_at)], ['下次重试', date(recovery.next_retry_at)],
      ['执行截止时间', date(budget.codex_deadline_at)], ['恢复截止时间', date(budget.recovery_deadline_at)],
      ['最近恢复', date(recovery.last_recovery_at)], ['恢复结果', describe(recovery.outcome, {success:'成功', recovered:'已恢复', failed:'失败', pending:'进行中'})],
    ];
  }

  function historyEvents(events, now) {
    const unique = new Map();
    for (const row of rows(events)) if (typeof row.id === 'string' && age(row.at, now) >= 0 && age(row.at, now) <= 7 * 86400)
      unique.set(row.id, row);
    const counts = new Map();
    return [...unique.values()].sort((a,b) => Date.parse(b.at) - Date.parse(a.at)).filter(row => {
      if (!row.task_id) return true;
      const count = (counts.get(row.task_id) || 0) + 1; counts.set(row.task_id, count); return count <= 20;
    }).slice(0, 100);
  }
  function eventText(row) {
    const detail = row.detail || {};
    return [...new Set([date(row.at), row.task_id ? '任务 ' + row.task_id.slice(0, 12) : '外部监测',
      row.run_id && row.run_id !== 'unknown' ? '运行 ' + row.run_id : '',
      row.kind === 'recovered' ? '确认恢复' : row.kind === 'fault' ? '发现异常' : row.kind === 'finished_failed' ? '恢复失败，任务已结束' : '',
      rows(row.codes).map(code => incidents[code]?.[1] || failureNames[code] || code).join('；'),
      detail.phase && describe(detail.phase, stages), detail.state && detail.state !== 'unknown' && describe(detail.state, recoveryStates), detail.failure_code && describe(detail.failure_code, failureNames),
      detail.action && describe(detail.action, recoveryActions), Number.isInteger(detail.attempt) ? '第 ' + detail.attempt + ' 次' : '',
      detail.outcome && describe(detail.outcome, {success:'恢复成功', recovered:'恢复成功', failed:'恢复失败', pending:'进行中'}),
    ].filter(Boolean))].join(' · ');
  }

  function groupHistory(events) {
    const groups = [], active = new Map();
    for (const row of [...events].reverse().sort((a,b) => Date.parse(a.at) - Date.parse(b.at))) {
      const detail = row.detail || {}, key = row.task_id && row.run_id && row.task_id !== 'unknown' && row.run_id !== 'unknown'
        && JSON.stringify([row.task_id, row.run_id]);
      if (!key || row.kind !== 'recovery') {
        groups.push({events:[row]});
        if (key && ['published', 'cancelled'].includes(detail.phase)) active.delete(key);
        continue;
      }
      let group = active.get(key);
      if (!group || (detail.failure_code && group.reason && detail.failure_code !== group.reason) || detail.state === 'normal') {
        group = {events:[], reason:detail.failure_code || ''}; groups.push(group); active.set(key, group);
      }
      group.events.push(row);
      group.reason ||= detail.failure_code || '';
      if (['normal', 'recovered', 'exhausted'].includes(detail.state) || ['success', 'recovered', 'failed'].includes(detail.outcome)) active.delete(key);
    }
    return groups.reverse().sort((a,b) => Date.parse(b.events.at(-1).at) - Date.parse(a.events.at(-1).at));
  }

  function mount(root) {
    const node = (tag, className, text) => { const item = document.createElement(tag); if (className) item.className = className; if (text) item.textContent = text; return item; };
    const heading = node('div', 'health-heading'), title = node('div');
    title.append(node('h2', '', '运行概览'), node('p', 'health-muted', source.worker + ' · Gitee 健康快照'));
    const refresh = node('button', 'button secondary', '刷新健康状态'); refresh.type = 'button';
    heading.append(title, refresh);
    const content = node('div'); content.setAttribute('aria-live', 'polite');
    root.append(heading, content);
    let worker = null, workerError = '', loading = false, renderedState = '', monitor = {events: []};
    let showAllEvents = false;
    let alerts = [], alertsError = '', retryAt = 0, sourceNotice = '';

    function render() {
      const cooling = Date.now() < retryAt;
      refresh.disabled = loading;
      refresh.textContent = loading ? '读取中…' : cooling ? '刷新备用数据' : '刷新健康状态';
      if (loading && !worker) { renderedState = ''; content.replaceChildren(node('p', 'health-muted', '正在读取健康数据…')); return; }
      const model = assess(worker, monitor.events, {workerError});
      const viewState = JSON.stringify([model, worker, monitor, alerts, alertsError, sourceNotice, cooling, showAllEvents]);
      if (viewState === renderedState) return;
      renderedState = viewState;
      const expanded = new Set([...content.querySelectorAll('details[open][data-history-key]')].map(item => item.dataset.historyKey));
      content.replaceChildren();
      const summary = node('div', 'health-summary');
      summary.append(node('strong', 'health-badge ' + model.tone, model.title));
      summary.append(node('span', 'health-muted', '采集：' + date(worker?.collected_at) + ' · Worker 心跳：' + date(worker?.poller?.heartbeat_at)));
      content.append(summary);
      if (sourceNotice) content.append(node('p', 'health-muted', sourceNotice));
      if (cooling) content.append(node('p', 'health-muted', 'Gitee 被限流，冷却期间只读取 Cloudflare；' + date(new Date(retryAt).toISOString()) + ' 后重试 Gitee。'));
      const cards = node('div', 'health-cards');
      for (const card of model.cards) {
        const item = node('div', 'health-card');
        item.append(node('span', 'health-muted', card.name), node('strong', 'health-badge ' + card.tone, card.text)); cards.append(item);
      }
      content.append(cards);
      const concerns = node('section', 'health-section');
      concerns.append(node('h3', '', '当前异常'));
      if (model.issues.length) {
        const list = node('ul', 'health-issues');
        for (const issue of model.issues) { const item = node('li'); item.append(node('strong', '', issue.category + '：'), document.createTextNode(issue.text)); list.append(item); }
        concerns.append(list);
      } else concerns.append(node('p', 'health-muted', '未发现已上报的异常。'));
      if (model.current && worker.active_task?.stage === 'running' && !worker.active_task.codex_status)
        concerns.append(node('p', 'health-muted', '当前服务器尚未上报 Codex 连接状态，无法据此判断模型服务是否可用。'));
      content.append(concerns);
      const grid = node('div', 'health-detail-grid');
      const details = node('section', 'health-section');
      details.append(node('h3', '', '服务与资源'));
      if (!model.current) details.append(node('p', 'health-muted', '以下为最后读取的快照，不能确认当前状态。'));
      const facts = node('dl', 'health-facts');
      for (const [label, value] of [
        ['外部监测缓存', date(monitor.updated_at)],
        ['可用磁盘', rows(worker?.storage).map(row => typeof row.filesystem_free_bytes === 'number' ? (row.filesystem_free_bytes / 1024 ** 3).toFixed(1) + ' GiB' : '未上报').join(' / ') || '未上报'],
      ]) facts.append(node('dt', '', label), node('dd', '', value));
      for (const service of rows(worker?.services)) {
        const label = serviceNames[service.name] || service.name;
        const value = !service.available ? '未知（无法查询）' : service.type === 'oneshot' && service.active_state === 'inactive' && (!service.result || service.result === 'success') ? '空闲（按需执行）' : (serviceStates[service.active_state] || '未知') + (service.result && service.result !== 'success' ? ' · ' + service.result : '');
        facts.append(node('dt', '', label), node('dd', '', model.current ? value : value + '（历史快照）'));
      }
      details.append(facts);
      const containers = rows(worker?.task_containers).filter(row => row.expected_running === true
        || rows(worker?.tasks).some(task => task.task_id === row.task_id && task.run_id === row.run_id));
      if (containers.length) {
        details.append(node('h4', '', '任务容器'));
        for (const row of containers) {
          const facts = node('dl', 'health-facts');
          const status = row.status === 'missing' ? '容器已丢失' : row.available !== true ? '未知（无法查询）'
            : row.running === true ? '运行中' : row.running === false ? '已停止' : '未上报';
          for (const [label, value] of [['任务 / 运行', (row.task_id || '未上报') + ' / ' + (row.run_id || '未上报')],
            ['容器状态', status], ['退出码', Number.isInteger(row.exit_code) ? String(row.exit_code) : '未上报'],
            ['内存溢出', typeof row.oom_killed === 'boolean' ? row.oom_killed ? '发生 OOM' : '未发生' : '未上报']])
            facts.append(node('dt', '', label), node('dd', '', value));
          details.append(facts);
        }
      }
      const task = node('section', 'health-section');
      task.append(node('h3', '', '任务执行与恢复'));
      const activeTasks = rows(worker?.tasks).filter(row => !['publish_pending', 'published'].includes(row.stage));
      if (!activeTasks.length && worker?.active_task && !['publish_pending', 'published'].includes(worker.active_task.stage)) activeTasks.push(worker.active_task);
      if (!model.current) task.append(node('p', 'health-muted', '最后快照中的任务，当前状态待确认。'));
      for (const active of activeTasks) {
        const facts = node('dl', 'health-facts');
        for (const [label, value] of taskFacts(active)) facts.append(node('dt', '', label), node('dd', '', value));
        task.append(facts);
      }
      if (!activeTasks.length) task.append(node('p', 'health-muted', worker?.tasks_available === false ? '任务状态采集异常，不能确认当前是否空闲。' : model.current ? '当前没有正在执行的任务。' : '当前任务状态未知。'));
      grid.append(details, task);
      content.append(grid);
      const uploads = node('section', 'health-section');
      uploads.append(node('h3', '', '结果上传等待'), node('p', 'health-muted', '这里只重传已封存结果，不重新构建或测试。'));
      if (!model.current) uploads.append(node('p', 'health-muted', '以下为最后读取的上传状态。'));
      for (const row of rows(worker?.uploads)) {
        const facts = node('dl', 'health-facts health-upload');
        for (const [label, value] of [['任务 / 运行', (row.task_id || '未上报') + ' / ' + (row.run_id || '未上报')],
          ['等待开始', date(row.queued_at)], ['上传尝试', Number.isInteger(row.attempts) ? String(row.attempts) : '未上报'],
          ['下次重传', date(row.next_retry_at)], ['失败原因', describe(row.failure_code, failureNames)]])
          facts.append(node('dt', '', label), node('dd', '', value));
        uploads.append(facts);
      }
      if (!rows(worker?.uploads).length) uploads.append(node('p', 'health-muted', worker?.uploads_available === false ? '上传队列采集异常，不能确认结果是否已交付。' : Array.isArray(worker?.uploads) ? '没有等待上传的结果。' : '上传状态未上报。'));
      content.append(uploads);
      const alertSection = node('section', 'health-section');
      alertSection.append(node('h3', '', 'Cloudflare 告警记录'));
      const allAlerts = node('a', '', '在 Gitee 查看全部 Issues');
      allAlerts.href = 'https://gitee.com/' + source.repository + '/issues';
      alertSection.append(allAlerts);
      if (alertsError) alertSection.append(node('p', 'health-muted', alertsError + '；以下旧记录不能代表当前告警状态。'));
      const alertList = node('ul', 'health-history'), closedList = node('ul', 'health-history');
      for (const alert of alerts) {
        const closed = ['closed', 'rejected'].includes(alert.state);
        const item = node('li'), link = node('a', '', alert.title);
        link.href = alert.url;
        item.append(node('span', 'health-badge ' + (closed ? 'muted' : 'warn'),
          closed ? '已关闭' : '待处理'), document.createTextNode(' '), link,
          node('span', 'health-muted', ' · 更新于 ' + date(alert.updated_at)));
        (closed ? closedList : alertList).append(item);
      }
      alertSection.append(alertList.childElementCount ? alertList : node('p', 'health-muted', alertsError ? '告警记录暂不可用。' : alerts.length ? '已读取的告警中没有未关闭项。' : '暂无已记录的告警；这不表示 Cloudflare 监测已经启用。'));
      if (closedList.childElementCount) {
        const archived = node('details'); archived.dataset.historyKey = 'closed-alerts'; archived.open = expanded.has('closed-alerts');
        archived.append(node('summary', '', '查看已关闭告警（' + closedList.childElementCount + '）'), closedList);
        alertSection.append(archived);
      }
      alertSection.append(node('p', 'health-muted', '显示最近更新的告警。Issue 关闭不等于服务已确认恢复，当前状态以上方健康快照为准。'));
      content.append(alertSection);
      const records = node('section', 'health-section');
      records.append(node('h3', '', '异常与恢复记录（近 7 天）'));
      if (monitor.error) records.append(node('p', 'health-muted', monitor.error + '；服务器上报的记录仍会展示。'));
      const history = node('ul', 'health-history');
      const groups = groupHistory(model.history);
      for (const group of groups.slice(0, showAllEvents ? 100 : 20)) {
        const item = node('li'), latest = group.events.at(-1);
        if (group.events.length === 1) item.textContent = eventText(latest);
        else {
          const details = node('details'), first = group.events[0];
          details.dataset.historyKey = first.id; details.open = expanded.has(first.id);
          const switches = new Set(group.events.filter(row => row.detail?.state === 'recovering'
            && ['new_session', 'new_codex_session'].includes(row.detail.action) && Number.isInteger(row.detail.attempt)).map(row => row.detail.attempt)).size;
          details.append(node('summary', '', eventText(latest)
            + (group.reason && !latest.detail?.failure_code ? ' · 本段原因：' + describe(group.reason, failureNames) : '')
            + (switches ? ' · 已记录新 session 启动 ' + switches + ' 次' : '') + ' · 展开 ' + group.events.length + ' 条过程记录'));
          details.append(node('p', 'health-muted', '运行：' + latest.run_id + ' · 已记录时间：' + date(first.at) + ' — ' + date(latest.at)));
          const entries = node('ul', 'health-history');
          for (const row of [...group.events].reverse()) entries.append(node('li', '', eventText(row)));
          details.append(entries); item.append(details);
        }
        history.append(item);
      }
      records.append(groups.length ? history : node('p', 'health-muted', '近 7 天暂无已读取的异常与恢复记录；旧快照可能尚未上报这些字段。'));
      if (groups.length) records.append(node('p', 'health-muted', '同一运行的连续恢复过程已折叠；仅汇总保留的记录，尝试序号不是本段重试次数。'));
      if (groups.length > 20) {
        const expand = node('button', 'button secondary', showAllEvents ? '收起记录' : '展开全部 ' + groups.length + ' 组记录');
        expand.type = 'button'; expand.addEventListener('click', () => { showAllEvents = !showAllEvents; render(); }); records.append(expand);
      }
      content.append(records, node('p', 'health-muted', '健康快照超过 20 分钟未更新时标记过期；不可读不等于服务器宕机。恢复由服务器执行，Cloudflare 独立管理告警，本页只展示数据。'));

    }

    async function refreshHealth() {
      if (loading) return;
      loading = true;
      render();
      const response = await readHealth(retryAt, {worker}), results = response.results;
      monitor = response.monitor.events ? response.monitor : {...monitor, error: response.monitor.error};
      retryAt = response.retryAt; sourceNotice = response.notice;
      const error = result => result.status === 'fulfilled' ? result.error || '' : result.reason instanceof TypeError || ['TimeoutError', 'AbortError'].includes(result.reason?.name)
        ? '浏览器未能读取 Gitee（可能是网络、跨域限制或请求超时），当前状态无法确认'
        : result.reason?.message || '健康数据读取失败';
      workerError = error(results[0]);
      if (results[0].status === 'fulfilled') worker = results[0].value;
      alertsError = error(results[1]);
      if (results[1].status === 'fulfilled') alerts = results[1].value;
      loading = false; render();
    }
    refresh.addEventListener('click', refreshHealth);
    refreshHealth();
    setInterval(() => { if (!document.hidden) refreshHealth(); }, source.refreshMs);
    // Freshness also changes when no new data arrives or a background tab resumes.
    setInterval(() => { if (!document.hidden) render(); }, 60000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) render(); });
  }
  if (typeof module !== 'undefined') module.exports = {assess, readSnapshot, readAlerts, readHealth, source, taskFacts, historyEvents, eventText};
  if (typeof document !== 'undefined') {
    const root = document.getElementById('serverHealth');
    if (root) mount(root);
  }
})();
