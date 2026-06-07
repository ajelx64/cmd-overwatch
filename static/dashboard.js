// Claude Overwatch — Real-time dashboard client
// ES module loaded by index.html with type="module"

// ─── WebSocket ────────────────────────────────────────────────────────────────

const WS_URL = 'ws://localhost:8765/ws';
let ws = null;
let reconnectDelay = 1000;

function connectWS() {
    ws = new WebSocket(WS_URL);
    ws.onopen = () => { setLiveStatus(true); reconnectDelay = 1000; };
    ws.onclose = () => {
        setLiveStatus(false);
        setTimeout(connectWS, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 2, 30000);
    };
    ws.onerror = () => {};
    ws.onmessage = (e) => { handleEvent(JSON.parse(e.data)); };
}

// ─── Live status ──────────────────────────────────────────────────────────────

function setLiveStatus(isLive) {
    document.getElementById('pulse-dot').classList.toggle('live', isLive);
    document.getElementById('live-label').textContent = isLive ? 'LIVE' : 'OFFLINE';
}

// ─── Event routing ────────────────────────────────────────────────────────────

function handleEvent(event) {
    if (event.event_type === 'task') {
        updateTaskBoard(event);
    } else if (event.event_type === 'session') {
        addSessionMarker(event);
    } else {
        addFeedRow(event);
        incrementStats();
    }
}

// ─── Task board ───────────────────────────────────────────────────────────────

const taskCards = new Map();

function updateTaskBoard(event) {
    const { task_id, title, status } = event;
    if (status === 'deleted') {
        if (taskCards.has(task_id)) {
            taskCards.get(task_id).remove();
            taskCards.delete(task_id);
            updateSectionVisibility();
        }
        return;
    }
    hideEmptyState('task-empty');
    if (taskCards.has(task_id)) {
        const card = taskCards.get(task_id);
        moveTaskCard(card, status);
        card.querySelector('.task-meta').textContent = status;
    } else {
        const card = createTaskCard(task_id, title, status);
        taskCards.set(task_id, card);
        getTaskSection(status).appendChild(card);
    }
    updateSectionVisibility();
}

function createTaskCard(task_id, title, status) {
    const div = document.createElement('div');
    div.className = `task-card status-${status}`;
    div.dataset.taskId = task_id;
    const titleEl = document.createElement('div');
    titleEl.className = 'task-title';
    titleEl.textContent = title;
    const metaEl = document.createElement('div');
    metaEl.className = 'task-meta';
    metaEl.textContent = status;
    div.appendChild(titleEl);
    div.appendChild(metaEl);
    return div;
}

function moveTaskCard(card, newStatus) {
    card.className = `task-card status-${newStatus}`;
    getTaskSection(newStatus).appendChild(card);
}

function getTaskSection(status) {
    return document.getElementById(`tasks-${status}`) || document.getElementById('tasks-pending');
}

function updateSectionVisibility() {
    ['in_progress', 'pending', 'completed'].forEach(status => {
        const section = document.getElementById(`section-${status}`);
        const list = document.getElementById(`tasks-${status}`);
        if (section && list) section.style.display = list.children.length > 0 ? 'block' : 'none';
    });
}

// ─── Live feed ────────────────────────────────────────────────────────────────

const TOOL_ICONS = {
    Read: '📖', Edit: '✏️', Write: '💾', Bash: '▶', WebSearch: '🔍',
    WebFetch: '🌐', Agent: '⬡', TaskCreate: '＋', TaskUpdate: '↻', TaskGet: '◎',
};

function getToolIcon(toolName) { return TOOL_ICONS[toolName] || '◦'; }

function getToolClass(toolName) {
    if (['Read'].includes(toolName)) return 'tool-Read';
    if (['Edit', 'Write'].includes(toolName)) return 'tool-Edit';
    if (['Bash'].includes(toolName)) return 'tool-Bash';
    if (['Agent'].includes(toolName)) return 'tool-Agent';
    if (['TaskCreate', 'TaskUpdate', 'TaskGet'].includes(toolName)) return 'tool-Task';
    return '';
}

