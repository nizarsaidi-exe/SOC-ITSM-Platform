// =====================================================================
// SOC & ITSM Control Center — Interactive Frontend
// =====================================================================
const BACKEND = '';
const API_URL = '/api/tickets';
const WS_PROTO = window.location.protocol === 'https:' ? 'wss' : 'ws';
const WS_URL = `${WS_PROTO}://${window.location.host}/ws`;
const PROFILE_API = '/api/user/profile';
const LOGIN_WEBHOOK = `${window.location.origin}/n8n/webhook/soc-login`;

function getToken() {
    try {
        const s = JSON.parse(localStorage.getItem('soc_session') || '{}');
        return s.token || null;
    } catch { return null; }
}

function authFetch(url, opts = {}) {
    const token = getToken();
    const headers = { ...(opts.headers || {}) };
    if (token) headers['Authorization'] = `Bearer ${token}`;
    if (opts.body && typeof opts.body === 'string' && !headers['Content-Type']) {
        headers['Content-Type'] = 'application/json';
    }
    return fetch(url, { ...opts, headers });
}

let currentCategory = 'ALL';
let currentFilter = 'all';
let searchQuery = '';
let ticketsData = [];
let socket = null;
let reconnectDelay = 1000;
let firstLoad = true;
let autoRefreshTimer = null;

// RBAC ticket queue scope + User Directory state
let queueScope = 'all';
let userDirData = [];
let userDirQuery = '';
let userDirPage = 1;
const USER_DIR_PAGE_SIZE = 10;

// Dynamic department filtering (management dashboard + user directory)
let departmentsData = [];
let deptFilterCode = '';
let deptFilterName = '';

const categoryMap = {
    'ALL': 'All Operations',
    'ANALYTICS': 'Analytics',
    'INCIDENT_MGMT': 'Incident Management',
    'REQUEST_MGMT': 'Request Management',
    'CHANGE_MGMT': 'Change Management',
    'ASSET_MGMT': 'Asset Management',
    'CONFIG_MGMT': 'Configuration Management',
    'KNOWLEDGE_MGMT': 'Knowledge Base',
    'IAM': 'User & Access Management'
};

const severityBadgeClass = {
    'LOW':      'badge-low',
    'MEDIUM':   'badge-medium',
    'HIGH':     'badge-high',
    'CRITICAL': 'badge-critical'
};

const statusConfig = {
    'UNASSIGNED':       { label: 'Unassigned',   cls: 'badge-medium' },
    'OPEN':             { label: 'In Progress',   cls: 'badge-open' },
    'IN_INVESTIGATION': { label: 'Investigating', cls: 'badge-pending' },
    'MITIGATED':        { label: 'Resolved',      cls: 'badge-resolved' }
};

// =====================================================================
// THEME TOGGLE
// =====================================================================
function initTheme() {
    const saved = localStorage.getItem('soc-theme');
    const theme = saved || 'dark';
    document.documentElement.setAttribute('data-theme', theme);
    updateThemeIcon(theme);
}

function toggleTheme() {
    const current = document.documentElement.getAttribute('data-theme');
    const next = current === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('soc-theme', next);
    updateThemeIcon(next);
}

function updateThemeIcon(theme) {
    const icon = document.getElementById('theme-icon');
    if (!icon) return;
    icon.className = theme === 'dark'
        ? 'toggle-icon fa-solid fa-moon'
        : 'toggle-icon fa-solid fa-sun';
}

initTheme();

// =====================================================================
// METRICS BANNER
// =====================================================================
function updateMetrics() {
    const total = ticketsData.length;
    const inProgress = ticketsData.filter(t => t.status === 'OPEN' || t.status === 'UNASSIGNED' || t.status === 'IN_INVESTIGATION').length;
    const resolved = ticketsData.filter(t => t.status === 'MITIGATED').length;

    // Active analysts = unique assigned users with non-resolved tickets
    const analysts = new Set(
        ticketsData
            .filter(t => t.status !== 'MITIGATED' && t.assigned_user && t.assigned_user.toLowerCase() !== 'unassigned')
            .map(t => t.assigned_user)
    );

    animateCounter('stat-total', total);
    animateCounter('stat-progress', inProgress);
    animateCounter('stat-resolved', resolved);
    animateCounter('stat-analysts', analysts.size);
}

function animateCounter(elementId, targetValue) {
    const el = document.getElementById(elementId);
    if (!el) return;
    const startValue = parseInt(el.textContent) || 0;
    if (startValue === targetValue) return;

    const duration = 600;
    const startTime = performance.now();

    function tick(now) {
        const elapsed = now - startTime;
        const progress = Math.min(elapsed / duration, 1);
        // Ease-out cubic
        const eased = 1 - Math.pow(1 - progress, 3);
        const current = Math.round(startValue + (targetValue - startValue) * eased);
        el.textContent = current;
        if (progress < 1) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
}

// =====================================================================
// SEARCH & FILTER
// =====================================================================
function handleSearch(query) {
    searchQuery = query.trim().toLowerCase();
    renderTickets();
}

function setFilter(filter, btn) {
    currentFilter = filter;
    document.querySelectorAll('.filter-chip').forEach(c => c.classList.remove('active'));
    if (btn) btn.classList.add('active');
    renderTickets();
}

function getFilteredTickets() {
    let filtered = [...ticketsData];

    // RBAC queue scope (client-side refinement on top of backend-scoped data)
    const session = JSON.parse(localStorage.getItem('soc_session') || '{}');
    if (queueScope === 'mine') {
        filtered = filtered.filter(t => t.owner_id === session.id);
    } else if (queueScope === 'assigned') {
        filtered = filtered.filter(t => t.assigned_user_id === session.id);
    }

    // Status filter
    if (currentFilter === 'open') {
        filtered = filtered.filter(t => t.status === 'OPEN' || t.status === 'UNASSIGNED');
    } else if (currentFilter === 'investigating') {
        filtered = filtered.filter(t => t.status === 'IN_INVESTIGATION');
    } else if (currentFilter === 'resolved') {
        filtered = filtered.filter(t => t.status === 'MITIGATED');
    }

    // Dynamic department filter (chips rendered from the database)
    if (deptFilterCode) {
        filtered = filtered.filter(t => (t.reporter_department_code || '') === deptFilterCode);
    }

    // Search query (incl. reporter + reporter department)
    if (searchQuery) {
        filtered = filtered.filter(t => {
            const hay = [
                t.title,
                t.description,
                `TCK-${t.id}`,
                `${t.id}`,
                t.severity,
                t.assigned_user,
                t.category,
                t.current_activity,
                t.reporter_name,
                t.reporter_email,
                t.reporter_department_name
            ].filter(Boolean).join(' ').toLowerCase();
            return hay.includes(searchQuery);
        });
    }

    return filtered;
}

// Keyboard shortcut: Cmd/Ctrl+K to focus search
document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        const searchInput = document.getElementById('search-input');
        if (searchInput) searchInput.focus();
    }
    if (e.key === 'Escape') {
        const modal = document.getElementById('ticket-modal');
        if (!modal.classList.contains('hidden')) closeModal();
        const profileModal = document.getElementById('profile-settings-modal');
        if (profileModal && !profileModal.classList.contains('hidden')) closeProfileSettings();
    }
});

// =====================================================================
// SKELETON LOADING
// =====================================================================
function renderSkeleton() {
    const container = document.getElementById('ticket-list');
    container.innerHTML = Array.from({ length: 6 }).map(() => `
        <div class="ticket-card" style="opacity: 0.6;">
            <div class="space-y-3">
                <div class="h-3 w-24 rounded skeleton-shimmer"></div>
                <div class="h-5 w-3/4 rounded skeleton-shimmer"></div>
                <div class="h-3 w-full rounded skeleton-shimmer"></div>
                <div class="flex gap-2 mt-4">
                    <div class="h-6 w-16 rounded-full skeleton-shimmer"></div>
                    <div class="h-6 w-20 rounded-full skeleton-shimmer"></div>
                </div>
            </div>
        </div>
    `).join('');
}

// =====================================================================
// FETCH TICKETS
// =====================================================================
async function fetchTickets() {
    try {
        if (firstLoad) renderSkeleton();
        const fetchCat = currentCategory === 'ANALYTICS' ? 'ALL' : currentCategory;
        const url = fetchCat === 'ALL' ? API_URL : `${API_URL}?category=${fetchCat}`;
        const res = await authFetch(url);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        ticketsData = await res.json();
        renderTickets();
        updateMetrics();
    } catch (err) {
        console.error("Failed to fetch ticket state:", err);
        document.getElementById('ticket-list').innerHTML = `
            <div class="empty-state" style="grid-column: 1 / -1;">
                <div class="empty-icon">
                    <i class="fa-solid fa-wifi" style="color: #ef4444; font-size: 18px;"></i>
                </div>
                <h3 class="text-sm font-bold mb-1" style="color: var(--ink);">Connection Error</h3>
                <p class="text-xs max-w-xs mb-4" style="color: var(--muted);">Couldn't reach the API. Check the console for details.</p>
                <button onclick="fetchTickets()" class="btn-primary">
                    <i class="fa-solid fa-arrows-rotate text-[10px]"></i> Retry
                </button>
            </div>`;
    } finally {
        firstLoad = false;
    }
}

// =====================================================================
// WEBSOCKET LIVE SYNC
// =====================================================================
function connectWebSocket() {
    socket = new WebSocket(WS_URL);

    socket.onopen = () => {
        const token = getToken();
        if (token) {
            socket.send(JSON.stringify({ token }));
        } else {
            socket.close();
        }
        reconnectDelay = 1000;
        setWsStatus('live');
    };

    socket.onmessage = (event) => {
        try {
            handleSocketMessage(JSON.parse(event.data));
        } catch (err) {
            console.error("Bad WS payload:", err);
        }
    };

    socket.onclose = () => {
        setWsStatus('offline');
        setTimeout(connectWebSocket, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 1.5, 10000);
    };

    socket.onerror = () => socket.close();
}

function handleSocketMessage(msg) {
    const ticket = msg.data;
    if (!ticket) return;
        const belongsToView = currentCategory === 'ALL' || currentCategory === 'ANALYTICS' || ticket.category === currentCategory;

    if (msg.type === 'ticket_created') {
        if (belongsToView) {
            ticketsData.unshift(ticket);
            renderTickets();
            updateMetrics();
        }
    } else if (msg.type === 'ticket_updated') {
        const idx = ticketsData.findIndex(t => t.id === ticket.id);
        if (idx !== -1) {
            if (belongsToView) ticketsData[idx] = ticket;
            else ticketsData.splice(idx, 1);
            renderTickets();
            updateMetrics();
        } else if (belongsToView) {
            ticketsData.unshift(ticket);
            renderTickets();
            updateMetrics();
        }
    }
}

function setWsStatus(state) {
    const dot = document.getElementById('ws-dot');
    const label = document.getElementById('ws-label');
    if (!dot || !label) return;
    if (state === 'live') {
        dot.style.background = '#22c55e';
        dot.style.boxShadow = '0 0 6px rgba(34,197,94,0.5)';
        label.innerText = "Live";
        label.style.color = '#22c55e';
    } else {
        dot.style.background = '#ef4444';
        dot.style.boxShadow = 'none';
        dot.style.animation = 'pulseRing 1.5s infinite';
        label.innerText = "Reconnecting";
        label.style.color = '#ef4444';
    }
}

