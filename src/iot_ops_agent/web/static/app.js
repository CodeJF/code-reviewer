const state = { me: null, csrf: '', incidents: [], diagnoses: [], users: [], members: [], notificationEnabled: false, aiEnabled: false, quality: null };
const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value = '') => String(value).replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
const labels = {
  admin: '管理员', oncall: '值班', viewer: '只读', open: '待处理', investigating: '排查中', mitigated: '已缓解', resolved: '已解决',
  planned: '待批准', queued: '排队中', running: '执行中', completed: '已完成', failed: '失败', expired: '已过期', skipped: '未启用', delivered: '已发送', retrying: '等待重试',
};

async function request(path, options = {}) {
  const method = (options.method || 'GET').toUpperCase();
  const headers = { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...(options.headers || {}) };
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method) && state.csrf) headers['X-CSRF-Token'] = state.csrf;
  const response = await fetch(path, { ...options, method, headers, credentials: 'same-origin' });
  const data = response.status === 204 ? null : await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data?.detail === 'string' ? data.detail : `请求失败 (${response.status})`);
  return data;
}

function readableTime(value) { return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'; }
function canOperate() { return ['admin', 'oncall'].includes(state.me?.role); }
function isAdmin() { return state.me?.role === 'admin'; }
function showToast(message) { const toast = $('#toast'); toast.textContent = message; toast.classList.add('visible'); setTimeout(() => toast.classList.remove('visible'), 2800); }
function formQuery(form) { const params = new URLSearchParams(new FormData(form)); [...params.entries()].forEach(([key, value]) => { if (!value) params.delete(key); }); return params.toString(); }
async function copyText(value) { await navigator.clipboard.writeText(value); showToast('链接已复制，请通过安全渠道发送'); }

function renderIdentity() {
  $('#identity').innerHTML = state.me
    ? `<span><strong>${escapeHtml(state.me.display_name)}</strong><small>@${escapeHtml(state.me.username)} · ${labels[state.me.role]}</small></span><button class="secondary" id="logout">退出</button>`
    : '<span>LOCAL ACCOUNT · HTTPS</span>';
  $('#logout')?.addEventListener('click', async () => { await request('/api/auth/logout', { method: 'POST' }); window.location.reload(); });
}

function renderIncidents() {
  const list = $('#incident-list');
  $('#incident-count').textContent = state.incidents.filter((item) => item.status !== 'resolved').length;
  if (!state.incidents.length) { list.innerHTML = '<div class="empty-state">当前筛选条件下没有事件。完成诊断后，由值班成员人工升级为 Incident。</div>'; return; }
  list.innerHTML = state.incidents.map((item) => `<button class="incident-row" data-incident="${item.id}">
    <span class="severity ${item.risk_level}">${escapeHtml(item.risk_level || 'unknown').toUpperCase()}</span>
    <span><span class="incident-title">${escapeHtml(item.title)}</span><span class="incident-meta">${escapeHtml(item.service || '未识别服务')} · ${escapeHtml(item.assignee_name || '待指派')} · ${readableTime(item.updated_at)}</span></span>
    <span class="status ${item.status}">${labels[item.status] || escapeHtml(item.status)}</span><span class="row-arrow">→</span></button>`).join('');
  document.querySelectorAll('[data-incident]').forEach((button) => button.addEventListener('click', () => openIncident(button.dataset.incident)));
}

function renderDiagnoses() {
  const list = $('#diagnosis-list');
  if (!state.diagnoses.length) { list.innerHTML = '<div class="empty-state">当前筛选条件下没有诊断记录。</div>'; return; }
  list.innerHTML = state.diagnoses.map((job) => `<button class="diagnosis-row" data-diagnosis="${job.id}">
    <span class="severity ${job.status === 'failed' ? 'high' : ''}">${escapeHtml(labels[job.status] || job.status)}</span>
    <span><span class="incident-title">${escapeHtml(job.query)}</span><span class="diagnosis-meta">${readableTime(job.created_at)}${job.retry_of_id ? ' · 失败重试任务' : ''} · ${job.execution_mode === 'ai_assisted' ? 'AI 辅助' : '规则模式'} · 留存至 ${readableTime(job.expires_at)}</span></span>
    <span class="status ${job.status}">${job.no_remote ? '仅 ES' : 'ES + 回退'}</span><span class="row-arrow">→</span></button>`).join('');
  document.querySelectorAll('[data-diagnosis]').forEach((button) => button.addEventListener('click', () => openDiagnosis(button.dataset.diagnosis)));
}

function populateAssignees() {
  const select = $('#assignee-filter');
  const current = select.value;
  select.innerHTML = '<option value="">全部负责人</option>' + state.users.map((user) => `<option value="${user.id}">${escapeHtml(user.display_name)}</option>`).join('');
  select.value = current;
}

async function refresh() {
  const incidentQuery = formQuery($('#incident-filters'));
  const diagnosisQuery = formQuery($('#diagnosis-filters'));
  const [incidents, diagnoses, users] = await Promise.all([
    request(`/api/incidents${incidentQuery ? `?${incidentQuery}` : ''}`),
    request(`/api/diagnoses${diagnosisQuery ? `?${diagnosisQuery}` : ''}`),
    request('/api/users'),
  ]);
  state.incidents = incidents.items; state.diagnoses = diagnoses.items; state.users = users.items;
  populateAssignees(); renderIncidents(); renderDiagnoses();
}

function showDialog(html) { $('#incident-detail').innerHTML = html; $('#incident-dialog').showModal(); }
function renderValue(value) {
  if (value === null || value === undefined || value === '') return '<p class="muted">暂无内容</p>';
  if (Array.isArray(value)) return `<ul class="report-list">${value.map((item) => `<li>${typeof item === 'object' ? `<pre>${escapeHtml(JSON.stringify(item, null, 2))}</pre>` : escapeHtml(item)}</li>`).join('')}</ul>`;
  if (typeof value === 'object') return `<pre>${escapeHtml(JSON.stringify(value, null, 2))}</pre>`;
  return `<p>${escapeHtml(value)}</p>`;
}
function pick(report, keys) { for (const key of keys) if (report?.[key] !== undefined) return report[key]; return null; }
function reportBlock(report) {
  if (!report) return '<div class="empty-state">诊断报告已按留存策略清除，或尚未完成。</div>';
  const sections = [
    ['结论', ['conclusion', 'summary', 'result_status', 'diagnosis_summary']],
    ['根因', ['root_cause', 'root_causes', 'ranked_root_causes', 'causes']],
    ['证据', ['evidence', 'evidences', 'facts']],
    ['时间线', ['timeline', 'time_window', 'events']],
    ['建议', ['recommendations', 'suggested_actions', 'actions', 'next_actions', 'next_steps']],
  ];
  return `<div class="structured-report">${sections.map(([title, keys]) => `<section><h4>${title}</h4>${renderValue(pick(report, keys))}</section>`).join('')}<details><summary>查看完整脱敏 JSON</summary><pre>${escapeHtml(JSON.stringify(report, null, 2))}</pre></details></div>`;
}

function planBlock(plan) {
  if (!plan) return '<div class="empty-state">这是兼容旧版创建的诊断，没有计划快照。</div>';
  const window = plan.time_window || {};
  const budget = plan.budget || {};
  return `<div class="plan-card">
    <div><small>目标服务</small><strong>${escapeHtml((plan.chain_services || []).join(' → ') || '自动识别')}</strong></div>
    <div><small>时间窗口</small><strong>${escapeHtml(window.start_local || '—')}<br/>${escapeHtml(window.end_local || '—')}</strong></div>
    <div><small>数据源</small><strong>Elasticsearch${plan.allow_remote ? ' → 白名单文件回退' : ''}</strong></div>
    <div><small>执行预算</small><strong>${escapeHtml(budget.max_turns || 3)} 回合 · ${escapeHtml(budget.max_tool_calls || 6)} 工具 · ${escapeHtml(budget.timeout_seconds || 120)} 秒</strong></div>
    <div class="plan-query"><small>查询关键词</small><code>${escapeHtml(plan.keyword || '—')}</code></div>
  </div>`;
}

function traceBlock(calls = []) {
  if (!calls.length) return '<div class="empty-state compact">规则模式或尚未产生 Agent 工具轨迹。</div>';
  return `<ol class="agent-trace">${calls.map((call) => `<li class="${escapeHtml(call.status)}"><span>${String(call.sequence).padStart(2, '0')}</span><div><strong>${escapeHtml(call.tool_name)}</strong><small>${escapeHtml(call.status)} · ${escapeHtml(call.duration_ms)} ms${call.evidence_refs?.length ? ` · ${call.evidence_refs.length} 条证据` : ''}</small>${call.error ? `<p>${escapeHtml(call.error)}</p>` : ''}</div></li>`).join('')}</ol>`;
}

function feedbackBlock(feedback) {
  const rating = feedback?.rating || '';
  return `<form id="diagnosis-feedback" class="feedback-card">
    <p>这份诊断是否帮助你更快找到有效证据？</p>
    <div class="feedback-options">
      <label><input type="radio" name="rating" value="useful" ${rating === 'useful' ? 'checked' : ''} required/> 有用</label>
      <label><input type="radio" name="rating" value="partial" ${rating === 'partial' ? 'checked' : ''}/> 部分有用</label>
      <label><input type="radio" name="rating" value="not_useful" ${rating === 'not_useful' ? 'checked' : ''}/> 无用</label>
    </div>
    <label>证据是否准确<select name="evidence_correct"><option value="">暂不判断</option><option value="true" ${feedback?.evidence_correct === true ? 'selected' : ''}>准确</option><option value="false" ${feedback?.evidence_correct === false ? 'selected' : ''}>不准确</option></select></label>
    <label>补充说明<textarea name="note" rows="2" maxlength="2000" placeholder="可选；内容会脱敏保存">${escapeHtml(feedback?.note || '')}</textarea></label>
    <button class="secondary" type="submit">保存反馈</button>
  </form>`;
}

function pollDiagnosis(id) {
  const timer = setInterval(async () => {
    const { job } = await request(`/api/v1/diagnoses/${id}`);
    if (!['queued', 'running'].includes(job.status)) {
      clearInterval(timer); await refresh(); openDiagnosis(id);
    }
  }, 2500);
}

async function openDiagnosis(id) {
  const { job, tool_calls: toolCalls = [], feedback = null } = await request(`/api/v1/diagnoses/${id}`);
  const promote = canOperate() && job.status === 'completed' ? '<button class="primary" id="promote">升级为 Incident <b>→</b></button>' : '';
  const retry = canOperate() && job.status === 'failed' ? '<button class="primary" id="retry-diagnosis">创建关联重试 <b>↻</b></button>' : '';
  const approval = canOperate() && job.status === 'planned' ? `<div class="approval-panel"><div><p class="section-label">HUMAN GATE</p><strong>确认计划后才会读取日志</strong><small>规则模式不调用外部模型；AI 辅助模式只发送脱敏 facts，并受计划和预算约束。</small></div><div class="detail-actions"><button class="secondary" id="execute-rules">按规则执行</button>${state.aiEnabled ? '<button class="primary" id="execute-ai">同意并使用 AI <b>→</b></button>' : '<span class="muted">AI 辅助未启用</span>'}</div></div>` : '';
  const usage = job.usage || {};
  showDialog(`<article class="detail"><p class="eyebrow">DIAGNOSIS / ${escapeHtml(job.status)}</p><h2>${escapeHtml(job.query)}</h2><div class="detail-grid"><div><small>状态 / 模式</small>${labels[job.status] || escapeHtml(job.status)} · ${job.execution_mode === 'ai_assisted' ? 'AI 辅助' : '规则'}</div><div><small>耗时 / 工具</small>${usage.duration_ms || 0} ms · ${usage.tool_call_count || 0} 次</div><div><small>Token / 留存</small>${usage.input_tokens || 0} + ${usage.output_tokens || 0} · ${readableTime(job.expires_at)}</div></div>${job.error ? `<div class="notice danger">${escapeHtml(job.error)}</div>` : ''}<h3>已批准的查询计划</h3>${planBlock(job.plan)}${approval}<div class="detail-actions">${promote}${retry}</div><h3>Agent 工具轨迹</h3>${traceBlock(toolCalls)}<h3>诊断报告</h3>${reportBlock(job.report)}${job.status === 'completed' ? `<h3>结果反馈</h3>${feedbackBlock(feedback)}` : ''}</article>`);
  const execute = async (mode) => {
    await request(`/api/v1/diagnoses/${job.id}/execute`, { method: 'POST', body: JSON.stringify({ mode, external_ai_consent: mode === 'ai_assisted' }) });
    showToast(mode === 'ai_assisted' ? '计划已批准，受控 Agent 开始执行' : '计划已批准，规则诊断开始执行');
    $('#incident-dialog').close(); await refresh(); pollDiagnosis(job.id);
  };
  $('#execute-rules')?.addEventListener('click', () => execute('rules'));
  $('#execute-ai')?.addEventListener('click', () => execute('ai_assisted'));
  $('#promote')?.addEventListener('click', async () => {
    const title = window.prompt('事件标题', job.query); if (title === null) return;
    const data = await request('/api/incidents', { method: 'POST', body: JSON.stringify({ diagnosis_id: job.id, title }) });
    $('#incident-dialog').close(); await refresh(); openIncident(data.incident.id);
  });
  $('#retry-diagnosis')?.addEventListener('click', async () => {
    const data = await request(`/api/diagnoses/${job.id}/retry`, { method: 'POST' });
    $('#incident-dialog').close(); await refresh(); showToast(`已创建重试任务 ${data.job.id.slice(0, 8)}`);
  });
  $('#diagnosis-feedback')?.addEventListener('submit', async (event) => {
    event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget));
    await request(`/api/v1/diagnoses/${job.id}/feedback`, { method: 'POST', body: JSON.stringify({ rating: data.rating, evidence_correct: data.evidence_correct === '' ? null : data.evidence_correct === 'true', corrected_incident_types: [], note: data.note || '' }) });
    showToast('反馈已保存'); openDiagnosis(job.id);
  });
}

