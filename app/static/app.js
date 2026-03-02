const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 120) + 'px';
}

function parseMdTable(text) {
  const lines = text.trim().split('\n');
  if (lines.length < 2 || !lines[0].trim().startsWith('|') || !lines[1].includes('---')) return null;
  const row = r => r.trim().replace(/^\||\|$/g,'').split('|').map(c => esc(c.trim()));
  const heads = row(lines[0]);
  const rows = lines.slice(2).map(row).filter(r => r.length > 1);
  return `<table class="md-table"><thead><tr>${heads.map(h=>`<th>${h}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${r.map(c=>`<td>${c}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
}

function renderJsonValue(val) {
  if (val === null || val === undefined) return '';
  if (typeof val === 'object') return `<pre class="json-pre">${esc(JSON.stringify(val, null, 2))}</pre>`;
  return esc(String(val));
}

function renderJsonCard(obj) {
  if (Array.isArray(obj)) {
    if (!obj.length) return `<div class="json-card">Empty list</div>`;
    const first = obj[0];
    if (first && typeof first === 'object' && !Array.isArray(first)) {
      const cols = Object.keys(first).slice(0, 8);
      const rows = obj.slice(0, 25);
      const head = cols.map(c => `<th>${esc(c)}</th>`).join('');
      const body = rows.map(r => `<tr>${cols.map(c => `<td>${renderJsonValue(r[c])}</td>`).join('')}</tr>`).join('');
      return `<div class="json-card"><table class="md-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
    }
    const items = obj.map(v => `<li>${renderJsonValue(v)}</li>`).join('');
    return `<div class="json-card"><ul class="json-list">${items}</ul></div>`;
  }
  const entries = Object.entries(obj || {});
  if (!entries.length) return `<div class="json-card">Empty object</div>`;
  return `<div class="json-card"><div class="json-grid">${entries.map(([k,v]) => (
    `<div class="json-key">${esc(k)}</div><div class="json-val">${renderJsonValue(v)}</div>`
  )).join('')}</div></div>`;
}

function renderMd(text) {
  if (!text) return '';
  try {
    const parsed = JSON.parse(text);
    return renderJsonCard(parsed);
  } catch { }
  const tbl = parseMdTable(text);
  if (tbl) return tbl;
  return `<div style="font-size:13px;line-height:1.65;">${esc(text).replace(/\n/g,'<br/>')}</div>`;
}

function timeAgo(iso) {
  const d = new Date(iso), now = new Date(), diff = (now-d)/1000;
  if (diff < 60) return 'Just now';
  if (diff < 3600) return Math.floor(diff/60)+'m ago';
  if (diff < 86400) return Math.floor(diff/3600)+'h ago';
  return d.toLocaleDateString();
}

const S = { tab:'proposal', proposal:{chatId:null}, websearch:{chatId:null}, hubspot:{chatId:null}, proposalFile:null };
const MODULE_LABELS = {proposal:'Proposal', websearch:'Web Search', hubspot:'HubSpot'};

// ── Tab switch ──
document.querySelectorAll('.sidebar-nav-btn[data-tab]').forEach(btn => {
  btn.addEventListener('click', () => {
    const tab = btn.dataset.tab;
    S.tab = tab;
    document.querySelectorAll('.sidebar-nav-btn[data-tab]').forEach(b => b.classList.toggle('active', b.dataset.tab===tab));
    document.querySelectorAll('.mini-rail-btn[data-tab]').forEach(b => b.classList.toggle('active', b.dataset.tab===tab));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id===`panel-${tab}`));
    $('hist-module-label').textContent=MODULE_LABELS[tab];
    refreshHistory();
    if (tab==='hubspot') { loadHubspotStatus(); }
  });
});

// ── Sidebar collapse/open ──
const leftSidebar = $('left-sidebar');
const miniRail = $('mini-rail');
const btnCollapse = $('btn-collapse-sidebar');
const btnOpen = $('btn-open-sidebar');

function collapseSidebar() {
  leftSidebar.style.display = 'none';
  miniRail.style.display = 'flex';
}
function expandSidebar() {
  leftSidebar.style.display = '';
  miniRail.style.display = 'none';
}

