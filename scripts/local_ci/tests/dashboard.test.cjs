const test = require('node:test');
const assert = require('node:assert/strict');
const { normalize, business, blockerGroups } = require('../../../dashboard/data.js');
const task = (id, date) => ({task_id:id, repository:'example/repo',pr_number:7,target_branch:'main',tested_sha:id.repeat(40),captured_at:date});

test('old pass cannot override a newer pending or cancelled task', () => {
  const data = normalize({schema:'triton-anchor-dashboard',tasks:[
    {task:task('a','2026-09-09'),status:'cancelled',result:{status:'pass',run_id:'old'}},
    {task:task('b','2026-09-10'),status:'pending'}]});
  assert.equal(data.runs[0].is_current,true);
  assert.equal(data.runs[0].conclusion,'waiting');
  assert.equal(data.runs[1].is_current,false);
  assert.equal(data.runs[1].conclusion,'cancelled');
});

test('omitted files and unsafe URLs are not clickable', () => {
  const artifacts = [{path:'missing',omitted:'Too large'},{path:'safe'},{path:'unsafe'}];
  const data = normalize({schema:'triton-anchor-dashboard',tasks:[{task:task('a','2026-09-10'),
    result:{artifacts},artifact_urls:{missing:'https://gitee.com/missing',safe:'https://gitee.com/report',unsafe:'javascript:alert(1)'}}]});
  assert.deepEqual(data.runs[0].artifacts.map(a=>a.url),['','https://gitee.com/report','']);
});

test('full view excludes impact-only selection and preserves failing operator details', () => {
  const run = {task:task('a','2026-09-10'),result:{environment:{profile:'fixture'},checks:[{tool_id:'flaggems',status:'fail',details:{"flaggems-summary":{mode:'full',results:[{op:'softmax',test_status:'失败',first_failed_stage:'准确率验证',duration_seconds:2}]}}}]}};
  let data = business(normalize({schema:'triton-anchor-dashboard',tasks:[run]}));
  assert.equal(data.fullTest.operators[0].status,'failed');
  assert.equal(data.fullTest.operators[0].failure_stage,'准确率验证');
  run.result.checks[0].details["flaggems-summary"].mode='impact';
  data = business(normalize({schema:'triton-anchor-dashboard',tasks:[run]}));
  assert.equal(data.fullTest.operators.length,0);
});

test('empty initial feed does not create placeholder success data', () => {
  const data = business(normalize({schema:'triton-anchor-dashboard',tasks:[]}));
  assert.equal(data.fullTest.operators.length,0);
  assert.equal(data.backends.backends.length,0);
  assert.equal(data.performance.compile_time.kernels.length,0);
});


test('performance views read runner candidate and comparison report keys', () => {
  const data = business(normalize({schema:'triton-anchor-dashboard',tasks:[{
    task:task('a','2026-09-10'),status:'pass',result:{checks:[
      {tool_id:'compile_time',status:'pass',details:{candidate:{summary:{add:{compile_est:{median_ms:12}}}},comparison:{kernels:[{kernel:'add',change_ratio:0.2}]}}},
      {tool_id:'pass_profile',status:'pass',details:{candidate:{summary:{add:{passes:{canonicalize:{wall_ms:{median_ms:3}}}}}}}},
      {tool_id:'ir_serialization',status:'pass',details:{candidate:{summary:{add:{metrics:{serialize:{median_ms:2}}}}}}}
    ]}
  }]}));
  assert.equal(data.performance.compile_time.kernels[0].candidate_ms,12);
  assert.equal(data.performance.compile_time.kernels[0].delta_percent,20);
  assert.equal(data.performance.pass_profile.hotspots[0].median_ms,3);
  assert.equal(data.performance.ir_serialization.metrics[0].median_ms,2);
});


