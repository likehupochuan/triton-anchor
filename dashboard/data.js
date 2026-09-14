/* Shared result projection for task, operator and performance views. */
(function (global) {
  const array = value => Array.isArray(value) ? value : [];
  const status = value => ({pass:'passed',fail:'failed',infra_error:'error',pending:'waiting',not_selected:'skipped'}[value] || value || 'unknown');
  const safeUrl = value => { try { const url = new URL(value); return url.protocol === 'https:' ? url.href : ''; } catch { return ''; } };
  const subject = task => task.repository + '/' + (task.pr_number ? 'pr/' + task.pr_number : 'branch/' + task.target_branch);
  const timestamp = value => typeof value === 'number' ? value * 1000 : Date.parse(value) || 0;
  const blockerCategories = {
    environment: {label:'服务器环境问题', hint:'检查服务器配置、容器、依赖版本、权限和资源；不据此认定 PR 代码有问题。'},
    network: {label:'网络 / 连接问题', hint:'检查目标服务连通性、DNS、代理或 TLS；结合原始错误确认故障位置。'},
    review: {label:'审查阻塞', hint:'审查明确报告未通过或高风险发现，需要结合代码与证据处理。'},
    validation: {label:'构建 / 测试未通过', hint:'核对失败用例与日志；检查失败本身不证明根因一定在 PR。'},
    execution: {label:'执行中断 / 超时', hint:'确认中断位置与时间预算；仅凭超时不能判定是网络或服务器故障。'},
    publication: {label:'结果校验 / 发布异常', hint:'核对结果文件与发布记录；发布异常不改写已经取得的测试结论。'},
    unknown: {label:'原因待确认', hint:'现有结果不足以可靠分类，请查看原始原因与完整执行报告。'},
  };

  // Presentation-only classification. Keep the machine verdict and source text
  // untouched, and do not mistake a missing review for a negative review.
  function blockerGroups(run) {
    const checks = array(run.checks), review = run.ai_review || {};
    const reviews = ['pr_info','architecture','intent'].flatMap(kind => review[kind] ? [{kind,...review[kind]}] : []);
    const findings = array(review.findings).filter(item => item.blocking === true || ['high','critical'].includes(item.severity));
    const negative = value => ['fail','failure','failed'].includes(value);
    const unfinished = value => ['infra_error','error','skipped','not_selected','unknown'].includes(value);
    const originals = array(run.blocking_reasons).filter(value => typeof value === 'string' && value.trim());
    const entries = [], seen = new Set();
    function classify(text, context = {}) {
      // Structured review findings describe the change, not infrastructure health.
      if (context.finding || negative(context.review?.status) || findings.some(item => item.summary === text)) return 'review';
      const namedReview = reviews.find(item => text === item.summary || text.startsWith('必要审查未通过：' + item.kind) ||
        text.startsWith(item.kind + ':') || text.startsWith(item.kind + '：'));
      if (negative(namedReview?.status)) return 'review';
      if (/Trusted profile and exact LLVM revision are required|LLVM.{0,30}(?:mismatch|not found|missing)|dependency version mismatch|配置.{0,12}(?:缺失|错误|不匹配)|容器.{0,12}(?:启动失败|不可用)/i.test(text)) return 'environment';
      if (/connection (?:refused|reset|timed out)|could not resolve (?:host|hostname)|(?:temporary failure in|failed) name resolution|NameResolutionError|network is unreachable|failed to (?:connect|establish a new connection)|SSL certificate problem|certificate verify failed|TLS handshake(?::| has)? (?:failed|failure|error|timeout)|网络(?:连接)?(?:失败|异常|不可达|超时)|连接(?:被拒绝|重置|超时)|域名解析失败|DNS(?: (?:lookup|resolution|query))?(?: has| is|:)? (?:failed|failure|error|timed out)|DNS.{0,10}(?:失败|异常|超时)|无法连接/i.test(text)) return 'network';
      if (/worker preparation(?: failed|:|$)|worker revision differs from installed control|environment (?:registry|operation|subprocess).{0,45}(?:unreadable|incomplete|failed|could not|invalid)|rootless Docker.{0,40}(?:required|failed|invalid)|cannot connect to the docker daemon|no space left on device|out of memory|\bOOM(?:Killed)?\b|permission denied|no module named|ModuleNotFoundError|shared librar(?:y|ies).{0,40}(?:not found|cannot open)|服务器环境.{0,15}(?:异常|失败|不匹配)|环境准备.{0,15}(?:失败|未完成)|依赖.{0,15}(?:缺失|不匹配)|内存不足|磁盘空间不足|权限不足/i.test(text)) return 'environment';
      if (/证据文件不存在|result.{0,30}(?:invalid|mismatch|changed|unreadable)|结果.{0,20}(?:校验|发布|上传|读取).{0,25}(?:失败|异常|错误|未完成|无法|不存在)|tested tracked source changed during execution/i.test(text)) return 'publication';
      if (/timed? ?out|timeout|time budget exhausted|cancelled|canceled|超时|已取消|执行中断/i.test(text)) return 'execution';
      const check = context.check || checks.find(item => text.startsWith(item.id + ':') || text.startsWith(item.id + '：') ||
        text.startsWith('最低必检未通过：' + item.id));
      if (check?.id === 'environment' && (negative(check.status) || unfinished(check.status))) return 'environment';
      if (negative(check?.status)) return 'validation';
      if (context.review || namedReview || /必要审查未通过|最低必检未通过|missing required check|必检尚未完成|审查.{0,12}(?:未完成|未执行)|变更验证必须说明/i.test(text)) return 'incomplete';
      if (unfinished(check?.status) && check.status !== 'error' && check.status !== 'infra_error') return 'incomplete';
      return 'unknown';
    }
    function add(reason, source, context) {
      if (typeof reason !== 'string' || !reason.trim()) return;
      const category = classify(reason, context), id = category + '\n' + reason;
      if (!seen.has(id)) { seen.add(id); entries.push({category, reason, source}); }
    }
    for (const reason of originals) add(reason, '原始阻塞原因');
    // An environment failure may appear only in the task summary, while the
    // recorded blockers merely list reviews that never got a chance to run.
    if (['error','failure','failed','infra_error','fail','cancelled'].includes(run.local_conclusion || run.conclusion)) {
      const summary = review.summary;
      if (summary && (classify(summary) !== 'unknown' || !originals.length || originals.every(reason => classify(reason) === 'incomplete'))) add(summary, '任务摘要');
    }
    const covered = text => typeof text === 'string' && text.trim() && originals.some(reason => reason.includes(text));
    for (const check of checks) {
      if ((negative(check.status) || check.status === 'error' || (check.required && unfinished(check.status))) && !covered(check.reason)) {
        add(check.reason || '最低必检未通过：' + check.id, '检查：' + check.id, {check});
      }
    }
    for (const item of reviews) {
      if ((negative(item.status) || unfinished(item.status)) && !covered(item.summary)) {
        add(item.summary || '必要审查未通过：' + item.kind, '审查：' + item.kind, {review:item});
      }
    }
    for (const finding of findings) if (!covered(finding.summary)) add(finding.summary, '审查发现', {finding:true});
    if (run.receiver_message) {
      const category = classify(run.receiver_message);
      entries.push({category:category === 'unknown' ? 'publication' : category, reason:run.receiver_message, source:'结果接收器'});
    }
    if (!entries.length && ['error','failure','failed','infra_error','fail'].includes(run.conclusion)) {
      add('尚无足够的错误详情，无法确定根因。', '任务状态');
    }
    const incomplete = entries.filter(entry => entry.category === 'incomplete');
    const causes = [...new Set(entries.filter(entry => entry.category !== 'incomplete').map(entry => entry.category))];
    // Associate generic incompletion with a single recorded execution cause only.
    // Multiple causes (or a review finding alone) cannot establish this relation.
    const impactCategory = causes.length === 1 && ['environment','network','execution'].includes(causes[0]) ? causes[0] : 'unknown';
    if (incomplete.length && impactCategory === 'unknown' && !entries.some(entry => entry.category === 'unknown')) {
      entries.push({category:'unknown', reason:'未完成项目的具体原因尚不能确定，请结合执行日志核对。', source:'展示说明'});
    }
    return Object.entries(blockerCategories).flatMap(([id, category]) => {
      const reasons = entries.filter(entry => entry.category === id);
      const impacts = id === impactCategory ? incomplete : [];
      return reasons.length || impacts.length ? [{id,...category,reasons,impacts}] : [];
    });
  }
  function normalize(feed) {
    if (feed.schema !== 'triton-anchor-dashboard' || !Array.isArray(feed.tasks)) throw new Error('结果数据格式不兼容');
    const ordered = [...feed.tasks].sort((a,b) => timestamp(b.task?.captured_at) - timestamp(a.task?.captured_at));
    const latest = new Map();
    for (const item of ordered) if (!latest.has(subject(item.task || {}))) latest.set(subject(item.task || {}), item.task?.task_id);
    const runs = ordered.map(item => {
      const task = item.task || {}, result = item.result || {};
      const artifacts = array(result.artifacts).map(entry => ({...entry,
        status: entry.omitted ? 'skipped' : 'ready',
        url: entry.omitted ? '' : safeUrl(item.artifact_urls?.[entry.path])}));
      const required = new Set(array(result.policy?.required_checks));
      const checks = array(result.checks).map(check => ({...check,
        id: check.tool_id, status: status(check.status), required: required.has(check.tool_id),
        reason: check.summary || ''}));
      const local = item.status || result.status || 'pending';
      const conclusion = local === 'pass' ? 'success' : local === 'fail' ? 'failure' : status(local);
      const reviews = Object.fromEntries(array(result.reviews).map(review => [review.kind, review]));
      const evidence = checks.flatMap(check => array(check.evidence).map(path => ({
        tool: check.tool_id, path, status: check.status,
        log_url: artifacts.find(artifact => artifact.path === path)?.url || ''})));
      const performance = checks.filter(check => ['compile_time','pass_profile','ir_serialization'].includes(check.id))
        .map(check => ({...check, tool: check.tool_id, summary: check.summary || '测量和比较结果见所选报告。'}));
      const blockers = array(result.blocking_reasons);
      return {...task, task_id: task.task_id || result.task?.task_id || '', run_id: result.run_id || 'pending',
        completed_at: result.completed_at || Math.max(0,...checks.map(c => timestamp(c.finished_at))) / 1000 || task.captured_at,
        is_current: latest.get(subject(task)) === task.task_id, conclusion, local_conclusion: status(result.status || local),
        artifacts, checks, evidence, performance,
        policy: {...(result.policy || {}), docs_only: result.policy?.impact?.level === 'non_executable',
                 manual_full: task.full, changed_paths: array(result.policy?.changes).map(c => c.path)},
        blocking_reasons: blockers,
        ai_review: {summary: result.summary,
          ...Object.fromEntries(['pr_info','architecture','intent'].filter(kind => reviews[kind])
            .map(kind => [kind, {...reviews[kind], status: status(reviews[kind].status)}])),
          findings: array(result.findings)},
        environment: result.environment || {}, raw: result, receiver_message: item.receiver_message || '',
        result_url: safeUrl(item.result_url), artifacts_url: '', worker_revision_sha: task.worker_revision_sha};
    });
    return {schema:feed.schema, data_mode:feed.data_mode || 'live', generated_at:feed.generated_at,
      runs, warnings:ordered.filter(item => item.receiver_error).map(item => ({path:item.task?.task_id, reason:item.receiver_message || item.receiver_error}))};
  }
  function business(data) {
    const runs = data.runs;
    const full = runs.find(run => run.checks.some(check => check.details?.["flaggems-summary"]?.mode === 'full'));
    const fg = full?.checks.find(check => check.details?.["flaggems-summary"]?.mode === 'full')?.details["flaggems-summary"];
    const operators = array(fg?.results).map((row,index) => ({index:row.index || index+1, name:row.op,
      status:({'通过':'passed','成功':'passed','失败':'failed','未通过':'failed','执行错误':'error','infra_error':'error','error':'error','超时':'timeout'}[row.test_status] || (row.exit_code === 0 && row.passed > 0 ? 'passed' : 'failed')),
      failure_stage:(row.first_failed_stage === '全部通过' ? '' : row.first_failed_stage) || row.timeout_reason || '', duration_ms:row.duration_seconds * 1000,
      log_url:full?.artifacts.find(a => row.log_file && a.path.endsWith(row.log_file) && a.url)?.url || ''}));
    const latestBackends = new Map();
    for (const run of runs) {
      const profile = run.environment.profile || run.environment.generation || '未记录环境';
      if (run.checks.some(c => c.id.startsWith('backend_') && c.status !== 'not_applicable') && !latestBackends.has(profile)) latestBackends.set(profile,run);
    }
    const backends = [...latestBackends].map(([profile,run]) => ({id:profile,name:profile,profile,
      state:run.conclusion === 'success' ? 'passed' : status(run.conclusion),sha:run.tested_sha,tested_at:run.completed_at,
      tests:{backend:run.checks.find(c => c.id === 'backend_tests')?.status || 'unknown',
        ...Object.fromEntries(['compile_time','pass_profile','ir_serialization'].map(id => [id,run.checks.find(c => c.id === id)?.status || 'unknown']))},
      result_url:run.result_url}));
    const measured = runs.find(run => run.checks.some(c => c.details?.candidate));
    const compile = measured?.checks.find(c => c.id === 'compile_time')?.details || {};
    const passes = measured?.checks.find(c => c.id === 'pass_profile')?.details || {};
    const ir = measured?.checks.find(c => c.id === 'ir_serialization')?.details || {};
    const compileRows = Object.entries(compile.candidate?.summary || {}).map(([name,value]) => {
      const compare = array(compile.comparison?.kernels).find(row => row.kernel === name || row.name === name);
      return {name,candidate_ms:value.compile_est?.median_ms,delta_percent:compare?.change_ratio == null ? null : compare.change_ratio * 100,status:compare?.status || 'passed'};
    }).filter(row => Number.isFinite(row.candidate_ms));
    const passRows = Object.entries(passes.candidate?.summary || {}).flatMap(([kernel,value]) =>
      Object.entries(value.passes || {}).map(([name,timing]) => ({name:kernel + ' · ' + name,median_ms:timing.wall_ms?.median_ms})))
      .filter(row => Number.isFinite(row.median_ms)).sort((a,b) => b.median_ms-a.median_ms).slice(0,20);
    const irRows = Object.entries(ir.candidate?.summary || {}).flatMap(([kernel,value]) =>
      Object.entries(value.metrics || {}).map(([name,timing]) => ({name:kernel + ' · ' + name,median_ms:timing.median_ms})))
      .filter(row => Number.isFinite(row.median_ms));
    return {manifest:{generated_at:data.generated_at,mode:data.data_mode === 'fixture' ? 'mock' : 'live',downloads:{}},
      fullTest:{run:{backend:full?.environment.profile || '尚无全量算子结果',sha:full?.tested_sha || ''},operators},
      backends:{backends},performance:{backend:measured?.environment.profile || '尚无有效测量',compile_time:{kernels:compileRows},pass_profile:{hotspots:passRows},ir_serialization:{metrics:irRows}}};
  }
  global.LocalCIData = {normalize,business,status,safeUrl,blockerGroups};
  if (typeof module !== 'undefined' && module.exports) module.exports = global.LocalCIData;
})(typeof window === 'undefined' ? globalThis : window);