function addFeedRow(event) {
    hideEmptyState('feed-empty');
    const feed = document.getElementById('live-feed');
    const row = document.createElement('div');
    row.className = `feed-row ${getToolClass(event.tool_name)}`.trim();
    const timeStr = event.duration_ms != null
        ? `${(event.duration_ms / 1000).toFixed(1)}s`
        : formatTime(event.timestamp);
    const iconEl = document.createElement('span');
    iconEl.className = 'feed-icon';
    iconEl.textContent = getToolIcon(event.tool_name);
    const toolEl = document.createElement('span');
    toolEl.className = 'feed-tool';
    toolEl.textContent = event.tool_name || '?';
    const detailEl = document.createElement('span');
    detailEl.className = 'feed-detail';
    detailEl.textContent = event.input_summary || '';
    const timeEl = document.createElement('span');
    timeEl.className = 'feed-time';
    timeEl.textContent = timeStr;
    row.appendChild(iconEl);
    row.appendChild(toolEl);
    row.appendChild(detailEl);
    row.appendChild(timeEl);
    const emptyEl = document.getElementById('feed-empty');
    feed.insertBefore(row, emptyEl ? emptyEl.nextSibling : feed.firstChild);
    const rows = feed.querySelectorAll('.feed-row');
    if (rows.length > 200) rows[rows.length - 1].remove();
}

function addSessionMarker(event) {
    const feed = document.getElementById('live-feed');
    const marker = document.createElement('div');
    marker.className = 'feed-session';
    marker.textContent = `─── Session ${event.session_type} · ${new Date(event.timestamp).toLocaleTimeString()} ───`;
    feed.insertBefore(marker, feed.firstChild);
}

// ─── Stats counter ────────────────────────────────────────────────────────────

const eventTimestamps = [];

function incrementStats() {
    const now = Date.now();
    eventTimestamps.push(now);
    const cutoff = now - 60000;
    while (eventTimestamps.length > 0 && eventTimestamps[0] < cutoff) eventTimestamps.shift();
    document.getElementById('stats').textContent = `${eventTimestamps.length} events/min`;
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function escapeHtml(str) {
    return String(str)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function formatTime(isoTimestamp) {
    try { return new Date(isoTimestamp).toLocaleTimeString(); } catch { return ''; }
}

function hideEmptyState(id) {
    const el = document.getElementById(id);
    if (el) el.style.display = 'none';
}

function setUpdated(id) {
    const el = document.getElementById(id);
    if (el) el.textContent = `updated ${new Date().toLocaleTimeString()}`;
}

// ─── Tab system ───────────────────────────────────────────────────────────────

let activeTab = 'live';
let pollTimer = null;

function switchTab(tabName) {
    // Deactivate current
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => {
        c.classList.remove('active');
        c.classList.add('hidden');
    });
    // Activate new
    const btn = document.querySelector(`.tab-btn[data-tab="${tabName}"]`);
    const content = document.getElementById(`tab-${tabName}`);
    if (btn) btn.classList.add('active');
    if (content) { content.classList.add('active'); content.classList.remove('hidden'); }
    activeTab = tabName;
    // Fetch immediately on switch
    if (tabName === 'health')    fetchHealth();
    if (tabName === 'issues')    fetchIssues();
    if (tabName === 'approvals') fetchApprovals();
    if (tabName === 'aar')       fetchAar();
    // Restart poll timer
    clearInterval(pollTimer);
    if (tabName !== 'live') {
        pollTimer = setInterval(() => {
            if (activeTab === 'health')    fetchHealth();
            if (activeTab === 'issues')    fetchIssues();
            if (activeTab === 'approvals') fetchApprovals();
            if (activeTab === 'aar')       fetchAar();
        }, 30000);
    }
}

document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
});

// ─── Health tab ───────────────────────────────────────────────────────────────

function sevClass(sev) {
    return `sev-${sev || 'low'}`;
}

async function fetchHealth() {
    try {
        const res = await fetch('/api/health-board');
        if (!res.ok) throw new Error(res.statusText);
        const data = await res.json();
        renderHealth(data);
        setUpdated('health-updated');
    } catch (e) {
        document.getElementById('health-tiles').innerHTML =
            `<div class="empty-state">Error loading health data: ${escapeHtml(String(e))}</div>`;
    }
}

