const state={username:'',page:'invoices',invoices:[],groups:[],accounts:[],emails:[],notifications:[],selected:new Set(),selectedRecords:new Map(),invoicePage:0,invoiceTotal:0,invoiceCount:0,unpaidCount:0,unpaidAmount:0,reviewCount:0,filter:'全部',query:'',invoiceSort:'imported_desc',account:'',emailFilter:'all',emailPage:0,emailTotal:0,pendingPage:0,pendingTotal:0,preview:null,settings:['抬头','商户','金额','开票日期'],ai:{base_url:'',model:'',auto_enabled:false,has_key:false}};
const content=document.querySelector('#content'),dialog=document.querySelector('#dialog'),form=document.querySelector('#dialog-form');
const mobileNavQuery='(max-width:640px), (orientation:landscape) and (max-width:950px) and (max-height:500px) and (pointer:coarse)';
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const cash=n=>n==null?'—':'¥'+(n/100).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
const date=x=>x?esc(x.slice(0,10)):'—';
const titles={invoices:'发票库',groups:'报销组',emails:'全部邮件',failed:'识别失败',accounts:'邮箱管理',ai:'AI 辅助识别',notifications:'消息推送',settings:'偏好设置',backup:'备份与恢复'};
const routePage=()=>location.hash==='#security'?'settings':Object.hasOwn(titles,location.hash.slice(1))?location.hash.slice(1):'invoices';
state.page=routePage();
if(location.hash==='#security')history.replaceState(null,'','#settings');
function clearSelection(){state.selected.clear();state.selectedRecords.clear()}
function navigate(page){state.page=page;if(location.hash!=='#'+page)location.hash=page}
window.addEventListener('hashchange',()=>{const page=routePage();if(location.hash==='#security')history.replaceState(null,'','#settings');if(page!==state.page){closeNav();state.page=page;clearSelection();load().catch(error=>toast(error.message))}});
async function api(url,options={}){const headers={'X-Piaoxu-Request':'1',...(options.headers||{})};if(options.body && !(options.body instanceof FormData))headers['Content-Type']='application/json';const response=await fetch(url,{...options,headers});if(!response.ok){if(response.status===401&&url!=='/api/auth/login')showLogin();let data={};try{data=await response.json()}catch{}throw Error(typeof data.detail==='string'?data.detail:'请求失败，请重试')}return response}
let timer;function toast(message){const el=document.querySelector('#toast');el.textContent=message;el.classList.add('show');clearTimeout(timer);timer=setTimeout(()=>el.classList.remove('show'),3500)}
let loadSequence=0,lastLoadSnapshot='',pointerActive=false,lastInteraction=0,exporting=false,selectingDates=false;
function backgroundRefreshBlocked(){return selectingDates||exporting||pointerActive||Date.now()-lastInteraction<300||mailReprocessBusy.size>0||dialog.open||['INPUT','SELECT','TEXTAREA'].includes(document.activeElement.tagName)}
async function load({background=false}={}){
  if(background&&backgroundRefreshBlocked())return;
  const seq=++loadSequence,page=state.page,requests=[['summary','/api/summary'],['accounts','/api/accounts']];
  if(page==='invoices'){const params=new URLSearchParams({limit:'50',offset:String(state.invoicePage*50),query:state.query,reimbursement:({'全部':'all','未报销':'unreimbursed','已报销':'reimbursed'})[state.filter],sort:state.invoiceSort||'imported_desc'});requests.push(['invoiceResults','/api/invoices?'+params])}
  if(page==='failed')requests.push(['invoices','/api/invoices?view=review']);
  if(page==='groups')requests.push(['groups','/api/groups']);
  if(page==='settings')requests.push(['settings','/api/settings']);
  if(page==='ai')requests.push(['ai','/api/ai-settings']);
  if(page==='notifications')requests.push(['notifications','/api/notification-channels']);
  if(page==='emails'){
    const params=new URLSearchParams({limit:'50',offset:String(state.emailPage*50),invoice_status:state.emailFilter});
    if(state.account)params.set('account_id',state.account);
    requests.push(['emailResults','/api/emails?'+params]);
  }else if(page==='failed')requests.push(['emailResults','/api/emails?attention=true&limit=50&offset='+state.pendingPage*50]);
  const values=await Promise.all(requests.map(async ([key,url])=>[key,await (await api(url)).json()]));
  if(seq!==loadSequence||page!==state.page||(background&&backgroundRefreshBlocked()))return;
  const data=Object.fromEntries(values),snapshot=JSON.stringify([page,data]);
  if(background&&snapshot===lastLoadSnapshot)return;
  for(const key of ['invoices','groups','accounts','ai','notifications'])if(data[key])state[key]=data[key];
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
const uiIcon=name=>`<svg class="button-icon" aria-hidden="true"><use href="/icons.svg?v=8#${name}"></use></svg>`;
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
let pendingUpload=[],pendingDelete=[],pendingMerge=null;
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
function showMergeDialog(id){
  const source=state.invoices.find(item=>item.id===id),target=state.invoices.find(item=>item.id===source?.duplicate_of);
  if(!source||!target){toast('未找到对应发票，请刷新后重试');return}
  const fields=[['buyer','发票抬头'],['buyer_tax_id','购买方税号'],['seller','消费商户'],['seller_tax_id','销售方税号'],['amount_cents','金额'],['invoice_date','开票日期'],['invoice_number','发票号码']];
  const conflicts=fields.filter(([key])=>source[key]!=null&&source[key]!==''&&target[key]!=null&&target[key]!==''&&source[key]!==target[key]);
  const card=(item,title)=>`<section><h3>${title}</h3><p>${esc(item.original_name)}</p><dl>${fields.map(([key,label])=>`<div${conflicts.some(([name])=>name===key)?' class="merge-conflict"':''}><dt>${label}</dt><dd>${key==='amount_cents'?cash(item[key]):esc(item[key]||'未识别')}</dd></div>`).join('')}</dl><a class="button" href="/api/invoices/${item.id}/original" target="_blank" rel="noopener">查看原件</a></section>`;
  openDialog('合并到已有发票',`<p class="sub">两张记录的全部原件和来源邮件将归到已有发票，报销只计一张。合并后保留已有发票的信息。</p><div class="merge-comparison">${card(source,'待合并的发票')}${card(target,'保留的已有发票')}</div>${conflicts.length?'<p class="hint">关键信息存在差异，请先核对并重新识别或人工补充，再进行合并。</p>':''}`,'确认合并');
  pendingMerge={id:source.id,target_id:target.id,sha256:source.sha256,target_sha256:target.sha256};
  form.dataset.submit='merge-invoices';form.querySelector('[type=submit]').disabled=conflicts.length>0;
}
async function submitInvoiceMerge(){
  if(!pendingMerge||form.dataset.uploading)return;
  const {id,...payload}=pendingMerge,buttons=[...form.querySelectorAll('button')],button=form.querySelector('[type=submit]');
  form.querySelector('.inline-error').textContent='';form.dataset.uploading='1';buttons.forEach(item=>item.disabled=true);button.textContent='正在合并…';
  try{
    await api(`/api/invoices/${id}/merge`,{method:'POST',body:JSON.stringify(payload)});
    dialog.close();state.selected.delete(id);state.selectedRecords.delete(id);pdfPages.delete(id);state.preview=null;
    try{await load()}catch{toast('已合并，请刷新页面查看');return}
    toast('已合并，全部原件和来源邮件已保留');
  }finally{
    delete form.dataset.uploading;buttons.forEach(item=>item.disabled=false);button.textContent='确认合并';
  }
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
  const viewer=button.closest('[data-pdf-viewer]'),page=Number(viewer.dataset.pdfCurrent)+Number(button.dataset.pdfStep),total=Number(viewer.dataset.pdfTotal),image=viewer.querySelector('img');
  if(!image||!Number.isFinite(page)||!Number.isFinite(total)||page<1||page>total)return;
  pdfPages.set(Number(viewer.dataset.pdfViewer),page);viewer.dataset.pdfCurrent=page;image.onload=()=>{uiMotion(image);image.onload=null};image.src=`/api/invoices/${viewer.dataset.pdfViewer}/preview?page=${page}${viewer.hasAttribute('data-document-viewer')?'&width=3000':''}`;
  viewer.querySelector('.document-scroll')?.scrollTo(0,0);
  image.alt=`PDF 第 ${page} 页预览`;updatePdfPager(viewer);
}
function invoiceOriginals(current){
  if(!current.other_originals?.length)return '';
  return `<section class="preview-originals" aria-label="其他原件"><div class="originals-heading"><h3>其他原件</h3><span>${current.other_originals.length} 份</span></div><ul>${current.other_originals.map(file=>{
    const dot=file.name.lastIndexOf('.'),stem=dot>0?file.name.slice(0,dot):file.name,extension=dot>0?file.name.slice(dot):'',format=extension.slice(1).toLowerCase();
    const prefix=stem.length>18?stem.slice(0,-6):stem,suffix=stem.length>18?stem.slice(-6)+extension:extension;
    const kind=format==='xml'?'结构化文件':format==='ofd'?'版式文件':format==='pdf'?'PDF 文件':/^(png|jpe?g)$/.test(format)?'图片文件':'原始文件';
    return `<li><a class="original-file" href="/api/invoices/${current.id}/originals/${file.id}" download="${esc(file.name)}" title="${esc(file.name)}" aria-label="下载 ${esc(file.name)}"><span class="original-format" aria-hidden="true">${esc(format.toUpperCase()||'FILE')}</span><span class="original-info" aria-hidden="true"><span class="original-filename"><span>${esc(prefix)}</span><span>${esc(suffix)}</span></span><span class="original-kind">${kind}</span></span><span class="original-download" aria-hidden="true">${uiIcon('download')}</span></a></li>`;
  }).join('')}</ul></section>`;
}
function invoicePreview(current,inDialog=false){
  if(!current)return `<div class="preview-heading"><span>发票详情</span></div>${empty('选择发票后查看原件和详情。')}`;
  const ext=(current.stored_name.split('.').pop()||'').toLowerCase(),page=pdfPages.get(current.id)||1,visual=ext==='pdf'||/^(png|jpg|jpeg)$/.test(ext);
  const heading=`<div class="preview-head"><h2>${esc(current.seller||'未填写商户')}</h2><p>${esc(current.original_name)}</p></div>`;
  const details=`<dl class="preview-fields"><dt>发票抬头</dt><dd>${esc(current.buyer||'—')}</dd><dt>开票日期</dt><dd>${date(current.invoice_date)}</dd><dt>发票号码</dt><dd>${esc(current.invoice_number||'—')}</dd><dt>发票金额</dt><dd><strong>${cash(current.amount_cents)}</strong></dd><dt>导入日期</dt><dd>${date(current.imported_at)}</dd><dt>发票来源</dt><dd class="preview-source">${sourceDetails(current)}</dd></dl>${invoiceOriginals(current)}<div class="preview-actions"><a class="button preview-link" href="/api/invoices/${current.id}/original" target="_blank" rel="noopener">${ext==='pdf'||/^(png|jpg|jpeg)$/.test(ext)?'打开原件':'下载原件'}</a>${inDialog?`<button class="button" type="button" data-edit="${current.id}">编辑信息</button>`:''}<button class="button danger" type="button" data-delete="${current.id}">删除</button></div>`;
  if(inDialog){
    const source=ext==='pdf'?`/api/invoices/${current.id}/preview?page=${page}&width=3000`:`/api/invoices/${current.id}/original`;
    const document=visual?`<section class="document-reader" data-document-viewer data-preview-scale="100" ${ext==='pdf'?`data-pdf-viewer="${current.id}"`:''} aria-label="发票原件预览"><div class="document-toolbar">${ext==='pdf'?pdfPager():'<span class="muted">图片预览</span>'}<div class="document-zoom" role="group" aria-label="预览缩放"><button class="button" type="button" data-preview-zoom="-25" aria-label="缩小预览">${uiIcon('zoom-out')}</button><span data-preview-scale-label role="status" aria-live="polite">100%</span><button class="button" type="button" data-preview-zoom="25" aria-label="放大预览">${uiIcon('zoom-in')}</button><button class="button" type="button" data-preview-zoom="fit">适应宽度</button></div></div><div class="document-scroll" tabindex="0" aria-label="发票票面，可滚动查看放大后的内容"><div class="document-canvas"><img src="${source}" alt="${ext==='pdf'?`PDF 第 ${page} 页预览`:'发票原件预览'}"></div></div></section>`:`<div class="preview-document"><div class="preview-unavailable">${esc(ext.toUpperCase()||'该格式')} 原件可下载查看</div></div>`;
    return `<div class="preview-workspace">${document}<section class="preview-details" aria-label="发票信息">${heading}${details}</section></div>`;
  }
  const document=visual?`<button class="preview-document preview-open" type="button" data-preview="${current.id}" aria-label="放大查看发票原件"><img src="${ext==='pdf'?`/api/invoices/${current.id}/preview?page=${page}`:`/api/invoices/${current.id}/original`}" alt="${ext==='pdf'?`PDF 第 ${page} 页预览`:'发票原件预览'}"></button>`:`<div class="preview-document"><div class="preview-unavailable">${esc(ext.toUpperCase()||'该格式')} 原件可下载查看</div></div>`;
  return `<div class="preview-heading"><span>发票详情</span><button class="button text-button" data-edit="${current.id}">编辑</button></div>${heading}${ext==='pdf'?`<div data-pdf-viewer="${current.id}">${document}${pdfPager()}</div>`:document}${details}`;
}
function openInvoicePreview(invoice){
  if(!invoice)return;
  if(state.page==='invoices'){
    state.preview=invoice.id;render();
    content.querySelector(`.invoice-name[data-preview="${invoice.id}"]`)?.focus({preventScroll:true});
  }
  if(dialog.open)dialog.close();
  openDialog('发票预览',invoicePreview(invoice,true));
  form.querySelector('.dialog-actions').remove();delete form.dataset.submit;
}
function zoomInvoicePreview(button){
  const viewer=button.closest('[data-document-viewer]'),image=viewer?.querySelector('img');if(!image)return;
  const zoom=button.dataset.previewZoom==='fit'?100:Math.max(50,Math.min(300,Number(viewer.dataset.previewScale||100)+Number(button.dataset.previewZoom)));
  if(!Number.isFinite(zoom))return;
  viewer.dataset.previewScale=String(zoom);image.style.width=zoom+'%';
  viewer.querySelector('[data-preview-scale-label]').textContent=zoom+'%';
  viewer.querySelector('[data-preview-zoom="-25"]').disabled=zoom<=50;
  viewer.querySelector('[data-preview-zoom="25"]').disabled=zoom>=300;
  if(button.dataset.previewZoom==='fit')viewer.querySelector('.document-scroll').scrollTo(0,0);
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
  const selectedAmount=cash(selected.reduce((n,x)=>n+(x.amount_cents||0),0));
  const current=filtered.find(x=>x.id===state.preview)||filtered[0];
  const sortedByDate=!state.invoiceSort||state.invoiceSort==='imported_desc';
  const sortLabel=sortedByDate?'按导入时间整理':state.invoiceSort==='amount_asc'?'金额从低到高':'金额从高到低';
  const byDay=new Map();
  filtered.forEach(x=>{const day=sortedByDate?(x.imported_at?x.imported_at.slice(0,10):'未知日期'):'';if(!byDay.has(day))byDay.set(day,[]);byDay.get(day).push(x)});
  const rows=[...byDay].map(([day,items])=>`${sortedByDate?`<tr class="date-row"><td colspan="5"><div class="date-heading"><div><strong>${esc(day)}</strong><span>${state.invoiceTotal>filtered.length?'本页 ':''}${items.length} 张</span></div>${/^\d{4}-\d{2}-\d{2}$/.test(day)?`<button class="button text-button" type="button" data-select-import-day="${day}" aria-label="选择 ${day} 导入的发票">选择当天</button>`:''}</div></td></tr>`:''}${items.map(x=>`<tr class="invoice-row ${cash(x.amount_cents).length>12?'wide-amount':''} ${current&&current.id===x.id?'focused':''} ${state.selected.has(x.id)?'is-selected':''}"><td class="check-cell"><label class="check-hit"><input type="checkbox" data-select="${x.id}" aria-label="选择 ${esc(x.original_name)}" ${state.selected.has(x.id)?'checked':''}></label></td><td class="name-cell"><button class="invoice-name" data-preview="${x.id}" title="${esc(x.seller||'未填写商户')}"><strong>${esc(x.seller||'未填写商户')}</strong><span title="${esc(x.original_name)}">${esc(x.original_name)}</span><span class="invoice-source" title="${esc(sourceSummary(x))}">${esc(sourceSummary(x))}</span></button></td><td class="invoice-date">${date(x.invoice_date)}</td><td class="amount">${cash(x.amount_cents)}</td><td class="invoice-status"><span class="status ${x.reimbursed?'done':''}">${x.reimbursed?'已报销':'未报销'}</span></td></tr>`).join('')}`).join('');
  const selectionBar=selected.length?`<div class="selection-bar" role="region" aria-label="已选发票操作"><div class="selection-summary"><strong class="selection-full">已选择 ${selected.length} 张</strong><strong class="selection-short">已选 ${selected.length} 张</strong><span class="selection-full">合计 ${selectedAmount}</span><span class="selection-short" title="合计 ${selectedAmount}">${selectedAmount}</span></div><div class="selection-actions"><button class="button primary" data-action="group" aria-label="创建报销组"><span class="selection-full">创建报销组</span><span class="selection-short">成组</span></button><details class="export-menu"><summary class="button" aria-label="导出原件"><span class="selection-full">导出原件</span><span class="selection-short">导出</span>${uiIcon('chevron-down')}</summary><div class="export-options"><button type="button" data-action="export" aria-label="导出原格式 ZIP">导出原格式 ZIP</button><button type="button" data-action="export-pdf" aria-label="仅导出 PDF ZIP">仅导出 PDF ZIP</button><p class="export-note">OFD、XML 将保留原格式</p></div></details><button class="button danger selection-full" type="button" data-action="delete-selected">删除</button><details class="export-menu selection-short"><summary class="button" aria-label="更多已选发票操作">更多</summary><div class="export-options"><button type="button" data-action="clear-selection">取消选择</button><button class="danger" type="button" data-action="delete-selected">删除发票</button></div></details></div></div>`:'';
  const actions=`<button class="button header-export" data-action="export" ${selected.length?'':'disabled'}>导出原件</button><button class="button primary" data-action="upload">${uiIcon('plus')}导入发票</button><input id="upload" type="file" accept=".pdf,.ofd,.xml,.jpg,.jpeg,.png,application/pdf,application/xml,text/xml,image/jpeg,image/png" multiple hidden>`;
  const summary=`<div class="invoice-summary"><div class="invoice-metrics"><div><p>未报销金额</p><strong class="metric-amount">${cash(state.unpaidAmount)}</strong></div><div><p>未报销</p><strong>${state.unpaidCount}<small>张</small></strong></div><div><p>已报销</p><strong>${state.invoiceCount-state.unpaidCount}<small>张</small></strong></div></div><button class="review-summary ${state.reviewCount?'has-review':''}" data-go="failed"><span><strong>${state.reviewCount} 项待核对</strong><small>${state.reviewCount?'发票与邮件检查':'暂时没有待处理事项'}</small></span>${uiIcon('arrow-up-right')}</button></div>`;
  return header('发票库',`共 ${state.invoiceCount} 张发票，收好每一笔支出。`,actions)+summary+`<div class="invoice-layout"><section class="invoice-list ${selected.length?'has-selection':''}"><div class="invoice-tabs" aria-label="报销状态筛选">${['全部','未报销','已报销'].map(f=>`<button class="tab ${state.filter===f?'active':''}" data-filter="${f}" aria-pressed="${state.filter===f}">${f}</button>`).join('')}</div><div class="invoice-toolbar"><label class="search-field">${uiIcon('search')}<input id="search" type="search" aria-label="搜索发票" placeholder="搜索商户、抬头或文件名" value="${esc(state.query)}"></label><div class="invoice-tools"><button class="button" type="button" data-action="select-invoice-dates">按日期选择</button><label class="invoice-sort"><span class="invoice-sort-value" aria-hidden="true">${sortedByDate?'导入时间':sortLabel}${uiIcon('chevron-down')}</span><select id="invoice-sort" aria-label="发票排序">${[['imported_desc','导入时间'],['amount_asc','金额从低到高'],['amount_desc','金额从高到低']].map(([value,label])=>`<option value="${value}" ${(state.invoiceSort||'imported_desc')===value?'selected':''}>${label}</option>`).join('')}</select></label></div></div><div class="tablewrap invoice-table"><table><colgroup><col class="col-check"><col class="col-name"><col class="col-date"><col class="col-amount"><col class="col-status"></colgroup><thead><tr><th><label class="check-hit"><input type="checkbox" id="select-visible" aria-label="选择当前列表全部发票" ${filtered.length&&filtered.every(x=>state.selected.has(x.id))?'checked':''}></label></th><th>商户 / 文件名</th><th class="invoice-date">开票日期</th><th class="amount">金额</th><th>报销状态</th></tr></thead><tbody>${rows}</tbody></table>${filtered.length?'':empty(state.query||state.filter!=='全部'?'没有符合条件的发票。':'暂无发票。上传原件或连接邮箱后，发票会显示在这里。')}</div><div class="invoice-list-footer"><span>${state.invoiceTotal} 张发票</span><span>${sortLabel}</span></div>${listPager(state.invoiceTotal,state.invoicePage,'invoice')}${selectionBar}</section><aside class="invoice-preview" aria-label="发票预览">${invoicePreview(current)}</aside></div>`;
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
  const invoiceRow=i=>`<div class="row failed-invoice"><strong>${esc(i.original_name)}</strong><div><span class="badge fail">${i.duplicate_state==='confirmed'?'已确认重复':esc(i.failure_reason)}</span><div class="muted">${esc(sourceSummary(i))} · ${date(i.imported_at)}</div></div><div class="actions">${i.email_id?`<button class="button link" data-email="${i.email_id}">查看邮件</button>`:''}${i.duplicate_of?`<a class="button" href="/api/invoices/${i.id}/original" target="_blank" rel="noopener">查看本张原件</a><button class="button" data-view-duplicate="${i.duplicate_of}">查看相同票号发票</button>`:''}${i.duplicate_state==='confirmed'?`${i.duplicate_of?`<button class="button" data-merge="${i.id}">合并到已有发票</button>`:''}<button class="button" data-duplicate-decision="reopen" data-id="${i.id}">撤销重复标记</button>`:`${i.duplicate_of?`<button class="button" data-merge="${i.id}">合并到已有发票</button><button class="button" data-duplicate-decision="distinct" data-id="${i.id}">确认非重复</button>`:i.duplicate_state==='unverified'?`<button class="button" data-duplicate-decision="distinct" data-id="${i.id}">确认无票号并继续</button>`:''}<button class="button primary" data-edit="${i.id}">人工补充</button><button class="button" data-retry="${i.id}">重新识别</button><button class="button" data-ai="${i.id}">AI 辅助识别</button>`}<button class="button danger" type="button" data-delete="${i.id}">删除</button></div></div>`;
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
  content.innerHTML=({invoices:invoicePage,groups:groupPage,emails:emailsPage,failed:failedPage,accounts:accountsPage,ai:aiPage,notifications:notificationsPage,settings:settingsPage,backup:backupPage}[state.page])();
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
    if(result.skipped_links)parts.push(`跳过 ${result.skipped_links} 个重复下载入口`);
    if(result.errors)parts.push(`${result.errors} 项处理失败${result.error_reasons?.length?'（'+result.error_reasons.join('、')+'）':''}`);
    if(result.retry_failed)parts.push(`${result.retry_failed} 张重新识别失败`);
    if(!result.files_found&&!result.errors&&!result.skipped_links)parts.push('未发现可识别的发票文件');
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
function showInvoiceDateSelection(){
  openDialog('按导入日期选择',`<p class="sub">选择日期区间，批量选中其中的发票，已有选择会保留。</p><div class="fields" style="margin-top:20px"><div class="field"><label>开始日期<input type="date" name="imported_from" required></label></div><div class="field"><label>结束日期<input type="date" name="imported_to" required></label></div></div><p class="hint">包含起止日期，仅选择符合当前搜索和报销状态的发票，最多选择 500 张。</p>`,'选择发票');
  form.dataset.submit='select-invoice-dates';
}
async function selectInvoiceDates(from,to){
  if(!from||!to||from>to)throw Error('请选择有效日期，开始日期不能晚于结束日期');
  if(selectingDates)throw Error('正在选择发票，请稍候');
  const query=state.query,filter=state.filter;
  selectingDates=true;
  try{
    const params=new URLSearchParams({limit:'500',query,reimbursement:({'全部':'all','未报销':'unreimbursed','已报销':'reimbursed'})[filter],imported_from:from,imported_to:to});
    const result=await (await api('/api/invoices?'+params)).json();
    if(state.page!=='invoices'||state.query!==query||state.filter!==filter)throw Error('页面或筛选条件已变化，请重新选择日期');
    const combined=new Set([...state.selected,...result.items.map(item=>item.id)]);
    if(result.total>500||combined.size>500)throw Error('最多选择 500 张发票，请缩小日期范围或减少已有选择');
    result.items.forEach(item=>{state.selected.add(item.id);state.selectedRecords.set(item.id,item)});
    return result.items.length;
  }finally{selectingDates=false}
}
async function submitInvoiceDateSelection(data){
  const controls=[...form.querySelectorAll('button,input')].map(el=>[el,el.disabled]),button=form.querySelector('[type=submit]');
  form.dataset.uploading='1';controls.forEach(([el])=>el.disabled=true);button.textContent='正在选择…';
  try{
    const count=await selectInvoiceDates(data.get('imported_from'),data.get('imported_to'));
    delete form.dataset.uploading;dialog.close();render();if(!count||!matchMedia(mobileNavQuery).matches)toast(count?`已选择该日期范围内 ${count} 张发票`:'该日期范围内没有符合条件的发票');
  }finally{delete form.dataset.uploading;controls.forEach(([el,disabled])=>el.disabled=disabled);button.textContent='选择发票'}
}
async function exportInvoices(pdfOnly=false){
  if(exporting)return;
  const ids=[...state.selected];if(!ids.length){toast('请先选择发票');return}
  exporting=true;
  const controls=[...document.querySelectorAll('[data-action="export"],[data-action="export-pdf"]')].map(button=>[button,button.disabled]);
  controls.forEach(([button])=>button.disabled=true);toast('正在打包发票，请稍候…');
  try{
    const result=await (await api('/api/invoices/export/prepare',{method:'POST',body:JSON.stringify({ids,pdf_only:pdfOnly})})).json();
    const url='/api/invoices/export/download/'+encodeURIComponent(result.download_id);
    openDialog('发票已打包',`<p class="sub">已打包 ${ids.length} 张发票，正在发起下载。若下载没有开始，可点击下方按钮重试。</p><p class="hint">下载链接 30 分钟内有效。</p><div class="actions" style="margin-top:20px"><a class="button primary" href="${url}" download="${esc(result.filename)}" data-export-download>${uiIcon('download')}下载 ZIP</a></div>`);
    delete form.dataset.submit;form.querySelector('.dialog-actions button').textContent='完成';
    form.querySelector('[data-export-download]').click();
  }finally{
    exporting=false;controls.forEach(([button,disabled])=>button.disabled=disabled);
  }
}
async function action(name){if(name==='clear-selection'){clearSelection();render();return}if(name==='select-invoice-dates')return showInvoiceDateSelection();if(name==='delete-selected'){showDeleteDialog([...state.selected]);return}if(name==='logout'){await api('/api/auth/logout',{method:'POST'});showLogin('已退出登录');return}if(name==='select-restore')return document.querySelector('#restore-file').click();if(name==='upload')return document.querySelector('#upload').click();if(name==='add-account')return addAccount();if(name==='group'){openDialog('创建报销组',`<p class="sub">已选择 ${state.selected.size} 张发票。</p><div class="field" style="margin-top:16px"><label>报销组名称<input name="name" required placeholder="例如：11 月差旅"></label></div>`,'创建');form.dataset.submit='group';return}if(name==='export'||name==='export-pdf')return exportInvoices(name==='export-pdf');if(name==='save-template'){const fields=document.querySelector('#template').value.split(/[,，]/).map(x=>x.trim()).filter(Boolean);await api('/api/settings',{method:'PUT',body:JSON.stringify({fields})});toast('命名规则已保存');await load()}}
function closeNav(){const toggle=document.querySelector('#nav-toggle');toggle.setAttribute('aria-expanded','false');document.querySelector('#mobile-more').setAttribute('aria-expanded','false');toggle.querySelector('use').setAttribute('href','/icons.svg?v=6#menu');toggle.setAttribute('aria-label','展开菜单');document.querySelector('.shell > aside').classList.remove('menu-open');document.querySelector('#nav-backdrop').hidden=true;document.querySelector('main').inert=false;document.body.classList.remove('nav-open')}
async function handleClick(e){const b=e.target.closest('button');if(!b)return;try{if(b.dataset.selectImportDay){b.disabled=true;try{const count=await selectInvoiceDates(b.dataset.selectImportDay,b.dataset.selectImportDay);render();if(!count||!matchMedia(mobileNavQuery).matches)toast(count?`已选择当天 ${count} 张发票`:'当天没有符合条件的发票')}finally{b.disabled=false}return}if(b.dataset.previewZoom){zoomInvoicePreview(b);return}if(b.dataset.pdfStep){turnPdfPage(b);return}if(b.dataset.emailPage||b.dataset.pendingPage||b.dataset.invoicePage){const key=b.dataset.emailPage?'emailPage':b.dataset.pendingPage?'pendingPage':'invoicePage';state[key]+=Number(b.dataset.emailPage||b.dataset.pendingPage||b.dataset.invoicePage);await load();return}if(b.id==='nav-toggle'||b.id==='mobile-more'){const toggle=document.querySelector('#nav-toggle'),more=document.querySelector('#mobile-more'),open=toggle.getAttribute('aria-expanded')!=='true';toggle.setAttribute('aria-expanded',String(open));more.setAttribute('aria-expanded',String(open));toggle.querySelector('use').setAttribute('href',open?'/icons.svg?v=6#x':'/icons.svg?v=6#menu');toggle.setAttribute('aria-label',open?'关闭菜单':'展开菜单');document.querySelector('.shell > aside').classList.toggle('menu-open',open);document.querySelector('#nav-backdrop').hidden=!open;document.querySelector('main').inert=open;document.body.classList.toggle('nav-open',open);return}if(b.hasAttribute('data-close')){dialog.close();return}if(b.dataset.page||b.dataset.go){if(dialog.open)dialog.close();const fromMenu=b.dataset.page&&document.querySelector('#nav-toggle').getAttribute('aria-expanded')==='true';closeNav();navigate(b.dataset.page||b.dataset.go);clearSelection();await load();if(fromMenu){const heading=content.querySelector('h1');heading.tabIndex=-1;heading.focus({preventScroll:true})}return}if(b.dataset.filter){const filter=b.dataset.filter;state.filter=filter;state.invoicePage=0;await load();content.querySelector(`[data-filter="${filter}"]`)?.focus({preventScroll:true});return}if(b.dataset.preview){openInvoicePreview(state.invoices.find(x=>x.id===Number(b.dataset.preview)));return}if(b.dataset.action){const menu=b.closest('details');if(menu)menu.open=false;return await action(b.dataset.action);}if(b.dataset.delete){showDeleteDialog([Number(b.dataset.delete)]);return}if(b.dataset.applyAi){const item=state.invoices.find(x=>x.id===Number(b.dataset.applyAi));let suggested={};try{suggested=JSON.parse(item.ai_suggestion||'{}')}catch{}for(const key of ['buyer','seller','invoice_date','invoice_number'])if(suggested[key]&&form.elements[key])form.elements[key].value=suggested[key];if(suggested.amount_cents!=null)form.elements.amount.value=(suggested.amount_cents/100).toFixed(2);toast('已填入建议，请核对后保存');return}if(b.dataset.duplicateDecision){await api(`/api/invoices/${b.dataset.id}/duplicate`,{method:'PATCH',body:JSON.stringify({decision:b.dataset.duplicateDecision})});await load();toast(b.dataset.duplicateDecision==='confirm'?'已标记为重复':b.dataset.duplicateDecision==='distinct'?'已确认非重复':'已撤销重复标记');return}if(b.dataset.merge){showMergeDialog(Number(b.dataset.merge));return}if(b.dataset.viewDuplicate){openInvoicePreview(state.invoices.find(x=>x.id===Number(b.dataset.viewDuplicate)));return}if(b.dataset.edit){if(dialog.open)dialog.close();return edit(Number(b.dataset.edit))}if(b.dataset.ai){b.disabled=true;b.textContent='AI 识别中…';try{const result=await (await api(`/api/invoices/${b.dataset.ai}/ai`,{method:'POST'})).json();await load();toast(result.message)}finally{b.disabled=false;b.textContent='AI 辅助识别'}return}if(b.dataset.reprocess)return await reprocessMessage(b);if(b.dataset.email)return await showEmail(Number(b.dataset.email));if(b.dataset.retry){toast('正在识别，请稍候');const result=await (await api(`/api/invoices/${b.dataset.retry}/retry`,{method:'POST'})).json();await load();toast(result.reason||'识别完成')}if(b.dataset.credentials){openDialog('更新邮箱授权码','<div class="field"><label>新的 QQ 邮箱授权码<input type="password" name="auth_code" required autocomplete="new-password"></label></div>','保存');form.dataset.submit='credentials';form.dataset.id=b.dataset.credentials;return}if(b.dataset.toggleAccount){await api(`/api/accounts/${b.dataset.toggleAccount}`,{method:'PATCH',body:JSON.stringify({enabled:b.dataset.enabled==='1'})});await load();toast('邮箱监控状态已更新')}if(b.dataset.sync){await api(`/api/accounts/${b.dataset.sync}/sync`,{method:'POST'});toast('已开始同步，请稍后刷新页面查看结果')}if(b.dataset.groupStatus){await api(`/api/groups/${b.dataset.groupStatus}`,{method:'PATCH',body:JSON.stringify({reimbursed:b.dataset.status==='1'})});await load();toast('报销状态已更新')}if(b.dataset.groupDetail){const g=state.groups.find(x=>x.id===Number(b.dataset.groupDetail));openDialog(esc(g.name),g.invoices.map(i=>`<div class="row group-detail-row"><span>${esc(i.seller||i.original_name)}</span><span>${date(i.invoice_date)}</span><strong>${cash(i.amount_cents)}</strong></div>`).join(''));delete form.dataset.submit}}catch(err){toast(err.message)}}
document.addEventListener('click',handleClick);
document.addEventListener('error',e=>{if(e.target.matches?.('.preview-document img,.document-canvas img')){const viewer=e.target.closest('[data-document-viewer]');viewer?.querySelectorAll('[data-preview-zoom]').forEach(button=>button.disabled=true);const message=document.createElement('div');message.className='preview-unavailable';message.textContent='预览暂不可用，请打开原件查看';e.target.replaceWith(message)}},true);
document.addEventListener('click',e=>{if(!e.target.closest('.shell > aside')&&document.querySelector('#nav-toggle').getAttribute('aria-expanded')==='true')closeNav()});
document.addEventListener('click',e=>{if(!e.target.closest('.export-menu'))document.querySelectorAll('.export-menu[open]').forEach(menu=>menu.open=false)});
matchMedia('(min-width:941px)').addEventListener('change',e=>{if(e.matches)closeNav()});
document.addEventListener('keydown',e=>{if(e.key==='Tab'&&document.querySelector('#nav-toggle').getAttribute('aria-expanded')==='true'){const trigger=matchMedia(mobileNavQuery).matches?document.querySelector('#mobile-more'):document.querySelector('#nav-toggle');const items=[trigger,...[...document.querySelectorAll('#main-nav button')].filter(item=>item.getClientRects().length)];const first=items[0],last=items[items.length-1];if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus()}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus()}else if(!e.shiftKey&&document.activeElement===first&&matchMedia(mobileNavQuery).matches){e.preventDefault();items[1]?.focus()}}if(e.key==='Escape'&&document.querySelector('.export-menu[open]')){const menu=document.querySelector('.export-menu[open]');menu.open=false;menu.querySelector('summary').focus();return}if(e.key==='Escape'&&document.querySelector('#nav-toggle').getAttribute('aria-expanded')==='true'){closeNav();document.querySelector(matchMedia(mobileNavQuery).matches?'#mobile-more':'#nav-toggle').focus()}});
content.addEventListener('change',async e=>{try{if(e.target.id==='invoice-sort'){state.invoiceSort=e.target.value;state.invoicePage=0;await load();content.querySelector('#invoice-sort')?.focus({preventScroll:true});return}if(e.target.id==='upload'){const input=e.target,files=[...input.files];if(!files.length)return;const button=content.querySelector('[data-action=upload]');button.disabled=true;try{await inspectUpload(files)}finally{input.value='';const current=content.querySelector('[data-action=upload]');if(current)current.disabled=false}}if(e.target.id==='restore-file'){const input=e.target,file=input.files[0];if(!file)return;const data=new FormData();data.append('file',file);const button=content.querySelector('[data-action=select-restore]');button.disabled=true;button.textContent='正在校验…';try{const result=await (await api('/api/backup/inspect',{method:'POST',body:data})).json();pendingRestore=file;showRestorePreview(result)}finally{input.value='';const current=content.querySelector('[data-action=select-restore]');if(current){current.disabled=false;current.textContent='选择备份文件'}}}if(e.target.id==='select-visible'){state.invoices.forEach(x=>{if(e.target.checked){state.selected.add(x.id);state.selectedRecords.set(x.id,x)}else{state.selected.delete(x.id);state.selectedRecords.delete(x.id)}});render();content.querySelector('#select-visible')?.focus({preventScroll:true})}if(e.target.dataset.select){const id=Number(e.target.dataset.select),item=state.invoices.find(x=>x.id===id);if(e.target.checked&&item){state.selected.add(id);state.selectedRecords.set(id,item)}else{state.selected.delete(id);state.selectedRecords.delete(id)}render();content.querySelector(`[data-select="${id}"]`)?.focus({preventScroll:true})}if(e.target.id==='account-filter'||e.target.id==='email-status-filter'){const select=e.target;select.closest('.email-select').querySelector('.email-select-value>span').textContent=select.selectedOptions[0].textContent;const id=select.id;if(id==='account-filter')state.account=e.target.value;else state.emailFilter=e.target.value;state.emailPage=0;await load();content.querySelector('#'+id)?.focus({preventScroll:true})}}catch(err){toast(err.message)}});
let searchTimer;content.addEventListener('input',e=>{if(e.target.id==='template'){const fields=e.target.value.split(/[,，]/).map(x=>x.trim());document.querySelector('#template-preview').textContent=templateExample(fields)}if(e.target.id==='search'){state.query=e.target.value;state.invoicePage=0;clearTimeout(searchTimer);searchTimer=setTimeout(()=>load().catch(error=>toast(error.message)),250)}});
form.addEventListener('change',e=>{if(e.target.id==='upload-select-all')form.querySelectorAll('[data-upload-choice]:not(:disabled)').forEach(input=>input.checked=e.target.checked);if(e.target.id==='upload-select-all'||e.target.hasAttribute('data-upload-choice'))updateUploadSelection()});
dialog.addEventListener('cancel',e=>{if(form.dataset.uploading==='1')e.preventDefault()});
dialog.addEventListener('close',()=>{pendingUpload=[];pendingRestore=null;pendingMerge=null;delete form.dataset.uploading});
form.addEventListener('submit',async e=>{e.preventDefault();const f=new FormData(form);const type=form.dataset.submit;try{if(type==='select-invoice-dates'){await submitInvoiceDateSelection(f);return}if(type==='merge-invoices'){await submitInvoiceMerge();return}if(type==='notification-save'||type==='notification-delete'){await submitNotification(type,f);return}if(type==='delete-invoices'){const items=[...pendingDelete],ids=items.map(item=>item.id),button=form.querySelector('[type=submit]'),controls=[...form.querySelectorAll('button')].map(el=>[el,el.disabled]);form.dataset.uploading='1';controls.forEach(([el])=>el.disabled=true);button.textContent='正在删除…';try{const result=await (await api('/api/invoices/delete',{method:'POST',body:JSON.stringify({items})})).json();delete form.dataset.uploading;dialog.close();ids.forEach(id=>{state.selected.delete(id);state.selectedRecords.delete(id)});state.preview=null;ids.forEach(id=>pdfPages.delete(id));try{await load()}catch{toast('已删除，请刷新页面查看');return}toast(result.cleanup_pending?'发票已删除，部分原件未清理，请检查权限并重启':'已删除发票')}catch(error){delete form.dataset.uploading;controls.forEach(([el,disabled])=>el.disabled=disabled);button.textContent='删除发票';throw error}return}if(type==='restore-backup'){if(!pendingRestore)return;const data=new FormData();data.append('file',pendingRestore);const button=form.querySelector('[type=submit]'),controls=[...form.querySelectorAll('button,input')].map(el=>[el,el.disabled]);form.dataset.uploading='1';controls.forEach(([el])=>el.disabled=true);button.textContent='正在恢复…';try{const restored=await (await api('/api/backup/restore',{method:'POST',body:data})).json();delete form.dataset.uploading;dialog.close();showLogin(restored.recovery_incomplete?'备份已恢复。恢复前的数据不完整，已保留原始副本。请使用备份中的账号密码登录。':'备份已恢复，请使用备份中的账号密码重新登录。')}catch(error){delete form.dataset.uploading;controls.forEach(([el,disabled])=>el.disabled=disabled);button.textContent='确认恢复';throw error}return}if(type==='upload-preview'){const selected=[...form.querySelectorAll('[data-upload-choice]:checked')].map(input=>pendingUpload[Number(input.dataset.uploadChoice)]);if(selected.length)await importUpload(selected);return}if(type==='invoice'){const amount=f.get('amount');await api(`/api/invoices/${form.dataset.id}`,{method:'PATCH',body:JSON.stringify({buyer:f.get('buyer'),seller:f.get('seller'),amount_cents:amount===''?null:Math.round(Number(amount)*100),invoice_date:f.get('invoice_date')||null,invoice_number:f.get('invoice_number'),export_name:f.get('export_name')})})}if(type==='credentials'){await api(`/api/accounts/${form.dataset.id}`,{method:'PATCH',body:JSON.stringify({auth_code:f.get('auth_code')})})}if(type==='account'){const account=await (await api('/api/accounts',{method:'POST',body:JSON.stringify(Object.fromEntries(f))})).json();await api(`/api/accounts/${account.id}/sync`,{method:'POST'})}if(type==='group'){await api('/api/groups',{method:'POST',body:JSON.stringify({name:f.get('name'),ids:[...state.selected]})});clearSelection();navigate('groups')}dialog.close();await load();toast(type==='account'?'邮箱已添加，正在首次同步':'已保存')}catch(err){form.querySelector('.inline-error').textContent=err.message}});
bootstrap();
document.addEventListener('pointerdown',()=>{pointerActive=true;document.body.dataset.inputMode='pointer'});
for(const event of ['pointerup','pointercancel'])document.addEventListener(event,()=>{pointerActive=false;lastInteraction=Date.now()});
window.addEventListener('blur',()=>{pointerActive=false;lastInteraction=Date.now()});
document.addEventListener('keydown',e=>{if(['Tab','Enter',' ','ArrowUp','ArrowDown','ArrowLeft','ArrowRight','Home','End','PageUp','PageDown'].includes(e.key))document.body.dataset.inputMode='keyboard';if(e.key==='Enter'||e.key===' ')lastInteraction=Date.now()});
setInterval(()=>{if(['accounts','emails'].includes(state.page))load({background:true}).catch(()=>{})},10000);

