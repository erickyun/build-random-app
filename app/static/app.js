const state = {
  csrf: null,
  jobsTimer: null,
  logTimer: null,
  activeLogJob: null,
};

const $ = (selector) => document.querySelector(selector);
const loginView = $('#loginView');
const appView = $('#appView');
const loginForm = $('#loginForm');
const jobForm = $('#jobForm');
const jobsList = $('#jobsList');
const logModal = $('#logModal');
const logOutput = $('#logOutput');

async function request(url, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.csrf && !['GET', 'HEAD'].includes((options.method || 'GET').toUpperCase())) {
    headers.set('X-CSRF-Token', state.csrf);
  }
  const response = await fetch(url, { ...options, headers, credentials: 'same-origin' });
  const contentType = response.headers.get('content-type') || '';
  const payload = contentType.includes('application/json') ? await response.json() : null;
  if (!response.ok) {
    const error = new Error(payload?.detail || `Request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

function showLogin() {
  loginView.classList.remove('hidden');
  appView.classList.add('hidden');
  clearInterval(state.jobsTimer);
}

function showApp() {
  loginView.classList.add('hidden');
  appView.classList.remove('hidden');
  loadJobs();
  clearInterval(state.jobsTimer);
  state.jobsTimer = setInterval(loadJobs, 2200);
}

async function boot() {
  try {
    const session = await request('/api/session');
    if (session.authenticated) {
      state.csrf = session.csrf;
      showApp();
    } else {
      showLogin();
    }
  } catch {
    showLogin();
  }
}

loginForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  $('#loginError').textContent = '';
  const button = loginForm.querySelector('button');
  button.disabled = true;
  try {
    const result = await request('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: $('#password').value }),
    });
    state.csrf = result.csrf;
    $('#password').value = '';
    showApp();
  } catch (error) {
    $('#loginError').textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

$('#logoutButton').addEventListener('click', async () => {
  try { await request('/api/logout', { method: 'POST' }); } catch {}
  state.csrf = null;
  showLogin();
});

for (const tab of document.querySelectorAll('.source-tab')) {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.source-tab').forEach((item) => item.classList.remove('active'));
    tab.classList.add('active');
    const source = tab.dataset.source;
    $('#sourceType').value = source;
    $('#uploadSource').classList.toggle('hidden', source !== 'upload');
    $('#urlSource').classList.toggle('hidden', source !== 'url');
  });
}

$('#mediaFile').addEventListener('change', (event) => {
  const file = event.target.files[0];
  $('#mediaFileName').textContent = file ? `${file.name} · ${formatBytes(file.size)}` : 'MKV, MP4, WebM, MOV, TS and other FFmpeg-readable formats';
});

$('#subtitleFile').addEventListener('change', (event) => {
  const file = event.target.files[0];
  $('#subtitleFileName').textContent = file ? `${file.name} · subtitle will be trimmed and rebased` : 'Optional. Cues are trimmed and shifted so the clip begins at 00:00:00.';
});

function updateFormatNotice() {
  const format = $('#outputFormat').value;
  const presetSelect = $('#preset');
  const notice = $('#formatNotice');
  for (const option of presetSelect.options) {
    option.disabled = format === 'webm' && !['copy', 'animethemes-vp9'].includes(option.value);
  }
  if (presetSelect.selectedOptions[0]?.disabled) presetSelect.value = 'animethemes-vp9';
  if (format === 'mkv') {
    notice.textContent = 'MKV is recommended: supported text subtitle tracks are trimmed and remuxed, while fonts and other Matroska attachments are preserved.';
  } else if (format === 'mp4') {
    notice.textContent = 'MP4 cannot preserve Matroska attachments. Compatible text subtitles are converted to mov_text and ASS styling may be lost.';
  } else {
    notice.textContent = 'WebM does not preserve Matroska attachments. Compatible text subtitles are converted to WebVTT and ASS styling may be lost.';
  }
}
$('#outputFormat').addEventListener('change', updateFormatNotice);
updateFormatNotice();

jobForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const errorNode = $('#formError');
  const button = $('#executeButton');
  errorNode.textContent = '';
  button.disabled = true;
  try {
    const formData = new FormData(jobForm);
    if (!formData.get('anime_filter')) formData.set('anime_filter', 'false');
    const result = await request('/api/jobs', { method: 'POST', body: formData });
    if (result.ok) {
      await loadJobs();
      jobsList.scrollTo({ top: 0, behavior: 'smooth' });
    }
  } catch (error) {
    errorNode.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

function formatBytes(value) {
  if (value === null || value === undefined) return '—';
  const bytes = Number(value);
  if (!Number.isFinite(bytes)) return '—';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let amount = bytes;
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) { amount /= 1024; unit += 1; }
  return `${amount.toFixed(unit > 1 ? 2 : 0)} ${units[unit]}`;
}

function formatDate(value) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
  }[character]));
}

function renderJobs(jobs) {
  if (!jobs.length) {
    jobsList.innerHTML = '<div class="empty-state"><strong>No jobs yet</strong><span>Create your first trim job from the panel.</span></div>';
    return;
  }
  jobsList.innerHTML = jobs.map((job) => {
    const status = escapeHtml(job.status);
    const progress = Math.max(0, Math.min(100, Number(job.progress || 0)));
    const canDownload = job.status === 'completed';
    const canDelete = !['preparing', 'downloading', 'running'].includes(job.status);
    return `
      <article class="job-card" data-job-id="${escapeHtml(job.id)}">
        <div class="job-top">
          <div class="job-title">
            <strong title="${escapeHtml(job.output_name)}">${escapeHtml(job.output_name)}</strong>
            <span>${escapeHtml(job.source_name || job.source_url || 'media source')}</span>
          </div>
          <span class="status ${status}">${status}</span>
        </div>
        <div class="progress-track"><div class="progress-bar" style="width:${progress}%"></div></div>
        <div class="job-meta">
          <span>${progress.toFixed(1)}%</span>
          <span>${escapeHtml(job.start_time)} → ${escapeHtml(job.end_time)}</span>
          <span>${escapeHtml(job.preset)}</span>
          <span>${formatBytes(job.file_size)}</span>
          <span>${escapeHtml(formatDate(job.created_at))}</span>
        </div>
        ${job.error ? `<div class="job-error">${escapeHtml(job.error)}</div>` : ''}
        <div class="job-actions">
          <button class="button ghost compact log-button" type="button">Log</button>
          ${canDownload ? `<a class="button primary compact" href="/api/jobs/${encodeURIComponent(job.id)}/download">Download</a>` : ''}
          ${canDelete ? `<button class="button ghost compact delete-button" type="button">Delete</button>` : ''}
        </div>
      </article>`;
  }).join('');
}

async function loadJobs() {
  try {
    const result = await request('/api/jobs');
    renderJobs(result.jobs || []);
  } catch (error) {
    if (error.status === 401) {
      state.csrf = null;
      showLogin();
    }
  }
}

jobsList.addEventListener('click', async (event) => {
  const card = event.target.closest('.job-card');
  if (!card) return;
  const jobId = card.dataset.jobId;
  if (event.target.closest('.log-button')) openLog(jobId);
  if (event.target.closest('.delete-button')) {
    if (!confirm('Delete this job and its stored files?')) return;
    try {
      await request(`/api/jobs/${encodeURIComponent(jobId)}`, { method: 'DELETE' });
      await loadJobs();
    } catch (error) {
      alert(error.message);
    }
  }
});

async function refreshLog() {
  if (!state.activeLogJob) return;
  try {
    const result = await request(`/api/jobs/${encodeURIComponent(state.activeLogJob)}/log`);
    logOutput.textContent = result.log || '(No log output yet.)';
    logOutput.scrollTop = logOutput.scrollHeight;
    if (['completed', 'failed'].includes(result.status)) clearInterval(state.logTimer);
  } catch (error) {
    logOutput.textContent = error.message;
  }
}

function openLog(jobId) {
  state.activeLogJob = jobId;
  logModal.classList.remove('hidden');
  logOutput.textContent = 'Loading…';
  refreshLog();
  clearInterval(state.logTimer);
  state.logTimer = setInterval(refreshLog, 1400);
}

function closeLog() {
  logModal.classList.add('hidden');
  state.activeLogJob = null;
  clearInterval(state.logTimer);
}

$('#closeLogButton').addEventListener('click', closeLog);
logModal.addEventListener('click', (event) => { if (event.target === logModal) closeLog(); });
document.addEventListener('keydown', (event) => { if (event.key === 'Escape' && !logModal.classList.contains('hidden')) closeLog(); });
$('#clearLogButton').addEventListener('click', async () => {
  if (!state.activeLogJob) return;
  try {
    await request(`/api/jobs/${encodeURIComponent(state.activeLogJob)}/log/clear`, { method: 'POST' });
    logOutput.textContent = '';
  } catch (error) {
    logOutput.textContent = error.message;
  }
});
$('#refreshButton').addEventListener('click', loadJobs);

boot();
