const state={username:'',page:'invoices',invoices:[],groups:[],accounts:[],emails:[],selected:new Set(),selectedRecords:new Map(),invoicePage:0,invoiceTotal:0,invoiceCount:0,unpaidCount:0,unpaidAmount:0,reviewCount:0,filter:'全部',query:'',account:'',emailFilter:'all',emailPage:0,emailTotal:0,pendingPage:0,pendingTotal:0,preview:null,settings:['抬头','商户','金额','开票日期'],ai:{base_url:'',model:'',auto_enabled:false,has_key:false}};
const content=document.querySelector('#content'),dialog=document.querySelector('#dialog'),form=document.querySelector('#dialog-form');
const mobileNavQuery='(max-width:640px), (orientation:landscape) and (max-width:950px) and (max-height:500px) and (pointer:coarse)';
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const cash=n=>n==null?'—':'¥'+(n/100).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
const date=x=>x?esc(x.slice(0,10)):'—';
const titles={invoices:'发票库',groups:'报销组',emails:'全部邮件',failed:'识别失败',accounts:'邮箱管理',ai:'AI 辅助识别',settings:'偏好设置',backup:'备份与恢复'};
const routePage=()=>location.hash==='#security'?'settings':Object.hasOwn(titles,location.hash.slice(1))?location.hash.slice(1):'invoices';
state.page=routePage();
if(location.hash==='#security')history.replaceState(null,'','#settings');
function clearSelection(){state.selected.clear();state.selectedRecords.clear()}
function navigate(page){state.page=page;if(location.hash!=='#'+page)location.hash=page}
window.addEventListener('hashchange',()=>{const page=routePage();if(location.hash==='#security')history.replaceState(null,'','#settings');if(page!==state.page){closeNav();state.page=page;clearSelection();load().catch(error=>toast(error.message))}});
async function api(url,options={}){const headers={'X-Piaoxu-Request':'1',...(options.headers||{})};if(options.body && !(options.body instanceof FormData))headers['Content-Type']='application/json';const response=await fetch(url,{...options,headers});if(!response.ok){if(response.status===401&&url!=='/api/auth/login')showLogin();let data={};try{data=await response.json()}catch{}throw Error(typeof data.detail==='string'?data.detail:'请求失败，请重试')}return response}
let timer;function toast(message){const el=document.querySelector('#toast');el.textContent=message;el.classList.add('show');clearTimeout(timer);timer=setTimeout(()=>el.classList.remove('show'),3500)}
let loadSequence=0,lastLoadSnapshot='',pointerActive=false,lastInteraction=0;
function backgroundRefreshBlocked(){return pointerActive||Date.now()-lastInteraction<300||mailReprocessBusy.size>0||dialog.open||['INPUT','SELECT','TEXTAREA'].includes(document.activeElement.tagName)}
async function load({background=false}={}){
  if(background&&backgroundRefreshBlocked())return;
  const seq=++loadSequence,page=state.page,requests=[['summary','/api/summary'],['accounts','/api/accounts']];
  if(page==='invoices'){const params=new URLSearchParams({limit:'50',offset:String(state.invoicePage*50),query:state.query,reimbursement:({'全部':'all','未报销':'unreimbursed','已报销':'reimbursed'})[state.filter]});requests.push(['invoiceResults','/api/invoices?'+params])}
  if(page==='failed')requests.push(['invoices','/api/invoices?view=review']);
  if(page==='groups')requests.push(['groups','/api/groups']);
  if(page==='settings')requests.push(['settings','/api/settings']);
  if(page==='ai')requests.push(['ai','/api/ai-settings']);
  if(page==='emails'){
    const params=new URLSearchParams({limit:'50',offset:String(state.emailPage*50),invoice_status:state.emailFilter});
    if(state.account)params.set('account_id',state.account);
    requests.push(['emailResults','/api/emails?'+params]);
  }else if(page==='failed')requests.push(['emailResults','/api/emails?attention=true&limit=50&offset='+state.pendingPage*50]);
  const values=await Promise.all(requests.map(async ([key,url])=>[key,await (await api(url)).json()]));
  if(seq!==loadSequence||page!==state.page||(background&&backgroundRefreshBlocked()))return;
  const data=Object.fromEntries(values),snapshot=JSON.stringify([page,data]);
  if(background&&snapshot===lastLoadSnapshot)return;
  for(const key of ['invoices','groups','accounts','ai'])if(data[key])state[key]=data[key];
  if(data.settings)state.settings=data.settings.fields;
  state.invoiceCount=data.summary.invoices;state.unpaidCount=data.summary.unreimbursed;state.unpaidAmount=data.summary.unreimbursed_amount_cents;state.reviewCount=data.summary.failed_invoices+data.summary.pending_emails;
  if(data.invoiceResults){const last=Math.max(0,Math.ceil(data.invoiceResults.total/50)-1);if(state.invoicePage>last){state.invoicePage=last;return load()}state.invoices=data.invoiceResults.items;state.invoiceTotal=data.invoiceResults.total;for(const item of state.invoices)if(state.selected.has(item.id))state.selectedRecords.set(item.id,item)}
  if(data.emailResults){
    const field=page==='emails'?'emailPage':'pendingPage',last=Math.max(0,Math.ceil(data.emailResults.total/50)-1);
    if(state[field]>last){state[field]=last;return load()}
    state.emails=data.emailResults.items;if(page==='emails')state.emailTotal=data.emailResults.total;else state.pendingTotal=data.emailResults.total;
  }
  lastLoadSnapshot=snapshot;document.querySelector('#failed-count').textContent=(data.summary.failed_invoices+data.summary.pending_emails)||'';
  document.querySelector('#side-foot').textContent=state.accounts.length?`${state.accounts.length} 个邮箱 · 定时同步`:'还未连接邮箱';const search=content.querySelector('#search'),focused=search&&document.activeElement===search,pos=focused?search.selectionStart:null;render();if(focused){const next=content.querySelector('#search');if(next){next.focus({preventScroll:true});next.setSelectionRange(pos,pos)}}
}
const uiIcon=name=>`<svg class="button-icon" aria-hidden="true"><use href="/icons.svg?v=6#${name}"></use></svg>`;
function uiMotion(element){if(element&&!matchMedia('(prefers-reduced-motion: reduce)').matches)element.animate([{opacity:.4,transform:'translateY(5px)'},{opacity:1,transform:'translateY(0)'}],{duration:210,easing:'cubic-bezier(.2,.8,.2,1)'})}
function header(title,sub,actions=''){return `<div class="head"><div><h1>${title}</h1><p class="sub">${sub}</p></div><div class="actions">${actions}</div></div>`}
function empty(text){return `<div class="empty">${text}</div>`}
function listPager(total,page,kind){
  const pages=Math.ceil(total/50);if(pages<=1)return '';
  return `<div class="list-pager"><button class="button" type="button" data-${kind}-page="-1" ${page===0?'disabled':''}>上一页</button><span aria-live="polite">第 ${page+1} / ${pages} 页</span><button class="button" type="button" data-${kind}-page="1" ${page+1>=pages?'disabled':''}>下一页</button></div>`;
}
function sourceSummary(invoice){
  const labels=[];
  if(invoice.has_local_upload||invoice.source==='本地上传')labels.push('本地导入');
  for(const source of invoice.mail_sources||[]){const label=`邮箱：${source.account_name}（${source.account_email}）`;if(!labels.includes(label))labels.push(label)}
  return labels.length?labels.join('、'):(invoice.source==='邮件'?'邮箱导入':'来源未知');
}
function sourceDetails(invoice){
  const local=invoice.has_local_upload||invoice.source==='本地上传'?'<div class="source-entry"><span>本地导入</span></div>':'';
  const mail=(invoice.mail_sources||[]).map(source=>`<div class="source-entry"><span>邮箱：${esc(source.account_name)}（${esc(source.account_email)}）</span><button class="button link source-email" type="button" data-email="${source.email_id}">查看邮件</button></div>`).join('');
  return local+mail||esc(invoice.source==='邮件'?'邮箱导入':'来源未知');
}
let pendingUpload=[],pendingDelete=[];
const mailReprocessBusy=new Set(),mailReprocessFeedback=new Map();
function updateUploadSelection(){
  const choices=[...form.querySelectorAll('[data-upload-choice]:not(:disabled)')],selected=choices.filter(x=>x.checked).length;
  const all=form.querySelector('#upload-select-all'),submit=form.querySelector('[type=submit]');
  if(all){all.checked=choices.length>0&&selected===choices.length;all.indeterminate=selected>0&&selected<choices.length}
  if(submit){submit.disabled=!selected;submit.textContent=`导入所选（${selected}）`}
}
function uploadProgress(title,total){
  openDialog(title,`<p class="sub">请保持页面打开，系统正在逐个处理文件。</p><div class="upload-progress"><div class="upload-progress-heading"><strong class="upload-progress-step">准备中</strong><span class="upload-progress-count">0 / ${total}</span></div><progress max="${total}" value="0" aria-label="${title}进度"></progress><p class="upload-progress-file muted"></p><p class="upload-progress-summary" role="status" aria-live="polite"></p></div>`);
  form.dataset.uploading='1';
  form.querySelectorAll('button').forEach(button=>button.disabled=true);
}
function updateUploadProgress(done,total,name,summary,verb){
  form.querySelector('progress').value=done;
  form.querySelector('.upload-progress-step').textContent=`${verb} ${done} / ${total}`;
  form.querySelector('.upload-progress-count').textContent=`${Math.round(done/total*100)}%`;
  form.querySelector('.upload-progress-file').textContent=name?`当前文件：${name}`:'';
  form.querySelector('.upload-progress-summary').textContent=summary;
}
async function inspectUpload(files){
  uploadProgress('检查发票',files.length);
  const results=[],hashes=new Map(),numbers=new Map();
  for(const file of files){
    updateUploadProgress(results.length,files.length,file.name,`已检查 ${results.length} 个文件`,'已检查');
    const data=new FormData();data.append('files',file);
    let item;
    try{[item]=await (await api('/api/invoices/inspect',{method:'POST',body:data})).json()}
    catch(error){
      if(!dialog.open)return;
      for(const remaining of files.slice(results.length))results.push({name:remaining.name,error:'检查中断，请重新选择文件重试'});
      break;
    }
    if(!item.error){
      const number=String(item.fields?.invoice_number||'').trim();
      if(item.duplicate!=='existing_file'&&hashes.has(item.sha256)){item.duplicate='batch_file';item.matched_name=hashes.get(item.sha256)}
      else if(!item.duplicate&&number&&numbers.has(number)){item.duplicate='batch_invoice';item.matched_name=numbers.get(number)}
      if(!hashes.has(item.sha256))hashes.set(item.sha256,item.name);
      if(number&&!numbers.has(number))numbers.set(number,item.name);
    }
    results.push(item);
    const errors=results.filter(x=>x.error).length;
    updateUploadProgress(results.length,files.length,file.name,`已检查 ${results.length} 个 · 检查失败 ${errors} 个`,'已检查');
  }
  delete form.dataset.uploading;pendingUpload=files;showUploadPreview(results);
}
async function importUpload(files){
  uploadProgress('导入发票',files.length);
  const results=[];
  for(const file of files){
    updateUploadProgress(results.length,files.length,file.name,uploadSummary(results),'已处理');
    const data=new FormData();data.append('files',file);
    try{const [item]=await (await api('/api/invoices/upload',{method:'POST',body:data})).json();results.push(item)}
    catch(error){
      if(!dialog.open)return;
      results.push({name:file.name,unknown:true});
      for(const remaining of files.slice(results.length))results.push({name:remaining.name,unprocessed:true});
      break;
    }
    updateUploadProgress(results.length,files.length,file.name,uploadSummary(results),'已处理');
  }
  delete form.dataset.uploading;
  try{await load()}catch{toast('导入结果已返回，刷新页面可更新发票库')}
  if(dialog.open)showUploadResults(results);
}
function uploadSummary(results){
  const added=results.filter(x=>x.created&&!x.error).length,duplicates=results.filter(x=>!x.error&&!x.unknown&&!x.unprocessed&&!x.created).length,failed=results.filter(x=>x.error).length,review=results.filter(x=>!x.error&&x.recognition_status==='识别失败').length;
  return `新增 ${added} 个 · 已存在 ${duplicates} 个 · 导入失败 ${failed} 个 · 识别待核对 ${review} 个`;
}
function showDeleteDialog(ids){
  const items=ids.map(id=>state.selectedRecords.get(id)||state.invoices.find(item=>item.id===id)).filter(Boolean);if(!items.length)return;
  if(items.length>500){toast('一次最多删除 500 张发票');return}
  pendingDelete=items.map(item=>({id:item.id,sha256:item.sha256}));if(dialog.open)dialog.close();
  const grouped=items.filter(item=>item.group_name).length,reimbursed=items.filter(item=>item.reimbursed).length;
  const summary=items.length===1?`“${esc(items[0].original_name)}”`:`所选的 ${items.length} 张发票`;
  const related=grouped?`其中 ${grouped} 张已加入报销组，删除后将从组内移除；`:'';
  const paid=reimbursed?`其中 ${reimbursed} 张已标记报销；`:'';
  openDialog('删除发票',`<p class="sub">确定删除${summary}？</p><p class="sub">发票记录和保存的原件将被删除，邮件会保留。${related}${paid}此操作无法撤销。</p>`,'删除发票');
  form.dataset.submit='delete-invoices';form.querySelector('[type=submit]').classList.replace('primary','delete-confirm');
}
function showUploadPreview(results){
  const labels={existing_file:'原件已存在',existing_invoice:'发票号码已存在，可能重复',batch_file:'与本次文件内容重复',batch_invoice:'与本次文件的发票号码相同，可能重复'};
  const rows=results.map((item,index)=>{
    const f=item.fields||{},status=item.error||labels[item.duplicate]||(!f.invoice_number?'未识别发票号码，需核对是否重复':'未发现重复');
    const image=item.thumbnail?`<img src="data:image/jpeg;base64,${esc(item.thumbnail)}" alt="发票首页缩略图">`:'<span>无缩略图</span>';
    const info=[f.seller?`商户：${f.seller}`:'',f.buyer?`抬头：${f.buyer}`:'',f.amount_cents!=null?`金额：${cash(f.amount_cents)}`:'',f.invoice_date?`开票：${f.invoice_date}`:'',f.invoice_number?`票号：${f.invoice_number}`:''].filter(Boolean);
    return `<label class="upload-preview-row"><input type="checkbox" data-upload-choice="${index}" aria-label="选择 ${esc(item.name)}" ${item.error?'disabled':item.duplicate?'':'checked'}><span class="upload-thumbnail">${image}</span><span class="upload-preview-info"><strong>${esc(item.name)}</strong><span class="upload-preview-status ${item.error||item.duplicate?'warning':''}">${esc(status)}${item.matched_name?`：${esc(item.matched_name)}`:''}</span>${info.length?`<span>${info.map(esc).join(' · ')}</span>`:''}${item.reason?`<span>识别待核对：${esc(item.reason)}</span>`:''}</span></label>`
  }).join('');
  openDialog('选择要导入的发票',`<p class="sub">请核对发票信息并选择需要导入的文件。重复项默认不选，可手动勾选。</p><label class="upload-select-all"><input id="upload-select-all" type="checkbox"> 全选可导入文件（含重复项）</label><div class="upload-preview-list">${rows}</div>`,'导入所选');
  form.dataset.submit='upload-preview';updateUploadSelection();
}
function showUploadResults(results){
  const review=results.filter(x=>!x.error&&x.recognition_status==='识别失败').length,interrupted=results.some(x=>x.unknown);
  const rows=results.map(x=>`<li><span>${esc(x.name||'未命名文件')}</span><div><strong>${x.unknown?'结果未知':x.unprocessed?'未处理':x.error?'导入失败':x.created?'已导入':'已存在'}</strong>${x.unknown?'<small>连接中断，请先检查发票库再重试</small>':x.unprocessed?'<small>尚未提交，可重新选择导入</small>':x.error?`<small>${esc(x.error)}</small>`:x.recognition_status==='识别失败'?`<small>识别失败：${esc(x.failure_reason||'请检查发票')}</small>`:''}</div></li>`).join('');
  openDialog('批量导入结果',`<p class="sub">${uploadSummary(results)}</p>${interrupted?'<p class="hint">连接中断，部分文件未处理；结果未知的文件请先检查发票库。</p>':''}<ul class="upload-results">${rows}</ul>${review?'<button class="button link" type="button" data-go="failed">查看识别失败的发票</button>':''}`);
  delete form.dataset.submit;form.querySelector('.dialog-actions button').textContent='完成';
}
const pdfPages=new Map();
function pdfPager(){return `<div class="pdf-pager" hidden><button class="button" type="button" data-pdf-step="-1" aria-label="上一页">${uiIcon('chevron-left')}</button><span role="status" aria-live="polite" data-pdf-page></span><button class="button" type="button" data-pdf-step="1" aria-label="下一页">${uiIcon('chevron-right')}</button></div><p class="pdf-page-error" role="status" hidden>无法获取页数，请打开原件查看。</p>`}
async function initPdfPreviews(root){
  for(const viewer of root.querySelectorAll('[data-pdf-viewer]')){
    const pager=viewer.querySelector('.pdf-pager'),message=viewer.querySelector('.pdf-page-error');
    try{
      const result=await (await api(`/api/invoices/${viewer.dataset.pdfViewer}/preview?info=true`)).json();
      if(!viewer.isConnected)continue;
      if(!Number.isInteger(result.pages)||result.pages<1)throw Error('无法读取页数');
      viewer.dataset.pdfTotal=result.pages;viewer.dataset.pdfCurrent=Math.min(result.pages,pdfPages.get(Number(viewer.dataset.pdfViewer))||1);
      if(result.pages>1){pager.hidden=false;updatePdfPager(viewer)}
    }catch{if(viewer.isConnected)message.hidden=false}
  }
}
function updatePdfPager(viewer){
  const page=Number(viewer.dataset.pdfCurrent),total=Number(viewer.dataset.pdfTotal);
  viewer.querySelector('[data-pdf-page]').textContent=`第 ${page} / ${total} 页`;
  viewer.querySelector('[data-pdf-step="-1"]').disabled=page<=1;
  viewer.querySelector('[data-pdf-step="1"]').disabled=page>=total;
}
function turnPdfPage(button){
  const viewer=button.closest('[data-pdf-viewer]'),page=Number(viewer.dataset.pdfCurrent)+Number(button.dataset.pdfStep),total=Number(viewer.dataset.pdfTotal);
  if(page<1||page>total)return;
  pdfPages.set(Number(viewer.dataset.pdfViewer),page);viewer.dataset.pdfCurrent=page;const image=viewer.querySelector('img');image.onload=()=>{uiMotion(image);image.onload=null};image.src=`/api/invoices/${viewer.dataset.pdfViewer}/preview?page=${page}`;
  viewer.querySelector('img').alt=`PDF 第 ${page} 页预览`;updatePdfPager(viewer);
}
function invoicePreview(current,inDialog=false){
  const ext=current?(current.stored_name.split('.').pop()||'').toLowerCase():'',page=pdfPages.get(current?.id)||1;
  return current?`${inDialog?'':`<div class="preview-heading"><span>发票详情</span><button class="button text-button" data-edit="${current.id}">编辑</button></div>`}<div class="preview-head"><h2>${esc(current.seller||'未填写商户')}</h2><p>${esc(current.original_name)}</p></div>${ext==='pdf'?`<div data-pdf-viewer="${current.id}"><div class="preview-document"><img src="/api/invoices/${current.id}/preview?page=${page}" alt="PDF 第 ${page} 页预览"></div>${pdfPager()}</div>`:`<div class="preview-document">${/^(png|jpg|jpeg)$/.test(ext)?`<img src="/api/invoices/${current.id}/original" alt="发票原件预览">`:`<div class="preview-unavailable">${esc(ext.toUpperCase()||'该格式')} 原件可下载查看</div>`}</div>`}<dl class="preview-fields"><dt>发票抬头</dt><dd>${esc(current.buyer||'—')}</dd><dt>开票日期</dt><dd>${date(current.invoice_date)}</dd><dt>发票号码</dt><dd>${esc(current.invoice_number||'—')}</dd><dt>发票金额</dt><dd><strong>${cash(current.amount_cents)}</strong></dd><dt>导入日期</dt><dd>${date(current.imported_at)}</dd><dt>发票来源</dt><dd class="preview-source">${sourceDetails(current)}</dd></dl>${current.other_originals?.length?`<div class="preview-originals">其他原件 ${current.other_originals.map(file=>`<a href="/api/invoices/${current.id}/originals/${file.id}" target="_blank" rel="noopener">${esc(file.name)}</a>`).join(' · ')}</div>`:''}<div class="preview-actions"><a class="button preview-link" href="/api/invoices/${current.id}/original" target="_blank" rel="noopener">${ext==='pdf'||/^(png|jpg|jpeg)$/.test(ext)?'打开原件':'下载原件'}</a>${inDialog?`<button class="button" type="button" data-edit="${current.id}">编辑信息</button>`:''}<button class="button danger" type="button" data-delete="${current.id}">删除</button></div>`:`<div class="preview-heading"><span>发票详情</span></div>${empty('选择发票后查看原件和详情。')}`;
}
function manualInvoicePreview(invoice){
  const ext=(invoice.stored_name.split('.').pop()||'').toLowerCase(),visual=ext==='pdf'||/^(png|jpe?g)$/.test(ext),text=ext==='xml'||ext==='ofd';
  const original=`/api/invoices/${invoice.id}/original`,source=ext==='pdf'?`/api/invoices/${invoice.id}/preview?page=${pdfPages.get(invoice.id)||1}`:original;
  const label=ext==='pdf'?'PDF 原件预览':visual?'图片原件预览':text?`${ext.toUpperCase()} 文字预览`:'原件预览';
  return `<section class="manual-preview" aria-label="${label}" ${ext==='pdf'?`data-pdf-viewer="${invoice.id}"`:''}><div class="manual-preview-header"><strong>${label}</strong><a href="${original}" target="_blank" rel="noopener">${visual?'打开原件':'下载原件'}</a></div>${visual?`<a class="manual-preview-image" href="${original}" target="_blank" rel="noopener" aria-label="打开原件查看大图"><img src="${source}" alt="${label}" data-manual-image></a><p class="manual-preview-error" role="status" hidden>预览加载失败，请打开原件查看。</p>${ext==='pdf'?pdfPager():''}`:text?`<pre class="manual-preview-text" data-manual-text>正在加载原件内容…</pre><p class="manual-preview-note">文字预览不代表原件版式，请以原件为准。</p>`:'<p class="manual-preview-note">此格式暂不支持预览，请下载原件查看。</p>'}</section>`;
}
async function loadManualPreview(invoice){
  const image=form.querySelector('[data-manual-image]'),error=form.querySelector('.manual-preview-error');
  if(image){const showError=()=>{if(image.isConnected){image.parentElement.hidden=true;error.hidden=false}};image.addEventListener('error',showError,{once:true});if(image.complete&&!image.naturalWidth)showError()}
  const text=form.querySelector('[data-manual-text]');if(!text)return;
  try{
    const result=await (await api(`/api/invoices/${invoice.id}/text-preview`)).json();
    if(text.isConnected)text.textContent=result.text?(result.text+(result.truncated?'\n…内容较长，已截断。':'')):'未能从原件中提取文字，请下载原件查看。';
  }catch{if(text.isConnected)text.textContent='无法加载原件内容，请下载原件查看。'}
}
function invoicePage(){
  const filtered=state.invoices,selected=[...state.selectedRecords.values()];
  const current=filtered.find(x=>x.id===state.preview)||filtered[0];
  const byDay=new Map();
  filtered.forEach(x=>{const day=x.imported_at?x.imported_at.slice(0,10):'未知日期';if(!byDay.has(day))byDay.set(day,[]);byDay.get(day).push(x)});
  const rows=[...byDay].map(([day,items])=>`<tr class="date-row"><td colspan="5"><strong>${esc(day)}</strong><span>${items.length} 张</span></td></tr>${items.map(x=>`<tr class="invoice-row ${cash(x.amount_cents).length>12?'wide-amount':''} ${current&&current.id===x.id?'focused':''} ${state.selected.has(x.id)?'is-selected':''}"><td class="check-cell"><label class="check-hit"><input type="checkbox" data-select="${x.id}" aria-label="选择 ${esc(x.original_name)}" ${state.selected.has(x.id)?'checked':''}></label></td><td class="name-cell"><button class="invoice-name" data-preview="${x.id}" title="${esc(x.seller||'未填写商户')}"><strong>${esc(x.seller||'未填写商户')}</strong><span title="${esc(x.original_name)}">${esc(x.original_name)}</span><span class="invoice-source" title="${esc(sourceSummary(x))}">${esc(sourceSummary(x))}</span></button></td><td class="invoice-date">${date(x.invoice_date)}</td><td class="amount">${cash(x.amount_cents)}</td><td class="invoice-status"><span class="status ${x.reimbursed?'done':''}">${x.reimbursed?'已报销':'未报销'}</span></td></tr>`).join('')}`).join('');
  const selectionBar=selected.length?`<div class="selection-bar" role="region" aria-label="已选发票操作"><div class="selection-summary"><strong>已选择 ${selected.length} 张</strong><span>合计 ${cash(selected.reduce((n,x)=>n+(x.amount_cents||0),0))}</span></div><div class="selection-actions"><button class="button primary" data-action="group">创建报销组</button><details class="export-menu"><summary class="button">导出原件 ${uiIcon('chevron-down')}</summary><div class="export-options"><button type="button" data-action="export" aria-label="导出原格式 ZIP">导出原格式 ZIP</button><button type="button" data-action="export-pdf" aria-label="仅导出 PDF ZIP">仅导出 PDF ZIP</button><p class="export-note">OFD、XML 将保留原格式</p></div></details><button class="button danger" type="button" data-action="delete-selected">删除</button></div></div>`:'';
  const actions=`<button class="button header-export" data-action="export" ${selected.length?'':'disabled'}>导出原件</button><button class="button primary" data-action="upload">${uiIcon('plus')}导入发票</button><input id="upload" type="file" accept=".pdf,.ofd,.xml,.jpg,.jpeg,.png,application/pdf,application/xml,text/xml,image/jpeg,image/png" multiple hidden>`;
  const summary=`<div class="invoice-summary"><div class="invoice-metrics"><div><p>未报销金额</p><strong class="metric-amount">${cash(state.unpaidAmount)}</strong></div><div><p>未报销</p><strong>${state.unpaidCount}<small>张</small></strong></div><div><p>已报销</p><strong>${state.invoiceCount-state.unpaidCount}<small>张</small></strong></div></div><button class="review-summary ${state.reviewCount?'has-review':''}" data-go="failed"><span><strong>${state.reviewCount} 项待核对</strong><small>${state.reviewCount?'发票与邮件检查':'暂时没有待处理事项'}</small></span>${uiIcon('arrow-up-right')}</button></div>`;
  return header('发票库',`共 ${state.invoiceCount} 张发票，收好每一笔支出。`,actions)+summary+`<div class="invoice-layout"><section class="invoice-list ${selected.length?'has-selection':''}"><div class="invoice-tabs" aria-label="报销状态筛选">${['全部','未报销','已报销'].map(f=>`<button class="tab ${state.filter===f?'active':''}" data-filter="${f}" aria-pressed="${state.filter===f}">${f}</button>`).join('')}</div><div class="invoice-toolbar"><label class="search-field">${uiIcon('search')}<input id="search" type="search" aria-label="搜索发票" placeholder="搜索商户、抬头或文件名" value="${esc(state.query)}"></label><span class="sort-label">按导入时间整理</span></div><div class="tablewrap invoice-table"><table><colgroup><col class="col-check"><col class="col-name"><col class="col-date"><col class="col-amount"><col class="col-status"></colgroup><thead><tr><th><label class="check-hit"><input type="checkbox" id="select-visible" aria-label="选择当前列表全部发票" ${filtered.length&&filtered.every(x=>state.selected.has(x.id))?'checked':''}></label></th><th>商户 / 文件名</th><th class="invoice-date">开票日期</th><th class="amount">金额</th><th>报销状态</th></tr></thead><tbody>${rows}</tbody></table>${filtered.length?'':empty(state.query||state.filter!=='全部'?'没有符合条件的发票。':'暂无发票。上传原件或连接邮箱后，发票会显示在这里。')}</div><div class="invoice-list-footer"><span>${state.invoiceTotal} 张发票</span><span>按导入时间整理</span></div>${listPager(state.invoiceTotal,state.invoicePage,'invoice')}${selectionBar}</section><aside class="invoice-preview" aria-label="发票预览">${invoicePreview(current)}</aside></div>`;
}
function groupPage(){return header('报销组','将发票整理成组，统一管理报销进度。',`<button class="button primary" data-go="invoices">${uiIcon('plus')}选择发票成组</button>`)+`<div class="section-head"><h2>我的报销组 <span class="muted">${state.groups.length} 组</span></h2></div>${state.groups.length?`<div class="grid">${state.groups.map(g=>`<section class="box group"><div class="section-head"><div><h2>${esc(g.name)}</h2><div class="muted">创建于 ${date(g.created_at)} · ${g.invoices.length} 张发票</div></div><span class="badge ${g.reimbursed?'gray':''}">${g.reimbursed?'已报销':'待报销'}</span></div><p class="muted">报销金额</p><strong>${cash(g.invoices.reduce((n,i)=>n+(i.amount_cents||0),0))}</strong><div class="actions"><button class="button ${g.reimbursed?'':'primary'}" data-group-status="${g.id}" data-status="${g.reimbursed?0:1}">${g.reimbursed?'撤销报销标记':'整组标记已报销'}</button><button class="button" data-group-detail="${g.id}">查看组内发票</button></div></section>`).join('')}</div>`:empty('还没有报销组，前往发票库选择发票创建。')}`}
function emailStatusFlags(message){
  const count=Number(message.invoice_count)||0,recognized=Number(message.recognized_count)||0,failed=Number(message.failed_count)||0,duplicates=Number(message.duplicate_count)||0;
  const uncertain=['发票已删除','待处理','导入失败','发票链接待处理','识别失败'].includes(message.status);
  return {count,recognized,failed,duplicates,invoice:count>0,has:count>0&&(recognized>0||duplicates>0),none:count===0&&!uncertain};
}
function emailLabels(message){
  const {count,recognized,failed,duplicates,has,none}=emailStatusFlags(message);
  const labels=[];
  if(count){
    labels.push([has?'有发票':'疑似发票','invoice']);
    if(recognized)labels.push(['识别成功 '+recognized+' 张','success']);
    if(failed)labels.push(['识别失败 '+failed+' 张','failure']);
    if(duplicates)labels.push(['重复发票 '+duplicates+' 张','neutral']);
    if(!recognized&&!failed&&!duplicates)labels.push(['待识别','pending']);
  }else if(message.status==='发票已删除'){
    labels.push(['已检查','neutral'],['发票已删除','pending']);
  }else if(!none){
    labels.push(['发票待确认','pending']);
  }else{
    labels.push(['已检查','neutral'],['未发现发票','neutral']);
  }
  if(message.status==='待处理')labels.push(['邮件待处理','pending']);
  if(message.status==='导入失败'||(message.status==='识别失败'&&!count))labels.push(['邮件处理失败','failure']);
  if(message.status==='发票链接待处理')labels.push(['链接待处理','pending']);
  return `<div class="email-labels" aria-label="邮件发票状态">${labels.map(([label,tone])=>`<span class="email-tag ${tone}">${label}</span>`).join('')}</div>`;
}
function emailFilterSelect(id,value,options){
  const selected=options.find(([key])=>key===value)||options[0];
  return `<span class="email-select"><span class="email-select-value" aria-hidden="true"><span>${esc(selected[1])}</span>${uiIcon('chevron-down')}</span><select id="${id}">${options.map(([key,label])=>`<option value="${esc(key)}" ${key===value?'selected':''}>${esc(label)}</option>`).join('')}</select></span>`;
}
function emailsPage(){const list=state.emails;return header('全部邮件','集中查看已连接邮箱各文件夹的邮件。','<button class="button" data-go="accounts">管理邮箱</button>')+`<div class="notice">${state.accounts.length} 个邮箱 · 自动定时同步</div><div class="toolbar email-filters"><div class="email-filter-field"><label for="account-filter">邮箱</label>${emailFilterSelect('account-filter',state.account,[['','全部邮箱'],...state.accounts.map(a=>[String(a.id),a.name])])}</div><div class="email-filter-field"><label for="email-status-filter">发票状态</label>${emailFilterSelect('email-status-filter',state.emailFilter,[['all','全部状态'],['has','有发票'],['suspected','疑似发票'],['none','未发现发票'],['recognized','识别成功'],['failed','识别失败'],['deleted','发票已删除'],['duplicate','重复发票'],['pending','待处理'],['processing_failed','邮件处理失败'],['link_pending','链接待处理']])}</div></div><div class="section-head"><h2>全部文件夹 <span class="muted" aria-live="polite">${state.emailTotal} 封邮件</span></h2></div><div class="box list-box">${list.length?list.map(m=>`<div class="row email-row"><strong title="${esc(m.sender||'未知发件人')}">${esc(m.sender||'未知发件人')}</strong><div><button class="button link" data-email="${m.id}" title="${esc(m.subject||'无主题')}">${esc(m.subject||'无主题')}</button><div class="muted">${esc(m.account_name)} · ${esc(m.folder)}</div>${emailLabels(m)}<p class="email-feedback" role="status" data-reprocess-status="${m.id}">${esc(mailReprocessFeedback.get(m.id)||'')}</p></div><div class="email-row-meta"><span class="muted">${date(m.received_at)}</span><button class="button" type="button" data-reprocess="${m.id}" aria-label="重新识别 ${esc(m.subject||'无主题')}" ${mailReprocessBusy.has(m.id)?'disabled aria-busy="true"':''}>${mailReprocessBusy.has(m.id)?'识别中…':'重新识别'}</button></div></div>`).join(''):empty(state.account||state.emailFilter!=='all'?'没有符合条件的邮件，请调整筛选条件。':'没有邮件。添加邮箱并同步后，邮件会显示在这里。')}</div>${listPager(state.emailTotal,state.emailPage,'email')}`}
function failedPage(){
  const list=state.invoices.filter(x=>x.recognition_status==='识别失败'&&x.duplicate_state!=='confirmed');
  const confirmed=state.invoices.filter(x=>x.duplicate_state==='confirmed');
  const pending=state.emails.filter(x=>['导入失败','发票链接待处理'].includes(x.status));
  const invoiceRow=i=>`<div class="row failed-invoice"><strong>${esc(i.original_name)}</strong><div><span class="badge fail">${i.duplicate_state==='confirmed'?'已确认重复':esc(i.failure_reason)}</span><div class="muted">${esc(sourceSummary(i))} · ${date(i.imported_at)}</div></div><div class="actions">${i.email_id?`<button class="button link" data-email="${i.email_id}">查看邮件</button>`:''}${i.duplicate_of?`<a class="button" href="/api/invoices/${i.id}/original" target="_blank" rel="noopener">查看本张原件</a><button class="button" data-view-duplicate="${i.duplicate_of}">查看相同票号发票</button>`:''}${i.duplicate_state==='confirmed'?`<button class="button" data-duplicate-decision="reopen" data-id="${i.id}">撤销重复标记</button>`:`${i.duplicate_of?`<button class="button" data-duplicate-decision="confirm" data-id="${i.id}">确认重复</button><button class="button" data-duplicate-decision="distinct" data-id="${i.id}">确认非重复</button>`:i.duplicate_state==='unverified'?`<button class="button" data-duplicate-decision="distinct" data-id="${i.id}">确认无票号并继续</button>`:''}<button class="button primary" data-edit="${i.id}">人工补充</button><button class="button" data-retry="${i.id}">重新识别</button><button class="button" data-ai="${i.id}">AI 辅助识别</button>`}<button class="button danger" type="button" data-delete="${i.id}">删除</button></div></div>`;
  return header('识别失败',`${list.length} 张发票及 ${state.pendingTotal} 封邮件需要人工检查。`)+`<div class="box list-box">${list.length?list.map(invoiceRow).join(''):pending.length?'':empty('没有待处理的发票。')}${pending.map(m=>`<div class="row"><strong>${esc(m.subject||'无主题')}</strong><div><span class="badge fail">${esc(m.status)}</span><div class="muted">${esc(m.account_name)} · ${esc(m.folder)} · ${date(m.received_at)}</div></div><button class="button" data-email="${m.id}">查看邮件</button></div>`).join('')}</div>${listPager(state.pendingTotal,state.pendingPage,'pending')}${confirmed.length?`<div class="section-head" style="margin-top:24px"><h2>已确认重复 <span class="muted">${confirmed.length} 张</span></h2></div><div class="box list-box">${confirmed.map(invoiceRow).join('')}</div>`:''}`;
}
function accountsPage(){return header('邮箱管理','同时监控多个 QQ 邮箱，各自记录同步状态。',`<button class="button primary" data-action="add-account">${uiIcon('plus')}添加邮箱</button>`)+`<div class="section-head"><h2>已连接的邮箱 <span class="muted">${state.accounts.length} 个</span></h2></div><div class="box list-box">${state.accounts.length?state.accounts.map(a=>`<div class="row account-row"><strong>${esc(a.name)}</strong><div>${esc(a.email)}<div class="muted">${!a.enabled?'已暂停':a.last_error?esc(a.last_error):a.last_sync?'最近同步 '+esc(a.last_sync.replace('T',' ').slice(0,16)):'等待首次同步'}</div></div><div class="actions"><button class="button" data-sync="${a.id}">立即同步</button><button class="button" data-toggle-account="${a.id}" data-enabled="${a.enabled?0:1}">${a.enabled?'暂停':'恢复'}</button><button class="button" data-credentials="${a.id}">更新授权码</button></div></div>`).join(''):empty('还未连接邮箱。添加 QQ 邮箱后可自动定时收取邮件。')}</div>`}
function settingsPage(){return header('偏好设置','文件命名、登录安全与数据备份。')+`<div class="settings-sections"><section class="settings-section"><div class="settings-description"><h2>文件命名</h2><p>设置导出原件时使用的文件名称与字段顺序。</p></div><div class="settings-panel"><label class="field" for="template">字段顺序<input id="template" value="${esc(state.settings.join(','))}" aria-describedby="template-help"></label><p class="hint" id="template-help">以逗号分隔，可使用：抬头、商户、金额、开票日期。</p><p class="filename-example" id="template-preview">${esc(templateExample(state.settings))}</p><div class="actions"><button class="button primary" data-action="save-template">保存命名规则</button></div></div></section><section class="settings-section" id="account-settings"><div class="settings-description"><h2>账号与密码</h2><p>管理登录票序使用的账号和密码。</p></div>${securityForm()}</section><section class="settings-section"><div class="settings-description"><h2>备份与恢复</h2><p>妥善保存原件、邮件、报销组和配置。</p></div><div class="settings-panel"><p class="sub">完整备份包含原件与邮箱配置，请保存在受信任的位置。</p><div class="actions"><button class="button" data-go="backup">管理备份 ${uiIcon('arrow-up-right')}</button></div></div></section></div>`}
function templateExample(fields){const example={'抬头':'示例工作室','商户':'示例商户','金额':'128.00','开票日期':'2026-11-02'};return fields.map(key=>example[key]).filter(Boolean).join('_')+'.pdf'}
let pendingRestore=null;
function backupPage(){return header('备份与恢复','保存完整数据，或从票序备份恢复。','<button class="button" data-go="settings">返回偏好设置</button>')+`<div class="grid"><section class="box"><h2>下载备份</h2><p class="sub">备份包含发票原件、邮件、报销组、邮箱及 AI 配置。</p><p class="hint">备份包含解密所需密钥，请妥善保管。</p><div class="actions backup-actions"><a class="button primary" href="/api/backup/export" download>下载完整备份</a></div></section><section class="box"><h2>恢复备份</h2><p class="sub">选择票序生成的 ZIP 备份，校验并确认后恢复。</p><p class="hint">恢复会替换当前数据，系统会保留恢复前副本。</p><div class="actions backup-actions"><button class="button" type="button" data-action="select-restore">选择备份文件</button><input id="restore-file" type="file" accept=".zip,application/zip" hidden></div></section></div>`}
function showRestorePreview(result){const c=result.counts||{},when=result.created_at?new Date(result.created_at).toLocaleString('zh-CN'):'未知';openDialog('确认恢复备份',`<p class="sub">备份时间：${esc(when)}</p><p class="sub">发票 ${Number(c.invoices)||0} 张 · 邮件 ${Number(c.emails)||0} 封 · 邮箱 ${Number(c.accounts)||0} 个 · 报销组 ${Number(c.groups)||0} 组</p><div class="notice" style="margin-top:16px">恢复会替换当前数据和配置；系统会在配置目录保留一份恢复前副本。</div>`,'确认恢复');form.dataset.submit='restore-backup'}
function aiPage(){const a=state.ai;return header('AI 辅助识别','为难以识别的票据，增加一次辅助核对。')+`<form id="ai-settings-form" class="box"><h2>模型服务</h2><p class="sub">启用后，本地识别失败的发票可自动使用 AI 辅助；也可以在识别失败页面手动触发。</p><div class="notice" style="margin-top:16px">使用云端模型时，当前发票的前十页图片或发票文本将发送给所配置的服务商，可能产生费用。邮件正文和邮箱授权码不会发送。</div><div class="fields"><label class="field full">API 地址<input name="base_url" type="url" value="${esc(a.base_url)}" placeholder="https://服务地址/v1" aria-describedby="ai-url-help"><span class="hint" id="ai-url-help">可填写兼容接口的基础地址；Docker 中访问电脑本机服务可使用 host.docker.internal。</span></label><label class="field full">模型名称<input name="model" value="${esc(a.model)}" placeholder="填写支持图片识别的模型名称"></label><label class="field full">API 密钥<input name="api_key" type="password" autocomplete="new-password" placeholder="${a.has_key?'已保存；留空保持不变':'本地服务如不需要可留空'}"><span class="hint">密钥加密保存在本地；更换服务地址时需重新填写。</span></label></div><div class="check-list"><label><input type="checkbox" name="auto_enabled" ${a.auto_enabled?'checked':''}> 本地识别失败后自动使用 AI 辅助</label>${a.has_key?'<label><input type="checkbox" name="clear_key"> 清除已保存的 API 密钥</label>':''}</div><p class="hint">AI 输出仅作为建议，仍需人工核对；请求失败或超时不会自动重复调用。已导入的失败发票可手动触发 AI，不会自动批量处理。</p><div class="inline-error" role="alert"></div><div class="actions" style="justify-content:flex-start;margin-top:18px"><button class="button primary" type="submit">保存 AI 设置</button><button class="button" type="button" data-ai-test="connection">测试连接</button><button class="button" type="button" data-ai-test="recognition">示例识别测试</button></div><p class="hint">示例测试发送虚拟发票图片，不包含真实发票；云端调用可能产生费用。</p><div class="ai-test-result" role="status" aria-live="polite"></div></form>`}