// =====================================================================
// CATEGORY SWITCHING
// =====================================================================
function updateNavPill() {
    var links = document.getElementById('topnav-links');
    var pill = document.getElementById('nav-active-pill');
    if (!links || !pill) return;
    var active = links.querySelector('.clay-active');
    if (!active) { pill.style.opacity = '0'; return; }
    var linksRect = links.getBoundingClientRect();
    var activeRect = active.getBoundingClientRect();
    var left = activeRect.left - linksRect.left + links.scrollLeft;
    pill.style.left = left + 'px';
    pill.style.width = activeRect.width + 'px';
    pill.style.opacity = '1';
}

function selectCategory(cat) {
    currentCategory = cat;
    document.getElementById('current-module-title').innerText = categoryMap[cat] || cat;

    // All nav button IDs
    const allNavIds = ['cat-ALL', 'cat-ANALYTICS', 'cat-INCIDENT_MGMT', 'cat-REQUEST_MGMT',
                       'cat-CHANGE_MGMT', 'cat-ASSET_MGMT', 'cat-CONFIG_MGMT', 'cat-KNOWLEDGE_MGMT',
                       'cat-USERS', 'cat-IAM'];

    allNavIds.forEach(id => {
        const btn = document.getElementById(id);
        if (!btn) return;
        btn.classList.remove('clay-active');
    });

    // Map category to button ID
    const catToId = {
        'ALL': 'cat-ALL',
        'ANALYTICS': 'cat-ANALYTICS',
        'INCIDENT_MGMT': 'cat-INCIDENT_MGMT',
        'REQUEST_MGMT': 'cat-REQUEST_MGMT',
        'CHANGE_MGMT': 'cat-CHANGE_MGMT',
        'ASSET_MGMT': 'cat-ASSET_MGMT',
        'CONFIG_MGMT': 'cat-CONFIG_MGMT',
        'KNOWLEDGE_MGMT': 'cat-KNOWLEDGE_MGMT',
        'USERS': 'cat-USERS',
        'IAM': 'cat-IAM'
    };

    const targetId = catToId[cat] || 'cat-ALL';
    const targetBtn = document.getElementById(targetId);
    if (targetBtn) targetBtn.classList.add('clay-active');

    requestAnimationFrame(function() { updateNavPill(); });

    // Toggle between Knowledge Base, IAM, Directory, and ticket views
    const kbView = document.getElementById('knowledge-base-view');
    const iamView = document.getElementById('iam-view');
    const usersView = document.getElementById('users-view');
    const metricsBanner = document.getElementById('metrics-banner');
    const searchFilterBar = document.getElementById('search-filter-bar');
    const ticketList = document.getElementById('ticket-list');

    // Hide all special views first
    if (kbView) kbView.classList.add('hidden');
    if (iamView) iamView.classList.add('hidden');
    if (usersView) usersView.classList.add('hidden');

    if (cat === 'KNOWLEDGE_MGMT') {
        if (kbView) kbView.classList.remove('hidden');
        if (metricsBanner) metricsBanner.classList.add('hidden');
        if (searchFilterBar) searchFilterBar.classList.add('hidden');
        if (ticketList) ticketList.classList.add('hidden');
        renderKnowledgeBase();
    } else if (cat === 'IAM') {
        if (iamView) iamView.classList.remove('hidden');
        if (metricsBanner) metricsBanner.classList.add('hidden');
        if (searchFilterBar) searchFilterBar.classList.add('hidden');
        if (ticketList) ticketList.classList.add('hidden');
        renderIAMView();
    } else if (cat === 'USERS') {
        if (usersView) usersView.classList.remove('hidden');
        if (metricsBanner) metricsBanner.classList.add('hidden');
        if (searchFilterBar) searchFilterBar.classList.add('hidden');
        if (ticketList) ticketList.classList.add('hidden');
        renderUsersView();
    } else {
        if (metricsBanner) metricsBanner.classList.remove('hidden');
        if (searchFilterBar) searchFilterBar.classList.remove('hidden');
        if (ticketList) ticketList.classList.remove('hidden');
        firstLoad = true;
        fetchTickets();
    }
}

// =====================================================================
// RENDER KNOWLEDGE BASE VIEW
// =====================================================================
function renderKnowledgeBase() {
    const container = document.getElementById('knowledge-base-view');
    if (!container) return;

    const categories = [
        {
            title: 'SOC Runbooks',
            icon: 'fa-solid fa-book-skull',
            color: '#ef4444',
            items: [
                { name: 'Wazuh Alert Triage', desc: 'Step-by-step triage procedure for Wazuh SIEM alerts including severity classification and initial containment actions.', btn: 'Open Runbook' },
                { name: 'Malware Containment SOP', desc: 'Isolation workflow for confirmed malware infections across endpoint and server fleet.', btn: 'Open Runbook' },
                { name: 'Phishing Response Playbook', desc: 'Email analysis, IOC extraction, and user notification procedures for phishing campaigns.', btn: 'Open Runbook' }
            ]
        },
        {
            title: 'Incident Response SOPs',
            icon: 'fa-solid fa-bolt',
            color: '#f59e0b',
            items: [
                { name: 'Critical Incident Escalation', desc: 'P1 incident escalation matrix with SLA timelines, notification chains, and war room setup.', btn: 'Read SOP' },
                { name: 'Data Breach Response', desc: 'Regulatory notification requirements, evidence preservation, and forensic handoff checklist.', btn: 'Read SOP' },
                { name: 'DDoS Mitigation Procedure', desc: 'Network-layer and application-layer DDoS response with CDN failover and rate-limiting.', btn: 'Read SOP' }
            ]
        },
        {
            title: 'Network Configuration Guides',
            icon: 'fa-solid fa-network-wired',
            color: '#3b82f6',
            items: [
                { name: 'Firewall CARP Failover SOP', desc: 'High-availability firewall configuration using CARP for automatic failover on pfSense/OPNsense.', btn: 'Open Guide' },
                { name: 'VPN Tunnel provisioning', desc: 'IKEv2 and WireGuard site-to-site tunnel setup with certificate-based authentication.', btn: 'Open Guide' },
                { name: 'DNS Zone Management', desc: 'Internal DNS zone authoring, split-horizon configuration, and resolver health checks.', btn: 'Open Guide' }
            ]
        },
        {
            title: 'n8n & Webhook Documentation',
            icon: 'fa-solid fa-diagram-project',
            color: '#a855f7',
            items: [
                { name: 'Webhook Ingest Pipeline', desc: 'Configuring authenticated webhooks for ticket ingestion from Wazuh, Suricata, and custom sources.', btn: 'Open Docs' },
                { name: 'n8n Workflow Templates', desc: 'Pre-built automation workflows for alert enrichment, Slack notifications, and Jira sync.', btn: 'Open Docs' },
                { name: 'API Integration Guide', desc: 'REST and WebSocket integration patterns for external SIEM and SOAR platform connectors.', btn: 'Open Docs' }
            ]
        }
    ];

    container.innerHTML = categories.map(cat => `
        <div class="kb-category">
            <div class="kb-category-header">
                <div class="kb-category-icon" style="background: ${cat.color}20; color: ${cat.color};">
                    <i class="${cat.icon}"></i>
                </div>
                <h3 class="kb-category-title" style="color: var(--ink);">${cat.title}</h3>
            </div>
            <div class="kb-items-grid">
                ${cat.items.map(item => `
                    <div class="kb-card">
                        <h4 class="kb-card-title" style="color: var(--ink);">${item.name}</h4>
                        <p class="kb-card-desc">${item.desc}</p>
                        <button class="kb-card-btn" style="color: ${cat.color}; border-color: ${cat.color}30; background: ${cat.color}10;">
                            <i class="fa-solid fa-arrow-right text-[10px]"></i> ${item.btn}
                        </button>
                    </div>
                `).join('')}
            </div>
        </div>
    `).join('');
}

// =====================================================================
// RENDER TICKETS AS CARDS
// =====================================================================
function renderTickets() {
    const container = document.getElementById('ticket-list');
    container.innerHTML = '';

    const filtered = getFilteredTickets();

    if (filtered.length === 0) {
        const isSearching = searchQuery || currentFilter !== 'all';
        container.innerHTML = `
            <div class="empty-state" style="grid-column: 1 / -1;">
                <div class="empty-icon">
                    <i class="fa-solid ${isSearching ? 'fa-filter-circle-xmark' : 'fa-inbox'}" style="color: var(--muted); font-size: 20px;"></i>
                </div>
                <h3 class="text-sm font-bold mb-1" style="color: var(--ink);">
                    ${isSearching ? 'No matching results' : 'No active records'}
                </h3>
                <p class="text-xs max-w-xs mb-4" style="color: var(--muted);">
                    ${isSearching
                        ? 'Try adjusting your search or filter criteria.'
                        : `Nothing is queued under ${categoryMap[currentCategory]} right now.`}
                </p>
                ${isSearching
                    ? `<button onclick="clearSearch()" class="btn-outline"><i class="fa-solid fa-xmark text-[10px]"></i> Clear Filters</button>`
                    : `<button onclick="openModal()" class="btn-primary"><i class="fa-solid fa-plus text-[10px]"></i> Create ticket</button>`}
            </div>`;
        return;
    }

    filtered.forEach((ticket, idx) => {
        const card = createTicketCard(ticket);
        card.style.opacity = '0';
        card.style.animation = `cardReveal 0.55s cubic-bezier(0.22, 1, 0.36, 1) ${Math.min(idx * 70, 600)}ms forwards`;
        card.addEventListener('mousemove', function(e) {
            var rect = card.getBoundingClientRect();
            card.style.setProperty('--mouse-x', ((e.clientX - rect.left) / rect.width * 100) + '%');
            card.style.setProperty('--mouse-y', ((e.clientY - rect.top) / rect.height * 100) + '%');
        });
        container.appendChild(card);
    });

    updateMetrics();
}

function clearSearch() {
    searchQuery = '';
    currentFilter = 'all';
    const searchInput = document.getElementById('search-input');
    if (searchInput) searchInput.value = '';
    document.querySelectorAll('.filter-chip').forEach(c => c.classList.remove('active'));
    const allChip = document.querySelector('[data-filter="all"]');
    if (allChip) allChip.classList.add('active');
    renderTickets();
}

// =====================================================================
// DYNAMIC DEPARTMENT FILTER CHIPS (fetched from the database)
// =====================================================================
async function loadDepartments() {
    try {
        const res = await authFetch(BACKEND + '/api/departments');
        if (!res.ok) return;
        departmentsData = await res.json();
        renderDeptFilterChips();
    } catch (err) {
        console.error('Failed to load departments:', err);
    }
}

function renderDeptFilterChips() {
    const bars = ['dept-filter-chips', 'users-dept-filter-chips'];
    bars.forEach(id => {
        const bar = document.getElementById(id);
        if (!bar) return;
        const chip = (d) => `
            <button type="button" class="dyn-dept-chip${(d.code || '') === deptFilterCode ? ' active' : ''}"
                    data-dept-code="${esc(d.code || '')}" data-dept-name="${esc(d.name)}"
                    onclick="setDeptFilter(this.dataset.deptCode, this.dataset.deptName, this)">${esc(d.name)}</button>`;
        bar.innerHTML = `
            <button type="button" class="dyn-dept-chip${deptFilterCode === '' ? ' active' : ''}"
                    data-dept-code="" data-dept-name=""
                    onclick="setDeptFilter('', '', this)">All</button>` +
            departmentsData.map(chip).join('');
    });
}

function setDeptFilter(code, name, btn) {
    deptFilterCode = code || '';
    deptFilterName = name || '';
    document.querySelectorAll('.dyn-dept-chip').forEach(c => {
        c.classList.toggle('active', (c.dataset.deptCode || '') === deptFilterCode);
    });
    renderTickets();
    renderUsersView();
}