async function loadQuality() {
  const data = await request('/api/v1/admin/quality'); state.quality = data;
  const diagnoses = data.diagnoses; const feedback = data.feedback;
  $('#quality-grid').innerHTML = `
    <article><small>完成率</small><strong>${(diagnoses.completion_rate * 100).toFixed(0)}%</strong><span>${diagnoses.completed} 完成 / ${diagnoses.failed} 失败</span></article>
    <article><small>AI 辅助运行</small><strong>${diagnoses.ai_assisted}</strong><span>总诊断 ${diagnoses.total}</span></article>
    <article><small>平均执行耗时</small><strong>${(diagnoses.average_duration_ms / 1000).toFixed(1)}s</strong><span>持久化运行指标</span></article>
    <article><small>反馈覆盖</small><strong>${(feedback.coverage_rate * 100).toFixed(0)}%</strong><span>${feedback.total} 份反馈</span></article>
    <article><small>有用率</small><strong>${(feedback.useful_rate * 100).toFixed(0)}%</strong><span>有用 + 部分有用</span></article>`;
}

async function openIncident(id) {
  const data = await request(`/api/incidents/${id}`); const { incident, diagnosis, comments, audit, notifications } = data;
  const transitions = { open: ['investigating', 'resolved'], investigating: ['mitigated', 'resolved'], mitigated: ['investigating', 'resolved'], resolved: ['investigating'] };
  const transitionLabels = { investigating: incident.status === 'resolved' ? '重新排查' : '开始排查', mitigated: '已缓解', resolved: '解决事件' };
  const statusButtons = (transitions[incident.status] || []).map((next) => `<button class="secondary" data-status="${next}">${transitionLabels[next]}</button>`).join('');
  const controls = canOperate() ? `<div class="detail-actions">${statusButtons}
    <label class="inline-select">负责人<select id="incident-assignee"><option value="">待指派</option>${state.users.map((user) => `<option value="${user.id}" ${user.id === incident.assignee_id ? 'selected' : ''}>${escapeHtml(user.display_name)}</option>`).join('')}</select></label></div>` : '';
  const notificationRows = notifications.map((item) => `<div class="comment"><small>${readableTime(item.created_at)} · ${escapeHtml(item.channel)} · 尝试 ${item.attempts} 次</small>${labels[item.status] || escapeHtml(item.status)}${item.error ? ` · ${escapeHtml(item.error)}` : ''}${isAdmin() && !['delivered', 'skipped'].includes(item.status) ? ` <button class="text-button" data-retry-notification="${item.id}">重试</button>` : ''}</div>`).join('');
  showDialog(`<article class="detail"><p class="eyebrow">INCIDENT / ${escapeHtml(incident.id.slice(0, 8))}</p><h2>${escapeHtml(incident.title)}</h2><div class="detail-grid"><div><small>风险</small>${escapeHtml(incident.risk_level)}</div><div><small>状态</small>${labels[incident.status] || escapeHtml(incident.status)}</div><div><small>服务 / 负责人</small>${escapeHtml(incident.service || '—')} / ${escapeHtml(incident.assignee_name || '待指派')}</div></div>${controls}<h3>诊断报告</h3>${reportBlock(diagnosis?.report)}<h3>协作记录</h3><div class="comment-list">${comments.map((item) => `<div class="comment"><small>${escapeHtml(item.author_name)} · ${readableTime(item.created_at)}</small>${escapeHtml(item.body)}</div>`).join('') || '<div class="empty-state">还没有评论。</div>'}</div>${canOperate() ? '<form class="comment-form" id="comment-form"><textarea placeholder="补充处置进度；内容会再次脱敏后保存" required></textarea><button class="primary">追加记录 <b>→</b></button></form>' : ''}<h3>通知状态</h3><div class="comment-list">${notificationRows || '<div class="empty-state">暂无通知记录。</div>'}</div><h3>审计</h3><div class="comment-list">${audit.map((item) => `<div class="comment"><small>${readableTime(item.created_at)} · ${escapeHtml(item.actor_name)}</small>${escapeHtml(item.action)}</div>`).join('')}</div></article>`);
  document.querySelectorAll('[data-status]').forEach((button) => button.addEventListener('click', async () => { await request(`/api/incidents/${incident.id}`, { method: 'PATCH', body: JSON.stringify({ status: button.dataset.status }) }); await refresh(); openIncident(incident.id); }));
  $('#incident-assignee')?.addEventListener('change', async (event) => { await request(`/api/incidents/${incident.id}`, { method: 'PATCH', body: JSON.stringify({ assignee_id: event.target.value || null }) }); await refresh(); openIncident(incident.id); });
  $('#comment-form')?.addEventListener('submit', async (event) => { event.preventDefault(); const body = event.currentTarget.querySelector('textarea').value; await request(`/api/incidents/${incident.id}/comments`, { method: 'POST', body: JSON.stringify({ body }) }); await refresh(); openIncident(incident.id); });
  document.querySelectorAll('[data-retry-notification]').forEach((button) => button.addEventListener('click', async () => { await request(`/api/notifications/${button.dataset.retryNotification}/retry`, { method: 'POST' }); openIncident(incident.id); }));
}

