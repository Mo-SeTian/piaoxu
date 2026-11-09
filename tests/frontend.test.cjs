const test=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const source=fs.readFileSync(require('node:path').join(__dirname,'../static/app.js'),'utf8');
function section(start,end){return source.slice(source.indexOf(start),source.indexOf(end,source.indexOf(start)))}
function dateSelectionHarness(total=55){
  const calls=[],items=Array.from({length:Math.min(total,500)},(_,i)=>({id:i+1,amount_cents:100}));
  const context=vm.createContext({Set,URLSearchParams,selectingDates:false,
    state:{page:'invoices',query:'餐饮',filter:'未报销',selected:new Set([1000]),selectedRecords:new Map([[1000,{id:1000}]])},
    api:async url=>{calls.push(url);return {json:async()=>({items,total})}}});
  vm.runInContext(section('async function selectInvoiceDates(', '\nasync function submitInvoiceDateSelection('),context);
  return {context,calls,run:()=>context.selectInvoiceDates('2026-11-09','2026-11-10')};
}
test('date selection includes invoices beyond the visible page and preserves earlier choices',async()=>{
  const h=dateSelectionHarness();assert.equal(await h.run(),55);
  const query=new URL(h.calls[0],'http://localhost').searchParams;
  assert.equal(query.get('limit'),'500');assert.equal(query.get('query'),'餐饮');assert.equal(query.get('reimbursement'),'unreimbursed');
  assert.equal(query.get('imported_from'),'2026-11-09');assert.equal(query.get('imported_to'),'2026-11-10');
  assert.equal(h.context.state.selected.size,56);assert.equal(h.context.state.selectedRecords.size,56);
});
test('excessive date matches and changed filters leave the existing selection intact',async()=>{
  const excessive=dateSelectionHarness(501);await assert.rejects(excessive.run(),/500/);
  assert.equal(excessive.context.state.selected.size,1);assert.equal(excessive.context.selectingDates,false);
  const h=dateSelectionHarness();let resolve;
  h.context.api=()=>new Promise(done=>resolve=done);
  const pending=h.run();h.context.state.query='其他';resolve({json:async()=>({items:[{id:1}],total:1})});
  await assert.rejects(pending,/筛选条件已变化/);assert.equal(h.context.state.selected.size,1);
});
test('amount display preserves global server order instead of regrouping by date',()=>{
  const rows=[1,2,3].map(id=>({id,stored_name:id+'.pdf',original_name:id+'.pdf',seller:'商户'+id,amount_cents:id*100,imported_at:id===2?'2026-11-10':'2026-11-09'}));
  const context=vm.createContext({Map,state:{invoices:rows,selectedRecords:new Map(),selected:new Set(),invoiceSort:'amount_asc',filter:'全部',query:'',invoiceCount:3,invoiceTotal:3},
    esc:String,cash:n=>'¥'+n/100,date:String,sourceSummary:()=>'',uiIcon:()=>'',header:()=>'',invoicePreview:()=>'',listPager:()=>''});
  vm.runInContext(section('function invoicePage(', '\nfunction groupPage('),context);
  const html=context.invoicePage();
  assert.ok(html.indexOf('data-preview="1"')<html.indexOf('data-preview="2"'));
  assert.ok(html.indexOf('data-preview="2"')<html.indexOf('data-preview="3"'));
  assert.doesNotMatch(html,/class="date-row"/);assert.match(html,/金额从低到高/);
});
function exportHarness(){
  const controls=[{disabled:false},{disabled:true}],link={clicks:0,click(){this.clicks++}},done={},messages=[],calls=[];
  const context=vm.createContext({exporting:false,state:{selected:new Set([1,2])},
    document:{querySelectorAll:()=>controls},form:{dataset:{submit:'old'},querySelector:s=>s==='[data-export-download]'?link:done},
    toast:message=>messages.push(message),esc:String,uiIcon:()=>'',openDialog:(title,html)=>{context.html=html},
    api:async(url,options)=>{calls.push({url,payload:JSON.parse(options.body)});return {json:async()=>({download_id:'a'.repeat(48),filename:'发票_2张_20261110_153012.zip'})}}});
  vm.runInContext(section('async function exportInvoices(', '\nasync function action('),context);
  return {context,controls,link,done,messages,calls};
}
test('invoice export uses a native HTTP download and keeps a retry link',async()=>{
  const h=exportHarness();await h.context.exportInvoices(true);
  assert.equal(h.calls[0].url,'/api/invoices/export/prepare');assert.equal(h.calls[0].payload.pdf_only,true);
  assert.deepEqual(h.calls[0].payload.ids,[1,2]);
  assert.match(h.context.html,/href="\/api\/invoices\/export\/download\/[a-f0-9]{48}" download="发票_2张_20261110_153012.zip"/);
  assert.equal(h.link.clicks,1);assert.equal(h.done.textContent,'完成');
  assert.equal(h.context.exporting,false);assert.equal(h.controls[0].disabled,false);assert.equal(h.controls[1].disabled,true);
});
test('export blocks repeat requests and recovers after a failed preparation',async()=>{
  const h=exportHarness();let reject,calls=0;
  h.context.api=()=>{calls++;return new Promise((resolve,fail)=>reject=fail)};
  const first=h.context.exportInvoices();await h.context.exportInvoices();
  assert.equal(calls,1);assert.equal(h.controls[0].disabled,true);
  reject(Error('打包失败'));await assert.rejects(first,/打包失败/);
  assert.equal(h.context.exporting,false);assert.equal(h.controls[0].disabled,false);assert.equal(h.link.clicks,0);
  assert.equal(h.context.state.selected.size,2);
});
function previewHarness(expanded=true){
  const image={style:{}},previous={},next={},label={},scale={},scroll={calls:0,scrollTo(){this.calls++}};
  const viewer={dataset:{pdfViewer:'9',pdfCurrent:'1',pdfTotal:'2',previewScale:'100'},
    hasAttribute:()=>expanded,querySelector:selector=>({'img':image,'[data-pdf-step="-1"]':previous,'[data-pdf-step="1"]':next,'[data-pdf-page]':label,'[data-preview-scale-label]':scale,'[data-preview-zoom="-25"]':previous,'[data-preview-zoom="25"]':next,'.document-scroll':scroll}[selector])};
  const context=vm.createContext({Map,Number,uiMotion:()=>{}});
  vm.runInContext(section('const pdfPages=new Map();','\nfunction manualInvoicePreview('),context);
  return {context,viewer,image,previous,next,label,scale,scroll,
    zoom:value=>context.zoomInvoicePreview({dataset:{previewZoom:String(value)},closest:()=>viewer}),
    turn:step=>context.turnPdfPage({dataset:{pdfStep:String(step)},closest:()=>viewer})};
}
test('large preview zoom is bounded and fit resets the scroll position',()=>{
  const h=previewHarness();
  h.zoom(25);assert.equal(h.image.style.width,'125%');assert.equal(h.scale.textContent,'125%');
  for(let i=0;i<20;i++)h.zoom(25);
  assert.equal(h.image.style.width,'300%');assert.equal(h.next.disabled,true);
  for(let i=0;i<20;i++)h.zoom(-25);
  assert.equal(h.image.style.width,'50%');assert.equal(h.previous.disabled,true);
  h.zoom('fit');assert.equal(h.image.style.width,'100%');assert.equal(h.scroll.calls,1);
  assert.equal(h.previous.disabled,false);assert.equal(h.next.disabled,false);
});
test('large PDF pagination retains zoom and resolution and stops at the last page',()=>{
  const h=previewHarness();h.zoom(25);h.turn(1);
  assert.equal(h.image.src,'/api/invoices/9/preview?page=2&width=3000');
  assert.equal(h.image.style.width,'125%');assert.equal(h.label.textContent,'第 2 / 2 页');
  assert.equal(h.next.disabled,true);assert.equal(h.scroll.calls,1);
  h.turn(1);assert.equal(h.viewer.dataset.pdfCurrent,2);assert.equal(h.scroll.calls,1);
  const sidebar=previewHarness(false);sidebar.turn(1);
  assert.equal(sidebar.image.src,'/api/invoices/9/preview?page=2');
});
test('desktop invoice clicks open the modal and preserve the row for focus return',()=>{
  const calls=[],invoice={id:9};
  const context=vm.createContext({state:{page:'invoices'},dialog:{open:false},
    content:{querySelector:()=>({focus:()=>calls.push('focus')})},
    form:{dataset:{submit:'old-action'},querySelector:()=>({remove:()=>calls.push('footer removed')})},
    render:()=>calls.push('render'),invoicePreview:(value,expanded)=>{assert.equal(value,invoice);assert.equal(expanded,true);return 'large preview'},
    openDialog:(title,body)=>calls.push([title,body])});
  vm.runInContext(section('function openInvoicePreview(', '\nfunction zoomInvoicePreview('),context);
  context.openInvoicePreview(invoice);
  assert.equal(context.state.preview,9);assert.equal(context.form.dataset.submit,undefined);
  assert.deepEqual(calls,['render','focus',['发票预览','large preview'],'footer removed']);
});
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