function createTicketCard(ticket) {
    const card = document.createElement('div');
    const sevClass = `sev-${ticket.severity || 'MEDIUM'}`;
    card.className = `ticket-card ${sevClass}`;

    const status = statusConfig[ticket.status] || statusConfig['OPEN'];
    const sevBadge = severityBadgeClass[ticket.severity] || 'badge-medium';

    // Team avatars with pulse rings
    let teamAvatarsHTML = '';
    if (ticket.active_team && Array.isArray(ticket.active_team) && ticket.active_team.length > 0) {
        teamAvatarsHTML = `<div class="flex items-center">` +
            ticket.active_team.map((url, i) => `
                <div class="avatar-pulse" style="margin-left: ${i > 0 ? '-6px' : '0'}; z-index: ${10 - i};">
                    <img src="${url}" class="w-6 h-6 rounded-full object-cover" style="border: 2px solid var(--surface);" alt="Team member">
                </div>
            `).join('') +
            `</div>`;
    }

    // Time ago (if created_at is available)
    let timeAgo = '';
    if (ticket.created_at) {
        const created = new Date(ticket.created_at);
        const diff = Date.now() - created.getTime();
        const mins = Math.floor(diff / 60000);
        const hours = Math.floor(mins / 60);
        const days = Math.floor(hours / 24);
        if (days > 0) timeAgo = `${days}d ago`;
        else if (hours > 0) timeAgo = `${hours}h ago`;
        else if (mins > 0) timeAgo = `${mins}m ago`;
        else timeAgo = 'Just now';
    }

    card.innerHTML = `
        <!-- Card Header -->
        <div class="flex items-start justify-between mb-3">
            <div class="flex items-center gap-2">
                <span class="mono text-[10px] font-bold px-2 py-0.5 rounded-md"
                      style="background: var(--navy-tint); color: var(--navy);">TCK-${ticket.id}</span>
                <span class="mono text-[10px] font-semibold uppercase tracking-wide"
                      style="color: var(--muted);">${ticket.category ? ticket.category.replace('_MGMT', '').replace('_', ' ') : 'SOC'}</span>
            </div>
            ${timeAgo ? `<span class="text-[10px]" style="color: var(--muted);">${timeAgo}</span>` : ''}
        </div>

        <!-- Title -->
        <h3 class="text-sm font-bold mb-2 leading-snug" style="color: var(--ink);">${ticket.title}</h3>

        <!-- Description -->
        <p class="text-xs leading-relaxed mb-4" style="color: var(--muted); display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;">
            ${ticket.description}
        </p>

        <!-- Reported by (reporter = creator account, auto-recorded server-side) -->
        <div class="flex items-center gap-1.5 mb-3">
            <i class="fa-solid fa-circle-user text-[10px]" style="color: var(--muted);"></i>
            <span class="text-[10px] font-medium" style="color: var(--muted);">Reported by: <span class="text-[11px] font-semibold" style="color: var(--ink);">${esc(ticket.reporter_name || ticket.reporter_email || 'Unknown')}</span></span>
        </div>

        <!-- Badges -->
        <div class="flex flex-wrap gap-2 mb-4">
            <span class="mono text-[10px] font-bold uppercase tracking-wide px-2.5 py-1 rounded-full ${sevBadge}">${ticket.severity || 'MEDIUM'}</span>
            <span class="text-[10px] font-semibold px-2.5 py-1 rounded-full ${status.cls}">${status.label}</span>
        </div>

        <!-- Activity -->
        <div class="flex items-center gap-1.5 mb-4">
            <span class="relative flex h-2 w-2">
                <span class="animate-ping absolute inline-flex h-full w-full rounded-full opacity-75"
                      style="background: ${ticket.status === 'MITIGATED' ? '#22c55e' : '#6366f1'};"></span>
                <span class="relative inline-flex rounded-full h-2 w-2"
                      style="background: ${ticket.status === 'MITIGATED' ? '#22c55e' : '#6366f1'};"></span>
            </span>
            <span class="text-[11px] font-medium" style="color: var(--muted);">${ticket.current_activity || 'Triage'}</span>
        </div>

        <!-- Footer -->
        <div class="flex items-center justify-between pt-3" style="border-top: 1px solid var(--border);">
            <div class="flex items-center gap-2.5">
                <div class="${ticket.status !== 'MITIGATED' ? 'avatar-pulse' : ''}">
                    <img src="${ticket.user_avatar || 'https://images.unsplash.com/photo-1534528741775-53994a69daeb?w=100'}"
                         class="w-7 h-7 rounded-full object-cover" style="border: 2px solid var(--surface);" alt="${ticket.assigned_user || 'Unassigned'}">
                </div>
                <div>
                    <p class="text-[11px] font-semibold" style="color: var(--ink);">${ticket.assigned_user || 'Unassigned'}</p>
                    <p class="text-[9px]" style="color: var(--muted);">Analyst</p>
                </div>
            </div>

            <div class="flex items-center gap-2">
                ${teamAvatarsHTML}
                ${ticket.status === 'UNASSIGNED' ? `
                    <button onclick="event.stopPropagation(); claimTicket(${ticket.id})"
                            class="text-[10px] font-bold px-3 py-1.5 rounded-lg transition-all hover:-translate-y-0.5"
                            style="background: rgba(245,158,11,0.12); color: #f59e0b; border: 1px solid rgba(245,158,11,0.2);">
                        <i class="fa-solid fa-hand-point-up text-[8px] mr-1"></i>Claim Ticket
                    </button>` : ''}
                ${ticket.status === 'OPEN' ? `
                    <button onclick="event.stopPropagation(); updateTicketState(${ticket.id}, 'IN_INVESTIGATION', 'Deep Analysis')"
                            class="text-[10px] font-bold px-3 py-1.5 rounded-lg transition-all hover:-translate-y-0.5"
                            style="background: rgba(59,130,246,0.12); color: #3b82f6; border: 1px solid rgba(59,130,246,0.2);">
                        <i class="fa-solid fa-search text-[8px] mr-1"></i>Investigate
                    </button>` : ''}
                ${ticket.status === 'IN_INVESTIGATION' ? `
                    <button onclick="event.stopPropagation(); updateTicketState(${ticket.id}, 'MITIGATED', 'Remediation Applied')"
                            class="text-[10px] font-bold px-3 py-1.5 rounded-lg transition-all hover:-translate-y-0.5"
                            style="background: rgba(34,197,94,0.12); color: #22c55e; border: 1px solid rgba(34,197,94,0.2);">
                        <i class="fa-solid fa-check text-[8px] mr-1"></i>Resolve
                    </button>` : ''}
            </div>
        </div>
    `;

    card.style.cursor = 'pointer';
    card.addEventListener('click', function () { openTicketDetail(ticket); });

    return card;
}

// =====================================================================
// TICKET ACTIONS
// =====================================================================
async function updateTicketState(ticketId, newStatus, activity) {
    try {
        await authFetch(`${API_URL}/${ticketId}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: newStatus, current_activity: activity })
        });
    } catch (err) {
        console.error("Failed to update ticket state:", err);
    }
}

// =====================================================================
// MODAL
// =====================================================================
function openModal() {
    const modal = document.getElementById('ticket-modal');
    const backdrop = document.getElementById('modal-backdrop');
    const panel = document.getElementById('modal-panel');
    modal.classList.remove('hidden');
    modal.classList.add('flex');
    requestAnimationFrame(() => {
        backdrop.classList.remove('opacity-0');
        panel.style.opacity = '1';
        panel.style.transform = 'translateY(0)';
    });
}

function closeModal() {
    const modal = document.getElementById('ticket-modal');
    const backdrop = document.getElementById('modal-backdrop');
    const panel = document.getElementById('modal-panel');
    backdrop.classList.add('opacity-0');
    panel.style.opacity = '0';
    panel.style.transform = 'translateY(12px)';
    setTimeout(() => {
        modal.classList.add('hidden');
        modal.classList.remove('flex');
        document.getElementById('ticket-form').reset();
    }, 300);
}

async function submitTicket(event) {
    event.preventDefault();

    const payload = {
        title: document.getElementById('field-title').value.trim(),
        description: document.getElementById('field-description').value.trim(),
        severity: document.getElementById('field-severity').value
    };

    try {
        const res = await authFetch(API_URL, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        if (!res.ok) throw new Error(`Server responded ${res.status}`);
        closeModal();
    } catch (err) {
        console.error("Failed to create ticket:", err);
        alert("Couldn't create the ticket. Check the console for details.");
    }
}

async function claimTicket(ticketId) {
    try {
        const res = await authFetch(`${API_URL}/${ticketId}/claim`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || `HTTP ${res.status}`);
        }
        const updated = await res.json();
        const idx = ticketsData.findIndex(t => t.id === ticketId);
        if (idx !== -1) ticketsData[idx] = updated;
        renderTickets();
        updateMetrics();
    } catch (err) {
        console.error("Failed to claim ticket:", err);
        alert(err.message || "Couldn't claim the ticket.");
    }
}

// =====================================================================
// TICKET DETAIL + REAL-TIME CHAT (stage 15)
// =====================================================================
const CHAT_WS_BASE = `${WS_PROTO}://${window.location.host}/api/tickets`;
const chatState = { ticketId: null, afterId: 0, ws: null, polling: null, heartbeat: null, intentionalClose: false };

function getStoredSession() {
    try { return JSON.parse(localStorage.getItem('soc_session') || 'null'); }
    catch (e) { return null; }
}

function escapeHtml(str) {
    return String(str == null ? '' : str)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function chatTime(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    const now = new Date();
    const hm = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    if (d.toDateString() === now.toDateString()) return hm;
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + hm;
}

function appendChatMessage(msg) {
    if (!msg || !msg.id) return;
    if (msg.id <= chatState.afterId || document.getElementById('td-msg-' + msg.id)) return;
    const thread = document.getElementById('td-thread');
    if (!thread) return;
    const session = getStoredSession();
    const mine = session && session.id && (msg.sender_id === session.id || (msg.sender_name && msg.sender_name === session.name));
    const div = document.createElement('div');
    div.id = 'td-msg-' + msg.id;
    div.className = 'flex ' + (mine ? 'justify-end' : 'justify-start');
    const style = mine
        ? "background: linear-gradient(135deg, rgba(99,102,241,0.16), rgba(99,102,241,0.08)); border: 1px solid rgba(99,102,241,0.22);"
        : "background: var(--surface-hover); border: 1px solid var(--border-subtle);";
    div.innerHTML = `
        <div class="max-w-[78%] px-3.5 py-2.5 rounded-2xl ${mine ? 'rounded-br-md' : 'rounded-bl-md'}" style="${style}">
            <div class="flex items-baseline justify-between gap-3 mb-1">
                <span class="text-[10px] font-bold" style="color: ${mine ? '#6366f1' : 'var(--muted)'};">${escapeHtml(msg.sender_name || 'Unknown')}</span>
                <span class="text-[9px]" style="color: var(--muted);">${chatTime(msg.created_at)}</span>
            </div>
            <p class="text-xs leading-relaxed" style="color: var(--ink); white-space: pre-wrap; word-break: break-word;">${escapeHtml(msg.message)}</p>
        </div>`;
    thread.appendChild(div);
    chatState.afterId = Math.max(chatState.afterId, msg.id);
    thread.scrollTop = thread.scrollHeight;
}

async function loadChatHistory(ticketId) {
    const thread = document.getElementById('td-thread');
    try {
        const res = await authFetch(`${API_URL}/${ticketId}/chat`);
        if (!res.ok) throw new Error('Failed to load chat');
        const messages = await res.json();
        if (thread) thread.innerHTML = '';
        if (messages && messages.length > 0) {
            messages.forEach(appendChatMessage);
        } else if (thread) {
            thread.innerHTML = '<div class="text-xs text-center py-8" style="color: var(--muted);">No messages yet. Start the conversation.</div>';
        }
        if (thread) thread.scrollTop = thread.scrollHeight;
    } catch (err) {
        console.error('chat load:', err);
        if (thread) thread.innerHTML = '<div class="text-xs text-center py-8" style="color: var(--muted);">Could not load conversation.</div>';
    }
}

function stopChatPolling() {
    if (chatState.polling) { clearInterval(chatState.polling); chatState.polling = null; }
}

function stopChatHeartbeat() {
    if (chatState.heartbeat) { clearInterval(chatState.heartbeat); chatState.heartbeat = null; }
}

function startPollFallback(ticketId) {
    if (chatState.polling || chatState.intentionalClose) return;
    stopChatPolling();
    chatState.polling = setInterval(async () => {
        if (chatState.intentionalClose || chatState.ticketId !== ticketId) { stopChatPolling(); return; }
        try {
            const res = await authFetch(`${API_URL}/${ticketId}/chat?after_id=${chatState.afterId || 0}`);
            if (!res.ok) return;
            const messages = await res.json();
            if (messages && messages.length > 0) messages.forEach(appendChatMessage);
        } catch (e) { /* keep polling */ }
    }, 4000);
}

function connectChatWs(ticketId) {
    const session = getStoredSession();
    if (!session || !session.token) return;
    stopChatPolling();
    if (chatState.ws) { try { chatState.ws.close(); } catch (e) {} }
    chatState.ws = null;
    chatState.intentionalClose = false;
    try {
        const ws = new WebSocket(`${CHAT_WS_BASE}/${ticketId}/chat/ws`);
        chatState.ws = ws;
        ws.onopen = function () {
            stopChatPolling();
            ws.send(JSON.stringify({ token: session.token }));
        };
        ws.onerror = function () { startPollFallback(ticketId); };
        ws.onclose = function () {
            chatState.ws = null;
            stopChatHeartbeat();
            if (chatState.ticketId === ticketId && !chatState.intentionalClose) startPollFallback(ticketId);
        };
        ws.onmessage = function (evt) {
            try {
                const msg = JSON.parse(evt.data);
                if (msg.type === 'chat_message' && msg.data) appendChatMessage(msg.data);
            } catch (e) {}
        };
        stopChatHeartbeat();
        chatState.heartbeat = setInterval(function () {
            if (chatState.ws && chatState.ws.readyState === WebSocket.OPEN) {
                chatState.ws.send('ping');
            }
        }, 2500);
    } catch (e) {
        startPollFallback(ticketId);
    }
}

async function sendChatMessage(event) {
    event.preventDefault();
    const input = document.getElementById('td-message-input');
    const txt = (input.value || '').trim();
    if (!chatState.ticketId || !txt) return;
    input.value = '';
    try {
        const res = await authFetch(`${API_URL}/${chatState.ticketId}/chat`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message: txt })
        });
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to send message');
        }
        const msg = await res.json();
        appendChatMessage(msg);
    } catch (err) {
        input.value = txt;
        if (typeof showToast === 'function') showToast(err.message, 'error');
        else alert(err.message);
    }
}