function renderMembers() {
  const list = $('#member-list');
  list.innerHTML = state.members.map((member) => `<article class="member-row ${member.is_active ? '' : 'disabled'}" data-member="${member.id}">
    <div><strong>${escapeHtml(member.display_name)}</strong><small>@${escapeHtml(member.username)} · ${member.last_login_at ? `最后登录 ${readableTime(member.last_login_at)}` : '尚未登录'}</small></div>
    <select class="member-role"><option value="admin" ${member.role === 'admin' ? 'selected' : ''}>管理员</option><option value="oncall" ${member.role === 'oncall' ? 'selected' : ''}>值班</option><option value="viewer" ${member.role === 'viewer' ? 'selected' : ''}>只读</option></select>
    <label class="member-active"><input type="checkbox" ${member.is_active ? 'checked' : ''}/> 启用</label>
    <button class="secondary save-member">保存</button><button class="text-button reset-member">重置密码</button>
  </article>`).join('') || '<div class="empty-state">还没有成员。</div>';
  document.querySelectorAll('[data-member]').forEach((row) => {
    const id = row.dataset.member;
    row.querySelector('.save-member').addEventListener('click', async () => {
      try {
        await request(`/api/admin/users/${id}`, { method: 'PATCH', body: JSON.stringify({ role: row.querySelector('.member-role').value, is_active: row.querySelector('.member-active input').checked }) });
        await loadMembers(); showToast('成员权限已保存');
      } catch (error) { showToast(error.message); }
    });
    row.querySelector('.reset-member').addEventListener('click', async () => {
      try { const data = await request(`/api/admin/users/${id}/reset-link`, { method: 'POST' }); showOneTimeLink('密码重置链接', data.reset_url, data.expires_at); } catch (error) { showToast(error.message); }
    });
  });
}
async function loadMembers() { state.members = (await request('/api/admin/users')).items; renderMembers(); }
function showOneTimeLink(title, link, expiresAt) {
  showDialog(`<article class="detail"><p class="eyebrow">SHOW ONCE</p><h2>${escapeHtml(title)}</h2><p class="muted">仅在本次响应中显示，${readableTime(expiresAt)} 前有效。请立即复制并通过安全渠道发送。</p><div class="one-time-link"><code>${escapeHtml(link)}</code><button class="primary" id="copy-one-time">复制链接</button></div></article>`);
  $('#copy-one-time').addEventListener('click', () => copyText(link));
}

