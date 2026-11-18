let previewInvoiceRecord=null,invoiceTagEditor=null,tagRenameId=null,tagRenameDraft='',tagFilterSearch='',savingTag=false;

function appendTagFilters(params){
  for(const id of state.tagIds||[])params.append('tag_id',String(id));
  if(state.untagged)params.set('untagged','true');
  return params;
}
function tagFilterKey(){return JSON.stringify([state.tagIds||[],!!state.untagged])}
function tagChips(tags){
  return (tags||[]).map(tag=>`<span class="tag-chip" title="${esc(tag.name)}">${esc(tag.name)}</span>`).join('');
}
function invoiceTagsCell(invoice){
  return `<div class="invoice-tag-stack" aria-label="发票标签">${tagChips(invoice.tags)||'<span class="muted">—</span>'}</div>`;
}
function tagFilterOptions(){
  const tags=(state.tags||[]).filter(tag=>tag.name.toLocaleLowerCase().includes(tagFilterSearch.toLocaleLowerCase()));
  return tags.map(tag=>`<label class="tag-option"><input type="checkbox" id="tag-filter-${tag.id}" data-tag-filter="${tag.id}" ${(state.tagIds||[]).includes(tag.id)?'checked':''}><span>${esc(tag.name)}</span><small>${tag.invoice_count}</small></label>`).join('')+
    (!tagFilterSearch?`<label class="tag-option"><input type="checkbox" id="tag-filter-untagged" data-tag-filter="untagged" ${state.untagged?'checked':''}><span>无标签</span><small>${state.untaggedCount||0}</small></label>`:'')+
    (tagFilterSearch&&!tags.length?'<p class="hint">没有找到标签。</p>':'');
}
function tagFilterControl(){
  return `<details class="tag-filter export-menu"><summary id="tag-filter-trigger" class="button" aria-label="标签筛选">${uiIcon('tag')}标签筛选${uiIcon('chevron-down')}</summary><div class="export-options tag-filter-options"><strong>筛选标签</strong><label class="tag-search">${uiIcon('search')}<input type="search" id="tag-filter-search" aria-label="搜索筛选标签" placeholder="搜索标签" value="${esc(tagFilterSearch)}"></label><div class="tag-filter-choices">${tagFilterOptions()}</div><p class="hint">匹配任一所选标签</p><button type="button" data-tag-clear>清除筛选</button></div></details>`;
}
function tagFilterStrip(){
  const selected=(state.tags||[]).filter(tag=>(state.tagIds||[]).includes(tag.id));
  if(state.untagged)selected.push({id:'untagged',name:'无标签'});
  if(!selected.length)return '';
  return `<div class="tag-filter-strip" aria-label="当前标签筛选"><span>已选标签</span>${selected.map(tag=>`<button type="button" class="tag-filter-chip" data-tag-remove-filter="${tag.id}" aria-label="取消筛选 ${esc(tag.name)}" title="${esc(tag.name)}"><span>${esc(tag.name)}</span>${uiIcon('x')}</button>`).join('')}<span aria-live="polite">${state.invoiceTotal} 张发票</span><button class="button text-button" type="button" data-tag-clear>清除筛选</button></div>`;
}
function tagPage(){
  return header('标签管理','管理发票标签和自动打标规则。')+`<section class="box tag-management"><form id="tag-create-form" class="tag-create"><label class="field">新建标签<input name="name" aria-label="新标签名称" placeholder="输入新标签名称" maxlength="100" required></label><button class="button primary" type="submit">新建标签</button><p class="inline-error" role="alert"></p></form><div class="tag-manage-head"><span>标签名称</span><span>关联发票</span><span>操作</span></div>${state.tags.length?state.tags.map(tag=>`<div class="tag-manage-row" data-tag-row="${tag.id}">${tagRenameId===tag.id?`<form id="tag-rename-form" class="tag-rename" data-tag-id="${tag.id}"><input name="name" aria-label="标签完整名称" value="${esc(tagRenameDraft)}" maxlength="100" required><span>${tag.invoice_count} 张</span><div class="actions"><button class="button text-button" type="submit">保存</button><button class="button text-button" type="button" data-tag-cancel-rename="${tag.id}">取消</button></div><p class="inline-error" role="alert"></p></form>`:`<span class="tag-chip tag-management-name" title="${esc(tag.name)}">${esc(tag.name)}</span><span>${tag.invoice_count} 张</span><div class="actions"><button class="button text-button" type="button" data-tag-rename="${tag.id}" aria-label="重命名 ${esc(tag.name)}">重命名</button><button class="button text-button danger" type="button" data-tag-delete="${tag.id}" aria-label="删除标签 ${esc(tag.name)}">删除</button></div>`}</div>`).join(''):empty('还没有标签，可以先新建标签，再在发票预览中添加。')}<p class="hint tag-management-help">删除标签会移除发票上的对应标签和自动打标规则，发票本身保留。</p></section>`+tagRulesSection();
}
function tagRulesSection(){
  const rules=(state.tagRules||[]).map(rule=>{
    const tag=state.tags.find(tag=>tag.id===rule.tag_id);
    if(!tag)return '';
    return `<div class="tag-rule-row"><div><strong>${esc(rule.buyer)}</strong><span class="preview-tag">${esc(tag.name)}</span></div><button class="button text-button danger" type="button" data-tag-rule-delete="${rule.id}" aria-label="删除规则 ${esc(rule.buyer)} ${esc(tag.name)}">删除</button></div>`;
  }).join('');
  const options=state.tags.map(tag=>`<option value="${tag.id}">${esc(tag.name)}</option>`).join('');
  return `<section class="box tag-management tag-rules" aria-labelledby="tag-rules-title">
    <h2 id="tag-rules-title">自动标签</h2>
    <p class="hint">发票识别成功后，完整抬头匹配的规则会自动添加标签。同一抬头可添加多个标签。</p>
    ${options?`<form id="tag-rule-form">
      <div class="fields"><label class="field">发票抬头<input name="buyer" maxlength="200" required placeholder="输入完整抬头"></label>
      <label class="field">自动添加标签<select name="tag_id" required><option value="">请选择标签</option>${options}</select></label></div>
      <div class="tag-rule-actions"><label><input type="checkbox" name="apply_existing">同时给已有发票打标签</label>
      <button class="button primary" type="submit">添加规则</button></div>
      <p class="inline-error" role="alert"></p>
    </form>`:'<p class="hint">先新建一个标签，再添加规则。</p>'}
    ${rules||'<p class="hint">暂无自动标签规则。</p>'}
  </section>`;
}
function invoiceTagsSection(invoice){
  const editor=invoiceTagEditor?.id===invoice.id?invoiceTagEditor:null;
  const full=(invoice.tags||[]).map(tag=>`<span class="preview-tag" title="${esc(tag.name)}">${esc(tag.name)}</span>`).join('');
  return `<section class="invoice-tags-section" data-invoice-tags="${invoice.id}" aria-label="发票标签"><div class="invoice-tags-heading"><h3>标签</h3><button class="button text-button" type="button" data-tag-edit="${invoice.id}" ${editor?'hidden':''}>${uiIcon('plus')}${invoice.tags?.length?'编辑标签':'添加标签'}</button></div>${full?`<div class="preview-tags">${full}</div>`:editor?'':'<p class="muted">暂无标签</p>'}${editor?`<div class="invoice-tag-editor"><label class="tag-search">${uiIcon('search')}<input type="search" data-tag-search aria-label="搜索或新建标签" placeholder="搜索或新建标签" maxlength="100" value="${esc(editor.query)}" ${editor.saving?'disabled':''}></label><div class="invoice-tag-choices">${invoiceTagChoices()}</div><p class="inline-error" role="alert">${esc(editor.error||'')}</p><div class="invoice-note-actions"><button class="button" type="button" data-tag-cancel ${editor.saving?'disabled':''}>取消</button><button class="button primary" type="button" data-tag-save ${editor.saving?'disabled':''}>${editor.saving?'保存中…':'保存标签'}</button></div></div>`:''}</section>`;
}
function invoiceTagChoices(){
  const editor=invoiceTagEditor,query=editor.query.trim();
  const tags=state.tags.filter(tag=>tag.name.toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  const exact=state.tags.some(tag=>tag.name.toLocaleLowerCase()===query.toLocaleLowerCase());
  return tags.map(tag=>`<label class="tag-option"><input type="checkbox" data-tag-choice="${tag.id}" ${editor.ids.has(tag.id)?'checked':''} ${editor.saving?'disabled':''}><span>${esc(tag.name)}</span></label>`).join('')+
    (query&&!exact?`<button class="button text-button tag-create-choice" type="button" data-tag-create-choice ${editor.saving?'disabled':''}>${uiIcon('plus')}<span>新建“${esc(query)}”并选中</span></button>`:'')+
    (!query&&!tags.length?'<p class="hint">输入名称即可新建标签。</p>':'');
}
function refreshInvoiceTagSection(invoice){
  const section=form.querySelector(`[data-invoice-tags="${invoice.id}"]`);
  if(section)section.outerHTML=invoiceTagsSection(invoice);
}
async function handleInvoiceTags(button){
  const invoice=previewInvoiceRecord;
  if(!invoice||invoiceTagEditor?.saving)return;
  if(button.hasAttribute('data-tag-edit')){
    const tags=await (await api('/api/tags')).json();
    if(previewInvoiceRecord!==invoice)return;
    state.tags=tags;
    const baseIds=(invoice.tags||[]).map(tag=>tag.id).filter(id=>state.tags.some(tag=>tag.id===id));
    invoiceTagEditor={id:invoice.id,baseIds,ids:new Set(baseIds),query:'',saving:false,error:''};
    refreshInvoiceTagSection(invoice);form.querySelector('[data-tag-search]')?.focus();return;
  }
  if(button.hasAttribute('data-tag-cancel')){
    invoiceTagEditor=null;refreshInvoiceTagSection(invoice);form.querySelector('[data-tag-edit]')?.focus();return;
  }
  const editor=invoiceTagEditor;if(!editor)return;
  editor.saving=true;editor.error='';refreshInvoiceTagSection(invoice);
  try{
    if(button.hasAttribute('data-tag-create-choice')){
      const tag=await (await api('/api/tags',{method:'POST',body:JSON.stringify({name:editor.query.trim()})})).json();
      if(invoiceTagEditor!==editor)return;
      state.tags.push(tag);
      editor.ids.add(tag.id);editor.query='';editor.saving=false;refreshInvoiceTagSection(invoice);form.querySelector('[data-tag-search]')?.focus();return;
    }
    const payload={tag_ids:[...editor.ids],base_tag_ids:editor.baseIds};
    const result=await (await api(`/api/invoices/${invoice.id}/tags`,{method:'PUT',body:JSON.stringify(payload)})).json();
    loadSequence++;
    for(const record of [invoice,...state.invoices,...state.selectedRecords.values()])if(record.id===invoice.id)record.tags=result.tags;
    if(invoiceTagEditor===editor){invoiceTagEditor=null;refreshInvoiceTagSection(invoice);form.querySelector('[data-tag-edit]')?.focus()}
    try{await load();toast('标签已保存')}catch{toast('标签已保存，请刷新列表查看')}
  }catch(error){
    if(invoiceTagEditor!==editor)return;
    editor.saving=false;editor.error=error.message;refreshInvoiceTagSection(invoice);form.querySelector('[data-tag-search]')?.focus();
  }
}
async function changeTagFilters(value,checked){
  if(value==='untagged')state.untagged=checked;
  else if(checked)state.tagIds=[...new Set([...state.tagIds,Number(value)])];
  else state.tagIds=state.tagIds.filter(id=>id!==Number(value));
  state.invoicePage=0;await load();
}
async function submitTagDelete(){
  const button=form.querySelector('[type=submit]');if(button.disabled)return;
  const controls=[...form.querySelectorAll('button')];controls.forEach(control=>control.disabled=true);form.dataset.uploading='1';
  try{
    const id=Number(form.dataset.tagId);
    const rule=form.dataset.submit==='delete-tag-rule';
    await api(`/api/${rule?'tag-rules':'tags'}/${id}`,{method:'DELETE'});
    if(!rule){state.tagIds=state.tagIds.filter(value=>value!==id);state.invoicePage=0}
    delete form.dataset.uploading;dialog.close();
    const message=rule?'规则已删除':'标签已删除';
    try{await load();toast(message)}catch{toast(message+'，请刷新页面查看')}
  }catch(error){delete form.dataset.uploading;controls.forEach(control=>control.disabled=false);throw error}
}
document.addEventListener('click',async event=>{
  const button=event.target.closest('button');if(!button)return;
  try{
    if(button.matches('[data-tag-edit],[data-tag-cancel],[data-tag-save],[data-tag-create-choice]')){await handleInvoiceTags(button);return}
    if(button.hasAttribute('data-tag-clear')){
      state.tagIds=[];state.untagged=false;state.invoicePage=0;tagFilterSearch='';await load();content.querySelector('#tag-filter-trigger')?.focus();return;
    }
    if(button.dataset.tagRemoveFilter){await changeTagFilters(button.dataset.tagRemoveFilter,false);content.querySelector('#tag-filter-trigger')?.focus();return}
    if(button.dataset.tagRename){
      tagRenameId=Number(button.dataset.tagRename);tagRenameDraft=state.tags.find(tag=>tag.id===tagRenameId).name;
      render();content.querySelector('#tag-rename-form input').focus();return;
    }
    if(button.dataset.tagCancelRename){tagRenameId=null;render();content.querySelector(`[data-tag-rename="${button.dataset.tagCancelRename}"]`)?.focus();return}
    if(button.dataset.tagRuleDelete){
      const rule=state.tagRules.find(rule=>rule.id===Number(button.dataset.tagRuleDelete));
      openDialog('删除自动标签规则',`<p class="sub">确认删除“${esc(rule.buyer)}”的这条规则？已打上的标签会保留。</p>`,'删除规则');
      form.dataset.submit='delete-tag-rule';form.dataset.tagId=rule.id;
      return;
    }
    if(button.dataset.tagDelete){
      const tag=state.tags.find(tag=>tag.id===Number(button.dataset.tagDelete));
      openDialog('删除标签',`<p class="sub">确认删除“${esc(tag.name)}”？</p><p class="hint">此标签关联了 ${tag.invoice_count} 张发票。删除后会移除所有关联，发票本身保留。</p>`,'删除标签');
      form.dataset.submit='delete-tag';form.dataset.tagId=tag.id;
    }
  }catch(error){toast(error.message)}
});
document.addEventListener('input',event=>{
  if(event.target.id==='tag-filter-search'){
    tagFilterSearch=event.target.value;content.querySelector('.tag-filter-choices').innerHTML=tagFilterOptions();
  }
  if(event.target.matches('[data-tag-search]')&&invoiceTagEditor){
    invoiceTagEditor.query=event.target.value;form.querySelector('.invoice-tag-choices').innerHTML=invoiceTagChoices();
  }
  if(event.target.closest('#tag-rename-form')&&event.target.name==='name')tagRenameDraft=event.target.value;
});
document.addEventListener('change',async event=>{
  const input=event.target;
  if(input.hasAttribute('data-tag-choice')&&invoiceTagEditor){
    if(input.checked)invoiceTagEditor.ids.add(Number(input.dataset.tagChoice));else invoiceTagEditor.ids.delete(Number(input.dataset.tagChoice));
  }
  if(input.hasAttribute('data-tag-filter')){
    try{
      await changeTagFilters(input.dataset.tagFilter,input.checked);
      const menu=content.querySelector('.tag-filter');if(menu)menu.open=true;
      content.querySelector('#tag-filter-'+input.dataset.tagFilter)?.focus();
    }catch(error){toast(error.message)}
  }
});
document.addEventListener('focusin',event=>{const menu=document.querySelector('.tag-filter[open]');if(menu&&!menu.contains(event.target))menu.open=false});
document.addEventListener('submit',async event=>{
  const target=event.target;if(!['tag-create-form','tag-rename-form','tag-rule-form'].includes(target.id))return;
  event.preventDefault();if(savingTag)return;
  savingTag=true;
  const fields=target.elements,rule=target.id==='tag-rule-form';
  const payload=rule?{buyer:fields.buyer.value.trim(),tag_id:Number(fields.tag_id.value),apply_existing:fields.apply_existing.checked}:{name:fields.name.value.trim()};
  const controls=[...content.querySelectorAll('.tag-management input,.tag-management button,.tag-management select')];controls.forEach(control=>control.disabled=true);
  const error=target.querySelector('.inline-error');error.textContent='';
  const rename=target.id==='tag-rename-form';
  try{
    const response=await api(rule?'/api/tag-rules':rename?`/api/tags/${target.dataset.tagId}`:'/api/tags',{method:rename?'PATCH':'POST',body:JSON.stringify(payload)});
    const applied=rule?(await response.json()).applied:null;
    if(rename)tagRenameId=null;
    const message=rule?(payload.apply_existing?`规则已添加，已为 ${applied} 张发票添加标签`:'规则已添加'):rename?'标签已重命名':'标签已创建';
    try{await load();toast(message)}catch{toast(message+'，请刷新页面查看')}
    if(state.page==='tags')content.querySelector(rule?'#tag-rule-form input':'#tag-create-form input')?.focus();
  }catch(err){error.textContent=err.message}
  finally{savingTag=false;controls.forEach(control=>control.disabled=false);if(error.textContent&&target.isConnected)(rule?fields.buyer:fields.name).focus()}
});
