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
    task_no_progress: ['任务进展异常', '超过 30 分钟未记录新进展；不能据此认定进程卡死'],
    snapshot_stale: ['心跳过期', '服务器健康快照已过期；宕机、断网或采集服务故障均可能导致'],
  };
  const codexStates = {
    starting: ['启动中', 'info'], running: ['执行中', 'good'], retrying: ['恢复重试中', 'warn'],
    connection_error: ['模型连接异常', 'bad'], auth_error: ['模型认证失败', 'bad'],
    rate_limited: ['模型服务限流', 'warn'], failed: ['执行失败，原因待确认', 'bad'],
    timeout: ['执行超时', 'bad'], succeeded: ['已完成', 'good'], cancelled: ['已取消', 'info'],
  };
  const serviceNames = {
    'triton-anchor-local-ci.service': 'Worker',
    'triton-anchor-local-ci-health.timer': '健康采集定时器',
    'triton-anchor-local-ci-watchdog.timer': '异常检测定时器',
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

  function assess(worker, watchdog, {now = Date.now(), workerError = '', watchdogError = ''} = {}) {
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
      cards[0] = {name: 'Worker 服务', text: workerOk ? (active ? '处理任务中' : '运行中') : '已停止 / 心跳异常', tone: workerOk ? 'good' : 'bad'};
      if (!workerOk) incident('poller_unavailable');
      const runtimeOk = worker.runtime?.available === true;
      cards[1] = {name: 'Docker 服务', text: runtimeOk ? '可用' : '不可用', tone: runtimeOk ? 'good' : 'bad'};
      if (!runtimeOk) incident('runtime_unavailable');
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
      if (rows(worker.uploads).some(row => age(row.queued_at, now) > 1200)) incident('delivery_pending');
      if (active?.stage === 'running' && age(active.last_progress_at, now) > 1800) incident('task_no_progress');
      for (const service of rows(worker.services)) {
        const persistent = service.name.endsWith('.timer') || service.name === 'triton-anchor-local-ci.service';
        if (!service.available || !serviceStates[service.active_state])
          add('service_unknown_' + service.name, '服务状态待确认', (serviceNames[service.name] || service.name) + '：无法查询当前状态', 'warn');
        if (service.active_state === 'failed' || (persistent && service.available && service.active_state === 'inactive'))
          add('service_' + service.name, '服务异常', (serviceNames[service.name] || service.name) + '：' + serviceStates[service.active_state]);
      }
      const controlUpdate = controlUpdateStates[worker.control_update?.state];
      if (controlUpdate) add('control_update', '控制更新异常', controlUpdate);
    }
    const watchdogFresh = !!watchdog && !watchdogError && fresh(watchdog.updated_at, now);
    if (watchdogError || !watchdog) add('watchdog_read', '监测数据异常', watchdogError || '尚未取得 watchdog 记录', 'warn');
    else if (!watchdogFresh) add('watchdog_stale', '监测数据异常', 'watchdog 记录已过期，不能用其旧结论判断当前健康', 'warn');
    else if (watchdog.source_state === 'unknown') add('watchdog_unknown', '监测数据异常', 'watchdog 最近未能读取 Gitee 健康数据', 'warn');
    // Never let an observation of an older heartbeat override newer service data.
    const observed = rows(watchdog?.worker_health).find(row => row.worker_id === source.worker);
    if (current && watchdogFresh && watchdog.source_state === 'readable' && Date.parse(observed?.collected_at) >= Date.parse(worker.collected_at)) {
      for (const row of Object.values(watchdog.active || {})) if (row.worker_id === source.worker) incident(row.code);
    }
    const history = rows(watchdog?.history).filter(row => row.key?.startsWith(source.worker + ':')
      && age(row.at, now) >= 0 && age(row.at, now) <= 7 * 24 * 60 * 60).slice(-5).reverse();
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

  async function readHealth(retryAt = 0, previous = {}) {
    const cooling = Date.now() < retryAt;
    let results = cooling
      ? Array.from({length: 3}, () => ({status: 'rejected', reason: new Error('Gitee 限流冷却中')}))
      : await Promise.allSettled([
        readSnapshot('worker-health.json', 'snapshot/' + source.worker, 'triton-anchor-worker-health'),
        readSnapshot('watchdog.json', 'watchdog', 'triton-anchor-local-ci-watchdog'),
        readAlerts(),
      ]);
    if (results.some(result => result.status === 'rejected' && result.reason?.rateLimited))
      retryAt = Date.now() + 15 * 60 * 1000;
    let notice = '';
    if (results.some(result => result.status === 'rejected')) {
      try {
        const response = await fetch(source.cacheUrl, {credentials: 'omit', signal: AbortSignal.timeout(15000)});
        if (!response.ok) throw new Error('Cloudflare 备用数据读取失败（HTTP ' + response.status + '）');
        const cache = await response.json();
        if (cache.schema !== 'triton-anchor-worker-health-cache' || cache.worker_id !== source.worker
          || !Number.isFinite(Date.parse(cache.updated_at))) throw new Error('Cloudflare 缓存格式或服务器身份不匹配');
        const stale = !fresh(cache.updated_at, Date.now());
        const names = ['worker', 'watchdog', 'alerts'], used = [];
        results = results.map((result, index) => {
          const name = names[index], value = cache[name];
          if (result.status === 'fulfilled' || value == null) return result;
          used.push(['健康快照', 'watchdog', '告警记录'][index]);
          const timestamp = index === 0 ? 'collected_at' : 'updated_at';
          if (index < 2 && Date.parse(value[timestamp]) < Date.parse(previous[name]?.[timestamp]))
            return {status: 'fulfilled', value: previous[name], error: '备用缓存早于已读取的快照，保留较新数据，当前状态待确认'};
          return {status: 'fulfilled', value,
            error: cache.errors?.[name] || (stale ? 'Cloudflare 缓存已过期，当前状态无法确认' : '')};
        });
        notice = used.length
          ? 'Gitee 读取未成功，已使用 Cloudflare 备用缓存：' + used.join('、') + '。缓存更新：' + date(cache.updated_at)
          : 'Cloudflare 尚无可用的备用数据。';
      } catch (error) {
        notice = error instanceof TypeError || ['TimeoutError', 'AbortError'].includes(error.name)
          ? 'Cloudflare 备用数据也无法读取（网络、跨域或请求超时）；保留最后读取的数据，当前状态待确认。'
          : error.message;
      }
    }
    return {results, retryAt, notice};
  }

  function mount(root) {
    const node = (tag, className, text) => { const item = document.createElement(tag); if (className) item.className = className; if (text) item.textContent = text; return item; };
    const heading = node('div', 'health-heading'), title = node('div');
    title.append(node('h2', '', '运行概览'), node('p', 'health-muted', source.worker + ' · Gitee 健康快照'));
    const refresh = node('button', 'button secondary', '刷新健康状态'); refresh.type = 'button';
    heading.append(title, refresh);
    const content = node('div'); content.setAttribute('aria-live', 'polite');
    root.append(heading, content);
    let worker = null, watchdog = null, workerError = '', watchdogError = '', loading = false, renderedState = '';
    let alerts = [], alertsError = '', retryAt = 0, sourceNotice = '';

    function render() {
      const cooling = Date.now() < retryAt;
      refresh.disabled = loading;
      refresh.textContent = loading ? '读取中…' : cooling ? '刷新备用数据' : '刷新健康状态';
      if (loading && !worker) { renderedState = ''; content.replaceChildren(node('p', 'health-muted', '正在读取健康数据…')); return; }
      const model = assess(worker, watchdog, {workerError, watchdogError});
      const viewState = JSON.stringify([model, worker?.collected_at, watchdog?.updated_at, alerts, alertsError, sourceNotice, cooling]);
      if (viewState === renderedState) return;
      renderedState = viewState;
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
        ['watchdog 检测', date(watchdog?.updated_at)],
        ['可用磁盘', rows(worker?.storage).map(row => typeof row.filesystem_free_bytes === 'number' ? (row.filesystem_free_bytes / 1024 ** 3).toFixed(1) + ' GiB' : '未上报').join(' / ') || '未上报'],
      ]) facts.append(node('dt', '', label), node('dd', '', value));
      for (const service of rows(worker?.services)) {
        const label = serviceNames[service.name] || service.name;
        const value = !service.available ? '无法查询' : service.active_state === 'inactive' && !service.name.endsWith('.timer') && service.name !== 'triton-anchor-local-ci.service' ? '未执行（按需服务）' : serviceStates[service.active_state] || '未知';
        facts.append(node('dt', '', label), node('dd', '', model.current ? value : value + '（历史快照）'));
      }
      details.append(facts);
      const task = node('section', 'health-section');
      task.append(node('h3', '', '当前任务'));
      if (worker?.active_task) {
        if (!model.current) task.append(node('p', 'health-muted', '最后快照中的任务，当前状态待确认。'));
        const stages = {preparing:'准备环境', running:'执行中', sealing:'汇总结果', publish_pending:'等待上传', published:'已发布'};
        const taskFacts = node('dl', 'health-facts');
        for (const [label, value] of [
          ['任务 ID', worker.active_task.task_id || '未上报'],
          ['任务阶段', stages[worker.active_task.stage] || '未知'],
          ['状态更新', date(worker.active_task.updated_at)],
          ['最近进展', date(worker.active_task.last_progress_at)],
        ]) taskFacts.append(node('dt', '', label), node('dd', '', value));
        task.append(taskFacts);
      } else task.append(node('p', 'health-muted', model.current ? '当前没有正在处理的任务。' : '当前任务状态未知。'));
      grid.append(details, task);
      content.append(grid);
      const alertSection = node('section', 'health-section');
      alertSection.append(node('h3', '', 'Cloudflare 告警记录'));
      const allAlerts = node('a', '', '在 Gitee 查看全部 Issues');
      allAlerts.href = 'https://gitee.com/' + source.repository + '/issues';
      alertSection.append(allAlerts);
      if (alertsError) alertSection.append(node('p', 'health-muted', alertsError + '；以下旧记录不能代表当前告警状态。'));
      const alertList = node('ul', 'health-history');
      for (const alert of alerts) {
        const item = node('li'), link = node('a', '', alert.title);
        link.href = alert.url;
        item.append(node('span', 'health-badge ' + (['closed', 'rejected'].includes(alert.state) ? 'muted' : 'warn'),
          ['closed', 'rejected'].includes(alert.state) ? '已关闭' : '待处理'), document.createTextNode(' '), link,
          node('span', 'health-muted', ' · 更新于 ' + date(alert.updated_at)));
        alertList.append(item);
      }
      alertSection.append(alerts.length ? alertList : node('p', 'health-muted', alertsError ? '告警记录暂不可用。' : '暂无已记录的告警；这不表示 Cloudflare 监测已经启用。'));
      alertSection.append(node('p', 'health-muted', '显示最近更新的告警。Issue 关闭不等于服务已确认恢复，当前状态以上方健康快照为准。'));
      content.append(alertSection);
      const records = node('section', 'health-section');
      records.append(node('h3', '', '服务器 watchdog 记录（近 7 天）'));
      const history = node('ul', 'health-history');
      for (const row of model.history) {
        const code = row.key.slice(source.worker.length + 1);
        history.append(node('li', '', date(row.at) + ' · ' + (row.transition === 'recovered' ? '已恢复' : '发现异常') + ' · ' + (incidents[code]?.[1] || code)));
      }
      records.append(model.history.length ? history : node('p', 'health-muted', '近 7 天暂无已读取的异常记录。'));
      content.append(records, node('p', 'health-muted', '心跳超过 20 分钟未更新时标记过期；健康数据不可读不等于服务器宕机。Cloudflare 启用后独立记录告警，本页不触发检测或通知。'));
    }

    async function refreshHealth() {
      if (loading) return;
      loading = true;
      render();
      const response = await readHealth(retryAt, {worker, watchdog}), results = response.results;
      retryAt = response.retryAt; sourceNotice = response.notice;
      const error = result => result.status === 'fulfilled' ? result.error || '' : result.reason instanceof TypeError || ['TimeoutError', 'AbortError'].includes(result.reason?.name)
        ? '浏览器未能读取 Gitee（可能是网络、跨域限制或请求超时），当前状态无法确认'
        : result.reason?.message || '健康数据读取失败';
      workerError = error(results[0]); watchdogError = error(results[1]);
      if (results[0].status === 'fulfilled') worker = results[0].value;
      if (results[1].status === 'fulfilled') watchdog = results[1].value;
      alertsError = error(results[2]);
      if (results[2].status === 'fulfilled') alerts = results[2].value;
      loading = false; render();
    }
    refresh.addEventListener('click', refreshHealth);
    refreshHealth();
    setInterval(() => { if (!document.hidden) refreshHealth(); }, source.refreshMs);
    // Freshness also changes when no new data arrives or a background tab resumes.
    setInterval(() => { if (!document.hidden) render(); }, 60000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) render(); });
  }
  if (typeof module !== 'undefined') module.exports = {assess, readSnapshot, readAlerts, readHealth, source};
  if (typeof document !== 'undefined') {
    const root = document.getElementById('serverHealth');
    if (root) mount(root);
  }
})();