btnCollapse && btnCollapse.addEventListener('click', collapseSidebar);
btnOpen && btnOpen.addEventListener('click', expandSidebar);

// Mini rail new chat button
const miniNewChat = $('mini-new-chat');
miniNewChat && miniNewChat.addEventListener('click', () => {
  $('btn-new-chat').click();
});

// Mini rail tab buttons sync
document.querySelectorAll('.mini-rail-btn[data-tab]').forEach(btn => {
  btn.addEventListener('click', () => {
    const tab = btn.dataset.tab;
    // trigger tab switch using sidebar nav logic
    const sidebarBtn = document.querySelector(`.sidebar-nav-btn[data-tab="${tab}"]`);
    if (sidebarBtn) sidebarBtn.click();
    // update mini rail active
    document.querySelectorAll('.mini-rail-btn[data-tab]').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  });
});

// ── Bubbles ──
function hideWelcome(tab) { const w=$(`${tab}-welcome`); if(w) w.style.display='none'; }

function appendBubble(logId, role, content, fileName, isHtml=false) {
  const log = $(logId);
  const row = document.createElement('div');
  row.className = `msg-row ${role==='user'?'user':''}`;

  const av = document.createElement('div');
  av.className = `msg-avatar ${role==='user'?'uav':'ai'}`;
  av.innerHTML = role==='user'
    ? `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2"><path d="M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>`
    : `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#1E1E1E" stroke-width="2"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg>`;

  const bub = document.createElement('div');
  bub.className = `msg-bubble ${role==='user'?'user':'ai'}`;

  let inner = '';
  if (fileName && role==='user') inner += `<div class="file-chip-user"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14,2 14,8 20,8"/></svg>${esc(fileName)}</div><br/>`;
  if (fileName && role==='ai') inner += `<div class="file-chip-ai"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14,2 14,8 20,8"/></svg>Analysing: ${esc(fileName)}</div><br/>`;

  if (isHtml) bub.innerHTML = inner + content;
  else { if(inner) bub.innerHTML=inner; bub.appendChild(document.createTextNode(content)); }

  row.appendChild(av); row.appendChild(bub);
  log.appendChild(row);
  log.scrollTop = log.scrollHeight;
  return bub;
}

function appendTyping(logId) {
  const log=$(logId), row=document.createElement('div');
  row.className='msg-row'; row.id=`typing-${logId}`;
  const av=document.createElement('div'); av.className='msg-avatar ai';
  av.innerHTML=`<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#1E1E1E" stroke-width="2"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg>`;
  const bub=document.createElement('div'); bub.className='typing-bub';
  bub.innerHTML=`<span class="dot"></span><span class="dot"></span><span class="dot"></span>`;
  row.appendChild(av); row.appendChild(bub);
  log.appendChild(row); log.scrollTop=log.scrollHeight;
}
function removeTyping(logId){const e=$(`typing-${logId}`);if(e)e.remove();}

// ── History ──
async function refreshHistory() {
  const tab=S.tab;
  const prefix=tab==='proposal'?'/api/proposal':(tab==='websearch'?'/api/websearch':'/api/hubspot');
  const list=$('hist-list'), cntEl=$('hist-count'), emptyEl=$('hist-empty');
  try {
    const r=await fetch(`${prefix}/chats`), d=await r.json();
    const chats=(d.chats||[]).slice().reverse();
    cntEl.textContent=chats.length;
    list.querySelectorAll('.hist-item').forEach(el=>el.remove());
    if(!chats.length){emptyEl.style.display='flex';return;}
    emptyEl.style.display='none';
    chats.forEach(chat=>{
      const item=document.createElement('div');
      const active=chat.id===S[tab].chatId;
      item.className=`hist-item${active?' active':''}`;
      const iconColor=active?'#1E1E1E':'#9a9a9a';
      let iconSvg='';
      if (tab==='proposal') {
        iconSvg=`<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="${iconColor}" stroke-width="2"><path d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>`;
      } else if (tab==='websearch') {
        iconSvg=`<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="${iconColor}" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>`;
      } else {
        iconSvg=`<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="${iconColor}" stroke-width="2"><path d="M3 7h18M5 7v10a2 2 0 002 2h10a2 2 0 002-2V7"/><path d="M7 7V5a2 2 0 012-2h6a2 2 0 012 2v2"/></svg>`;
      }
      item.innerHTML=`<div class="hist-item-icon">${iconSvg}</div><div style="min-width:0;flex:1;"><div class="hist-item-title">${esc(chat.title||'New Chat')}</div><div class="hist-item-time">${timeAgo(chat.created_at)}</div></div>`;
      item.onclick=()=>loadChat(tab,chat.id);
      list.appendChild(item);
    });
  } catch { emptyEl.style.display='flex'; }
}