test('all Codex reviews retain their summaries and source references', () => {
  const reviews = ['pr_info','architecture','intent'].map(kind => ({kind,status:'pass',summary:kind+' reviewed',evidence:['src/example.py:7']}));
  const run = normalize({schema:'triton-anchor-dashboard',tasks:[{task:task('a','2026-09-10'),result:{reviews}}]}).runs[0];
  for (const review of reviews) {
    assert.equal(run.ai_review[review.kind].summary,review.summary);
    assert.deepEqual(run.ai_review[review.kind].evidence,review.evidence);
    assert.equal(run.ai_review[review.kind].status,'passed');
  }
});

const errorRun = result => normalize({schema:'triton-anchor-dashboard', tasks:[{
  task:task('a','2026-09-10'), status:result.status || 'infra_error', result,
}]}).runs[0];

test('server failure in summary is separated from reviews that never completed', () => {
  const run = errorRun({status:'infra_error', summary:'Task worker revision differs from installed control',
    blocking_reasons:['必要审查未通过：pr_info','必要审查未通过：architecture']});
  const before = JSON.stringify(run);
  const groups = blockerGroups(run);
  assert.deepEqual(groups.map(group=>group.id), ['environment']);
  assert.equal(groups[0].reasons[0].reason,run.ai_review.summary);
  assert.equal(groups[0].impacts.length,2);
  assert.equal(JSON.stringify(run),before);
});

test('explicit network errors are not hidden by a generic preparation prefix', () => {
  for (const reason of ['worker preparation: Connection refused','Could not resolve host: gitee.com',
    '网络连接失败：代理不可达','certificate verify failed']) {
    assert.equal(blockerGroups(errorRun({blocking_reasons:[reason]}))[0].id,'network');
  }
  for (const reason of ['Out of memory','No space left on device',"ModuleNotFoundError: No module named 'torch'",
    'Cannot connect to the Docker daemon']) {
    assert.equal(blockerGroups(errorRun({blocking_reasons:[reason]}))[0].id,'environment');
  }
});

test('a negative review about networking is a review blocker, not an outage', () => {
  const summary = '网络连接失败时未释放资源，违反接口契约';
  const groups = blockerGroups(errorRun({status:'fail',summary,
    reviews:[{kind:'architecture',status:'fail',summary}],
    blocking_reasons:['必要审查未通过：architecture — '+summary]}));
  assert.deepEqual(groups.map(group=>group.id),['review']);
  const finding = {summary:'DNS failure handling leaks credentials',severity:'high'};
  assert.equal(blockerGroups(errorRun({findings:[finding],blocking_reasons:[finding.summary]}))[0].id,'review');
});

test('test failure, incomplete coverage, and absent evidence stay distinct', () => {
  const run = errorRun({status:'fail',policy:{required_checks:['frontend_tests','frontend_build']},
    checks:[{tool_id:'frontend_tests',status:'fail',summary:'断言失败：期望 2，实际 3'},
      {tool_id:'frontend_build',status:'skipped',summary:'尚未执行'}],
    blocking_reasons:['frontend_tests：断言失败：期望 2，实际 3',
      '最低必检未通过：frontend_build','frontend_smoke 引用的证据文件不存在：smoke.log']});
  assert.deepEqual(blockerGroups(run).map(group=>group.id),['validation','publication','unknown']);
  assert.ok(blockerGroups(run).find(group=>group.id==='unknown').impacts.length);
});

test('ambiguous failures and generic timeouts do not guess network or server causes', () => {
  assert.equal(blockerGroups(errorRun({summary:'Codex task time budget exhausted'}))[0].id,'execution');
  assert.equal(blockerGroups(errorRun({blocking_reasons:['Command timed out']}))[0].id,'execution');
  assert.equal(blockerGroups(errorRun({blocking_reasons:['任务返回 137，原因未记录']}))[0].id,'unknown');
  assert.equal(blockerGroups(errorRun({}))[0].id,'unknown');
  for (const summary of ['Rootless Docker preparation succeeded; unspecified failure later',
    'TLS handshake succeeded; unspecified failure later','结果校验通过，另有问题待定位']) {
    assert.equal(blockerGroups(errorRun({summary}))[0].id,'unknown');
  }
});