function mergeHarness(){
  const submit={disabled:false,textContent:''},error={textContent:''},buttons=[submit],calls=[],messages=[];
  const invoice={id:1,sha256:'a'.repeat(64),original_name:'base.xml',buyer:'星辰公司',seller:'远航公司',amount_cents:1690,invoice_date:'2026-11-09',invoice_number:'12345678'};
  const context=vm.createContext({Number,console,pendingMerge:null,
    state:{invoices:[invoice,{...invoice,id:2,sha256:'b'.repeat(64),duplicate_of:1,original_name:'copy.xml'}],selected:new Set([2]),selectedRecords:new Map([[2,{}]])},
    form:{dataset:{},querySelector:selector=>selector==='.inline-error'?error:submit,querySelectorAll:()=>buttons},
    dialog:{close:()=>{context.pendingMerge=null}},pdfPages:new Map([[2,1]]),
    esc:String,cash:value=>'¥'+value/100,toast:message=>messages.push(message),load:async()=>{},
    openDialog:(title,body,label)=>{context.body=body;submit.textContent=label},
    api:async(url,options)=>{calls.push({url,payload:JSON.parse(options.body)})}
  });
  vm.runInContext(section('function showMergeDialog(', '\nfunction showUploadPreview('),context);
  return {context,submit,error,calls,messages};
}
test('merge captures both record identities and blocks double submission',async()=>{
  const h=mergeHarness();h.context.showMergeDialog(2);assert.equal(h.submit.disabled,false);
  h.context.state.invoices[0].sha256='c'.repeat(64);
  let release;h.context.api=async(url,options)=>{h.calls.push({url,payload:JSON.parse(options.body)});await new Promise(resolve=>release=resolve)};
  const pending=h.context.submitInvoiceMerge();await h.context.submitInvoiceMerge();
  assert.equal(h.calls.length,1);assert.equal(h.calls[0].url,'/api/invoices/2/merge');
  assert.equal(h.calls[0].payload.target_sha256,'a'.repeat(64));assert.equal(h.calls[0].payload.sha256,'b'.repeat(64));
  release();await pending;
  assert.equal(h.context.state.selected.has(2),false);assert.equal(h.context.pdfPages.has(2),false);
  assert.equal(h.submit.disabled,false);assert.match(h.messages[0],/已合并/);
});
test('merge shows conflicts and keeps the dialog usable after a failed request',async()=>{
  const h=mergeHarness();h.context.state.invoices[1].amount_cents=2190;h.context.showMergeDialog(2);
  assert.equal(h.submit.disabled,true);assert.match(h.context.body,/merge-conflict/);
  h.context.state.invoices[1].amount_cents=1690;
  h.context.state.invoices[0].buyer_tax_id='T'.repeat(18);h.context.state.invoices[1].buyer_tax_id='S'.repeat(18);h.context.showMergeDialog(2);
  assert.equal(h.submit.disabled,true);assert.match(h.context.body,/购买方税号/);
  h.context.state.invoices[1].buyer_tax_id='T'.repeat(18);h.context.showMergeDialog(2);
  h.context.api=async()=>{throw Error('发票已发生变化')};
  await assert.rejects(h.context.submitInvoiceMerge(),/发票已发生变化/);
  assert.equal(h.submit.disabled,false);assert.ok(h.context.pendingMerge);assert.equal(h.context.form.dataset.uploading,undefined);
});

test('mail retry separates redundant links from real processing failures',async()=>{
  const h=reprocessHarness();
  h.context.api=async()=>({json:async()=>({existing:1,files_found:1,skipped_links:1,errors:0})});
  await h.run();assert.equal(h.status.textContent,'已存在 1 张 · 跳过 1 个重复下载入口');
  h.context.api=async()=>({json:async()=>({existing:1,files_found:1,errors:1,error_reasons:['链接下载失败']})});
  await h.run();assert.equal(h.status.textContent,'已存在 1 张 · 1 项处理失败（链接下载失败）');
});