async function loadChat(tab, chatId) {
  const prefix=tab==='proposal'?'/api/proposal':(tab==='websearch'?'/api/websearch':'/api/hubspot');
  const logId=tab==='proposal'?'proposal-log':(tab==='websearch'?'websearch-log':'hubspot-log');
  try {
    const r=await fetch(`${prefix}/chats/${chatId}`), d=await r.json();
    S[tab].chatId=chatId;
    $(logId).querySelectorAll('.msg-row').forEach(el=>el.remove());
    if(tab==='proposal'){
      $('proposal-chat-title').textContent=d.chat?.title||'Proposal Evaluator';
      if(d.chat?.last_upload_name){$('prop-file-badge').style.display='inline-flex';$('prop-file-badge-name').textContent=d.chat.last_upload_name;}
    }
    const msgs=d.messages||[];
    if(msgs.length) hideWelcome(tab);
    msgs.forEach(m=>{
      const isAi=m.role==='assistant';
      if(tab==='hubspot' && isAi){
        let parsed=null;
        try{parsed=JSON.parse(m.content);}catch{}
        if(parsed){
          if(parsed.error){appendBubble(logId,'ai',`Error: ${parsed.error}`);return;}
          appendBubble(logId,'ai',renderHubspotResponse(parsed),null,true);return;
        }
      }
      appendBubble(logId,isAi?'ai':'user',isAi?renderMd(m.content):m.content,null,isAi);
    });
    refreshHistory();
  } catch(e){console.error(e);}
}

async function createChat(tab) {
  const prefix=tab==='proposal'?'/api/proposal':(tab==='websearch'?'/api/websearch':'/api/hubspot');
  try {
    const r=await fetch(`${prefix}/chats`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:'New Chat'})});
    const d=await r.json(); S[tab].chatId=d.id; await refreshHistory(); return d.id;
  } catch{return null;}
}

// ── New chat ──
$('btn-new-chat').onclick = async()=>{
  const tab=S.tab;
  S[tab].chatId=null;
  if(tab==='proposal'){
    $('proposal-log').querySelectorAll('.msg-row').forEach(el=>el.remove());
    $('proposal-welcome').style.display='flex';
    $('proposal-chat-title').textContent='Proposal Evaluator';
    $('prop-file-badge').style.display='none';
    clearAttach();
  } else if(tab==='websearch') {
    $('websearch-log').querySelectorAll('.msg-row').forEach(el=>el.remove());
    $('websearch-welcome').style.display='flex';
  } else {
    $('hubspot-log').querySelectorAll('.msg-row').forEach(el=>el.remove());
    $('hubspot-welcome').style.display='flex';
  }
  await createChat(tab);
};

// ── Reset files ──
$('btn-reset-files').onclick = async()=>{
  const tab=S.tab; if(tab==='hubspot') return;
  if(!confirm('Reset all uploaded files?')) return;
  try{
    const r=await fetch('/api/proposal/reset-files',{method:'POST'}), d=await r.json();
    appendBubble('proposal-log','ai',renderMd(d.message||'Files reset.'),null,true);
    $('prop-file-badge').style.display='none'; clearAttach();
  }catch(e){console.error(e);}
};