test('successful runs stay successful when publishing has a separate error', () => {
  const run = errorRun({status:'pass',summary:'验证通过'});
  assert.deepEqual(blockerGroups(run),[]);
  run.receiver_message = '结果读取或 GitHub 发布未完成；稍后重试接收，不重跑构建。';
  assert.equal(blockerGroups(run)[0].id,'publication');
  assert.equal(run.conclusion,'success');
});

test('all original reasons survive classification, with exact duplicates collapsed', () => {
  const reasons = ['worker preparation failed','tested tracked source changed during execution',
    '未知异常 <img src=x onerror=alert(1)>','未知异常 <img src=x onerror=alert(1)>'];
  const groups = blockerGroups(errorRun({blocking_reasons:reasons}));
  assert.deepEqual(new Set(groups.flatMap(group=>group.reasons.map(item=>item.reason))),new Set(reasons));
  assert.equal(groups.flatMap(group=>group.reasons).length,3);
});

test('blocker groups render Chinese headings and preserve original text without HTML injection', () => {
  const vm = require('node:vm');
  const fs = require('node:fs');
  class Element {
    constructor(tag) { this.tag=tag; this.children=[]; this.textContent=''; }
    append(...children) { this.children.push(...children); }
    addEventListener() {}
    set innerHTML(_value) { throw new Error('Remote text must not be rendered as HTML'); }
  }
  const nodes = new Map();
  const document = {createElement:tag=>new Element(tag), getElementById:id=>{
    if(!nodes.has(id))nodes.set(id,new Element('div'));
    return nodes.get(id);
  }};
  const root = new Element('main');
  const original = '未知异常 <img src=x onerror=alert(1)>';
  const context = {document,URLSearchParams,URL,setInterval:()=>{},fetch:()=>new Promise(()=>{}),
    location:{search:''},LocalCIData:{blockerGroups},root,
    run:errorRun({summary:'Task worker revision differs from installed control',
      blocking_reasons:['必要审查未通过：architecture',original]})};
  vm.runInNewContext(fs.readFileSync(require.resolve('../../../dashboard/local-ci.js'),'utf8')+
    '\nrenderBlockers(root,run);',context);
  const flatten = node => [node,...node.children.flatMap(flatten)];
  const rendered = flatten(root);
  assert.ok(rendered.some(node=>node.tag==='h4'&&node.textContent.startsWith('服务器环境问题')));
  assert.ok(!rendered.some(node=>node.tag==='h4'&&node.textContent.startsWith('必检 / 审查未完成')));
  assert.ok(rendered.some(node=>node.textContent.startsWith('影响：以下必检 / 审查尚未完成')));
  assert.ok(rendered.some(node=>node.textContent==='架构契约审查尚未完成'));
  assert.ok(rendered.some(node=>node.tag==='summary'&&node.textContent==='原始原因与来源'));
  assert.ok(rendered.some(node=>node.textContent==='原始阻塞原因：'+original));
  assert.ok(!rendered.some(node=>node.tag==='img'));
});

test('incompletion is an impact under one recorded cause, otherwise its cause is unknown', () => {
  const missing = '必要审查未通过：architecture';
  for (const [reason, category] of [['Could not resolve host: gitee.com','network'],
    ['Codex task time budget exhausted','execution']]) {
    const groups = blockerGroups(errorRun({summary:reason,blocking_reasons:[missing]}));
    assert.deepEqual(groups.map(group=>group.id),[category]);
    assert.equal(groups[0].impacts[0].reason,missing);
  }
  const unknown = blockerGroups(errorRun({blocking_reasons:[missing]}));
  assert.deepEqual(unknown.map(group=>group.id),['unknown']);
  assert.equal(unknown[0].impacts[0].reason,missing);
  const mixed = blockerGroups(errorRun({blocking_reasons:['Out of memory','Connection refused',missing]}));
  assert.deepEqual(mixed.map(group=>group.id),['environment','network','unknown']);
  assert.equal(mixed.find(group=>group.id==='unknown').impacts[0].reason,missing);
  assert.ok(mixed.filter(group=>group.id!=='unknown').every(group=>group.impacts.length===0));
});