function openTicketDetail(ticket) {
    if (!ticket) return;
    chatState.ticketId = ticket.id;
    chatState.afterId = 0;
    chatState.intentionalClose = false;
    const modal = document.getElementById('ticket-detail-modal');
    const backdrop = document.getElementById('td-backdrop');
    const panel = document.getElementById('td-panel');
    document.getElementById('td-ticket-id').innerText = 'TCK-' + ticket.id;
    document.getElementById('td-ticket-title').innerText = ticket.title || '';
    const status = statusConfig[ticket.status] || statusConfig['OPEN'] || { label: ticket.status || 'OPEN', cls: 'badge-medium' };
    const st = document.getElementById('td-status');
    st.innerText = status.label || ticket.status || 'OPEN';
    st.className = 'mono text-[10px] font-bold px-2.5 py-1 rounded-full ' + (status.cls || 'badge-medium');
    document.getElementById('td-category').innerText = (ticket.category || 'SOC').replace('_MGMT', '').replace('_', ' ');
    function safeSet(id, value) {
        const el = document.getElementById(id);
        if (el) el.textContent = value;
    }
    const sevEl = document.getElementById('td-severity');
    if (sevEl) {
        sevEl.className = 'mono text-[10px] font-bold px-2.5 py-1 rounded-full ' + (severityBadgeClass[ticket.severity] || 'badge-medium');
        sevEl.textContent = ticket.severity || 'MEDIUM';
    }
    safeSet('td-reporter', ticket.reporter_name || ticket.reporter_email || 'Unknown');
    safeSet('td-reporter-contact', ticket.reporter_email || '');
    safeSet('td-reporter-dept', ticket.reporter_department_name || '—');
    safeSet('td-assigned', ticket.assigned_user || 'Unassigned');
    safeSet('td-created', ticket.created_at ? new Date(ticket.created_at).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : '—');
    safeSet('td-updated', ticket.updated_at ? new Date(ticket.updated_at).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : '—');
    safeSet('td-description', ticket.description || 'No description provided.');
    const wfEl = document.getElementById('td-workflow');
    if (wfEl) {
        const stepLabels = {
            UNASSIGNED: 'Unassigned', OPEN: 'Open', IN_INVESTIGATION: 'Investigating',
            MITIGATED: 'Mitigated', CLOSED: 'Closed'
        };
        const steps = ['UNASSIGNED', 'OPEN', 'IN_INVESTIGATION', 'MITIGATED', 'CLOSED'];
        let activeIdx = steps.indexOf(ticket.status || 'OPEN');
        if (activeIdx === -1) activeIdx = steps.indexOf('OPEN');
        wfEl.innerHTML = steps.map((s, i) => {
            const cls = i < activeIdx ? 'wf-done' : (i === activeIdx ? 'wf-active' : 'wf-pending');
            return `<div class="td-workflow-step ${cls}"><span class="td-wf-dot"></span><span>${stepLabels[s] || s}</span></div>`;
        }).join('<div class="td-wf-connector"></div>');
    }
    const input = document.getElementById('td-message-input');
    if (input) input.value = '';
    const thread = document.getElementById('td-thread');
    if (thread) thread.innerHTML = '<div class="text-xs text-center py-6" style="color: var(--muted);">Loading conversation...</div>';
    modal.classList.remove('hidden');
    modal.classList.add('flex');
    requestAnimationFrame(function () {
        if (backdrop) backdrop.classList.remove('opacity-0');
        if (panel) { panel.style.opacity = '1'; panel.style.transform = 'translateY(0)'; }
    });
    loadChatHistory(ticket.id);
    const t = setTimeout(function () { connectChatWs(ticket.id); clearTimeout(t); }, 300);
}

function closeTicketDetail() {
    chatState.intentionalClose = true;
    stopChatPolling();
    stopChatHeartbeat();
    if (chatState.ws) { try { chatState.ws.close(); } catch (e) {} }
    chatState.ws = null;
    chatState.ticketId = null;
    const modal = document.getElementById('ticket-detail-modal');
    const backdrop = document.getElementById('td-backdrop');
    const panel = document.getElementById('td-panel');
    if (backdrop) backdrop.classList.add('opacity-0');
    if (panel) { panel.style.opacity = '0'; panel.style.transform = 'translateY(12px)'; }
    setTimeout(function () {
        modal.classList.add('hidden');
        modal.classList.remove('flex');
    }, 300);
}

// =====================================================================
// AUTHENTICATION GATEWAY
// =====================================================================
let currentAuthMode = 'login';

