'use strict';
const $ = selector => document.querySelector(selector);
const state = { institution: 'bank', verdict: 'ALL', search: '', page: 0, size: 12, cases: [], summary: null, detail: null, request: 0, caseRequest: 0 };
const labels = { CLOSE: 'Close', RFI: 'Request information', ESCALATE: 'Escalate', PENDING: 'Pending' };
const descriptions = { bank: 'Current accounts · individuals and small businesses', emoney: 'App-based accounts · top-ups and transfers', investment: 'Broker and fund platform · individual investors', payments_fx: 'Business payments · suppliers, staff and foreign exchange' };
const icons = { bank: '▥', emoney: '▣', investment: '▧', payments_fx: '⇄' };
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const gbp = value => new Intl.NumberFormat('en-GB', { style: 'currency', currency: 'GBP' }).format(value || 0);
const dateLabel = value => new Date(value.slice(0,10)+'T12:00:00').toLocaleDateString('en-GB', {day:'numeric',month:'short',year:'numeric'});
const badge = value => `<span class="badge ${esc(value)}">${esc(labels[value] || value)}</span>`;
const initials = name => name.split(/\s+/).filter(Boolean).slice(0,2).map(s=>s[0]).join('').toUpperCase();

async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : { method:'POST', headers:{'Content-Type':'application/json','X-Alert-Review':'local'}, body:JSON.stringify(body) });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || 'The request could not be completed.');
  return value;
}
function notice(message, error=false) {
  $('#notice').textContent=message; $('#notice').className='notice'+(error?' error':''); $('#notice').hidden=!message;
}
async function refreshSummary() {
  state.summary=await api('/api/summary');
  $('#institutions').innerHTML=state.summary.institutions.map(i=>`<button class="institution ${i.id===state.institution?'active':''}" data-institution="${i.id}" aria-label="${esc(i.label)}" aria-current="${i.id===state.institution?'page':'false'}"><span class="nav-icon">${icons[i.id]}</span><span class="nav-text">${esc(i.label)}</span><span class="nav-count">${i.cases.toLocaleString()}</span></button>`).join('');
  const selected=state.summary.institutions.find(i=>i.id===state.institution);
  $('#total').textContent=selected.cases.toLocaleString();
  $('#total-note').textContent='In '+selected.label.toLowerCase();
  for (const [key,verdict] of [['close','CLOSE'],['rfi','RFI'],['escalate','ESCALATE']]) $('#'+key).textContent=(selected.counts[verdict]||0).toLocaleString();
  $('#queue-title').textContent=selected.label+' case queue';
  $('#institution-description').textContent=descriptions[selected.id];
  $('#export').href='/api/export?institution='+state.institution;
  $('#runtime').textContent=selected.metrics.seconds ? `${selected.metrics.mode === 'hybrid' ? 'Hybrid' : selected.metrics.mode === 'advisory' ? 'AI advisory + policy' : 'Local policy'} · ${selected.metrics.seconds.toFixed(1)}s / ${selected.cases.toLocaleString()} cases` : 'Local policy engine';
}
async function loadCases(institution=state.institution) {
  state.institution=institution; state.page=0; const request=++state.request;
  $('#cases').innerHTML='<tr><td colspan="6" class="empty">Loading the case queue…</td></tr>';
  await refreshSummary();
  const data=await api('/api/cases?institution='+encodeURIComponent(institution));
  if(request!==state.request) return;
  state.cases=data.cases; renderCases();
}
function filteredCases() {
  return state.cases.filter(c=>(state.verdict==='ALL'||c.verdict===state.verdict)&&`${c.customer} ${c.case_id} ${c.rules.join(' ')} ${c.reason_code}`.toLowerCase().includes(state.search));
}
function renderCases() {
  const rows=filteredCases(), pages=Math.max(1,Math.ceil(rows.length/state.size)); state.page=Math.min(state.page,pages-1);
  const shown=rows.slice(state.page*state.size,(state.page+1)*state.size);
  $('#all-count').textContent=state.cases.length.toLocaleString();
  document.querySelectorAll('[data-verdict]').forEach(b=>{b.classList.toggle('active',b.dataset.verdict===state.verdict);b.setAttribute('aria-pressed',String(b.dataset.verdict===state.verdict));});
  $('#cases').innerHTML=shown.length?shown.map(c=>`<tr class="case-row" tabindex="0" data-case="${esc(c.case_id)}" aria-label="Review ${esc(c.customer)}, ${esc(labels[c.verdict])}"><td><div class="customer-cell"><span class="avatar">${esc(initials(c.customer))}</span><div><div class="customer-name">${esc(c.customer)}</div><div class="case-id">${esc(c.case_id)}</div></div></div></td><td>${c.rules.map(r=>`<span class="rule-chip">${esc(r)}</span>`).join('')}</td><td>${badge(c.verdict)}</td><td><span class="evidence-count">▤ ${c.evidence_count} records</span></td><td>${dateLabel(c.date)}</td><td class="row-open">›</td></tr>`).join(''):'<tr><td colspan="6" class="empty">No cases match these filters. Try a different name, rule or decision.</td></tr>';
  $('#range').textContent=rows.length?`Showing ${(state.page*state.size+1).toLocaleString()}–${Math.min((state.page+1)*state.size,rows.length).toLocaleString()} of ${rows.length.toLocaleString()} cases`:'0 matching cases';
  $('#page').textContent=`${state.page+1} / ${pages}`; $('#previous').disabled=state.page===0; $('#next').disabled=state.page===pages-1;
}
function chips(evidence) {return evidence.map(e=>`<button class="evidence-chip" data-evidence="${esc(e)}">${esc(e)}</button>`).join('');}
function profileField(title,value) {return `<div class="profile-field"><div>${esc(title)}</div><div>${esc(value||'Not declared')}</div></div>`;}
async function openCase(key) {
  const request=++state.caseRequest; const institution=state.institution;
  $('#detail').innerHTML='<div class="loading">Gathering the evidence…</div>';
  if(!$('#detail-dialog').open) $('#detail-dialog').showModal();
  try {
    const d=await api(`/api/case?institution=${encodeURIComponent(institution)}&id=${encodeURIComponent(key)}`);
    if(request!==state.caseRequest) return;
    state.detail=d;state.detail.institution=institution; const result=d.result, customer=d.customer, declared=customer.declared;
    const cited=new Set(result.evidence); const alerted=new Set(d.alerts.flatMap(a=>a.transaction_ids));
    const transactions=d.transactions.filter(t=>alerted.has(t.transaction_id)||cited.has(t.transaction_id));
    $('#detail').innerHTML=`<div class="modal-header"><div><span class="eyebrow">CASE REVIEW · ${esc(d.mode.toUpperCase())}</span><h2>${esc(customer.name)}</h2><p>${esc(d.case.case_id)} · ${dateLabel(d.case.review_window.from)} – ${dateLabel(d.case.review_window.to)}</p></div><button class="icon-button" data-close-detail aria-label="Close case">×</button></div>
    <div class="detail-grid"><div class="detail-main"><div class="decision-box ${esc(result.verdict)}"><div class="decision-top">${badge(result.verdict)}<span class="reason-code">${esc(result.reason_code)}</span></div><p class="reason-text">${esc(result.reason)}</p></div>
    <h3 class="section-title">Every alert, accounted for</h3>${d.checks.map(f=>{const alert=d.alerts.find(a=>a.alert_id===f.check);return `<article class="check-card"><div class="check-header"><span>${esc(alert?alert.rule_id+' · '+alert.rule_name:f.check.replaceAll('_',' '))}</span>${badge(f.verdict)}</div><p>${esc(f.detail)}</p><div class="evidence-chips">${chips(f.evidence)}</div></article>`;}).join('')}
    <div class="transaction-section"><h3 class="section-title">Transactions relied on <span class="subtle">(${transactions.length})</span></h3><div class="table-scroll"><table class="transaction-table"><thead><tr><th>Date / ID</th><th>Counterparty / purpose</th><th>Amount</th></tr></thead><tbody>${transactions.map(t=>`<tr><td>${dateLabel(t.booked_at)}<div class="case-id">${esc(t.transaction_id)}</div></td><td>${esc(t.counterparty_name||t.channel.replaceAll('_',' '))}<div class="subtle">${esc(t.reference||'')} · ${esc(t.counterparty_country||'')}</div></td><td class="amount-${esc(t.direction)}">${t.direction==='in'?'+':'−'}${gbp(t.amount_gbp)}</td></tr>`).join('')}</tbody></table></div></div>
    ${d.previous_reviews.length?`<div class="transaction-section"><h3 class="section-title">Earlier reviews</h3>${d.previous_reviews.map(r=>`<p class="subtle">${dateLabel(r.reviewed_on)} · ${esc(r.rule_id)} · ${esc(r.note)}</p>`).join('')}<p class="subtle">Earlier reviews establish historical context; they do not clear new or larger activity.</p></div>`:''}</div>
    <aside class="detail-aside"><h3 class="section-title">Customer profile</h3>${profileField('Customer type',customer.kind)}${profileField('Declared source of funds',declared.source_of_funds)}${profileField('Account purpose',declared.account_purpose)}${profileField('Monthly income / annual volume',declared.monthly_income_gbp?gbp(declared.monthly_income_gbp)+' / month':declared.annual_turnover_gbp?gbp(declared.annual_turnover_gbp)+' / year':'Not declared')}${profileField('Expected countries',(declared.expected_countries||[]).join(', '))}
    <h3 class="section-title">Documents on file <span class="subtle">(${d.documents.length})</span></h3><p class="subtle">Outlined documents are cited in the decision.</p>${d.documents.map(doc=>`<button class="doc-item ${cited.has(doc.doc_id)?'cited':''}" data-evidence="${esc(doc.doc_id)}"><div>${esc(doc.doc_id)}</div><span>${esc(doc.title)}</span></button>`).join('')}
    ${d.internal_list.length?`<h3 class="section-title">Internal-list entries</h3>${d.internal_list.map(e=>`<button class="doc-item cited" data-evidence="${esc(e.entry_id)}"><div>${esc(e.entry_id)}</div><span>${esc(e.name)}</span></button>`).join('')}`:''}
    <div class="ai-review"><h3 class="section-title">AI second opinion</h3><p class="subtle">Review the same evidence with AI via OpenRouter. This second opinion does not change the saved decision.</p><button id="ai-review" class="secondary" ${state.summary.model_available?'':'disabled'}>${state.summary.model_available?'Review with AI':'OpenRouter key not configured'}</button><div id="ai-result" class="ai-result" role="status"></div></div></aside></div>`;
  } catch(error) {$('#detail').innerHTML=`<div class="modal-header"><h2>Case unavailable</h2><button class="icon-button" data-close-detail aria-label="Close case">×</button></div><p class="loading">${esc(error.message)}</p>`;}
}
function openEvidence(id) {
  const d=state.detail; const doc=d.documents.find(x=>x.doc_id===id), entry=d.internal_list.find(x=>x.entry_id===id), transaction=d.transactions.find(x=>x.transaction_id===id);
  if(doc){$('#document-title').textContent=doc.doc_id+' · '+doc.title;$('#document-content').textContent=doc.content;}
  else if(entry){$('#document-title').textContent=id+' · Internal list';$('#document-content').textContent=JSON.stringify(entry,null,2);}
  else if(transaction){$('#document-title').textContent=id+' · Transaction';$('#document-content').textContent=JSON.stringify(transaction,null,2);}
  else return;
  $('#document-dialog').showModal();
}
async function pollJob() {
  const job=await api('/api/job'); const button=$('#run');
  if(job.state==='running'){button.disabled=true;button.textContent=`Reviewing ${job.institution.replaceAll('_',' ')}…`;notice(`Review in progress · ${job.completed} of ${job.total} institutions complete.`);setTimeout(()=>pollJob().catch(e=>notice(e.message,true)),1500);}
  else{button.disabled=false;button.innerHTML='<span class="play-icon">▷</span> Run all cases';if(job.state==='complete'){notice('All four institutions reviewed. The case queue and JSONL exports are up to date.');await loadCases();}if(job.state==='failed')notice(job.message,true);}
}
document.addEventListener('click',async event=>{
  const b=event.target.closest('button');
  try{
    if(b?.dataset.institution) await loadCases(b.dataset.institution);
    else if(b?.dataset.verdict){state.verdict=b.dataset.verdict;state.page=0;renderCases();}
    else if(b?.dataset.filter){state.verdict=b.dataset.filter;state.page=0;renderCases();}
    else if(b?.dataset.evidence)openEvidence(b.dataset.evidence);
    else if(b?.hasAttribute('data-close-detail')){$('#detail-dialog').close();++state.caseRequest;}
    else if(b?.id==='previous'){state.page--;renderCases();}
    else if(b?.id==='next'){state.page++;renderCases();}
    else if(b?.id==='run'){b.disabled=true;await api('/api/run',{});await pollJob();}
    else if(b?.id==='close-document')$('#document-dialog').close();
    else if(b?.id==='close-policy')$('#policy-dialog').close();
    else if(b?.id==='policy'){const data=await api('/api/policy?institution='+state.institution);$('#policy-title').textContent=state.summary.institutions.find(i=>i.id===state.institution).label+' policy';$('#policy-content').textContent=data.policy;$('#policy-dialog').showModal();}
    else if(b?.id==='ai-review'){b.disabled=true;b.textContent='Reading the evidence…';const review=await api('/api/review',{institution:state.detail.institution,case_id:state.detail.case.case_id});$('#ai-result').textContent=review.status==='reviewed'?review.findings.map(f=>`${f.verdict} · ${f.reason_code}\n${f.detail}`).join('\n\n'):review.message;b.disabled=false;b.textContent='Review with AI';}
    else{const row=event.target.closest('[data-case]');if(row)await openCase(row.dataset.case);}
  }catch(error){notice(error.message,true);if(b){b.disabled=false;if(b.id==='ai-review')b.textContent='Review with AI';}}
});
$('#search').addEventListener('input',event=>{state.search=event.target.value.trim().toLowerCase();state.page=0;renderCases();});
document.addEventListener('keydown',event=>{if(event.key==='/'&&!['INPUT','TEXTAREA'].includes(event.target.tagName)&&!document.querySelector('dialog[open]')){event.preventDefault();$('#search').focus();}const row=event.target.closest('[data-case]');if(row&&(event.key==='Enter'||event.key===' ')){event.preventDefault();openCase(row.dataset.case);}});
for(const dialog of document.querySelectorAll('dialog'))dialog.addEventListener('click',event=>{if(event.target===dialog){const rect=dialog.getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)dialog.close();}});
loadCases().catch(error=>notice(error.message,true));
