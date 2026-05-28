// Claude Overwatch — Real-time dashboard client
// ES module loaded by index.html with type="module"

// ─── WebSocket ────────────────────────────────────────────────────────────────

const WS_URL = 'ws://localhost:8765/ws';
let ws = null;
let reconnectDelay = 1000; // start at 1s, double each failure, cap at 30s

function connectWS() {
    ws = new WebSocket(WS_URL);

    ws.onopen = () => {
        setLiveStatus(true);
        reconnectDelay = 1000; // reset backoff on success
    };

    ws.onclose = () => {
        setLiveStatus(false);
        setTimeout(connectWS, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 2, 30000);
    };

    ws.onerror = () => {}; // onclose handles reconnect

    ws.onmessage = (e) => {
        const event = JSON.parse(e.data);
        handleEvent(event);
    };
}

// ─── Live status ──────────────────────────────────────────────────────────────

function setLiveStatus(isLive) {
    const dot = document.getElementById('pulse-dot');
    const label = document.getElementById('live-label');
    dot.classList.toggle('live', isLive);
    label.textContent = isLive ? 'LIVE' : 'OFFLINE';
}

// ─── Event routing ────────────────────────────────────────────────────────────

function handleEvent(event) {
    if (event.event_type === 'task') {
        updateTaskBoard(event);
    } else if (event.event_type === 'session') {
        addSessionMarker(event);
    } else {
        // 'tool' events (default)
        addFeedRow(event);
        incrementStats();
    }
}

// ─── Task board ───────────────────────────────────────────────────────────────

const taskCards = new Map(); // task_id → card element

function updateTaskBoard(event) {
    hideEmptyState('task-empty');

    const { task_id, title, status } = event;

    if (taskCards.has(task_id)) {
        // Update existing card
        const card = taskCards.get(task_id);
        moveTaskCard(card, status);
        card.querySelector('.task-meta').textContent = status;
    } else {
        // Create new card
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
    // Remove old status class, add new
    card.className = `task-card status-${newStatus}`;
    // Move to correct section
    getTaskSection(newStatus).appendChild(card);
}

function getTaskSection(status) {
    // 'pending' → #tasks-pending, 'in_progress' → #tasks-in_progress, etc.
    const id = `tasks-${status}`;
    return document.getElementById(id) || document.getElementById('tasks-pending');
}

function updateSectionVisibility() {
    // Show/hide section headers based on whether they have children
    ['in_progress', 'pending', 'completed'].forEach(status => {
        const section = document.getElementById(`section-${status}`);
        const list = document.getElementById(`tasks-${status}`);
        if (section && list) {
            section.style.display = list.children.length > 0 ? 'block' : 'none';
        }
    });
}

// ─── Live feed ────────────────────────────────────────────────────────────────

const TOOL_ICONS = {
    Read: '📖',
    Edit: '✏️',
    Write: '💾',
    Bash: '▶',
    WebSearch: '🔍',
    WebFetch: '🌐',
    Agent: '⬡',
    TaskCreate: '＋',
    TaskUpdate: '↻',
    TaskGet: '◎',
};

function getToolIcon(toolName) {
    return TOOL_ICONS[toolName] || '◦';
}

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

    // Prepend (newest first) after the empty state
    const emptyEl = document.getElementById('feed-empty');
    feed.insertBefore(row, emptyEl ? emptyEl.nextSibling : feed.firstChild);

    // Cap at 200 rows
    const rows = feed.querySelectorAll('.feed-row');
    if (rows.length > 200) {
        rows[rows.length - 1].remove();
    }
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

    // Remove entries older than 60 seconds
    const cutoff = now - 60000;
    while (eventTimestamps.length > 0 && eventTimestamps[0] < cutoff) {
        eventTimestamps.shift();
    }

    document.getElementById('stats').textContent = `${eventTimestamps.length} events/min`;
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function escapeHtml(str) {
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function formatTime(isoTimestamp) {
    try {
        return new Date(isoTimestamp).toLocaleTimeString();
    } catch {
        return '';
    }
}

function hideEmptyState(id) {
    const el = document.getElementById(id);
    if (el) el.style.display = 'none';
}

// ─── Init ─────────────────────────────────────────────────────────────────────

// Hide all section headers initially
updateSectionVisibility();

// Start WebSocket connection
connectWS();