function showAuthMode() {
  $('#login-state').classList.remove('hidden');
  const params = new URLSearchParams(window.location.hash.slice(1));
  const invite = params.get('invite'); const reset = params.get('reset');
  $('#login-panel').classList.toggle('hidden', Boolean(invite || reset));
  $('#activation-panel').classList.toggle('hidden', !invite);
  $('#reset-panel').classList.toggle('hidden', !reset);
  return { invite, reset };
}

function bindAuthForms() {
  $('#login-form').addEventListener('submit', async (event) => {
    event.preventDefault(); const form = event.currentTarget; const message = form.querySelector('.form-message');
    try { await request('/api/auth/login', { method: 'POST', body: JSON.stringify(Object.fromEntries(new FormData(form))) }); window.location.replace('/'); } catch (error) { message.textContent = error.message; }
  });
  const bindTokenForm = (selector, endpoint, tokenKey, successMessage) => $(selector).addEventListener('submit', async (event) => {
    event.preventDefault(); const form = event.currentTarget; const message = form.querySelector('.form-message'); const data = Object.fromEntries(new FormData(form));
    if (data.password !== data.confirmation) { message.textContent = '两次输入的密码不一致'; return; }
    const params = new URLSearchParams(window.location.hash.slice(1));
    try { await request(endpoint, { method: 'POST', body: JSON.stringify({ token: params.get(tokenKey), [endpoint.includes('reset') ? 'new_password' : 'password']: data.password }) }); history.replaceState(null, '', '/'); showToast(successMessage); $('#activation-panel').classList.add('hidden'); $('#reset-panel').classList.add('hidden'); $('#login-panel').classList.remove('hidden'); } catch (error) { message.textContent = error.message; }
  });
  bindTokenForm('#activation-form', '/api/auth/accept-invite', 'invite', '账号已激活，请登录');
  bindTokenForm('#reset-form', '/api/auth/reset-password', 'reset', '密码已重置，请登录');
}

