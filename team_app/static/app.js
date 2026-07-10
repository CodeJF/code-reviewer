const state = { me: null, incidents: [], diagnoses: [], activeIncident: null };
const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value = '') => String(value).replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));

async function request(path, options = {}) {
  const response = await fetch(path, { headers: { 'Content-Type': 'application/json', ...(options.headers || {}) }, ...options });
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `请求失败 (${response.status})`);
  return response.status === 204 ? null : response.json();
}

function readableTime(value) { return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'; }
function canOperate() { return ['admin', 'oncall'].includes(state.me?.role); }

function renderIdentity() {
  $('#identity').innerHTML = state.me
    ? `<span>${escapeHtml(state.me.display_name || state.me.email)} · <b>${state.me.role.toUpperCase()}</b></span> <button class="secondary" id="logout">退出</button>`
    : '<a href="/api/auth/login">登录工作台 ↗</a>';
  $('#logout')?.addEventListener('click', async () => { await request('/api/auth/logout', { method: 'POST' }); window.location.reload(); });
}

function renderIncidents() {
  const list = $('#incident-list');
  $('#incident-count').textContent = state.incidents.filter((item) => item.status !== 'resolved').length;
  if (!state.incidents.length) { list.innerHTML = '<div class="empty-state">目前没有团队事件。完成诊断后，由值班成员人工升级为 Incident。</div>'; return; }
  list.innerHTML = state.incidents.map((item) => `<button class="incident-row" data-incident="${item.id}">
    <span class="severity ${item.risk_level}">${escapeHtml(item.risk_level || 'unknown').toUpperCase()}</span>
    <span><span class="incident-title">${escapeHtml(item.title)}</span><span class="incident-meta">${escapeHtml(item.service || '未识别服务')} · 更新于 ${readableTime(item.updated_at)}</span></span>
    <span class="status ${item.status}">${escapeHtml(item.status)}</span><span class="row-arrow">→</span></button>`).join('');
  document.querySelectorAll('[data-incident]').forEach((button) => button.addEventListener('click', () => openIncident(button.dataset.incident)));
}

function renderDiagnoses() {
  const list = $('#diagnosis-list');
  if (!state.diagnoses.length) { list.innerHTML = '<div class="empty-state">还没有诊断记录。</div>'; return; }
  list.innerHTML = state.diagnoses.map((job) => `<button class="diagnosis-row" data-diagnosis="${job.id}">
    <span class="severity ${job.status === 'failed' ? 'high' : ''}">${escapeHtml(job.status).toUpperCase()}</span>
    <span><span class="incident-title">${escapeHtml(job.query)}</span><span class="diagnosis-meta">${readableTime(job.created_at)} · 证据留存至 ${readableTime(job.expires_at)}</span></span>
    <span class="status ${job.status}">${job.no_remote ? 'ES ONLY' : 'ES + FALLBACK'}</span><span class="row-arrow">→</span></button>`).join('');
  document.querySelectorAll('[data-diagnosis]').forEach((button) => button.addEventListener('click', () => openDiagnosis(button.dataset.diagnosis)));
}

async function refresh() {
  [state.incidents, state.diagnoses] = await Promise.all([
    request('/api/incidents').then((data) => data.items), request('/api/diagnoses').then((data) => data.items),
  ]);
  renderIncidents(); renderDiagnoses();
}

function showDialog(html) { $('#incident-detail').innerHTML = html; $('#incident-dialog').showModal(); }
function reportBlock(report) { return report ? `<pre class="report">${escapeHtml(JSON.stringify(report, null, 2))}</pre>` : '<div class="empty-state">诊断报告已按留存策略清除，或尚未完成。</div>'; }

async function openDiagnosis(id) {
  const { job } = await request(`/api/diagnoses/${id}`);
  const promote = canOperate() && job.status === 'completed' ? `<button class="primary" id="promote">升级为 Incident <b>→</b></button>` : '';
  showDialog(`<article class="detail"><p class="eyebrow">DIAGNOSIS / ${escapeHtml(job.status)}</p><h2>${escapeHtml(job.query)}</h2><div class="detail-grid"><div><small>状态</small>${escapeHtml(job.status)}</div><div><small>发起时间</small>${readableTime(job.created_at)}</div><div><small>数据留存</small>${readableTime(job.expires_at)}</div></div>${job.error ? `<div class="empty-state">${escapeHtml(job.error)}</div>` : ''}<div class="detail-actions">${promote}</div><h3>脱敏报告</h3>${reportBlock(job.report)}</article>`);
  $('#promote')?.addEventListener('click', async () => {
    const title = window.prompt('事件标题', job.query); if (title === null) return;
    const data = await request('/api/incidents', { method: 'POST', body: JSON.stringify({ diagnosis_id: job.id, title }) });
    $('#incident-dialog').close(); await refresh(); openIncident(data.incident.id);
  });
}

async function openIncident(id) {
  const data = await request(`/api/incidents/${id}`); const { incident, diagnosis, comments, audit } = data;
  const controls = canOperate() ? `<div class="detail-actions">
    <button class="secondary" data-status="investigating">开始排查</button><button class="secondary" data-status="mitigated">已缓解</button><button class="secondary" data-status="resolved">解决事件</button><button class="secondary" id="assign-self">指派给我</button></div>` : '';
  showDialog(`<article class="detail"><p class="eyebrow">INCIDENT / ${escapeHtml(incident.id.slice(0, 8))}</p><h2>${escapeHtml(incident.title)}</h2><div class="detail-grid"><div><small>风险</small>${escapeHtml(incident.risk_level)}</div><div><small>状态</small>${escapeHtml(incident.status)}</div><div><small>服务 / 负责人</small>${escapeHtml(incident.service || '—')} / ${escapeHtml(incident.assignee_id || '待指派')}</div></div>${controls}<h3>脱敏诊断报告</h3>${reportBlock(diagnosis?.report)}<h3>协作记录</h3><div class="comment-list">${comments.map((item) => `<div class="comment"><small>${escapeHtml(item.author_id)} · ${readableTime(item.created_at)}</small>${escapeHtml(item.body)}</div>`).join('') || '<div class="empty-state">还没有评论。</div>'}</div>${canOperate() ? `<form class="comment-form" id="comment-form"><textarea placeholder="补充处置进度；内容会再次脱敏后保存" required></textarea><button class="primary">追加记录 <b>→</b></button></form>` : ''}<h3>审计</h3><div class="comment-list">${audit.map((item) => `<div class="comment"><small>${readableTime(item.created_at)} · ${escapeHtml(item.actor_id || 'system')}</small>${escapeHtml(item.action)}</div>`).join('')}</div></article>`);
  document.querySelectorAll('[data-status]').forEach((button) => button.addEventListener('click', async () => { await request(`/api/incidents/${incident.id}`, { method: 'PATCH', body: JSON.stringify({ status: button.dataset.status }) }); await refresh(); openIncident(incident.id); }));
  $('#assign-self')?.addEventListener('click', async () => { await request(`/api/incidents/${incident.id}`, { method: 'PATCH', body: JSON.stringify({ assign_to_me: true }) }); await refresh(); openIncident(incident.id); });
  $('#comment-form')?.addEventListener('submit', async (event) => { event.preventDefault(); const body = event.currentTarget.querySelector('textarea').value; await request(`/api/incidents/${incident.id}/comments`, { method: 'POST', body: JSON.stringify({ body }) }); await refresh(); openIncident(incident.id); });
}

async function boot() {
  try {
    const { user } = await request('/api/me'); state.me = user; renderIdentity(); $('#workspace').classList.remove('hidden'); $('#login-state').classList.add('hidden'); await refresh();
  } catch (_) { renderIdentity(); }
  $('#diagnosis-form')?.addEventListener('submit', async (event) => {
    event.preventDefault(); if (!canOperate()) return; const message = $('#form-message'); message.textContent = '正在进入队列…';
    try { const { job } = await request('/api/diagnoses', { method: 'POST', body: JSON.stringify({ query: $('#query').value, no_remote: $('#no-remote').checked }) }); message.textContent = `诊断 ${job.id.slice(0, 8)} 已入队；完成后可人工升级为事件。`; $('#query').value = ''; await refresh(); const timer = setInterval(async () => { const { job: latest } = await request(`/api/diagnoses/${job.id}`); if (!['queued', 'running'].includes(latest.status)) { clearInterval(timer); await refresh(); openDiagnosis(job.id); } }, 2500); } catch (error) { message.textContent = error.message; }
  });
  document.querySelectorAll('.nav-button').forEach((button) => button.addEventListener('click', () => { document.querySelectorAll('.nav-button,.view').forEach((item) => item.classList.remove('active')); button.classList.add('active'); $(`#view-${button.dataset.view}`).classList.add('active'); }));
  $('.dialog-close').addEventListener('click', () => $('#incident-dialog').close());
}
boot();