// ── Clear all ──
$('btn-clear-all').onclick = async()=>{
  const tab=S.tab;
  if(!confirm('Delete all chats?')) return;
  const prefix=tab==='proposal'?'/api/proposal':(tab==='websearch'?'/api/websearch':'/api/hubspot');
  try{const r=await fetch(`${prefix}/chats`),d=await r.json();await Promise.all((d.chats||[]).map(c=>fetch(`${prefix}/chats/${c.id}`,{method:'DELETE'})));}catch{}
  S[tab].chatId=null;
  const logId=tab==='proposal'?'proposal-log':(tab==='websearch'?'websearch-log':'hubspot-log');
  $(logId).querySelectorAll('.msg-row').forEach(el=>el.remove());
  $(`${tab}-welcome`).style.display='flex';
  refreshHistory();
};

// ── File attachment ──
const fileInputHidden = $('file-input-hidden');
const propPill=$('prop-pill'), propPillName=$('prop-pill-name'), propRm=$('prop-rm-file'), propAttachBtn=$('prop-attach-btn');
const propFileBadge=$('prop-file-badge'), propFileBadgeName=$('prop-file-badge-name');

propAttachBtn.addEventListener('click', ()=>fileInputHidden.click());
fileInputHidden.addEventListener('change', e=>{
  const f=e.target.files[0]; if(!f) return;
  S.proposalFile=f;
  propPillName.textContent=f.name;
  propPill.classList.add('show');
  propAttachBtn.classList.add('has-file');
});
propRm.addEventListener('click', clearAttach);
function clearAttach(){
  S.proposalFile=null; fileInputHidden.value='';
  propPill.classList.remove('show'); propAttachBtn.classList.remove('has-file');
}

// ── Auto-resize ──
['proposal-input','websearch-input','hubspot-input'].forEach(id=>{
  const el=$(id); if(!el) return;
  el.addEventListener('input',()=>autoResize(el));
  el.addEventListener('keydown',e=>{
    if(e.key==='Enter'&&!e.shiftKey){
      e.preventDefault();
      if(id==='proposal-input') sendProposal();
      else if(id==='websearch-input') sendWebSearch();
      else sendHubspot();
    }
  });
});

function fillInput(tab, text){ const el=$(`${tab}-input`); if(!el) return; el.value=text; autoResize(el); el.focus(); }

// ── Send Proposal ──
async function sendProposal(){
  const inp=$('proposal-input'), txt=inp.value.trim(), file=S.proposalFile;
  if(!txt&&!file) return;
  if(!S.proposal.chatId) await createChat('proposal');
  const display=txt||'evaluate';
  hideWelcome('proposal');
  appendBubble('proposal-log','user',display,file?file.name:null);
  appendTyping('proposal-log');
  inp.value=''; autoResize(inp);
  $('proposal-status').textContent='Processing…';
  const attachedFileName = file ? file.name : null;
  clearAttach();
  const fd=new FormData(); fd.append('input_text',display); if(file) fd.append('file',file);
  try{
    const r=await fetch(`/api/proposal/chats/${S.proposal.chatId}/messages`,{method:'POST',body:fd}), d=await r.json();
    removeTyping('proposal-log');
    if(d.output_text) appendBubble('proposal-log','ai',renderMd(d.output_text),null,true);
    if(d.upload_status?.filename){propFileBadge.style.display='inline-flex';propFileBadgeName.textContent=d.upload_status.filename;}
    if(d.chat?.title) $('proposal-chat-title').textContent=d.chat.title;
    $('proposal-status').textContent='Ready · Enter to send';
    refreshHistory();
  }catch(e){removeTyping('proposal-log');appendBubble('proposal-log','ai','Error: '+e.message);$('proposal-status').textContent='Error';}
}
$('proposal-send').onclick=sendProposal;

// ── Send Web Search ──
async function sendWebSearch(){
  const inp=$('websearch-input'), txt=inp.value.trim();
  if(!txt) return;
  if(!S.websearch.chatId) await createChat('websearch');
  hideWelcome('websearch');
  appendBubble('websearch-log','user',txt);
  appendTyping('websearch-log');
  inp.value=''; autoResize(inp);
  $('websearch-status').textContent='Searching the web…';
  try{
    const r=await fetch(`/api/websearch/chats/${S.websearch.chatId}/messages`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({input_text:txt})}), d=await r.json();
    removeTyping('websearch-log');
    if(d.output_text) appendBubble('websearch-log','ai',renderMd(d.output_text),null,true);
    $('websearch-status').textContent='Ready · Enter to send';
    refreshHistory();
  }catch(e){removeTyping('websearch-log');appendBubble('websearch-log','ai','Error: '+e.message);$('websearch-status').textContent='Error';}
}
$('websearch-send').onclick=sendWebSearch;