function renderHealth(data) {
    // DRY-RUN badge
    const dryBadge = document.getElementById('health-dry-run-badge');
    dryBadge.style.display = data.dry_run ? 'inline-block' : 'none';

    // Summary chips
    const summaryEl = document.getElementById('health-summary');
    const sevOrder = ['critical', 'high', 'medium', 'low'];
    summaryEl.innerHTML = sevOrder
        .filter(s => (data.active_by_severity || {})[s])
        .map(s => `<div class="sev-chip ${sevClass(s)}">
            <span>${s}</span><strong>${data.active_by_severity[s]}</strong>
        </div>`)
        .join('') || '<div class="sev-chip">All clear</div>';

    // Update approval badge
    const badge = document.getElementById('approval-badge');
    const count = data.pending_approvals || 0;
    badge.textContent = count;
    badge.style.display = count > 0 ? 'inline-block' : 'none';

    // Target tiles
    const tilesEl = document.getElementById('health-tiles');
    if (!data.tiles || data.tiles.length === 0) {
        tilesEl.innerHTML = '<div class="empty-state">No targets configured or all clear</div>';
    } else {
        tilesEl.innerHTML = data.tiles.map(t => `
            <div class="health-tile ${sevClass(t.worst_severity)}">
                <div class="tile-name">${escapeHtml(t.target)}</div>
                <div class="tile-count">${t.active_issues} active issue${t.active_issues !== 1 ? 's' : ''} · worst: ${t.worst_severity}</div>
            </div>
        `).join('');
    }

    // Host metrics
    const metricsEl = document.getElementById('host-metrics');
    if (data.host_metrics && data.host_metrics.length > 0) {
        metricsEl.innerHTML = `<div class="host-metrics-header">Host metrics</div>` +
            data.host_metrics.map(m => `
                <div class="metric-row">
                    <span>${escapeHtml(m.metric)}</span>
                    <span class="${m.healthy ? 'metric-healthy' : 'metric-warn'}">${escapeHtml(String(m.value))}</span>
                </div>
            `).join('');
    } else {
        metricsEl.innerHTML = '';
    }
}

// ─── Issues tab ───────────────────────────────────────────────────────────────

let allIssues = [];
let issueFilter = 'all';

document.querySelectorAll('.filter-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        issueFilter = btn.dataset.filter;
        renderIssues();
    });
});

async function fetchIssues() {
    try {
        const res = await fetch('/api/issues');
        if (!res.ok) throw new Error(res.statusText);
        allIssues = await res.json();
        renderIssues();
        setUpdated('issues-updated');
    } catch (e) {
        document.getElementById('issues-list').innerHTML =
            `<div class="empty-state">Error loading issues: ${escapeHtml(String(e))}</div>`;
    }
}

function renderIssues() {
    const filtered = issueFilter === 'all'
        ? allIssues
        : allIssues.filter(i => i.status === issueFilter);
    const list = document.getElementById('issues-list');
    if (filtered.length === 0) {
        list.innerHTML = `<div class="empty-state">No issues${issueFilter !== 'all' ? ` with status "${escapeHtml(issueFilter)}"` : ''}</div>`;
        return;
    }
    list.innerHTML = filtered.map(issue => `
        <div class="issue-card ${sevClass(issue.severity)}">
            <div class="issue-title">${escapeHtml(issue.title)}</div>
            <div class="issue-meta">
                <span class="status-pill status-${issue.status}">${issue.status}</span>
                <span>#${issue.id}</span>
                <span>${escapeHtml(issue.source)}</span>
                <span>seen ${issue.count}×</span>
            </div>
        </div>
    `).join('');
}

// ─── Approvals tab ────────────────────────────────────────────────────────────

async function fetchApprovals() {
    try {
        const res = await fetch('/api/approvals/pending');
        if (!res.ok) throw new Error(res.statusText);
        const items = await res.json();
        renderApprovals(items);
        setUpdated('approvals-updated');
        // Keep badge in sync
        const badge = document.getElementById('approval-badge');
        badge.textContent = items.length;
        badge.style.display = items.length > 0 ? 'inline-block' : 'none';
    } catch (e) {
        document.getElementById('approvals-list').innerHTML =
            `<div class="empty-state">Error loading approvals: ${escapeHtml(String(e))}</div>`;
    }
}