// =====================================================================
// SINGLE NEON TRACE LINE CANVAS ENGINE
// =====================================================================
(function initNeonTrace() {
    var canvas = document.getElementById('neon-tubes-canvas');
    if (!canvas) return;
    var ctx = canvas.getContext('2d');
    var W, H;
    var mouse = { x: 0, y: 0, tx: 0, ty: 0 };
    var trail = [];
    var TRAIL_LEN = 50;
    var time = 0;
    var hue = 260;

    function resize() {
        var panel = document.getElementById('neon-left-panel');
        if (!panel) return;
        W = canvas.width = panel.clientWidth;
        H = canvas.height = panel.clientHeight;
        trail = [];
        for (var i = 0; i < TRAIL_LEN; i++) {
            trail.push({ x: W / 2, y: H / 2 });
        }
    }

    function draw() {
        ctx.fillStyle = 'rgba(247,247,247,0.12)';
        ctx.fillRect(0, 0, W, H);

        time += 0.02;
        hue = (hue + 0.3) % 360;

        // Fast cursor tracking (lerp 0.45)
        mouse.x += (mouse.tx - mouse.x) * 0.45;
        mouse.y += (mouse.ty - mouse.y) * 0.45;

        // Head follows mouse with slight organic drift
        var head = trail[0];
        head.x = mouse.x + Math.sin(time * 2) * 8;
        head.y = mouse.y + Math.cos(time * 2.3) * 6;

        // Propagate trail with tight follow
        for (var i = trail.length - 1; i > 0; i--) {
            var p = trail[i];
            var prev = trail[i - 1];
            p.x += (prev.x - p.x) * 0.4;
            p.y += (prev.y - p.y) * 0.4;
        }

        // Outer glow layer
        ctx.save();
        ctx.shadowColor = 'hsl(' + hue + ', 80%, 60%)';
        ctx.shadowBlur = 30;
        ctx.strokeStyle = 'hsl(' + hue + ', 80%, 60%)';
        ctx.lineWidth = 4;
        ctx.lineCap = 'round';
        ctx.lineJoin = 'round';
        ctx.globalAlpha = 0.8;
        ctx.beginPath();
        ctx.moveTo(trail[0].x, trail[0].y);
        for (var i = 1; i < trail.length; i++) {
            var xc = (trail[i].x + trail[i - 1].x) / 2;
            var yc = (trail[i].y + trail[i - 1].y) / 2;
            ctx.quadraticCurveTo(trail[i - 1].x, trail[i - 1].y, xc, yc);
        }
        ctx.stroke();

        // Inner white core
        ctx.shadowBlur = 8;
        ctx.strokeStyle = 'rgba(255,255,255,0.85)';
        ctx.lineWidth = 1.5;
        ctx.globalAlpha = 1;
        ctx.stroke();
        ctx.restore();

        // Bright head dot
        ctx.save();
        ctx.shadowColor = 'hsl(' + hue + ', 90%, 70%)';
        ctx.shadowBlur = 18;
        ctx.fillStyle = '#fff';
        ctx.globalAlpha = 0.95;
        ctx.beginPath();
        ctx.arc(trail[0].x, trail[0].y, 4, 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();

        requestAnimationFrame(draw);
    }

    window.addEventListener('mousemove', function(e) {
        var panel = document.getElementById('neon-left-panel');
        if (!panel) return;
        var rect = panel.getBoundingClientRect();
        mouse.tx = e.clientX - rect.left;
        mouse.ty = e.clientY - rect.top;
    });

    canvas.addEventListener('click', function() {
        hue = Math.random() * 360;
    });

    window.addEventListener('resize', resize);
    resize();
    draw();
})();

// ================================================================
// STAGE 9: Password Complexity Validation & Strength Indicator
// ================================================================
function validatePasswordFrontend(password) {
    const errors = [];
    if (password.length < 12) errors.push('at least 12 characters');
    if (!/[A-Z]/.test(password)) errors.push('an uppercase letter');
    if (!/[a-z]/.test(password)) errors.push('a lowercase letter');
    if (!/\d/.test(password)) errors.push('a digit');
    if (!/[!@#$%^&*()_+\-=\[\]{}|;:,.<>?/~`]/.test(password)) errors.push('a special character');
    return errors;
}

function getPasswordStrength(password) {
    if (!password) return { score: 0, label: '', color: '' };
    let score = 0;
    if (password.length >= 12) score++;
    if (password.length >= 16) score++;
    if (/[A-Z]/.test(password) && /[a-z]/.test(password)) score++;
    if (/\d/.test(password)) score++;
    if (/[!@#$%^&*()_+\-=\[\]{}|;:,.<>?/~`]/.test(password)) score++;
    const labels = ['', 'Weak', 'Fair', 'Good', 'Strong', 'Very Strong'];
    const colors = ['', '#ef4444', '#f59e0b', '#3b82f6', '#22c55e', '#10b981'];
    return { score, label: labels[score] || '', color: colors[score] || '' };
}

function updatePasswordStrength(password) {
    const bar = document.getElementById('pw-strength-bar');
    const text = document.getElementById('pw-strength-text');
    if (!bar || !text) return;
    if (!password) { bar.style.display = 'none'; text.classList.add('hidden'); return; }
    bar.style.display = 'flex';
    text.classList.remove('hidden');
    const { score, label, color } = getPasswordStrength(password);
    const segs = bar.querySelectorAll('.pw-seg');
    segs.forEach((seg, i) => {
        seg.style.background = i < score ? color : '';
    });
    text.textContent = password.length < 12 ? `${label} — need ${12 - password.length} more chars` : label;
    text.style.color = color;
}

// First-time login setup mode
let firstTimeSetupMode = false;
let firstTimeSetupEmail = '';

function enterFirstTimeSetup(email) {
    firstTimeSetupMode = true;
    firstTimeSetupEmail = email;
    var heading = document.querySelector('#auth-form').closest('.space-y-8').querySelector('h3');
    var subtext = document.querySelector('#auth-form').closest('.space-y-8').querySelector('p');
    var submitBtn = document.getElementById('auth-submit-btn');
    var emailInput = document.getElementById('auth-email');
    if (heading) heading.innerText = 'Set Your Password';
    if (subtext) subtext.innerText = 'Create a strong password (min 12 chars, uppercase, lowercase, digit, special char).';
    if (submitBtn) submitBtn.querySelector('span').innerText = 'Set Password & Sign In';
    if (emailInput) { emailInput.value = email; emailInput.readOnly = true; emailInput.style.opacity = '0.6'; emailInput.style.background = '#F3F4F6'; }
    var pwInput = document.getElementById('auth-password');
    if (pwInput) { pwInput.value = ''; pwInput.focus(); }
    var strengthBar = document.getElementById('pw-strength-bar');
    if (strengthBar) strengthBar.style.display = 'flex';
    document.getElementById('auth-form').setAttribute('data-mode', 'setup');
}

function exitFirstTimeSetup() {
    firstTimeSetupMode = false;
    firstTimeSetupEmail = '';
    var emailInput = document.getElementById('auth-email');
    if (emailInput) { emailInput.readOnly = false; emailInput.style.opacity = '1'; emailInput.style.background = ''; }
    var heading = document.querySelector('#auth-form').closest('.space-y-8').querySelector('h3');
    var subtext = document.querySelector('#auth-form').closest('.space-y-8').querySelector('p');
    var submitBtn = document.getElementById('auth-submit-btn');
    if (heading) heading.innerText = 'Welcome back!';
    if (subtext) subtext.innerText = 'Please enter your details to stay connected.';
    if (submitBtn) submitBtn.querySelector('span').innerText = 'Request Access';
    var strengthBar = document.getElementById('pw-strength-bar');
    if (strengthBar) strengthBar.style.display = 'none';
    document.getElementById('auth-form').removeAttribute('data-mode');
}

// Build + persist the session from a login/token-login/refresh response
function completeSession(data) {
    const u = data.user;
    const initials = u.full_name ? u.full_name.split(' ').filter(Boolean).map(function(w){ return w[0]; }).join('').substring(0, 2).toUpperCase() : 'U';
    const userSession = {
        token: data.access_token,
        id: u.id,
        name: u.full_name,
        email: u.email,
        initials: initials,
        avatar: u.avatar || '',
        role_name: u.role_name || '',
        role_rank: u.role_rank || (u.role_name === 'Owner / Super Admin' ? 100 : 10),
        department_name: u.department_name || '',
        role_id: u.role_id,
        department_id: u.department_id,
        permissions: u.permissions || [],
        expires_at: data.expires_at || Math.floor(Date.now() / 1000) + 10800,
        temp: !!data.temp_access,
    };
    localStorage.setItem('soc_session', JSON.stringify(userSession));
    applyUserSession(userSession);
    showMainDashboard();
    startAutoTokenRefresh();
}

function togglePasswordVisibility() {
    var pwInput = document.getElementById('auth-password');
    var icon = document.getElementById('pw-eye-icon');
    if (!pwInput || !icon) return;
    if (pwInput.type === 'password') {
        pwInput.type = 'text';
        icon.className = 'fa-solid fa-eye-slash text-sm';
    } else {
        pwInput.type = 'password';
        icon.className = 'fa-solid fa-eye text-sm';
    }
}

// Handle Form Submission
async function handleAuthSubmit(event) {
    event.preventDefault();
    const email = document.getElementById('auth-email').value.trim();
    const password = document.getElementById('auth-password').value;
    const submitBtn = document.getElementById('auth-submit-btn');

    // First-time setup mode
    if (firstTimeSetupMode) {
        const pwErrors = validatePasswordFrontend(password);
        if (pwErrors.length > 0) {
            alert('Password must include: ' + pwErrors.join(', '));
            return;
        }
        submitBtn.disabled = true;
        submitBtn.querySelector('span').innerText = 'Setting password...';
        try {
            const setupRes = await fetch(BACKEND + '/api/auth/setup-password', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ email: firstTimeSetupEmail, password: password })
            });
            if (!setupRes.ok) {
                const err = await setupRes.json();
                throw new Error(err.detail || 'Failed to set password');
            }
            firstTimeSetupMode = false;
            firstTimeSetupEmail = '';
            var emailInput = document.getElementById('auth-email');
            if (emailInput) { emailInput.readOnly = false; emailInput.style.opacity = '1'; emailInput.style.background = ''; }
            exitFirstTimeSetup();
            document.getElementById('auth-email').value = email;
            document.getElementById('auth-password').value = password;
            submitBtn.querySelector('span').innerText = 'Authenticating...';
            const loginRes = await fetch(LOGIN_WEBHOOK, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username: email, password: password })
            });
            if (!loginRes.ok) throw new Error('Login failed after password setup');
            const loginData = await loginRes.json();
            completeSession(loginData);
        } catch (err) {
            alert(err.message);
        } finally {
            submitBtn.disabled = false;
            submitBtn.querySelector('span').innerText = 'Set Password & Sign In';
        }
        return;
    }

    // Passwordless token: if the "password" field holds a magic-login JWT,
    // consume it via token-login (no UI structure changes)
    if (/^eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(password)) {
        submitBtn.disabled = true;
        submitBtn.querySelector('span').innerText = 'Verifying token...';
        try {
            const res = await fetch(BACKEND + '/api/auth/token-login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ token: password })
            });
            if (!res.ok) {
                const err = await res.json();
                throw new Error(err.detail || 'Token login failed');
            }
            const data = await res.json();
            if (data.requires_password_setup) {
                enterFirstTimeSetup(data.email);
                submitBtn.disabled = false;
                submitBtn.querySelector('span').innerText = 'Set Password & Sign In';
                return;
            }
            completeSession(data);
        } catch (err) {
            alert(err.message);
        } finally {
            submitBtn.disabled = false;
            submitBtn.querySelector('span').innerText = 'Request Access';
        }
        return;
    }

    // Normal Login
    submitBtn.disabled = true;
    submitBtn.querySelector('span').innerText = 'Authenticating...';

    try {
        const res = await fetch(LOGIN_WEBHOOK, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username: email, password: password })
        });
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Login failed');
        }
        const data = await res.json();

        // Owner first-login — show inline password setup
        if (data.requires_password_setup) {
            enterFirstTimeSetup(data.email);
            submitBtn.disabled = false;
            submitBtn.querySelector('span').innerText = 'Set Password & Sign In';
            return;
        }

        completeSession(data);
    } catch (err) {
        alert(err.message);
    } finally {
        submitBtn.disabled = false;
        submitBtn.querySelector('span').innerText = 'Request Access';
    }
}

// Session Verification & Gateway Behavior
async function checkAuthSession() {
    const sessionStr = localStorage.getItem('soc_session');
    if (!sessionStr) { showAuthGateway(); return null; }
    try {
        const session = JSON.parse(sessionStr);
        if (!session.token) { localStorage.removeItem('soc_session'); showAuthGateway(); return null; }

        const res = await authFetch(`${BACKEND}/api/auth/me`);
        if (!res.ok) { localStorage.removeItem('soc_session'); showAuthGateway(); return null; }

        const u = await res.json();
        const initials = u.full_name ? u.full_name.split(' ').filter(Boolean).map(w => w[0]).join('').substring(0, 2).toUpperCase() : 'U';

        const perms = (session.permissions || []).concat(u.permissions || []);
        const mergedPerms = [...new Set(perms)];
        const updated = {
            ...session,
            name: u.full_name,
            email: u.email,
            initials,
            avatar: u.avatar || session.avatar || '',
            role_name: u.role_name || '',
            department_name: u.department_name || '',
            role_id: u.role_id,
            department_id: u.department_id,
            permissions: mergedPerms,
            last_verified_at: Math.floor(Date.now() / 1000),
        };
        localStorage.setItem('soc_session', JSON.stringify(updated));
        applyUserSession(updated);
        startAutoTokenRefresh();
        showMainDashboard();
        return updated;
    } catch (err) {
        localStorage.removeItem('soc_session');
        showAuthGateway();
        return null;
    }
}

function loadProfileFromDB(email) {
    if (!email) return;
    authFetch(`${BACKEND}/api/user/profile/${encodeURIComponent(email)}`)
        .then(res => { if (!res.ok) throw new Error(); return res.json(); })
        .then(profile => {
            const session = JSON.parse(localStorage.getItem('soc_session') || '{}');
            const merged = { ...session, name: profile.name || session.name, avatar: profile.avatar || session.avatar };
            localStorage.setItem('soc_session', JSON.stringify(merged));
            applyUserSession(merged);
        })
        .catch(() => {});
}

function showAuthGateway() {
    const gateway = document.getElementById('auth-gateway');
    const dashboard = document.getElementById('main-dashboard-app');
    if (gateway) gateway.classList.remove('hidden');
    if (dashboard) dashboard.classList.add('hidden');
}

function showMainDashboard() {
    const gateway = document.getElementById('auth-gateway');
    const dashboard = document.getElementById('main-dashboard-app');
    if (gateway) gateway.classList.add('hidden');
    if (dashboard) dashboard.classList.remove('hidden');

    fetchTickets();
    connectWebSocket();
    loadDepartments();
}

