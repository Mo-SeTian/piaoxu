function dashboardChart(months){
  const values=months.map(month=>month.amount_cents);
  const min=Math.min(0,...values),max=Math.max(0,...values);
  const step=Math.max(1,Math.pow(10,Math.floor(Math.log10(Math.max(100,max-min)/4))));
  const interval=Math.ceil(Math.max(400,max-min)/4/step)*step;
  const lower=Math.floor(min/interval)*interval,upper=Math.max(lower+interval,Math.ceil(max/interval)*interval);
  const percent=value=>100*(value-lower)/(upper-lower);
  const lines=[];
  for(let value=lower;value<=upper;value+=interval){
    lines.push(`<div class="dashboard-chart-line" style="bottom:${percent(value)}%"><span>${(value/100).toLocaleString('zh-CN')}</span></div>`);
  }
  return `<div class="dashboard-chart"><div class="dashboard-plot">
    <div aria-hidden="true">${lines.join('')}</div>
    <ul class="dashboard-bars" aria-label="各月开票金额">${months.map(month=>{
      const value=month.amount_cents,baseline=percent(0),end=percent(value);
      return `<li aria-label="${esc(month.month)}，${cash(value)}，${month.count} 张" title="${esc(month.month)} · ${cash(value)} · ${month.count} 张">
        <div class="dashboard-bar" aria-hidden="true" style="bottom:${Math.min(baseline,end)}%;height:${Math.abs(end-baseline)}%"></div>
        <span class="dashboard-bar-value ${value<0?'negative':''}" aria-hidden="true" style="${value<0?'top:'+String(100-end):'bottom:'+end}%">${cash(value)}</span>
        <span class="dashboard-month" aria-hidden="true">${esc(month.month)}</span>
      </li>`;
    }).join('')}</ul></div></div>
    <details class="dashboard-chart-details"><summary>查看每月金额</summary><dl>
      ${months.map(month=>`<div><dt>${esc(month.month)}</dt><dd>${cash(month.amount_cents)}</dd></div>`).join('')}
    </dl></details>`;
}

function dashboardPage(){
  const data=state.dashboard,summary=data.summary;
  const paid=summary.invoices-summary.unreimbursed;
  const progress=summary.invoices?100*paid/summary.invoices:0;
  const review=summary.failed_invoices+summary.pending_emails;
  const current=data.monthly[data.monthly.length-1];
  const library=(className,label)=>`<button class="${className}" type="button" data-go="invoices">${label} ${uiIcon('arrow-up-right')}</button>`;
  const tile=(label,value,detail='')=>`<div><dt>${label}</dt><dd>${value}${detail?`<p class="muted">${detail}</p>`:''}</dd></div>`;
  return header('首页','汇总发票金额、报销进度和近期导入。',library('button primary','查看发票库'))+
    `<div class="dashboard">
      <dl class="box dashboard-metrics" aria-label="发票概览">
        ${tile('发票金额',cash(summary.total_amount_cents))}
        ${tile('发票总数',summary.invoices+'<small> 张</small>')}
        ${tile('未报销金额',cash(summary.unreimbursed_amount_cents),'未报销 '+summary.unreimbursed+' 张')}
        ${tile('已报销金额',cash(summary.total_amount_cents-summary.unreimbursed_amount_cents),'已报销 '+paid+' 张')}
      </dl>
      <div class="dashboard-middle">
        <section class="box dashboard-trend" aria-labelledby="dashboard-trend-title">
          <div class="dashboard-heading"><h2 id="dashboard-trend-title">最近 6 个月开票金额</h2><span class="muted">按开票日期统计</span></div>
          ${dashboardChart(data.monthly)}
        </section>
        <div class="dashboard-side">
          <section class="box dashboard-progress" aria-labelledby="dashboard-progress-title">
            <h2 id="dashboard-progress-title">报销进度</h2>
            <p class="muted">已报销 ${paid} 张 / ${summary.invoices} 张</p>
            <progress aria-label="已报销发票占比" value="${progress}" max="100"></progress>
            <div class="dashboard-progress-labels"><span>已报销 ${paid} 张</span><span>未报销 ${summary.unreimbursed} 张</span></div>
          </section>
          <div class="dashboard-shortcuts">
            <button class="box dashboard-shortcut" type="button" data-go="failed"><span>待核对</span><strong>${review}<small> 项</small></strong><span class="muted">查看待处理发票和邮件</span>${uiIcon('arrow-up-right')}</button>
            <button class="box dashboard-shortcut" type="button" data-go="invoices" data-home-untagged><span>未加标签</span><strong>${summary.untagged_invoices}<small> 张</small></strong><span class="muted">查看未加标签的发票</span>${uiIcon('arrow-up-right')}</button>
          </div>
        </div>
      </div>
      <div class="dashboard-bottom">
        <section class="box dashboard-recent" aria-labelledby="dashboard-recent-title">
          <div class="dashboard-heading"><h2 id="dashboard-recent-title">最近导入</h2>${library('button text-button','查看全部')}</div>
          ${data.recent.length?`<table><thead><tr><th>商户 / 文件名</th><th>导入日期</th><th>金额</th><th>状态</th></tr></thead>
            <tbody>${data.recent.map(invoice=>`<tr><td><strong>${esc(invoice.seller||'未填写商户')}</strong><small>${esc(invoice.original_name)}</small></td><td>${date(invoice.imported_at)}</td><td>${cash(invoice.amount_cents)}</td><td><span class="status ${invoice.reimbursed?'done':''}"><span class="status-dot" aria-hidden="true"></span>${invoice.reimbursed?'已报销':'未报销'}</span></td></tr>`).join('')}</tbody></table>`:empty('还没有已识别的发票。')}
        </section>
        <section class="box dashboard-monthly" aria-labelledby="dashboard-monthly-title">
          <h2 id="dashboard-monthly-title">本月概况</h2>
          <dl><div><dt>本月发票</dt><dd>${current.count}<small> 张</small></dd></div>
          <div><dt>本月金额</dt><dd>${cash(current.amount_cents)}</dd></div>
          <div><dt>报销组</dt><dd><button class="button text-button" type="button" data-go="groups" aria-label="查看 ${data.group_count} 个报销组">${data.group_count}<small> 个</small>${uiIcon('arrow-up-right')}</button></dd></div></dl>
        </section>
      </div>
    </div>`;
}