function renderApprovals(items) {
    const list = document.getElementById('approvals-list');
    if (items.length === 0) {
        list.innerHTML = '<div class="empty-state">No pending approvals</div>';
        return;
    }
    list.innerHTML = items.map(item => {
        const { issue, solution } = item;
        return `
        <div class="approval-card" id="approval-card-${solution.id}">
            <div class="approval-issue-title">${escapeHtml(issue.title)}</div>
            <div class="approval-meta">
                <span class="${sevClass(issue.severity)}" style="color: var(--text-secondary)">${issue.severity}</span>
                <span>#${issue.id}</span>
                <span>${escapeHtml(issue.source)}</span>
            </div>
            <div class="approval-gate">Gate: ${escapeHtml(solution.gate_category || '?')}</div>
            <pre class="approval-plan">${escapeHtml(solution.plan || '(no plan)')}</pre>
            <div class="approval-actions">
                <button class="btn-approve" onclick="doDecide(${solution.id}, 'approved', this)">Approve</button>
                <button class="btn-deny" onclick="doDecide(${solution.id}, 'denied', this)">Deny</button>
                <label class="wontfix-label">
                    <input type="checkbox" id="wontfix-${solution.id}"> Won't fix
                </label>
                <span class="decision-result" id="result-${solution.id}"></span>
            </div>
        </div>`;
    }).join('');
}

async function doDecide(solutionId, decision, btn) {
    const card = document.getElementById(`approval-card-${solutionId}`);
    const wontfix = document.getElementById(`wontfix-${solutionId}`)?.checked ?? false;
    const resultEl = document.getElementById(`result-${solutionId}`);
    // Disable buttons
    card.querySelectorAll('button').forEach(b => b.disabled = true);
    resultEl.textContent = 'Processing…';
    resultEl.className = 'decision-result';
    try {
        const res = await fetch(`/api/approvals/${solutionId}/decision`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ decision, wontfix }),
        });
        const data = await res.json();
        if (!res.ok) {
            resultEl.textContent = `Error: ${escapeHtml(data.detail || res.statusText)}`;
            resultEl.className = 'decision-result error';
            card.querySelectorAll('button').forEach(b => b.disabled = false);
            return;
        }
        resultEl.textContent = decision === 'approved'
            ? `Approved → ${data.execution?.status || 'dispatched'}`
            : `Denied`;
        resultEl.className = `decision-result ${decision}`;
        // Fade out card after a moment
        setTimeout(() => {
            card.style.transition = 'opacity 0.4s';
            card.style.opacity = '0';
            setTimeout(() => card.remove(), 400);
        }, 1500);
    } catch (e) {
        resultEl.textContent = `Error: ${escapeHtml(String(e))}`;
        resultEl.className = 'decision-result error';
        card.querySelectorAll('button').forEach(b => b.disabled = false);
    }
}

// expose globally so inline onclick handlers work
window.doDecide = doDecide;

// ─── AAR tab ──────────────────────────────────────────────────────────────────

async function fetchAar() {
    const contentEl = document.getElementById('aar-content');
    const summaryEl = document.getElementById('aar-summary-bar');
    try {
        const res = await fetch('/api/aar/latest');
        if (res.status === 404) {
            contentEl.textContent = 'No AAR generated yet. Run: python -m overwatch.aar';
            summaryEl.textContent = '';
            return;
        }
        if (!res.ok) throw new Error(res.statusText);
        const data = await res.json();
        summaryEl.textContent = `${data.report_date} · ${data.summary || ''}`;
        contentEl.textContent = data.content || '(report file not found on disk)';
        setUpdated('aar-updated');
    } catch (e) {
        contentEl.textContent = `Error loading AAR: ${String(e)}`;
    }
}

// ─── Init ─────────────────────────────────────────────────────────────────────

updateSectionVisibility();
connectWS();