// User Profile Integration
function applyUserSession(user) {
    if (!user) return;
    const initialsEl = document.getElementById('nav-avatar-initials');
    const avatarImg = document.getElementById('nav-avatar-img');
    const nameEl = document.getElementById('user-chip-name');
    const emailEl = document.getElementById('user-chip-email');
    const roleEl = document.getElementById('user-menu-role');
    const deptEl = document.getElementById('user-menu-dept');

    const roleLabel = user.role_name || user.role || '';
    const deptLabel = user.department_name || user.dept || '';

    if (initialsEl) initialsEl.innerText = user.initials || 'U';
    if (nameEl) nameEl.innerText = user.name || 'User';
    if (emailEl) emailEl.innerText = user.email || '';
    if (roleEl) roleEl.innerText = roleLabel;
    if (deptEl) deptEl.innerText = deptLabel;

    if (avatarImg && initialsEl) {
        if (user.avatar) {
            avatarImg.src = user.avatar;
            avatarImg.classList.remove('hidden');
            initialsEl.classList.add('hidden');
        } else {
            avatarImg.classList.add('hidden');
            initialsEl.classList.remove('hidden');
        }
    }

    // Stage 8: Show/hide IAM nav tab based on permissions
    const iamBtn = document.getElementById('cat-IAM');
    if (iamBtn) {
        const isOwner = user.is_owner;
        const perms = user.permissions || [];
        iamBtn.style.display = (isOwner || perms.includes('users.view')) ? '' : 'none';
    }

    // Directory tab is available to every authenticated domain user
    const dirBtn = document.getElementById('cat-USERS');
    if (dirBtn) dirBtn.style.display = '';
    if (window.updateQueueScopeUI) updateQueueScopeUI();
}

function toggleUserMenu(event) {
    event.stopPropagation();
    const menu = document.getElementById('user-kebab-menu');
    if (menu) menu.classList.toggle('hidden');
}

function logoutUser() {
    try {
        authFetch(`${BACKEND}/api/auth/logout`, { method: 'POST' }).catch(() => {});
    } catch (e) { /* ignore */ }
    try { localStorage.removeItem('soc_session'); } catch (e) {}
    try { sessionStorage.clear(); } catch (e) {}
    try { closeTicketDetail(); } catch (e) {}
    try { stopAutoTokenRefresh(); } catch (e) {}
    try { exitFirstTimeSetup(); } catch (e) {}
    const menu = document.getElementById('user-kebab-menu');
    if (menu) menu.classList.add('hidden');
    const pwBar = document.getElementById('pw-strength-bar');
    const pwText = document.getElementById('pw-strength-text');
    if (pwBar) pwBar.style.display = 'none';
    if (pwText) pwText.classList.add('hidden');
    try { history.replaceState({}, '', '/login'); } catch (e) {}
    showAuthGateway();
}

// =====================================================================
// STAGE 14: AUTOMATIC JWT REFRESH
// Refreshes the 3-hour token ~15 minutes before it expires so active
// sessions never get interrupted. Re-schedules after each refresh.
// =====================================================================
const TOKEN_TTL_SEC = 3 * 60 * 60;
const REFRESH_BACKOFF_SEC = 15 * 60;

function startAutoTokenRefresh() {
    if (autoRefreshTimer) { clearTimeout(autoRefreshTimer); autoRefreshTimer = null; }
    const now = Date.now() / 1000;
    let waitSec = TOKEN_TTL_SEC - REFRESH_BACKOFF_SEC;
    try {
        const session = JSON.parse(localStorage.getItem('soc_session') || '{}');
        if (session.temp) return; // temporary sessions expire after 30 min; never auto-refresh
        if (session.expires_at) {
            waitSec = session.expires_at - now - REFRESH_BACKOFF_SEC;
        }
    } catch (e) { /* fall back to default */ }
    if (!isFinite(waitSec) || waitSec < 60) waitSec = TOKEN_TTL_SEC - REFRESH_BACKOFF_SEC;
    autoRefreshTimer = setTimeout(doAutoTokenRefresh, waitSec * 1000);
}

async function doAutoTokenRefresh() {
    if (!getToken()) { startAutoTokenRefresh(); return; }
    try {
        const res = await authFetch(`${BACKEND}/api/auth/refresh`, { method: 'POST' });
        if (res.ok) {
            const data = await res.json();
            const u = data.user || {};
            const session = JSON.parse(localStorage.getItem('soc_session') || '{}');
            const initials = u.full_name
                ? u.full_name.split(' ').filter(Boolean).map(w => w[0]).join('').substring(0, 2).toUpperCase()
                : (session.initials || 'U');
            const updated = {
                ...session,
                token: data.access_token,
                id: (u.id !== undefined ? u.id : session.id),
                name: u.full_name || session.name || 'User',
                email: u.email || session.email || '',
                initials,
                avatar: u.avatar || session.avatar || '',
                role_name: u.role_name || session.role_name || '',
                role_rank: (u.role_rank !== undefined ? u.role_rank : session.role_rank),
                department_name: u.department_name || session.department_name || '',
                role_id: (u.role_id !== undefined ? u.role_id : session.role_id),
                department_id: (u.department_id !== undefined ? u.department_id : session.department_id),
                permissions: u.permissions || session.permissions || [],
                expires_at: data.expires_at || (Math.floor(Date.now() / 1000) + TOKEN_TTL_SEC),
                last_refreshed_at: Math.floor(Date.now() / 1000),
            };
            localStorage.setItem('soc_session', JSON.stringify(updated));
            applyUserSession(updated);
        } else if (res.status === 401) {
            // Session truly invalid/expired — return to login
            stopAutoTokenRefresh();
            localStorage.removeItem('soc_session');
            showAuthGateway();
            return;
        }
    } catch (e) {
        // Network hiccup — keep retrying on the next cycle
    }
    startAutoTokenRefresh();
}

function stopAutoTokenRefresh() {
    if (autoRefreshTimer) { clearTimeout(autoRefreshTimer); autoRefreshTimer = null; }
}

// =====================================================================
// PROFILE SETTINGS MODAL
// =====================================================================
function openProfileSettings() {
    const menu = document.getElementById('user-kebab-menu');
    if (menu) menu.classList.add('hidden');

    const session = JSON.parse(localStorage.getItem('soc_session') || '{}');

    const nameInput = document.getElementById('profile-name');
    const emailInput = document.getElementById('profile-email');
    const roleInput = document.getElementById('profile-role');
    const deptInput = document.getElementById('profile-dept');
    const displayName = document.getElementById('profile-display-name');
    const displayRole = document.getElementById('profile-display-role');
    const avatarInitials = document.getElementById('profile-avatar-initials');
    const avatarImg = document.getElementById('profile-avatar-img');

    if (nameInput) nameInput.value = session.name || '';
    if (emailInput) emailInput.value = session.email || '';
    if (roleInput) roleInput.value = session.role_name || session.role || '';
    if (deptInput) deptInput.value = session.department_name || session.dept || '';
    if (displayName) displayName.textContent = session.name || 'User';
    if (displayRole) displayRole.textContent = session.role_name || session.role || 'Role';

    if (avatarInitials && avatarImg) {
        if (session.avatar) {
            avatarImg.src = session.avatar;
            avatarImg.classList.remove('hidden');
            avatarImg.style.display = 'block';
            avatarInitials.classList.add('hidden');
            avatarInitials.style.display = 'none';
        } else {
            avatarImg.classList.add('hidden');
            avatarImg.style.display = 'none';
            avatarInitials.classList.remove('hidden');
            avatarInitials.style.display = '';
            avatarInitials.textContent = session.initials || 'U';
        }
    }

    const modal = document.getElementById('profile-settings-modal');
    const backdrop = document.getElementById('profile-backdrop');
    const panel = document.getElementById('profile-panel');
    if (!modal || !backdrop || !panel) return;
    modal.classList.remove('hidden');
    modal.classList.add('flex');
    panel.classList.remove('profile-modal-out');
    panel.classList.add('profile-modal-animate');
    panel.style.opacity = '1';
    panel.style.transform = 'translateY(0)';
    requestAnimationFrame(() => {
        backdrop.classList.remove('opacity-0');
    });
}

function closeProfileSettings() {
    const modal = document.getElementById('profile-settings-modal');
    const backdrop = document.getElementById('profile-backdrop');
    const panel = document.getElementById('profile-panel');
    if (!modal || !backdrop || !panel) return;
    backdrop.classList.add('opacity-0');
    panel.classList.remove('profile-modal-animate');
    panel.classList.add('profile-modal-out');
    setTimeout(() => {
        modal.classList.add('hidden');
        modal.classList.remove('flex');
        panel.classList.remove('profile-modal-out');
        panel.style.opacity = '0';
        panel.style.transform = 'translateY(20px)';
    }, 300);
}