function securityForm(){return `<form id="security-form" class="settings-panel security-form"><label class="field">登录账号<input name="username" value="${esc(state.username)}" required maxlength="100" autocomplete="username"></label><label class="field">当前密码<input name="current_password" type="password" required maxlength="128" autocomplete="current-password"></label><label class="field">新密码<input name="password" type="password" maxlength="128" autocomplete="new-password"><span class="hint">留空则保留当前密码。</span></label><label class="field">确认新密码<input name="confirm_password" type="password" maxlength="128" autocomplete="new-password"></label><div class="inline-error" role="alert"></div><div class="actions" style="justify-content:flex-start;margin-top:20px"><button class="button primary" type="submit">保存账号设置</button><button class="button" type="button" data-action="logout">退出登录</button></div></form>`}
let renderedPage=null,renderedPreview=null;
function render(){
  const pageChanged=renderedPage!==state.page;
  document.body.dataset.page=state.page;
  document.querySelector('#breadcrumb').textContent=state.page==='backup'?'偏好设置 / 备份与恢复':titles[state.page];
  document.querySelectorAll('nav button[data-page]').forEach(b=>{const active=b.dataset.page===(state.page==='backup'?'settings':state.page);b.classList.toggle('active',active);if(active)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current')});
  const more=document.querySelector('#mobile-more'),activeMore=!['invoices','groups','emails'].includes(state.page);more.classList.toggle('active',activeMore);if(activeMore)more.setAttribute('aria-current','page');else more.removeAttribute('aria-current');
  content.innerHTML=({invoices:invoicePage,groups:groupPage,emails:emailsPage,failed:failedPage,accounts:accountsPage,ai:aiPage,settings:settingsPage,backup:backupPage}[state.page])();
  initPdfPreviews(content);
  if(pageChanged)uiMotion(content);else if(state.page==='invoices'&&renderedPreview!==state.preview)uiMotion(content.querySelector('.invoice-preview'));
  renderedPage=state.page;renderedPreview=state.preview;
}

function openDialog(title,body,submit){delete form.dataset.emailView;form.innerHTML=`<div class="dialog-head"><h2 id="dialog-title">${title}</h2><button class="icon-button" type="button" data-close aria-label="关闭">${uiIcon('x')}</button></div>${body}<div class="inline-error" role="alert"></div><div class="dialog-actions"><button class="button" type="button" data-close>取消</button>${submit?`<button class="button primary" type="submit">${submit}</button>`:''}</div>`;if(!dialog.open)dialog.showModal();initPdfPreviews(form)}
function edit(id){const i=state.invoices.find(x=>x.id===id);if(!i)return;let suggestion={};try{suggestion=JSON.parse(i.ai_suggestion||'{}')}catch{}const aiHint=Object.keys(suggestion).length?`<div class="notice" style="margin-top:14px">AI 建议：${['buyer','seller','amount_cents','invoice_date','invoice_number'].map(k=>suggestion[k]===undefined||suggestion[k]===null?'':esc(({buyer:'抬头',seller:'销售方',amount_cents:'金额',invoice_date:'日期',invoice_number:'票号'})[k])+ '：'+esc(k==='amount_cents'?cash(suggestion[k]):suggestion[k])).filter(Boolean).join('　')}<br><button type="button" class="button" data-apply-ai="${i.id}">采用 AI 建议</button></div>`:'';openDialog('发票详情',`<p class="sub">${esc(i.original_name)} · 导入 ${date(i.imported_at)}</p><div class="sub manual-source"><span>来源：</span><div>${sourceDetails(i)}</div></div>${manualInvoicePreview(i)}${i.email_id?`<button type="button" class="button link" data-email="${i.email_id}">查看来源邮件</button>`:''}${aiHint}<div class="fields" style="margin-top:16px"><label class="field">发票抬头<input name="buyer" value="${esc(i.buyer)}"></label><label class="field">消费商户 / 销售方<input name="seller" value="${esc(i.seller)}"></label><label class="field">金额（元）<input name="amount" type="number" min="0" step="0.01" value="${i.amount_cents==null?'':(i.amount_cents/100).toFixed(2)}"></label><label class="field">开票日期<input name="invoice_date" type="date" value="${esc(i.invoice_date)}"></label><label class="field">发票号码<input name="invoice_number" value="${esc(i.invoice_number)}"></label><label class="field">自定义导出文件名<input name="export_name" value="${esc(i.export_name)}" placeholder="留空使用命名规则"></label></div>`, '保存');form.dataset.submit='invoice';form.dataset.id=id;loadManualPreview(i)}
function addAccount(){openDialog('添加 QQ 邮箱','<p class="sub">请在 QQ 邮箱设置中开启 IMAP，并使用邮箱授权码。</p><div class="fields" style="margin-top:16px"><label class="field full">邮箱名称<input name="name" required placeholder="例如：工作邮箱"></label><label class="field full">QQ 邮箱地址<input type="email" name="email" required placeholder="name@qq.com"></label><label class="field full">邮箱授权码<input type="password" name="auth_code" required autocomplete="new-password"></label></div>','添加邮箱');form.dataset.submit='account'}
function emailFrameContent(markup){
  const images='data:';
  return `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src ${images}; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"><style>body{margin:16px;color:#202833;font:14px/1.6 -apple-system,BlinkMacSystemFont,sans-serif;overflow-wrap:anywhere}img{max-width:100%;height:auto}table{max-width:100%}</style></head><body>${markup}</body></html>`;
}
function renderEmailBody(message,notice=''){
  const host=form.querySelector('.email-body');if(!host)return;
  host.replaceChildren();
  if(message.html_body){
    const frame=document.createElement('iframe');frame.className='email-frame';frame.title='邮件正文';frame.setAttribute('sandbox','');frame.referrerPolicy='no-referrer';frame.srcdoc=emailFrameContent(message.html_body);host.append(frame);
    if(message.remote_images?.length)loadEmailImages(message,frame,host);
  }else{
    const text=document.createElement('div');text.className='detail';text.textContent=message.body||'邮件无纯文本内容';host.append(text);
  }
  if(notice){const note=document.createElement('p');note.className='hint';note.textContent=notice;host.prepend(note)}
}
async function loadEmailImages(message,frame,host){
  const note=document.createElement('p');note.className='hint';note.setAttribute('role','status');note.textContent='正在安全加载邮件图片…';host.prepend(note);
  const images=message.remote_images,results=new Map();let cursor=0,total=0,failed=0;
  async function worker(){
    while(cursor<images.length&&frame.isConnected){
      const image=images[cursor++];
      try{
        const response=await api('/api/email-image',{method:'POST',body:JSON.stringify({url:image.url})});
        const blob=await response.blob();total+=blob.size;
        if(total>20*1024*1024)throw Error('图片总量过大');
        const encoded=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=reject;reader.readAsDataURL(blob)});
        results.set(image.marker,encoded);
      }catch{failed++}
    }
  }
  await Promise.all(Array.from({length:Math.min(4,images.length)},worker));
  if(!frame.isConnected)return;
  let markup=message.html_body;
  for(const [marker,data] of results)markup=markup.split(marker).join(data);
  frame.srcdoc=emailFrameContent(markup);
  if(failed)note.textContent='部分邮件图片无法安全加载。';else note.remove();
}
function emailMeta(message){
  const attachments=message.attachments.length?message.attachments.map(i=>esc(i.name)).join('、'):'无';
  const invoices=message.invoices.length?message.invoices.map(i=>esc(i.original_name)).join('、'):'无';
  const busy=mailReprocessBusy.has(message.id);
  return `${emailLabels(message)}<p class="sub">附件：${attachments}<br>关联发票：${invoices}</p><p class="hint">从邮箱重新读取附件和链接；已存在发票不会重复导入。</p><div class="email-reprocess"><button class="button" type="button" data-reprocess="${message.id}" ${busy?'disabled aria-busy="true"':''}>${busy?'识别中…':'重新识别邮件'}</button><span class="hint" role="status" aria-live="polite" data-reprocess-status="${message.id}">${esc(mailReprocessFeedback.get(message.id)||(busy?'正在从邮箱重新读取附件和发票链接，请稍候…':''))}</span></div>`;
}
function reprocessFeedback(id,message){
  mailReprocessFeedback.set(id,message);
  document.querySelectorAll(`[data-reprocess-status="${id}"]`).forEach(item=>item.textContent=message);
}
async function reprocessMessage(button){
  const id=Number(button.dataset.reprocess);if(mailReprocessBusy.has(id))return;mailReprocessBusy.add(id);
  document.querySelectorAll(`[data-reprocess="${id}"]`).forEach(item=>{item.disabled=true;item.textContent='识别中…';item.setAttribute('aria-busy','true')});
  reprocessFeedback(id,'正在从邮箱重新读取附件和发票链接，请稍候…');
  try{
    const result=await (await api(`/api/emails/${id}/reprocess`,{method:'POST'})).json();
    const parts=[];
    if(result.created)parts.push(`新增 ${result.created} 张`);
    if(result.existing)parts.push(`已存在 ${result.existing} 张`);
    if(result.retried)parts.push(`重新识别 ${result.retried} 张`);
    if(result.errors||result.retry_failed)parts.push(`${(result.errors||0)+(result.retry_failed||0)} 项处理失败`);
    if(!result.files_found&&!result.errors)parts.push('未发现可识别的发票文件');
    if(result.status==='识别失败')parts.push('有发票需要核对');
    const summary=parts.join(' · ')||'邮件处理完成';
    reprocessFeedback(id,summary);
    try{
      if(['emails','failed','invoices'].includes(state.page))await load();
      if(dialog.open&&form.dataset.emailView===String(id)){
        const message=await (await api('/api/emails/'+id)).json();
        if(dialog.open&&form.dataset.emailView===String(id))form.querySelector('.fileline').innerHTML=emailMeta(message);
      }
    }catch{reprocessFeedback(id,summary+'；页面更新失败，请刷新后查看')}
    toast(mailReprocessFeedback.get(id));
  }catch(error){reprocessFeedback(id,error.message);toast(error.message)}
  finally{mailReprocessBusy.delete(id);document.querySelectorAll(`[data-reprocess="${id}"]`).forEach(item=>{item.disabled=false;item.textContent=item.closest('.email-reprocess')?'重新识别邮件':'重新识别';item.removeAttribute('aria-busy')})}
}