// ── HubSpot status ──
async function loadHubspotStatus() {
  const badge = $('hubspot-status-badge');
  if (!badge) return;
  badge.textContent = 'Checking…';
  try {
    const r = await fetch('/api/hubspot/status');
    const d = await r.json();
    if (d.connected) {
      badge.textContent = 'Connected';
      badge.classList.remove('badge-soon');
      badge.classList.add('badge-file');
    } else {
      badge.textContent = 'Not Connected';
      badge.classList.add('badge-soon');
      badge.classList.remove('badge-file');
    }
  } catch {
    badge.textContent = 'Status Error';
    badge.classList.add('badge-soon');
    badge.classList.remove('badge-file');
  }
}

function renderHubspotResponse(data) {
  if (!data) return '';

  if (typeof data.count === 'number') {
    const criteria = Object.keys(data.search_criteria || {}).length
      ? `Filters: ${esc(JSON.stringify(data.search_criteria))}`
      : 'No filters applied';
    return (
      `<div class="hs-result">
        <div class="hs-result-title">Count Result</div>
        <div class="hs-result-meta">${esc(data.object_type || 'records')} · ${data.count} total</div>
        <div class="hs-hint">${criteria}</div>
      </div>`
    );
  }

  const results = data.results || [];
  if (!results.length) {
    return `<div class="hs-result"><div class="hs-result-title">No records found.</div></div>`;
  }

  const blocks = results.map(item => {
    const props = Object.assign({}, item.properties || {});
    if (item.company_name) props.company_name = item.company_name;
    const propsHtml = Object.keys(props).length
      ? `<div class="hs-props">${Object.entries(props).map(([k,v]) => (
          `<div class="hs-prop-key">${esc(k)}</div><div class="hs-prop-val">${esc(v)}</div>`
        )).join('')}</div>`
      : `<div class="hs-hint">No properties returned.</div>`;

    return (
      `<div class="hs-result">
        <div class="hs-result-title">${esc(item.name || 'Record')}</div>
        ${item.id ? `<div class="hs-result-meta">ID ${esc(item.id)}</div>` : ''}
        ${propsHtml}
      </div>`
    );
  }).join('');

  const more = data.has_more ? `<div class="hs-hint">More results available. Type "next" to continue.</div>` : '';
  return `<div class="hs-results">${blocks}${more}</div>`;
}

// ── Send HubSpot query ──
async function sendHubspot(){
  const inp=$('hubspot-input'), txt=inp.value.trim();
  if(!txt) return;
  if(!S.hubspot.chatId) await createChat('hubspot');
  hideWelcome('hubspot');
  appendBubble('hubspot-log','user',txt);
  appendTyping('hubspot-log');
  inp.value=''; autoResize(inp);
  $('hubspot-status').textContent='Querying HubSpot…';
  try{
    const r=await fetch(`/api/hubspot/chats/${S.hubspot.chatId}/messages`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:txt})});
    const d=await r.json();
    removeTyping('hubspot-log');
    if(d.error || d.response?.error){
      appendBubble('hubspot-log','ai',`Error: ${d.error || d.response.error}`);
    } else {
      appendBubble('hubspot-log','ai',renderHubspotResponse(d.response||d),null,true);
    }
    $('hubspot-status').textContent='Ready · Enter to send';
    refreshHistory();
  }catch(e){
    removeTyping('hubspot-log');
    appendBubble('hubspot-log','ai','Error: '+e.message);
    $('hubspot-status').textContent='Error';
  }
}
$('hubspot-send').onclick=sendHubspot;

// ── History search ──
$('hist-search-inp').addEventListener('input',function(){
  const q=this.value.toLowerCase();
  $('hist-list').querySelectorAll('.hist-item').forEach(item=>{
    const t=item.querySelector('.hist-item-title')?.textContent.toLowerCase()||'';
    item.style.display=t.includes(q)?'':'none';
  });
});

// ── Init ──
refreshHistory();
loadHubspotStatus();