function bindWorkspace() {
  $('#notification-banner').classList.toggle('hidden', state.notificationEnabled);
  if (!state.notificationEnabled) $('#notification-banner').textContent = '通知未启用：事件协作不受影响；管理员配置飞书 Webhook 后会自动发送通知。';
  document.querySelectorAll('.admin-only').forEach((item) => item.classList.toggle('hidden', !isAdmin()));
  document.querySelectorAll('.operator-only').forEach((item) => item.classList.toggle('hidden', !canOperate()));
  $('#diagnosis-form').addEventListener('submit', async (event) => {
    event.preventDefault(); const message = $('#form-message'); message.textContent = '正在生成受控查询计划…';
    try {
      const { job } = await request('/api/v1/diagnoses', { method: 'POST', body: JSON.stringify({ query: $('#query').value, no_remote: $('#no-remote').checked }) });
      message.textContent = `计划 ${job.id.slice(0, 8)} 已生成，请确认范围和执行模式。`;
      $('#query').value = ''; await refresh();
      openDiagnosis(job.id);
    } catch (error) { message.textContent = error.message; }
  });
  $('#incident-filters').addEventListener('submit', async (event) => { event.preventDefault(); await refresh(); });
  $('#diagnosis-filters').addEventListener('submit', async (event) => { event.preventDefault(); await refresh(); });
  $('#invite-form').addEventListener('submit', async (event) => {
    event.preventDefault(); const form = event.currentTarget; const message = $('#invite-message');
    try { const data = await request('/api/admin/invites', { method: 'POST', body: JSON.stringify(Object.fromEntries(new FormData(form))) }); form.reset(); message.textContent = '邀请已生成；链接关闭后无法再次查看。'; showOneTimeLink('成员邀请链接', data.invite_url, data.invite.expires_at); } catch (error) { message.textContent = error.message; }
  });
  $('#account-settings').addEventListener('click', () => $('#account-dialog').showModal());
  $('#password-form').addEventListener('submit', async (event) => { event.preventDefault(); const form = event.currentTarget; const data = Object.fromEntries(new FormData(form)); try { await request('/api/auth/change-password', { method: 'POST', body: JSON.stringify(data) }); showToast('密码已修改，请重新登录'); setTimeout(() => window.location.reload(), 900); } catch (error) { form.querySelector('.form-message').textContent = error.message; } });
  document.querySelectorAll('.nav-button[data-view]').forEach((button) => button.addEventListener('click', async () => { document.querySelectorAll('.nav-button,.view').forEach((item) => item.classList.remove('active')); button.classList.add('active'); $(`#view-${button.dataset.view}`).classList.add('active'); if (button.dataset.view === 'members' && isAdmin()) await loadMembers(); if (button.dataset.view === 'quality' && isAdmin()) await loadQuality(); }));
}

async function boot() {
  bindAuthForms();
  const authParams = new URLSearchParams(window.location.hash.slice(1));
  if (authParams.get('invite') || authParams.get('reset')) {
    renderIdentity();
    showAuthMode();
    return;
  }
  try {
    const data = await request('/api/me'); state.me = data.user; state.csrf = data.csrf_token; state.notificationEnabled = data.notification_enabled; state.aiEnabled = data.ai_assisted_enabled;
    renderIdentity(); $('#workspace').classList.remove('hidden'); $('#login-state').classList.add('hidden'); bindWorkspace(); await refresh();
  } catch (_) { renderIdentity(); showAuthMode(); }
  $('.dialog-close').addEventListener('click', () => $('#incident-dialog').close());
  $('.account-close').addEventListener('click', () => $('#account-dialog').close());
}
boot();