document.addEventListener('submit',async e=>{if(e.target.id!=='ai-settings-form')return;e.preventDefault();const target=e.target, data=new FormData(target),button=target.querySelector('[type=submit]');button.disabled=true;const error=target.querySelector('.inline-error');error.textContent='';try{await api('/api/ai-settings',{method:'PUT',body:JSON.stringify({base_url:data.get('base_url'),model:data.get('model'),api_key:data.get('api_key'),auto_enabled:data.has('auto_enabled'),clear_key:data.has('clear_key')})});await load();toast('AI 设置已保存')}catch(err){error.textContent=err.message}finally{button.disabled=false}});

function showLogin(message=''){
  const screen=document.querySelector('#login-screen'),shell=document.querySelector('.shell');loadSequence++;
  pdfPages.clear();mailReprocessFeedback.clear();lastLoadSnapshot='';if(!shell.hidden){Object.assign(state,{username:'',invoices:[],groups:[],accounts:[],emails:[],notifications:[],preview:null,query:'',account:'',emailFilter:'all'});clearSelection();if(dialog.open)dialog.close();closeNav()}
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

const pushPlatforms = {wxpusher:'WxPusher', wecom:'企业微信应用', serverchan:'Server酱'};
const pushCredentials = {wxpusher:'AppToken', wecom:'应用 Secret', serverchan:'SendKey'};

function notificationsPage(){
  const heading = header('消息推送','邮箱自动收票成功或识别失败时，向已启用的渠道发送通知。',
    '<button type="button" class="button primary" data-push-action="add">'+uiIcon('plus')+'添加渠道</button>');
  const rows = state.notifications.map(channel=>{
    const when = channel.last_attempt_at ? new Date(channel.last_attempt_at).toLocaleString('zh-CN') : '';
    return `<div class="notification-row">
      <div class="notification-identity">
        <strong>${esc(channel.name)}</strong>
        <span>${esc(pushPlatforms[channel.provider])} · ${channel.enabled?'已启用':'已停用'}</span>
      </div>
      <div class="notification-delivery">
        <span role="status" data-push-result="${channel.id}">${esc(channel.last_result || '尚未发送')}</span>
        ${when?`<time datetime="${esc(channel.last_attempt_at)}">${esc(when)}</time>`:''}
      </div>
      <div class="notification-actions">
        <button class="button" type="button" data-push-action="test" data-id="${channel.id}">发送测试</button>
        <button class="button" type="button" data-push-action="edit" data-id="${channel.id}">编辑</button>
        <button class="button danger" type="button" data-push-action="delete" data-id="${channel.id}">删除</button>
      </div>
    </div>`;
  }).join('');
  return heading+`<div class="section-head"><h2>通知渠道 <span class="muted">${state.notifications.length} 个</span></h2></div>
    <div class="box list-box">${rows||empty('还没有通知渠道。添加后可发送测试消息。')}</div>
    <p class="hint">每封邮件按结果汇总通知，附公司名称、税号、含税金额、日期和票号；未识别字段会注明。推送凭证加密保存在本地。</p>`;
}

function updateNotificationFields(provider){
  form.querySelectorAll('[data-push-provider]').forEach(group=>{
    group.hidden = group.dataset.pushProvider !== provider;
    group.querySelectorAll('input').forEach(input=>input.disabled=group.hidden);
  });
  form.querySelector('[data-push-secret-label]').textContent=pushCredentials[provider];
}

function editNotification(channel){
  const provider = channel?.provider || 'wxpusher', options = channel?.options || {};
  const selection = Object.entries(pushPlatforms).map(([key,label])=>
    `<option value="${key}" ${key===provider?'selected':''}>${label}</option>`).join('');
  openDialog(channel?'编辑通知渠道':'添加通知渠道',`
    <div class="notification-form-fields">
    <div class="fields">
      <label class="field">渠道名称<input name="name" required maxlength="80" value="${esc(channel?.name)}" placeholder="例如：我的微信"></label>
      <label class="field">推送平台<select name="provider" ${channel?'disabled':''}>${selection}</select></label>
      <label class="field full"><span data-push-secret-label></span>
        <input name="secret" type="password" autocomplete="new-password" maxlength="512"
          ${channel?'placeholder="已保存，留空保留原凭证"':'required'}>
      </label>
    </div>
    <div data-push-provider="wxpusher" hidden><div class="fields">
      <label class="field full">接收 UID<input name="uids" maxlength="4096" value="${esc(options.uids?.join(', '))}" placeholder="UID_…"></label>
      <label class="field full">主题 ID<input name="topics" maxlength="100" value="${esc(options.topics?.join(', '))}" placeholder="例如：123">
        <span class="hint">UID 和主题至少填写一项，多项用逗号分隔。</span>
      </label>
    </div></div>
    <div data-push-provider="wecom" hidden><div class="fields">
      <label class="field">企业 ID<input name="corp_id" maxlength="128" value="${esc(options.corp_id)}" required></label>
      <label class="field">应用 AgentId<input name="agent_id" inputmode="numeric" maxlength="12" value="${esc(options.agent_id)}" required></label>
      <label class="field full">接收成员 UserID<input name="users" maxlength="4096" value="${esc(options.users?.join(', '))}" required>
        <span class="hint">多名成员用逗号分隔。单独填写 @all 会发送给应用可见范围内的所有成员。</span>
      </label>
    </div></div>
    <div data-push-provider="serverchan" hidden><p class="hint">支持 Server酱 Turbo 和 Server酱³ 的 SendKey。</p></div>
    </div>
    <div class="check-list"><label><input type="checkbox" name="enabled" ${channel?.enabled===false?'':'checked'}> 启用自动通知</label></div>
  `,'保存渠道');
  form.dataset.submit='notification-save';
  form.dataset.id=channel?.id || '';
  updateNotificationFields(provider);
}

form.addEventListener('change', e=>{
  if(form.dataset.submit==='notification-save' && e.target.name==='provider'){
    updateNotificationFields(e.target.value);
  }
});

async function submitNotification(type, data){
  if(form.dataset.uploading)return;
  const id=form.dataset.id, path='/api/notification-channels'+(id?'/'+id:'');
  const buttons=[...form.querySelectorAll('button')], button=form.querySelector('[type=submit]');
  const previousLabel=button.textContent;
  form.querySelector('.inline-error').textContent='';
  form.dataset.uploading='1';
  buttons.forEach(item=>item.disabled=true);
  button.textContent='正在处理…';
  try{
    if(type==='notification-delete')await api(path,{method:'DELETE'});
    else{
      const value=Object.fromEntries(data);
      value.provider=form.elements.provider.value;
      value.enabled=data.has('enabled');
      await api(path,{method:id?'PUT':'POST',body:JSON.stringify(value)});
    }
    dialog.close();
    try{await load()}catch{toast('已保存变更，请刷新查看');return}
    toast(type==='notification-delete'?'已删除通知渠道':'已保存通知渠道');
  }finally{
    delete form.dataset.uploading;
    buttons.forEach(item=>item.disabled=false);
    button.textContent=previousLabel;
  }
}

content.addEventListener('click',async e=>{
  const button=e.target.closest('[data-push-action]');
  if(!button)return;
  const action=button.dataset.pushAction;
  if(action==='add'){editNotification();return}
  const channel=state.notifications.find(item=>item.id===Number(button.dataset.id));
  if(!channel)return;
  if(action==='edit'){editNotification(channel);return}
  if(action==='delete'){
    openDialog('删除通知渠道',`<p class="sub">确定删除「${esc(channel.name)}」？此渠道将停止接收通知。</p>`,'删除渠道');
    form.dataset.submit='notification-delete';
    form.dataset.id=channel.id;
    return;
  }
  const result=content.querySelector(`[data-push-result="${channel.id}"]`);
  button.disabled=true;
  result.textContent='正在发送测试消息…';
  try{
    const response=await (await api(`/api/notification-channels/${channel.id}/test`,{method:'POST'})).json();
    result.textContent=response.message;
  }catch(error){result.textContent=error.message}
  finally{button.disabled=false}
});