async function showEmail(id){
  let message=await (await api('/api/emails/'+id)).json();
  if(dialog.open)dialog.close();
  openDialog(esc(message.subject||'无主题'),`<p class="sub">${esc(message.sender)} · ${esc(message.account_name)} · ${date(message.received_at)}</p><div class="email-body" style="margin-top:16px"></div><div class="fileline">${emailMeta(message)}</div>`);
  delete form.dataset.submit;form.dataset.emailView=String(id);
  const needsRefresh=!message.html_checked||message.html_version<2;
  renderEmailBody(needsRefresh?{...message,html_body:''}:message,needsRefresh?'正在从邮箱读取原始排版…':'');
  if(needsRefresh){
    try{message=await (await api('/api/emails/'+id+'/refresh-html',{method:'POST'})).json();if(dialog.open&&form.dataset.emailView===String(id))renderEmailBody(message)}
    catch{if(dialog.open&&form.dataset.emailView===String(id))renderEmailBody(message,message.html_body?'暂时无法更新排版，已显示本地保存的版本。':'暂时无法读取原始排版，已显示保存的文本。')}
  }
}
async function action(name){if(name==='delete-selected'){showDeleteDialog([...state.selected]);return}if(name==='logout'){await api('/api/auth/logout',{method:'POST'});showLogin('已退出登录');return}if(name==='select-restore')return document.querySelector('#restore-file').click();if(name==='upload')return document.querySelector('#upload').click();if(name==='add-account')return addAccount();if(name==='group'){openDialog('创建报销组',`<p class="sub">已选择 ${state.selected.size} 张发票。</p><div class="field" style="margin-top:16px"><label>报销组名称<input name="name" required placeholder="例如：11 月差旅"></label></div>`,'创建');form.dataset.submit='group';return}if(name==='export'||name==='export-pdf'){const response=await api('/api/invoices/export',{method:'POST',body:JSON.stringify({ids:[...state.selected],pdf_only:name==='export-pdf'})});const url=URL.createObjectURL(await response.blob());const a=document.createElement('a');a.href=url;a.download='invoices.zip';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);return}if(name==='save-template'){const fields=document.querySelector('#template').value.split(/[,，]/).map(x=>x.trim()).filter(Boolean);await api('/api/settings',{method:'PUT',body:JSON.stringify({fields})});toast('命名规则已保存');await load()}}
function closeNav(){const toggle=document.querySelector('#nav-toggle');toggle.setAttribute('aria-expanded','false');document.querySelector('#mobile-more').setAttribute('aria-expanded','false');toggle.querySelector('use').setAttribute('href','/icons.svg?v=6#menu');toggle.setAttribute('aria-label','展开菜单');document.querySelector('.shell > aside').classList.remove('menu-open');document.querySelector('#nav-backdrop').hidden=true;document.querySelector('main').inert=false;document.body.classList.remove('nav-open')}
async function handleClick(e){const b=e.target.closest('button');if(!b)return;try{if(b.dataset.pdfStep){turnPdfPage(b);return}if(b.dataset.emailPage||b.dataset.pendingPage||b.dataset.invoicePage){const key=b.dataset.emailPage?'emailPage':b.dataset.pendingPage?'pendingPage':'invoicePage';state[key]+=Number(b.dataset.emailPage||b.dataset.pendingPage||b.dataset.invoicePage);await load();return}if(b.id==='nav-toggle'||b.id==='mobile-more'){const toggle=document.querySelector('#nav-toggle'),more=document.querySelector('#mobile-more'),open=toggle.getAttribute('aria-expanded')!=='true';toggle.setAttribute('aria-expanded',String(open));more.setAttribute('aria-expanded',String(open));toggle.querySelector('use').setAttribute('href',open?'/icons.svg?v=6#x':'/icons.svg?v=6#menu');toggle.setAttribute('aria-label',open?'关闭菜单':'展开菜单');document.querySelector('.shell > aside').classList.toggle('menu-open',open);document.querySelector('#nav-backdrop').hidden=!open;document.querySelector('main').inert=open;document.body.classList.toggle('nav-open',open);return}if(b.hasAttribute('data-close')){dialog.close();return}if(b.dataset.page||b.dataset.go){if(dialog.open)dialog.close();const fromMenu=b.dataset.page&&document.querySelector('#nav-toggle').getAttribute('aria-expanded')==='true';closeNav();navigate(b.dataset.page||b.dataset.go);clearSelection();await load();if(fromMenu){const heading=content.querySelector('h1');heading.tabIndex=-1;heading.focus({preventScroll:true})}return}if(b.dataset.filter){const filter=b.dataset.filter;state.filter=filter;state.invoicePage=0;await load();content.querySelector(`[data-filter="${filter}"]`)?.focus({preventScroll:true});return}if(b.dataset.preview){const id=Number(b.dataset.preview);if(!content.querySelector('.invoice-preview')?.getClientRects().length){const current=state.invoices.find(x=>x.id===id);if(current){openDialog('发票详情',`<div class="dialog-preview">${invoicePreview(current,true)}</div>`);form.querySelector('.dialog-actions').remove();delete form.dataset.submit}return}state.preview=id;render();content.querySelector(`[data-preview="${id}"]`)?.focus({preventScroll:true});return}if(b.dataset.action){const menu=b.closest('details');if(menu)menu.open=false;return await action(b.dataset.action);}if(b.dataset.delete){showDeleteDialog([Number(b.dataset.delete)]);return}if(b.dataset.applyAi){const item=state.invoices.find(x=>x.id===Number(b.dataset.applyAi));let suggested={};try{suggested=JSON.parse(item.ai_suggestion||'{}')}catch{}for(const key of ['buyer','seller','invoice_date','invoice_number'])if(suggested[key]&&form.elements[key])form.elements[key].value=suggested[key];if(suggested.amount_cents!=null)form.elements.amount.value=(suggested.amount_cents/100).toFixed(2);toast('已填入建议，请核对后保存');return}if(b.dataset.duplicateDecision){await api(`/api/invoices/${b.dataset.id}/duplicate`,{method:'PATCH',body:JSON.stringify({decision:b.dataset.duplicateDecision})});await load();toast(b.dataset.duplicateDecision==='confirm'?'已标记为重复':b.dataset.duplicateDecision==='distinct'?'已确认非重复':'已撤销重复标记');return}if(b.dataset.viewDuplicate){const invoice=state.invoices.find(x=>x.id===Number(b.dataset.viewDuplicate));if(invoice){if(dialog.open)dialog.close();openDialog('已有发票',`<div class="dialog-preview">${invoicePreview(invoice,true)}</div>`);delete form.dataset.submit}return}if(b.dataset.edit){if(dialog.open)dialog.close();return edit(Number(b.dataset.edit))}if(b.dataset.ai){b.disabled=true;b.textContent='AI 识别中…';try{const result=await (await api(`/api/invoices/${b.dataset.ai}/ai`,{method:'POST'})).json();await load();toast(result.message)}finally{b.disabled=false;b.textContent='AI 辅助识别'}return}if(b.dataset.reprocess)return await reprocessMessage(b);if(b.dataset.email)return await showEmail(Number(b.dataset.email));if(b.dataset.retry){toast('正在识别，请稍候');const result=await (await api(`/api/invoices/${b.dataset.retry}/retry`,{method:'POST'})).json();await load();toast(result.reason||'识别完成')}if(b.dataset.credentials){openDialog('更新邮箱授权码','<div class="field"><label>新的 QQ 邮箱授权码<input type="password" name="auth_code" required autocomplete="new-password"></label></div>','保存');form.dataset.submit='credentials';form.dataset.id=b.dataset.credentials;return}if(b.dataset.toggleAccount){await api(`/api/accounts/${b.dataset.toggleAccount}`,{method:'PATCH',body:JSON.stringify({enabled:b.dataset.enabled==='1'})});await load();toast('邮箱监控状态已更新')}if(b.dataset.sync){await api(`/api/accounts/${b.dataset.sync}/sync`,{method:'POST'});toast('已开始同步，请稍后刷新页面查看结果')}if(b.dataset.groupStatus){await api(`/api/groups/${b.dataset.groupStatus}`,{method:'PATCH',body:JSON.stringify({reimbursed:b.dataset.status==='1'})});await load();toast('报销状态已更新')}if(b.dataset.groupDetail){const g=state.groups.find(x=>x.id===Number(b.dataset.groupDetail));openDialog(esc(g.name),g.invoices.map(i=>`<div class="row group-detail-row"><span>${esc(i.seller||i.original_name)}</span><span>${date(i.invoice_date)}</span><strong>${cash(i.amount_cents)}</strong></div>`).join(''));delete form.dataset.submit}}catch(err){toast(err.message)}}
document.addEventListener('click',handleClick);
document.addEventListener('error',e=>{if(e.target.matches?.('.preview-document img')){const message=document.createElement('div');message.className='preview-unavailable';message.textContent='预览暂不可用，请打开原件查看';e.target.replaceWith(message)}},true);
document.addEventListener('click',e=>{if(!e.target.closest('.shell > aside')&&document.querySelector('#nav-toggle').getAttribute('aria-expanded')==='true')closeNav()});
document.addEventListener('click',e=>{if(!e.target.closest('.export-menu'))document.querySelectorAll('.export-menu[open]').forEach(menu=>menu.open=false)});
matchMedia('(min-width:941px)').addEventListener('change',e=>{if(e.matches)closeNav()});
document.addEventListener('keydown',e=>{if(e.key==='Tab'&&document.querySelector('#nav-toggle').getAttribute('aria-expanded')==='true'){const trigger=matchMedia(mobileNavQuery).matches?document.querySelector('#mobile-more'):document.querySelector('#nav-toggle');const items=[trigger,...[...document.querySelectorAll('#main-nav button')].filter(item=>item.getClientRects().length)];const first=items[0],last=items[items.length-1];if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus()}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus()}else if(!e.shiftKey&&document.activeElement===first&&matchMedia(mobileNavQuery).matches){e.preventDefault();items[1]?.focus()}}if(e.key==='Escape'&&document.querySelector('.export-menu[open]')){const menu=document.querySelector('.export-menu[open]');menu.open=false;menu.querySelector('summary').focus();return}if(e.key==='Escape'&&document.querySelector('#nav-toggle').getAttribute('aria-expanded')==='true'){closeNav();document.querySelector(matchMedia(mobileNavQuery).matches?'#mobile-more':'#nav-toggle').focus()}});
content.addEventListener('change',async e=>{try{if(e.target.id==='upload'){const input=e.target,files=[...input.files];if(!files.length)return;const button=content.querySelector('[data-action=upload]');button.disabled=true;try{await inspectUpload(files)}finally{input.value='';const current=content.querySelector('[data-action=upload]');if(current)current.disabled=false}}if(e.target.id==='restore-file'){const input=e.target,file=input.files[0];if(!file)return;const data=new FormData();data.append('file',file);const button=content.querySelector('[data-action=select-restore]');button.disabled=true;button.textContent='正在校验…';try{const result=await (await api('/api/backup/inspect',{method:'POST',body:data})).json();pendingRestore=file;showRestorePreview(result)}finally{input.value='';const current=content.querySelector('[data-action=select-restore]');if(current){current.disabled=false;current.textContent='选择备份文件'}}}if(e.target.id==='select-visible'){state.invoices.forEach(x=>{if(e.target.checked){state.selected.add(x.id);state.selectedRecords.set(x.id,x)}else{state.selected.delete(x.id);state.selectedRecords.delete(x.id)}});render();content.querySelector('#select-visible')?.focus({preventScroll:true})}if(e.target.dataset.select){const id=Number(e.target.dataset.select),item=state.invoices.find(x=>x.id===id);if(e.target.checked&&item){state.selected.add(id);state.selectedRecords.set(id,item)}else{state.selected.delete(id);state.selectedRecords.delete(id)}render();content.querySelector(`[data-select="${id}"]`)?.focus({preventScroll:true})}if(e.target.id==='account-filter'||e.target.id==='email-status-filter'){const select=e.target;select.closest('.email-select').querySelector('.email-select-value>span').textContent=select.selectedOptions[0].textContent;const id=select.id;if(id==='account-filter')state.account=e.target.value;else state.emailFilter=e.target.value;state.emailPage=0;await load();content.querySelector('#'+id)?.focus({preventScroll:true})}}catch(err){toast(err.message)}});
let searchTimer;content.addEventListener('input',e=>{if(e.target.id==='template'){const fields=e.target.value.split(/[,，]/).map(x=>x.trim());document.querySelector('#template-preview').textContent=templateExample(fields)}if(e.target.id==='search'){state.query=e.target.value;state.invoicePage=0;clearTimeout(searchTimer);searchTimer=setTimeout(()=>load().catch(error=>toast(error.message)),250)}});
form.addEventListener('change',e=>{if(e.target.id==='upload-select-all')form.querySelectorAll('[data-upload-choice]:not(:disabled)').forEach(input=>input.checked=e.target.checked);if(e.target.id==='upload-select-all'||e.target.hasAttribute('data-upload-choice'))updateUploadSelection()});
dialog.addEventListener('cancel',e=>{if(form.dataset.uploading==='1')e.preventDefault()});
dialog.addEventListener('close',()=>{pendingUpload=[];pendingRestore=null;delete form.dataset.uploading});
form.addEventListener('submit',async e=>{e.preventDefault();const f=new FormData(form);const type=form.dataset.submit;try{if(type==='delete-invoices'){const items=[...pendingDelete],ids=items.map(item=>item.id),button=form.querySelector('[type=submit]'),controls=[...form.querySelectorAll('button')].map(el=>[el,el.disabled]);form.dataset.uploading='1';controls.forEach(([el])=>el.disabled=true);button.textContent='正在删除…';try{const result=await (await api('/api/invoices/delete',{method:'POST',body:JSON.stringify({items})})).json();delete form.dataset.uploading;dialog.close();ids.forEach(id=>{state.selected.delete(id);state.selectedRecords.delete(id)});state.preview=null;ids.forEach(id=>pdfPages.delete(id));try{await load()}catch{toast('已删除，请刷新页面查看');return}toast(result.cleanup_pending?'发票已删除，部分原件未清理，请检查权限并重启':'已删除发票')}catch(error){delete form.dataset.uploading;controls.forEach(([el,disabled])=>el.disabled=disabled);button.textContent='删除发票';throw error}return}if(type==='restore-backup'){if(!pendingRestore)return;const data=new FormData();data.append('file',pendingRestore);const button=form.querySelector('[type=submit]'),controls=[...form.querySelectorAll('button,input')].map(el=>[el,el.disabled]);form.dataset.uploading='1';controls.forEach(([el])=>el.disabled=true);button.textContent='正在恢复…';try{const restored=await (await api('/api/backup/restore',{method:'POST',body:data})).json();delete form.dataset.uploading;dialog.close();showLogin(restored.recovery_incomplete?'备份已恢复。恢复前的数据不完整，已保留原始副本。请使用备份中的账号密码登录。':'备份已恢复，请使用备份中的账号密码重新登录。')}catch(error){delete form.dataset.uploading;controls.forEach(([el,disabled])=>el.disabled=disabled);button.textContent='确认恢复';throw error}return}if(type==='upload-preview'){const selected=[...form.querySelectorAll('[data-upload-choice]:checked')].map(input=>pendingUpload[Number(input.dataset.uploadChoice)]);if(selected.length)await importUpload(selected);return}if(type==='invoice'){const amount=f.get('amount');await api(`/api/invoices/${form.dataset.id}`,{method:'PATCH',body:JSON.stringify({buyer:f.get('buyer'),seller:f.get('seller'),amount_cents:amount===''?null:Math.round(Number(amount)*100),invoice_date:f.get('invoice_date')||null,invoice_number:f.get('invoice_number'),export_name:f.get('export_name')})})}if(type==='credentials'){await api(`/api/accounts/${form.dataset.id}`,{method:'PATCH',body:JSON.stringify({auth_code:f.get('auth_code')})})}if(type==='account'){const account=await (await api('/api/accounts',{method:'POST',body:JSON.stringify(Object.fromEntries(f))})).json();await api(`/api/accounts/${account.id}/sync`,{method:'POST'})}if(type==='group'){await api('/api/groups',{method:'POST',body:JSON.stringify({name:f.get('name'),ids:[...state.selected]})});clearSelection();navigate('groups')}dialog.close();await load();toast(type==='account'?'邮箱已添加，正在首次同步':'已保存')}catch(err){form.querySelector('.inline-error').textContent=err.message}});
bootstrap();
document.addEventListener('pointerdown',()=>{pointerActive=true});
for(const event of ['pointerup','pointercancel'])document.addEventListener(event,()=>{pointerActive=false;lastInteraction=Date.now()});
window.addEventListener('blur',()=>{pointerActive=false;lastInteraction=Date.now()});
document.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' ')lastInteraction=Date.now()});
setInterval(()=>{if(['accounts','emails'].includes(state.page))load({background:true}).catch(()=>{})},10000);

