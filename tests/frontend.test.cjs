const test=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const source=fs.readFileSync(require('node:path').join(__dirname,'../static/app.js'),'utf8');
function section(start,end){return source.slice(source.indexOf(start),source.indexOf(end,source.indexOf(start)))}
function loadHarness(){
  const counters={renders:0},summary={invoices:0,unreimbursed:0,unreimbursed_amount_cents:0,failed_invoices:0,pending_emails:0};
  const context=vm.createContext({Date,URLSearchParams,Map,Set,console,summary,
    state:{page:'emails',emailPage:0,emailFilter:'all',accounts:[]},
    dialog:{open:false},mailReprocessBusy:new Set(),
    document:{activeElement:{tagName:'BODY'},querySelector:()=>({textContent:''})},
    content:{querySelector:()=>null},render:()=>counters.renders++,
    api:async url=>({json:async()=>url==='/api/summary'?summary:url==='/api/accounts'?[]:{items:[],total:0}})
  });
  vm.runInContext(section('let loadSequence=','\nconst uiIcon'),context);
  return {context,counters,load:options=>context.load(options)};
}
test('unchanged background refresh keeps existing buttons in the DOM',async()=>{
  const h=loadHarness();await h.load();await h.load({background:true});assert.equal(h.counters.renders,1);
  h.context.summary.pending_emails=1;await h.load({background:true});assert.equal(h.counters.renders,2);
});
test('background responses cannot replace controls during a pointer press',async()=>{
  const h=loadHarness();await h.load();h.context.summary.pending_emails=1;
  let release;const gate=new Promise(resolve=>release=resolve),api=h.context.api;
  h.context.api=async url=>{await gate;return api(url)};
  const pending=h.load({background:true});vm.runInContext('pointerActive=true',h.context);release();await pending;
  assert.equal(h.counters.renders,1);
  vm.runInContext('pointerActive=false',h.context);await h.load({background:true});assert.equal(h.counters.renders,2);
});
function reprocessHarness(){
  const button={dataset:{reprocess:'1'},disabled:false,textContent:'',setAttribute(){},removeAttribute(){},closest:()=>false};
  const status={textContent:''},toasts=[];
  const context=vm.createContext({Map,Set,Number,console,button,currentStatus:status,
    state:{page:'emails'},mailReprocessBusy:new Set(),mailReprocessFeedback:new Map(),dialog:{open:false},form:{dataset:{}},
    document:{querySelectorAll:selector=>selector.includes('data-reprocess-status')?[context.currentStatus]:[button]},
    load:async()=>{},toast:message=>toasts.push(message)});
  vm.runInContext(section('function reprocessFeedback(', '\nasync function showEmail('),context);
  return {context,button,status,toasts,run:()=>context.reprocessMessage(button)};
}
test('a reopened email shows errors from the pending request and can retry',async()=>{
  const h=reprocessHarness();let reject;
  h.context.api=()=>new Promise((resolve,fail)=>reject=fail);
  const pending=h.run();assert.equal(h.button.disabled,true);
  const reopened={textContent:'正在处理'};h.context.currentStatus=reopened;
  reject(Error('该邮箱正在同步，请稍后重试'));await pending;
  assert.match(reopened.textContent,/正在同步/);assert.equal(h.button.disabled,false);assert.equal(h.context.mailReprocessBusy.size,0);
  assert.equal(h.toasts.length,1);
});
test('successful processing stays successful if only the list refresh fails',async()=>{
  const h=reprocessHarness();h.context.api=async()=>({json:async()=>({created:1,existing:0,errors:0,retry_failed:0,files_found:1})});
  h.context.load=async()=>{throw Error('连接中断')};await h.run();
  assert.match(h.status.textContent,/新增 1 张/);assert.match(h.status.textContent,/页面更新失败/);assert.equal(h.button.disabled,false);
});
test('repeated clicks submit only one request while processing',async()=>{
  const h=reprocessHarness();let resolve,calls=0;
  h.context.api=()=>{calls++;return new Promise(done=>resolve=done)};
  const first=h.run();await h.run();assert.equal(calls,1);
  resolve({json:async()=>({created:0,existing:1,errors:0,retry_failed:0,files_found:1})});await first;
  assert.match(h.status.textContent,/已存在 1 张/);assert.equal(h.button.disabled,false);
});
test('finishing mail recognition does not replace an open settings form',async()=>{
  const h=reprocessHarness();h.context.state.page='settings';
  h.context.api=async()=>({json:async()=>({existing:1,files_found:1})});
  h.context.load=async()=>assert.fail('Settings must preserve unfinished edits');
  await h.run();assert.equal(h.status.textContent,'已存在 1 张');
});
test('new mail filters keep server results and their pagination total',()=>{
  const context=vm.createContext({Map,Set,
    state:{account:'7',emailFilter:'deleted',accounts:[{id:7,name:'工作邮箱'}],emails:[{id:23,account_id:7,status:'发票已删除',invoice_count:0,recognized_count:0,failed_count:0,duplicate_count:0,sender:'测试发件人',subject:'保留邮件以重新识别',folder:'INBOX',account_name:'工作邮箱',received_at:'2026-11-03'}],emailTotal:51,emailPage:1},
    esc:value=>String(value??''),uiIcon:name=>`<svg data-icon="${name}"></svg>`,date:value=>value,
    header:()=>'',empty:message=>message,listPager:(total,page)=>`pager:${total}:${page}`,
    mailReprocessBusy:new Set(),mailReprocessFeedback:new Map()});
  vm.runInContext(section('function emailStatusFlags(', '\nfunction failedPage('),context);
  const html=context.emailsPage();
  assert.match(html,/data-email="23"/);assert.match(html,/pager:51:1/);
  assert.match(html,/<option value="deleted" selected>发票已删除<\/option>/);
  assert.match(html,/<option value="has" >有发票<\/option>/);
});
