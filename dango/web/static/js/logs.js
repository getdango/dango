/**
 * Dango Logs Page - JavaScript
 *
 * Handles log viewing, filtering, sorting, and pagination
 */

// Global state
let ws = null;
let reconnectInterval = null;
let allLogs = [];
let filteredLogs = [];
let currentPage = 1;
const logsPerPage = 50;
let sortField = 'timestamp';
let sortAscending = false;

// Initialize on page load
document.addEventListener('DOMContentLoaded', async () => {
    console.log('Logs page initializing...');

    // Update Query Database link with proper Metabase URL
    await updateQueryDatabaseLink();

    // Load logs
    loadLogs();

    // Load sources for filter dropdown
    loadSources();

    // Connect WebSocket for real-time updates
    connectWebSocket();

    // Set up filter event listeners
    setupFilterListeners();
});

// ============================================================================
// Metabase Link Setup
// ============================================================================

async function updateQueryDatabaseLink() {
    try {
        const metabaseConfigResponse = await fetch('/api/metabase-config');
        if (metabaseConfigResponse.ok) {
            const metabaseConfig = await metabaseConfigResponse.json();
            const databaseId = metabaseConfig.database_id;

            if (databaseId) {
                // Create Metabase SQL query state object
                const queryState = {
                    "dataset_query": {
                        "database": databaseId,
                        "type": "native",
                        "native": {
                            "query": "",
                            "template-tags": {}
                        }
                    },
                    "display": "table",
                    "visualization_settings": {},
                    "type": "question"
                };

                // Base64 encode the state
                const encodedState = btoa(JSON.stringify(queryState));
                // Use proxied path to get auto-login
                const sqlQueryUrl = `/metabase/question#${encodedState}`;

                // Update navbar link
                const navQueryLink = document.getElementById('nav-query-database');
                if (navQueryLink) {
                    navQueryLink.href = sqlQueryUrl;
                }
            }
        }
    } catch (error) {
        console.log('Could not load Metabase config for SQL link:', error);
        // Link will use default href
    }
}

// ============================================================================
// WebSocket Connection
// ============================================================================