document.addEventListener('submit',async e=>{if(e.target.id!=='ai-settings-form')return;e.preventDefault();const target=e.target, data=new FormData(target),button=target.querySelector('[type=submit]');button.disabled=true;const error=target.querySelector('.inline-error');error.textContent='';try{await api('/api/ai-settings',{method:'PUT',body:JSON.stringify({base_url:data.get('base_url'),model:data.get('model'),api_key:data.get('api_key'),auto_enabled:data.has('auto_enabled'),clear_key:data.has('clear_key')})});await load();toast('AI 设置已保存')}catch(err){error.textContent=err.message}finally{button.disabled=false}});

function showLogin(message=''){
  const screen=document.querySelector('#login-screen'),shell=document.querySelector('.shell');loadSequence++;
  pdfPages.clear();mailReprocessFeedback.clear();lastLoadSnapshot='';if(!shell.hidden){Object.assign(state,{username:'',invoices:[],groups:[],accounts:[],emails:[],preview:null,query:'',account:'',emailFilter:'all'});clearSelection();if(dialog.open)dialog.close();closeNav()}
  shell.hidden=true;screen.hidden=false;
  if(message)screen.querySelector('.login-error').textContent=message;
}
async function bootstrap(){
  try{const user=await (await api('/api/auth/me')).json();state.username=user.username;document.querySelector('#login-screen').hidden=true;document.querySelector('.shell').hidden=false;await load()}
  catch(error){showLogin();if(error.message!=='请先登录')document.querySelector('.login-error').textContent=error.message}
}
document.querySelector('#login-form').addEventListener('submit',async e=>{
  e.preventDefault();const target=e.currentTarget,button=target.querySelector('button'),error=target.querySelector('.login-error'),data=new FormData(target);
  button.disabled=true;button.textContent='正在登录…';error.textContent='';
  try{const user=await (await api('/api/auth/login',{method:'POST',body:JSON.stringify({username:data.get('username'),password:data.get('password')})})).json();state.username=user.username;target.reset();document.querySelector('#login-screen').hidden=true;document.querySelector('.shell').hidden=false;await load()}
  catch(err){error.textContent=err.message}finally{button.disabled=false;button.textContent='登录'}
});
document.addEventListener('submit',async e=>{
  if(e.target.id!=='security-form')return;
  e.preventDefault();const target=e.target,data=new FormData(target),error=target.querySelector('.inline-error'),button=target.querySelector('[type=submit]');
  error.textContent='';if(data.get('password')!==data.get('confirm_password')){error.textContent='两次输入的新密码不一致';return}
  button.disabled=true;button.textContent='正在保存…';
  try{const user=await (await api('/api/auth/account',{method:'PUT',body:JSON.stringify({username:data.get('username'),current_password:data.get('current_password'),password:data.get('password')})})).json();state.username=user.username;render();toast('账号设置已保存')}
  catch(err){error.textContent=err.message}finally{button.disabled=false;button.textContent='保存账号设置'}
});