async function saveProfileSettings(event) {
    event.preventDefault();
    const btn = document.getElementById('profile-save-btn');
    const nameInput = document.getElementById('profile-name');
    const emailInput = document.getElementById('profile-email');
    if (!btn || !nameInput || !emailInput) return;

    btn.disabled = true;
    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin text-[10px]"></i> Saving...';

    const newName = nameInput.value.trim();
    const newEmail = emailInput.value.trim();

    const session = JSON.parse(localStorage.getItem('soc_session') || '{}');
    const avatarImg = document.getElementById('profile-avatar-img');
    const avatarData = (avatarImg && !avatarImg.classList.contains('hidden')) ? avatarImg.src : (session.avatar || '');

    const payload = {
        name: newName,
        email: newEmail,
        role: session.role_name || session.role || '',
        dept: session.department_name || session.dept || '',
        avatar: avatarData
    };

    try {
        const res = await authFetch(PROFILE_API, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        if (!res.ok) throw new Error(`Server responded ${res.status}`);

        const parts = newName.split(' ').filter(Boolean);
        const initials = parts.length > 1
            ? (parts[0][0] + parts[parts.length - 1][0]).toUpperCase()
            : newName.substring(0, 2).toUpperCase();

        const updatedSession = { ...session, name: newName, email: newEmail, initials, avatar: avatarData };
        localStorage.setItem('soc_session', JSON.stringify(updatedSession));
        applyUserSession(updatedSession);

        closeProfileSettings();
    } catch (err) {
        console.error("Failed to save profile:", err);
        alert("Couldn't save profile. Check the console for details.");
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="fa-solid fa-check text-[10px]"></i> Save Changes';
    }
}

// =====================================================================
// AVATAR UPLOAD / REMOVE
// =====================================================================
async function handleAvatarUpload(event) {
    const file = event.target.files[0];
    if (!file) return;
    if (file.size > 2 * 1024 * 1024) {
        alert('Image must be under 2 MB.');
        event.target.value = '';
        return;
    }
    const allowedTypes = ['image/jpeg', 'image/png', 'image/webp', 'image/gif'];
    if (!allowedTypes.includes(file.type)) {
        alert('Only JPG, PNG, WEBP or GIF images are allowed.');
        event.target.value = '';
        return;
    }

    const formData = new FormData();
    formData.append('file', file);
    try {
        const res = await authFetch(`${BACKEND}/api/user/avatar`, {
            method: 'POST',
            body: formData
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || 'Upload failed');
        }
        const data = await res.json();
        const url = data.avatar;
        const avatarImg = document.getElementById('profile-avatar-img');
        const avatarInitials = document.getElementById('profile-avatar-initials');
        if (avatarImg) {
            avatarImg.src = url;
            avatarImg.classList.remove('hidden');
            avatarImg.style.display = 'block';
        }
        if (avatarInitials) {
            avatarInitials.classList.add('hidden');
            avatarInitials.style.display = 'none';
        }
        const ring = document.getElementById('profile-avatar-ring');
        if (ring) {
            ring.classList.remove('avatar-pop-animate');
            void ring.offsetWidth;
            ring.classList.add('avatar-pop-animate');
        }
        try {
            const session = JSON.parse(localStorage.getItem('soc_session') || '{}');
            session.avatar = url;
            localStorage.setItem('soc_session', JSON.stringify(session));
            applyUserSession(session);
        } catch (e) { /* ignore */ }
    } catch (err) {
        console.error("Failed to upload avatar:", err);
        alert(err.message || "Couldn't upload the avatar.");
    } finally {
        event.target.value = '';
    }
}

function removeAvatar() {
    const avatarImg = document.getElementById('profile-avatar-img');
    const avatarInitials = document.getElementById('profile-avatar-initials');
    const fileInput = document.getElementById('avatar-file-input');
    if (avatarImg) {
        avatarImg.classList.add('hidden');
        avatarImg.style.display = 'none';
        avatarImg.src = '';
    }
    if (avatarInitials) {
        const session = JSON.parse(localStorage.getItem('soc_session') || '{}');
        avatarInitials.textContent = session.initials || 'U';
        avatarInitials.classList.remove('hidden');
        avatarInitials.style.display = '';
    }
    if (fileInput) fileInput.value = '';
}

document.addEventListener('click', (e) => {
    const menu = document.getElementById('user-kebab-menu');
    if (!menu || menu.classList.contains('hidden')) return;
    const chip = document.querySelector('.topnav-chip');
    if (chip && chip.contains(e.target)) return;
    const isClickInside = menu.contains(e.target);
    if (!isClickInside) menu.classList.add('hidden');
});

// =====================================================================
// STAGE 8: IAM — USER & ACCESS MANAGEMENT VIEW
// =====================================================================
let iamUsersData = [];
let iamRolesData = [];
let iamDeptsData = [];
let iamPermDefs = [];
let iamEditingUserId = null;
let iamDeptFilter = { code: '', name: '' };

async function renderIAMView() {
    const session = JSON.parse(localStorage.getItem('soc_session') || '{}');
    const perms = session.permissions || [];
    const iamBtn = document.getElementById('cat-IAM');
    if (iamBtn) {
        iamBtn.style.display = (session.is_owner || perms.includes('users.view')) ? '' : 'none';
    }

    try {
        const [usersRes, rolesRes, deptsRes, permsRes] = await Promise.all([
            authFetch(`${BACKEND}/api/admin/users`),
            authFetch(`${BACKEND}/api/roles`),
            authFetch(`${BACKEND}/api/departments`),
            authFetch(`${BACKEND}/api/permissions`)
        ]);

        if (!usersRes.ok) throw new Error('Failed to load users');
        iamUsersData = await usersRes.json();
        iamRolesData = await rolesRes.json();
        iamDeptsData = await deptsRes.json();
        iamPermDefs = await permsRes.json();

        applyIAMFilters();
    } catch (err) {
        console.error('IAM load error:', err);
    }
}

// Stage 14/15: Department filtering for User Management (with smooth animations).
// Filters by stable department_code (or department_name as fallback).
function setIAMDeptFilter(code, name) {
    iamDeptFilter = { code: code || '', name: name || '' };
    document.querySelectorAll('.dept-filter-chip').forEach(chip => {
        chip.classList.toggle('active', (chip.dataset.code || '') === iamDeptFilter.code);
    });
    applyIAMFilters();
}

// Wire click listeners on the filter tabs (called once at boot).
function initIAMDeptFilterTabs() {
    document.querySelectorAll('.dept-filter-chip').forEach(chip => {
        chip.addEventListener('click', () => {
            setIAMDeptFilter(chip.dataset.code || '', chip.dataset.name || '');
        });
    });
}

function applyIAMFilters() {
    const q = ((document.getElementById('iam-search') || {}).value || '').toLowerCase();
    let filtered = iamUsersData;
    const dc = iamDeptFilter.code.toLowerCase();
    const dn = iamDeptFilter.name.toLowerCase();

    if (dc || dn) {
        filtered = filtered.filter(u => {
            const code = ((u.department_code != null ? u.department_code : '') || '').toString().toLowerCase();
            const name = (u.department_name || '').toLowerCase();
            return (dc && code === dc) || (dn && name === dn);
        });
    }
    if (q) {
        filtered = filtered.filter(u =>
            (u.full_name || '').toLowerCase().includes(q) ||
            (u.email || '').toLowerCase().includes(q) ||
            (u.role_name || '').toLowerCase().includes(q) ||
            (u.department_name || '').toLowerCase().includes(q)
        );
    }
    renderIAMTable(filtered);
}

function renderIAMTable(users) {
    const tbody = document.getElementById('iam-user-table-body');
    const emptyEl = document.getElementById('iam-empty');
    if (!tbody) return;

    // Clean re-render: clear prior rows so the enter animation re-runs
    tbody.innerHTML = '';

    if (!users.length) {
        if (emptyEl) emptyEl.classList.remove('hidden');
        return;
    }
    if (emptyEl) emptyEl.classList.add('hidden');

    const colors = ['#6366f1','#14b8a6','#f59e0b','#ef4444','#8b5cf6','#ec4899','#06b6d4'];
    tbody.innerHTML = users.map((u, idx) => {
        const initials = u.full_name ? u.full_name.split(' ').map(w=>w[0]).join('').toUpperCase().slice(0,2) : 'U';
        const color = colors[u.id % colors.length];
        let statusHtml = '';
        if (u.is_owner) {
            statusHtml = `<span class="iam-status-badge iam-status-owner"><span class="iam-status-dot"></span>Owner</span>`;
        } else if (u.is_active) {
            statusHtml = `<span class="iam-status-badge iam-status-active"><span class="iam-status-dot"></span>Active</span>`;
        } else {
            statusHtml = `<span class="iam-status-badge iam-status-suspended"><span class="iam-status-dot"></span>Suspended</span>`;
        }
        const avatarHtml = u.avatar
            ? `<img src="${u.avatar}" style="width:34px;height:34px;border-radius:10px;object-fit:cover;">`
            : `<div class="iam-avatar-sm" style="background:${color};">${initials}</div>`;

        const animDelay = Math.min(idx * 30, 360);
        return `<tr class="iam-table-row" style="animation-delay:${animDelay}ms;">
            <td style="padding:12px 18px;"><div style="display:flex;align-items:center;gap:10px;">${avatarHtml}<span style="font-weight:600;color:var(--ink);font-size:13px;">${u.full_name || 'Unknown'}</span></div></td>
            <td style="padding:12px 18px;color:var(--ink-secondary);font-size:12.5px;">${u.email}</td>
            <td style="padding:12px 18px;"><span style="font-size:12px;color:var(--muted);">${u.department_name || '<span style="color:var(--border);font-style:italic;">Unassigned</span>'}</span></td>
            <td style="padding:12px 18px;"><span style="font-size:12px;color:var(--ink);font-weight:500;">${u.role_name || '<span style="color:var(--border);font-style:italic;">No Role</span>'}</span></td>
            <td style="padding:12px 18px;">${statusHtml}</td>
            <td style="padding:12px 18px;text-align:center;">
                <div style="display:flex;gap:6px;justify-content:center;">
                    <button class="iam-action-btn" onclick="openPermMatrix(${u.id})"><i class="fa-solid fa-key"></i> Manage Access</button>
                    ${!u.is_owner ? `<button class="iam-action-btn danger" onclick="toggleUserStatus(${u.id}, ${u.is_active})"><i class="fa-solid fa-${u.is_active ? 'ban' : 'check'}"></i> ${u.is_active ? 'Suspend' : 'Activate'}</button>` : ''}
                </div>
            </td>
        </tr>`;
    }).join('');
}

function filterIAMUsers(q) {
    applyIAMFilters();
}

async function toggleUserStatus(userId, currentlyActive) {
    const action = currentlyActive ? 'suspend' : 'activate';
    if (!confirm(`Are you sure you want to ${action} this user?`)) return;
    try {
        const res = await authFetch(`${BACKEND}/api/admin/users/${userId}/status`, {
            method: 'PATCH',
            body: JSON.stringify({ is_active: !currentlyActive })
        });
        if (res.ok) {
            showToast(`User ${action}d successfully`, 'success');
            renderIAMView();
        } else {
            const err = await res.json();
            showToast(err.detail || 'Failed to update user', 'error');
        }
    } catch (e) {
        showToast('Network error', 'error');
    }
}

// --- Permission Matrix Modal ---
let permMatrixUser = null;
let permMatrixOverrides = {};

async function openPermMatrix(userId) {
    try {
        const [userRes] = await Promise.all([
            authFetch(`${BACKEND}/api/admin/users/${userId}`)
        ]);
        if (!userRes.ok) throw new Error('Failed to load user');
        permMatrixUser = await userRes.json();

        // Init overrides from effective vs role_permissions
        permMatrixOverrides = {};
        const rolePerms = new Set(permMatrixUser.role_permissions || []);
        const effective = new Set(permMatrixUser.effective_permissions || []);

        // Build current effective state as our working copy
        effective.forEach(p => { permMatrixOverrides[p] = true; });

        document.getElementById('perm-matrix-title').textContent = 'Manage Access';
        document.getElementById('perm-matrix-subtitle').textContent = `${permMatrixUser.full_name} — ${permMatrixUser.email}`;

        // Populate role select
        const roleSelect = document.getElementById('perm-role-select');
        roleSelect.innerHTML = iamRolesData.map(r =>
            `<option value="${r.id}" ${r.id === permMatrixUser.role_id ? 'selected' : ''}>${r.name}</option>`
        ).join('');

        // Populate dept select
        const deptSelect = document.getElementById('perm-dept-select');
        deptSelect.innerHTML = `<option value="">— None —</option>` + iamDeptsData.map(d =>
            `<option value="${d.id}" ${d.id === permMatrixUser.department_id ? 'selected' : ''}>${d.name}</option>`
        ).join('');

        // Render permission toggles
        renderPermToggles();

        const modal = document.getElementById('perm-matrix-modal');
        modal.classList.remove('hidden');
        modal.style.display = 'flex';
    } catch (err) {
        console.error('Perm matrix error:', err);
        showToast('Failed to load user permissions', 'error');
    }
}

function renderPermToggles() {
    const container = document.getElementById('perm-toggles-container');
    if (!container || !permMatrixUser) return;

    const rolePerms = new Set(permMatrixUser.role_permissions || []);
    const currentPerms = new Set(permMatrixUser.effective_permissions || []);

    // Group by module
    const modules = {};
    iamPermDefs.forEach(p => {
        if (!modules[p.module]) modules[p.module] = [];
        modules[p.module].push(p);
    });

    const moduleIcons = {
        dashboard: 'fa-gauge-high', tickets: 'fa-ticket', incidents: 'fa-triangle-exclamation',
        requests: 'fa-envelope-open-text', changes: 'fa-code-branch', assets: 'fa-server',
        configuration: 'fa-gear', knowledge: 'fa-book', users: 'fa-users',
        roles: 'fa-user-shield', reports: 'fa-chart-bar', system: 'fa-cog'
    };

    container.innerHTML = Object.keys(modules).sort().map(mod => {
        const perms = modules[mod];
        return `<div class="iam-module-group">
            <div class="iam-module-header"><i class="fa-solid ${moduleIcons[mod] || 'fa-circle'}"></i> ${mod}</div>
            ${perms.map(p => {
                const isFromRole = rolePerms.has(p.code);
                const isCurrentlyOn = currentPerms.has(p.code);
                const isOverride = permMatrixOverrides[p.code] !== undefined;
                return `<div class="iam-perm-row">
                    <div>
                        <div class="iam-perm-label">${p.code}</div>
                        <div class="iam-perm-desc">${p.description || ''}${isFromRole ? ' <em style="color:var(--navy);">(role)</em>' : ''}</div>
                    </div>
                    <button class="iam-toggle ${isCurrentlyOn ? 'active' : ''}" onclick="togglePerm('${p.code}', this)" data-perm="${p.code}">
                        <span class="toggle-dot"></span>
                    </button>
                </div>`;
            }).join('')}
        </div>`;
    }).join('');
}

function togglePerm(code, btn) {
    const isCurrentlyOn = btn.classList.contains('active');
    if (isCurrentlyOn) {
        btn.classList.remove('active');
        permMatrixOverrides[code] = false;
    } else {
        btn.classList.add('active');
        permMatrixOverrides[code] = true;
    }
}

function onPermRoleChange() {
    // When role changes, update the effective perms shown by the toggles
    if (!permMatrixUser) return;
    const newRoleId = parseInt(document.getElementById('perm-role-select').value);
    const newRole = iamRolesData.find(r => r.id === newRoleId);
    if (!newRole) return;

    // Fetch the new role's permissions
    authFetch(`${BACKEND}/api/roles/${newRoleId}/permissions`).then(r => r.json()).then(perms => {
        const newRolePermCodes = new Set(perms.map(p => p.code));
        permMatrixUser.role_permissions = [...newRolePermCodes];
        // Reset effective: role perms + any granted overrides
        const effective = new Set(newRolePermCodes);
        (permMatrixUser.overrides || []).forEach(o => {
            if (o.granted) effective.add(o.code);
            else effective.delete(o.code);
        });
        permMatrixUser.effective_permissions = [...effective];
        permMatrixOverrides = {};
        renderPermToggles();
    }).catch(() => {});
}

async function savePermMatrix() {
    if (!permMatrixUser) return;
    const btn = document.getElementById('perm-save-btn');
    btn.disabled = true;
    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Saving...';

    try {
        // 1. Update role & department
        const newRoleId = parseInt(document.getElementById('perm-role-select').value);
        const newDeptId = document.getElementById('perm-dept-select').value ? parseInt(document.getElementById('perm-dept-select').value) : null;

        if (newRoleId !== permMatrixUser.role_id || newDeptId !== permMatrixUser.department_id) {
            await authFetch(`${BACKEND}/api/admin/users/${permMatrixUser.id}/role`, {
                method: 'PATCH',
                body: JSON.stringify({ role_id: newRoleId, department_id: newDeptId })
            });
        }

        // 2. Apply permission overrides
        for (const [code, granted] of Object.entries(permMatrixOverrides)) {
            await authFetch(`${BACKEND}/api/admin/users/${permMatrixUser.id}/permissions`, {
                method: 'PATCH',
                body: JSON.stringify({ permission_code: code, granted })
            });
        }

        showToast('Access updated successfully', 'success');
        closePermMatrix();
        renderIAMView();
    } catch (e) {
        showToast('Failed to save access changes', 'error');
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="fa-solid fa-check"></i> Save Access';
    }
}

function closePermMatrix() {
    const modal = document.getElementById('perm-matrix-modal');
    if (modal) {
        modal.style.display = 'none';
        modal.classList.add('hidden');
    }
    permMatrixUser = null;
    permMatrixOverrides = {};
}

function openAddUserModal() {
    showToast('User creation: use the Registration flow or API', 'info');
}

// --- Toast helper ---
function showToast(message, type) {
    let container = document.getElementById('toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        container.style.cssText = 'position:fixed;top:20px;right:20px;z-index:9999;display:flex;flex-direction:column;gap:10px;';
        document.body.appendChild(container);
    }
    const toast = document.createElement('div');
    const bgColor = type === 'success' ? '#22c55e' : type === 'error' ? '#ef4444' : '#6366f1';
    toast.style.cssText = `background:${bgColor};color:#fff;padding:12px 20px;border-radius:10px;font-size:13px;font-weight:600;box-shadow:0 4px 20px rgba(0,0,0,0.3);animation:slideInRight 0.3s ease forwards;display:flex;align-items:center;gap:8px;`;
    const icon = type === 'success' ? 'fa-check-circle' : type === 'error' ? 'fa-exclamation-circle' : 'fa-info-circle';
    const iconNode = document.createElement('i');
    iconNode.className = `fa-solid ${icon}`;
    const label = document.createElement('span');
    label.textContent = message;
    toast.appendChild(iconNode);
    toast.appendChild(label);
    container.appendChild(toast);
    setTimeout(() => { toast.style.opacity = '0'; toast.style.transform = 'translateX(20px)'; setTimeout(() => toast.remove(), 300); }, 3000);
}

// =====================================================================
// BOOT
// =====================================================================

// DOM is ready — script is at bottom of <body>, no need for DOMContentLoaded
(function bindProfileEvents() {
    const avatarBtn = document.getElementById('user-profile-avatar') || document.querySelector('.topnav-chip');
    if (avatarBtn) {
        avatarBtn.removeAttribute('onclick');
        avatarBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            toggleUserMenu(e);
        });
    }

    const profileSettingsBtn = document.querySelector('[onclick*="openProfileSettings"]');
    if (profileSettingsBtn) {
        profileSettingsBtn.removeAttribute('onclick');
        profileSettingsBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            openProfileSettings();
        });
    }

    const logoutBtn = document.querySelector('[onclick*="logoutUser"]');
    if (logoutBtn) {
        logoutBtn.removeAttribute('onclick');
        logoutBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            logoutUser();
        });
    }

    const profileBackdrop = document.getElementById('profile-backdrop');
    if (profileBackdrop) {
        profileBackdrop.addEventListener('click', () => closeProfileSettings());
    }
})();