function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws`;

    console.log('Connecting to WebSocket:', wsUrl);
    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
        console.log('WebSocket connected');
        updateConnectionStatus(true);
    };

    ws.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            handleWebSocketMessage(data);
        } catch (error) {
            console.error('Error parsing WebSocket message:', error);
        }
    };

    ws.onclose = () => {
        console.log('WebSocket disconnected');
        updateConnectionStatus(false);

        if (!reconnectInterval) {
            reconnectInterval = setInterval(() => {
                console.log('Attempting to reconnect...');
                connectWebSocket();
            }, 5000);
        }
    };

    ws.onerror = (error) => {
        console.error('WebSocket error:', error);
        updateConnectionStatus(false);
    };
}

function handleWebSocketMessage(data) {
    const { event, source, message, timestamp } = data;

    // Add new log entry
    const logEntry = {
        timestamp: timestamp || new Date().toISOString(),
        level: getLogLevelFromEvent(event),
        source: source || 'system',
        message: message || ''
    };

    // Add to beginning of logs
    allLogs.unshift(logEntry);

    // Keep only last 1000 entries in memory
    if (allLogs.length > 1000) {
        allLogs = allLogs.slice(0, 1000);
    }

    // Re-apply filters and render
    applyFilters();
}

function getLogLevelFromEvent(event) {
    if (event.includes('completed') || event.includes('success')) return 'success';
    if (event.includes('failed') || event.includes('error')) return 'error';
    if (event.includes('warning')) return 'warning';
    return 'info';
}

function updateConnectionStatus(connected) {
    const indicator = document.getElementById('status-indicator');
    const text = document.getElementById('status-text');

    if (connected) {
        indicator.className = 'h-2 w-2 bg-green-500 rounded-full';
        text.textContent = 'Connected';
        text.className = 'text-sm text-green-600';
    } else {
        indicator.className = 'h-2 w-2 bg-red-500 rounded-full animate-pulse';
        text.textContent = 'Disconnected';
        text.className = 'text-sm text-red-600';
    }
}

// ============================================================================
// API Calls
// ============================================================================

async function apiCall(endpoint, method = 'GET', body = null) {
    const options = {
        method,
        headers: {
            'Content-Type': 'application/json',
        },
    };

    if (body) {
        options.body = JSON.stringify(body);
    }

    try {
        const response = await fetch(endpoint, options);
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }
        return await response.json();
    } catch (error) {
        console.error(`API call failed: ${endpoint}`, error);
        throw error;
    }
}

async function loadLogs() {
    showLoading();

    try {
        const logs = await apiCall('/api/logs');
        allLogs = logs || [];
        applyFilters();
    } catch (error) {
        console.error('Error loading logs:', error);
        showToast('Failed to load logs', 'error');
        showEmpty();
    }
}

async function loadSources() {
    try {
        const sources = await apiCall('/api/sources');
        const filterSource = document.getElementById('filter-source');

        // Populate source filter dropdown
        sources.forEach(source => {
            const option = document.createElement('option');
            option.value = source.name;
            option.textContent = source.name;
            filterSource.appendChild(option);
        });
    } catch (error) {
        console.error('Error loading sources:', error);
    }
}

// ============================================================================
// Filtering and Sorting
// ============================================================================

function setupFilterListeners() {
    // Real-time search filter
    document.getElementById('filter-search').addEventListener('input', applyFilters);
}

function applyFilters() {
    const levelFilter = document.getElementById('filter-level').value;
    const sourceFilter = document.getElementById('filter-source').value;
    const timeFilter = document.getElementById('filter-time').value;
    const searchFilter = document.getElementById('filter-search').value.toLowerCase();

    filteredLogs = allLogs.filter(log => {
        // Level filter
        if (levelFilter && log.level !== levelFilter) return false;

        // Source filter
        if (sourceFilter && log.source !== sourceFilter) return false;

        // Time filter
        if (timeFilter !== 'all') {
            const logTime = new Date(log.timestamp);
            const now = new Date();
            const diff = now - logTime;

            switch (timeFilter) {
                case '1h':
                    if (diff > 60 * 60 * 1000) return false;
                    break;
                case '24h':
                    if (diff > 24 * 60 * 60 * 1000) return false;
                    break;
                case '7d':
                    if (diff > 7 * 24 * 60 * 60 * 1000) return false;
                    break;
                case '30d':
                    if (diff > 30 * 24 * 60 * 60 * 1000) return false;
                    break;
            }
        }

        // Search filter
        if (searchFilter) {
            const searchableText = `${log.source} ${log.message}`.toLowerCase();
            if (!searchableText.includes(searchFilter)) return false;
        }

        return true;
    });

    // Sort logs
    sortLogsArray();

    // Reset to first page
    currentPage = 1;

    // Update display
    renderLogs();
}

function clearFilters() {
    document.getElementById('filter-level').value = '';
    document.getElementById('filter-source').value = '';
    document.getElementById('filter-time').value = 'all';
    document.getElementById('filter-search').value = '';
    applyFilters();
}

function sortLogs(field) {
    if (sortField === field) {
        sortAscending = !sortAscending;
    } else {
        sortField = field;
        // For timestamp, default to descending (newest first)
        // For other fields, default to ascending
        sortAscending = field !== 'timestamp';
    }

    sortLogsArray();
    renderLogs();
}

function sortLogsArray() {
    filteredLogs.sort((a, b) => {
        let aVal = a[sortField];
        let bVal = b[sortField];

        // Convert timestamps to dates for proper comparison
        if (sortField === 'timestamp') {
            aVal = new Date(aVal);
            bVal = new Date(bVal);
        }

        if (aVal < bVal) return sortAscending ? -1 : 1;
        if (aVal > bVal) return sortAscending ? 1 : -1;
        return 0;
    });
}

// ============================================================================
// Rendering
// ============================================================================

function renderLogs() {
    const tbody = document.getElementById('logs-table-body');
    const loadingEl = document.getElementById('logs-loading');
    const emptyEl = document.getElementById('logs-empty');

    loadingEl.classList.add('hidden');

    if (filteredLogs.length === 0) {
        tbody.innerHTML = '';
        emptyEl.classList.remove('hidden');
        updatePagination();
        return;
    }

    emptyEl.classList.add('hidden');

    // Calculate pagination
    const startIndex = (currentPage - 1) * logsPerPage;
    const endIndex = Math.min(startIndex + logsPerPage, filteredLogs.length);
    const pageLog = filteredLogs.slice(startIndex, endIndex);

    // Render logs
    tbody.innerHTML = pageLog.map(log => {
        const timestamp = new Date(log.timestamp).toLocaleString();
        const levelBadge = getLevelBadge(log.level);
        // Keep original formatting - preserve newlines and spacing
        const message = log.message || '';
        const source = log.source || 'system';

        return `
            <tr class="hover:bg-gray-50">
                <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-900">
                    ${timestamp}
                </td>
                <td class="px-6 py-4 whitespace-nowrap">
                    ${levelBadge}
                </td>
                <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-600">
                    ${source}
                </td>
                <td class="px-6 py-4 text-sm text-gray-900"><pre class="log-message">${message}</pre></td>
            </tr>
        `;
    }).join('');

    updatePagination();
}

function getLevelBadge(level) {
    const badges = {
        'info': '<span class="px-2 py-1 text-xs rounded-full bg-blue-100 text-blue-800">Info</span>',
        'success': '<span class="px-2 py-1 text-xs rounded-full bg-green-100 text-green-800">Success</span>',
        'warning': '<span class="px-2 py-1 text-xs rounded-full bg-yellow-100 text-yellow-800">Warning</span>',
        'error': '<span class="px-2 py-1 text-xs rounded-full bg-red-100 text-red-800">Error</span>',
    };

    return badges[level] || badges['info'];
}

function updatePagination() {
    const totalLogs = filteredLogs.length;
    const totalPages = Math.ceil(totalLogs / logsPerPage);
    const startIndex = (currentPage - 1) * logsPerPage + 1;
    const endIndex = Math.min(currentPage * logsPerPage, totalLogs);

    // Update counts
    document.getElementById('log-count').textContent = totalLogs.toLocaleString();
    document.getElementById('showing-from').textContent = totalLogs > 0 ? startIndex : 0;
    document.getElementById('showing-to').textContent = endIndex;
    document.getElementById('total-logs').textContent = totalLogs.toLocaleString();
    document.getElementById('page-info').textContent = `Page ${currentPage} of ${totalPages || 1}`;

    // Update buttons
    document.getElementById('prev-btn').disabled = currentPage === 1;
    document.getElementById('next-btn').disabled = currentPage >= totalPages;
}

function showLoading() {
    document.getElementById('logs-loading').classList.remove('hidden');
    document.getElementById('logs-empty').classList.add('hidden');
}

function showEmpty() {
    document.getElementById('logs-loading').classList.add('hidden');
    document.getElementById('logs-empty').classList.remove('hidden');
}

// ============================================================================
// Pagination
// ============================================================================

function previousPage() {
    if (currentPage > 1) {
        currentPage--;
        renderLogs();
        window.scrollTo({ top: 0, behavior: 'smooth' });
    }
}

function nextPage() {
    const totalPages = Math.ceil(filteredLogs.length / logsPerPage);
    if (currentPage < totalPages) {
        currentPage++;
        renderLogs();
        window.scrollTo({ top: 0, behavior: 'smooth' });
    }
}

// ============================================================================
// Toast Notifications
// ============================================================================

function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');

    const bgColors = {
        'info': 'bg-blue-500',
        'success': 'bg-green-500',
        'warning': 'bg-yellow-500',
        'error': 'bg-red-500',
    };

    const toast = document.createElement('div');
    toast.className = `${bgColors[type] || bgColors.info} text-white px-6 py-3 rounded-lg shadow-lg transform transition-all duration-300 ease-in-out opacity-0 translate-x-full`;
    toast.textContent = message;

    container.appendChild(toast);

    setTimeout(() => {
        toast.classList.remove('opacity-0', 'translate-x-full');
    }, 10);

    setTimeout(() => {
        toast.classList.add('opacity-0', 'translate-x-full');
        setTimeout(() => {
            container.removeChild(toast);
        }, 300);
    }, 4000);
}