content.addEventListener('click',async e=>{
  const button=e.target.closest('[data-ai-test]');if(!button)return;
  const target=button.closest('form'),data=new FormData(target),result=target.querySelector('.ai-test-result'),buttons=[...target.querySelectorAll('button')];
  const kind=button.dataset.aiTest;buttons.forEach(item=>item.disabled=true);target.setAttribute('aria-busy','true');result.textContent=kind==='connection'?'正在测试连接…':'正在识别虚拟发票，请稍候…';
  try{
    const response=await (await api('/api/ai-settings/test-'+kind,{method:'POST',body:JSON.stringify({base_url:data.get('base_url'),model:data.get('model'),api_key:data.get('api_key'),clear_key:data.has('clear_key'),auto_enabled:false})})).json();
    if(kind==='connection'){result.textContent=response.message;return}
    const labels={buyer:'购买方',seller:'商户',amount_cents:'金额',invoice_date:'开票日期',invoice_number:'发票号码'};
    const show=(key,value)=>value==null||value===''?'未识别':key==='amount_cents'?cash(value):String(value);
    result.innerHTML=`<strong>示例识别：${response.matched.length} / 5 项与样例一致</strong><p class="hint">这是虚拟样例结果，真实发票仍需人工核对。</p><dl>${Object.entries(labels).map(([key,label])=>`<dt>${label}</dt><dd>${esc(show(key,response.actual[key]))} <span class="muted">（样例：${esc(show(key,response.expected[key]))}）</span></dd>`).join('')}</dl>`;
  }catch(error){result.textContent=error.message}finally{buttons.forEach(item=>item.disabled=false);target.removeAttribute('aria-busy')}
});