initIAMDeptFilterTabs();

checkAuthSession();

requestAnimationFrame(function() { updateNavPill(); });
window.addEventListener('resize', function() { updateNavPill(); });

// =====================================================================
// FEATURE 1: RBAC ticket queue scope (My / Department / Global)
// =====================================================================
function isGlobalUser() {
    const s = JSON.parse(localStorage.getItem('soc_session') || '{}');
    const r = Number(s.role_rank || 0);
    if (s.is_owner) return true;
    if ((s.permissions || []).includes('tickets.assign')) return true;
    if (s.role_name === 'Owner / Super Admin') return true;
    return r >= 45;
}

function updateQueueScopeUI() {
    const global = isGlobalUser();
    const bar = document.getElementById('queue-scope-bar');
    if (!bar) return;
    const first = bar.querySelector('[data-scope="primary"]');
    if (first) first.textContent = global ? 'Global Queue' : 'My Department';
    const scopeEl = document.getElementById('queue-scope-label');
    if (scopeEl) scopeEl.innerText = global ? 'Queue · Global Visibility' : 'Queue · Department Visibility';
    if (!global && queueScope === 'global') queueScope = 'all';
    bar.querySelectorAll('.filter-chip').forEach(c => {
        const active = (queueScope === 'mine' && c.dataset.scope === 'mine') ||
                       (queueScope === 'assigned' && c.dataset.scope === 'assigned') ||
                       (queueScope !== 'mine' && queueScope !== 'assigned' && c.dataset.scope === 'primary');
        c.classList.toggle('active', active);
    });
}

function setQueueScope(scope, btn) {
    queueScope = (scope === 'primary') ? 'all' : scope;
    if (btn) {
        const bar = document.getElementById('queue-scope-bar');
        if (bar) bar.querySelectorAll('.filter-chip').forEach(c => c.classList.remove('active'));
        btn.classList.add('active');
    }
    renderTickets();
}

// =====================================================================
// FEATURE 2: USERS DIRECTORY VIEW (search + pagination)
// =====================================================================
function dirColor(str) {
    let h = 0;
    for (let i = 0; i < str.length; i++) h = ((h << 5) - h + str.charCodeAt(i)) | 0;
    return 'hsl(' + Math.abs(h) % 360 + ',70%,45%)';
}

function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}

async function renderUsersView() {
    const q = (document.getElementById('dir-search') || {}).value || '';
    userDirQuery = q.trim().toLowerCase();
    try {
        if (userDirData.length === 0) {
            const res = await authFetch(BACKEND + '/api/users');
            if (!res.ok) throw new Error('HTTP ' + res.status);
            userDirData = await res.json();
        }
    } catch (err) {
        const tbody = document.getElementById('dir-user-table-body');
        if (tbody) tbody.innerHTML = '<tr><td colspan="6" style="padding:40px;text-align:center;color:var(--muted);">Could not load the directory.</td></tr>';
        return;
    }
    const filtered = userDirData.filter(u => {
        if (deptFilterCode && (u.department_code || '') !== deptFilterCode) return false;
        const hay = [u.full_name, u.email, u.department_name, u.role_name, u.sam_account_name, u.ad_domain]
            .filter(Boolean).join(' ').toLowerCase();
        return !userDirQuery || hay.indexOf(userDirQuery) !== -1;
    });
    const totalPages = Math.max(1, Math.ceil(filtered.length / USER_DIR_PAGE_SIZE));
    if (userDirPage > totalPages) userDirPage = totalPages;
    const countEl = document.getElementById('dir-count');
    if (countEl) countEl.innerText = filtered.length + ' user' + (filtered.length === 1 ? '' : 's') + ' · ' + totalPages + ' page' + (totalPages === 1 ? '' : 's');
    renderDirectoryTable(filtered);
}

function renderDirectoryTable(users) {
    const tbody = document.getElementById('dir-user-table-body');
    const emptyEl = document.getElementById('dir-empty');
    if (!tbody) return;
    if (users.length === 0) {
        tbody.innerHTML = '';
        if (emptyEl) emptyEl.classList.remove('hidden');
        return;
    }
    if (emptyEl) emptyEl.classList.add('hidden');
    const start = (userDirPage - 1) * USER_DIR_PAGE_SIZE;
    const page = users.slice(start, start + USER_DIR_PAGE_SIZE);
    const totalPages = Math.max(1, Math.ceil(users.length / USER_DIR_PAGE_SIZE));
    const pageInfo = document.getElementById('dir-page-info');
    if (pageInfo) pageInfo.innerText = 'Page ' + userDirPage + ' of ' + totalPages;
    const prevBtn = document.getElementById('dir-prev');
    const nextBtn = document.getElementById('dir-next');
    if (prevBtn) prevBtn.disabled = userDirPage <= 1;
    if (nextBtn) nextBtn.disabled = userDirPage >= totalPages;

    tbody.innerHTML = page.map(u => {
        const initials = u.full_name
            ? u.full_name.split(' ').filter(Boolean).map(w => w[0]).join('').substring(0, 2).toUpperCase()
            : 'U';
        const color = dirColor(u.full_name || u.email);
        const statusHtml = u.is_owner
            ? '<span class="iam-status-badge iam-status-owner"><span class="iam-status-dot"></span>Owner</span>'
            : (u.is_active
                ? '<span class="iam-status-badge iam-status-active"><span class="iam-status-dot"></span>Active</span>'
                : '<span class="iam-status-badge iam-status-suspended"><span class="iam-status-dot"></span>Suspended</span>');
        const sam = u.sam_account_name || ((u.email || '').split('@')[0] || '');
        const dom = u.ad_domain || '';
        const account = (sam + '@' + dom).replace(/@$/, '');
        const dept = u.department_name || '—';
        return `<tr class="iam-table-row">
            <td style="padding:12px 18px;"><div style="display:flex;align-items:center;gap:10px;"><div class="iam-avatar-sm" style="background:${color};">${initials}</div><span style="font-weight:600;color:var(--ink);font-size:13px;">${esc(u.full_name) || 'Unknown'}</span></div></td>
            <td style="padding:12px 18px;color:var(--muted);font-size:12px;">${esc(u.email) || '—'}</td>
            <td style="padding:12px 18px;color:var(--muted);font-size:12px;">${esc(dept)}</td>
            <td style="padding:12px 18px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--ink);font-size:11px;">${esc(account)}</td>
            <td style="padding:12px 18px;color:var(--muted);font-size:12px;">${esc(u.role_name) || '—'}</td>
            <td style="padding:12px 18px;">${statusHtml}</td>
        </tr>`;
    }).join('');
}

function dirPage(next) {
    userDirPage += next ? 1 : -1;
    if (userDirPage < 1) userDirPage = 1;
    renderUsersView();
}
