/**
 * Dango Dashboard - Main JavaScript
 *
 * Handles:
 * - WebSocket connection for real-time updates
 * - API calls to backend
 * - UI updates and interactions
 */

// Global state
let ws = null;
let reconnectInterval = null;
let sources = [];
let activityLog = [];
const MAX_LOG_ENTRIES = 50; // Keep compact for quick scanning
let isLoadingSources = false;
let isLoadingStatus = false;
// Track active syncs: Map<sourceName, startTimestamp>
let activeSyncs = new Map();
// Track pending loadSources retry timeout
let loadSourcesRetryTimeout = null;
// Track active upload/delete operations to prevent premature loadSources() retries
// Maps sourceName to Set of operation IDs
let activeFileOperations = new Map();

/**
 * Add a file operation for tracking
 * @param {string} sourceName - Source name
 * @param {string} operationId - Unique operation ID
 */
function addFileOperation(sourceName, operationId) {
    if (!activeFileOperations.has(sourceName)) {
        activeFileOperations.set(sourceName, new Set());
    }
    activeFileOperations.get(sourceName).add(operationId);
    const totalOps = getTotalFileOperations();
    console.log(`📋 [FileOps] Added ${operationId} for ${sourceName}, total: ${totalOps}`);
}

/**
 * Remove a file operation
 * @param {string} sourceName - Source name
 * @param {string} operationId - Unique operation ID
 */
function removeFileOperation(sourceName, operationId) {
    if (activeFileOperations.has(sourceName)) {
        activeFileOperations.get(sourceName).delete(operationId);
        if (activeFileOperations.get(sourceName).size === 0) {
            activeFileOperations.delete(sourceName);
        }
    }
    const totalOps = getTotalFileOperations();
    console.log(`📋 [FileOps] Removed ${operationId} for ${sourceName}, total: ${totalOps}`);
}

/**
 * Clear all file operations for a source (called when sync completes)
 * @param {string} sourceName - Source name
 */
function clearFileOperations(sourceName) {
    if (activeFileOperations.has(sourceName)) {
        const count = activeFileOperations.get(sourceName).size;
        activeFileOperations.delete(sourceName);
        const totalOps = getTotalFileOperations();
        console.log(`📋 [FileOps] Cleared ${count} operations for ${sourceName}, total: ${totalOps}`);
    }
}

/**
 * Get total number of active file operations across all sources
 * @returns {number}
 */
function getTotalFileOperations() {
    let total = 0;
    for (const ops of activeFileOperations.values()) {
        total += ops.size;
    }
    return total;
}

/**
 * Format a timestamp as relative time (e.g., "2 minutes ago")
 * Returns an HTML string with the relative time and a tooltip with the full timestamp
 *
 * @param {string|Date} timestamp - ISO timestamp string or Date object
 * @returns {string} HTML string with relative time and tooltip
 */
function formatRelativeTime(timestamp) {
    if (!timestamp) return 'Never';

    const date = new Date(timestamp);
    if (isNaN(date.getTime())) return 'Invalid date';

    const now = new Date();
    const seconds = Math.floor((now - date) / 1000);

    // Future dates
    if (seconds < 0) {
        return `<span title="${date.toLocaleString()}">Just now</span>`;
    }

    // Relative time ranges
    const intervals = [
        { seconds: 31536000, label: 'year' },
        { seconds: 2592000, label: 'month' },
        { seconds: 86400, label: 'day' },
        { seconds: 3600, label: 'hour' },
        { seconds: 60, label: 'minute' },
    ];

    for (const interval of intervals) {
        const count = Math.floor(seconds / interval.seconds);
        if (count >= 1) {
            const plural = count > 1 ? 's' : '';
            const relativeText = `${count} ${interval.label}${plural} ago`;
            const fullTimestamp = date.toLocaleString();
            return `<span title="${fullTimestamp}" class="cursor-help">${relativeText}</span>`;
        }
    }

    // Less than a minute
    const fullTimestamp = date.toLocaleString();
    return `<span title="${fullTimestamp}" class="cursor-help">Just now</span>`;
}

// Initialize dashboard on page load
document.addEventListener('DOMContentLoaded', () => {
    console.log('Dango Dashboard initializing...');

    // Initialize tab from URL hash or default to sources (without updating URL)
    const hash = window.location.hash.slice(1) || 'sources';
    switchTab(hash, false); // Don't add hash to URL on initial load

    // Load config and update dynamic URLs
    loadConfig();

    // Load initial data
    loadServiceStatus();
    loadSources();
    loadDbtModels();
    loadActivityLogs(); // Load persistent activity logs
    fetchPlatformHealth(); // Load platform health status

    // Connect WebSocket
    connectWebSocket();

    // Set up periodic refresh (every 30 seconds)
    setInterval(loadServiceStatus, 30000);
    setInterval(fetchPlatformHealth, 30000); // Also refresh health widget

    // Update sync counter periodically
    setInterval(updateSyncCounter, 1000);

    // Handle hash changes (browser back/forward)
    window.addEventListener('hashchange', () => {
        const newTab = window.location.hash.slice(1) || 'sources';
        switchTab(newTab, false); // false = don't update hash again
    });
});

// ============================================================================
// Configuration Loading
// ============================================================================

/**
 * Load configuration from API and update dynamic URLs in the page
 */
async function loadConfig() {
    try {
        const response = await fetch('/api/config');
        const config = await response.json();

        // Update dbt docs links (port 8081 is fixed, but fetch from config for consistency)
        const dbtDocsLinks = document.querySelectorAll('a[href="http://localhost:8081"]');
        dbtDocsLinks.forEach(link => {
            link.href = config.dbt_docs_url;
        });

        // Update project subtitle with organization and project name
        const subtitleElement = document.getElementById('project-subtitle');
        if (subtitleElement && config.project_name) {
            let subtitle = '';
            if (config.organization) {
                subtitle = `${config.organization} - ${config.project_name}`;
            } else {
                subtitle = config.project_name;
            }
            subtitleElement.textContent = subtitle;
        }

        // Update DuckDB link to point to SQL query editor with database pre-selected
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

                    // Update both the dashboard card and navbar link
                    const duckdbLink = document.getElementById('duckdb-sql-link');
                    if (duckdbLink) {
                        duckdbLink.href = sqlQueryUrl;
                    }

                    const navQueryLink = document.getElementById('nav-query-database');
                    if (navQueryLink) {
                        navQueryLink.href = sqlQueryUrl;
                    }
                }
            }
        } catch (error) {
            console.log('Could not load Metabase config for SQL link:', error);
            // Links will use default /metabase/question/new
        }

        console.log('Config loaded:', config);
    } catch (error) {
        console.error('Failed to load config:', error);
        // Links will use hardcoded defaults if config fails
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
        console.log('🔌 [WebSocket] Connection OPENED successfully');
        console.log('🔌 [WebSocket] ReadyState:', ws.readyState, '(1 = OPEN)');
        console.log('🔌 [WebSocket] URL:', wsUrl);
        updateConnectionStatus(true);
        addLogEntry('info', 'Connected to server', 'websocket');

        // Clear reconnect interval if it exists
        if (reconnectInterval) {
            clearInterval(reconnectInterval);
            reconnectInterval = null;
        }
    };

    ws.onmessage = (event) => {
        console.log('📨 [WebSocket] Raw message received, length:', event.data.length);
        try {
            const data = JSON.parse(event.data);
            console.log('📨 [WebSocket] Parsed successfully:', JSON.stringify(data));
            handleWebSocketMessage(data);
        } catch (error) {
            console.error('❌ [WebSocket] Error parsing message:', error);
            console.error('❌ [WebSocket] Raw data was:', event.data);
        }
    };

    ws.onclose = () => {
        console.log('WebSocket disconnected');
        updateConnectionStatus(false);
        addLogEntry('warning', 'Disconnected from server', 'websocket');

        // Attempt to reconnect every 5 seconds
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

async function handleWebSocketMessage(data) {
    const { event, source, message, timestamp } = data;

    // 🔍 DIAGNOSTIC: Log ALL incoming WebSocket messages
    console.log('🔔 [WebSocket] Received event:', event, {source, message, timestamp});

    switch (event) {
        case 'connected':
            console.log('✅ [WS] Connected to server');
            // Don't show toast - too spammy
            break;

        case 'csv_uploaded':
            console.log('📤 [WS] csv_uploaded - File uploaded, sync will start:', source);
            addLogEntry('info', message, source);
            // Don't show toast - optimistic update already showed it
            // Ensure syncing state is set (in case optimistic update didn't happen)
            if (!activeSyncs.has(source)) {
                activeSyncs.set(source, Date.now());
                updateSyncCounter();
                renderSourcesTable();
            }
            break;

        case 'sync_started':
            console.log('🔵 [WS] sync_started - Source:', source);
            addLogEntry('info', `Sync started`, source);
            // Don't show toast - optimistic update already showed it
            // Ensure syncing state is set (in case optimistic update didn't happen)
            if (!activeSyncs.has(source)) {
                activeSyncs.set(source, Date.now());
                updateSyncCounter();
                renderSourcesTable();
            }
            break;

        case 'sync_progress':
            addLogEntry('info', message, source);
            // Update progress if we add progress bars in the future
            break;

        case 'sync_completed':
            console.log('✅ [WS] sync_completed - Source:', source);
            addLogEntry('success', message || `Sync completed`, source);

            // Suppress success toast during batch operations (multi-file uploads)
            // The final toast will show when dbt completes
            if (!activeFileOperations.has(source)) {
                showToast(`${source} synced successfully`, 'success');
            } else {
                console.log('✅ [WS] Suppressing toast - batch upload operation in progress');
            }

            // DON'T clear activeSyncs here - dbt will run next and dbt_run_all_completed will clear it
            // New flow: all files uploaded to disk → sync once → dbt once
            console.log('✅ [WS] Sync completed, but keeping activeSyncs (dbt will run next)');

            // Don't call loadSources() - wait for dbt to complete
            break;

        case 'sync_failed':
            console.log('❌ [WS] sync_failed - Source:', source);
            activeSyncs.delete(source);
            updateSyncCounter();  // Update header counter
            addLogEntry('error', message || `Sync failed`, source);
            showToast(`${source} sync failed`, 'error');
            // Refresh from backend to show error status
            // Add small delay to ensure backend has persisted the failure
            setTimeout(() => {
                if (!isLoadingSources) {
                    console.log('❌ [WS] [DELAYED] Calling loadSources() to get error status');
                    loadSources();
                }
            }, 200);
            break;

        case 'csv_deleted':
            console.log('🗑️ [WS] csv_deleted - File deleted, dbt will run:', source);
            addLogEntry('info', message || 'File deleted', source);
            // Don't show toast - optimistic update already showed it
            // Ensure syncing state is set (in case optimistic update didn't happen)
            if (!activeSyncs.has(source)) {
                activeSyncs.set(source, Date.now());
                updateSyncCounter();
                renderSourcesTable();
            }
            break;

        case 'dbt_run_started':
            console.log('🟡 [WS] dbt_run_started - Individual model:', source);
            addLogEntry('info', message, source);
            // Extract model name from source (format: "dbt:model_name")
            if (source && source.startsWith('dbt:')) {
                const modelName = source.substring(4); // Remove "dbt:" prefix
                updateDbtModelStatus(modelName, true);
            }
            break;

        case 'dbt_run_all_started':
            console.log('🟡 [WS] dbt_run_all_started - Setting flag and refreshing');
            dbtRunStartTime = Date.now();
            console.log('🟡 [WS] dbtRunStartTime set to:', dbtRunStartTime);
            addLogEntry('info', message, source || 'dbt');
            // Don't show toast - reduces spam
            // Always update UI to show running state, but avoid querying DB during file operations
            const totalFileOpsStart = getTotalFileOperations();
            if (totalFileOpsStart === 0) {
                loadDbtModels();  // Query DB and refresh
            } else {
                console.log('🟡 [WS] Skipping DB query - just updating UI to show running state');
                renderDbtModelsTable();  // Just refresh UI with existing data to show "Running..." status
            }
            break;

        case 'dbt_run_progress':
            addLogEntry('info', message, source);
            break;

        case 'dbt_run_completed':
            console.log('✅ [WS] dbt_run_completed - Individual model:', source);
            addLogEntry('success', message, source);
            // Clear running state for this specific model
            if (source && source.startsWith('dbt:')) {
                const modelName = source.substring(4); // Remove "dbt:" prefix
                updateDbtModelStatus(modelName, false);
            }
            // Refresh to show updated status (only if no active file operations)
            const totalFileOpsRunComplete = getTotalFileOperations();
            if (totalFileOpsRunComplete === 0) {
                loadDbtModels();
            } else {
                console.log('✅ [WS] Skipping loadDbtModels() - active file operations:', totalFileOpsRunComplete);
            }
            break;

        case 'dbt_run_all_completed':
            console.log('🟢 [WS] dbt_run_all_completed - Clearing flag and refreshing');
            console.log('🟢 [WS] dbtRunStartTime before clear:', dbtRunStartTime);
            dbtRunStartTime = null;

            // Extract source name if this was triggered by a source operation
            // Format: "dbt (triggered by source_name)" or "dbt (triggered by source_name delete)"
            const sourceMatch = source?.match(/triggered by (\w+)/);
            const triggeredSource = sourceMatch ? sourceMatch[1] : null;

            if (triggeredSource) {
                console.log('🟢 [WS] dbt was triggered by source:', triggeredSource);

                // Check if this was a batch upload operation
                const hadUploadOps = activeFileOperations.has(triggeredSource);

                // Clean up any upload batch operations for this source
                if (hadUploadOps) {
                    const operations = activeFileOperations.get(triggeredSource);
                    for (const opId of operations) {
                        if (opId.startsWith('upload-batch-')) {
                            console.log(`🟢 [WS] Cleaning up upload batch operation: ${opId}`);
                            removeFileOperation(triggeredSource, opId);
                            // Show final success toast for the batch
                            showToast(`${triggeredSource}: Files synced and transformed successfully`, 'success');
                        }
                    }
                }
            }

            console.log('🟢 [WS] Cleared dbt flag. Showing completion...');
            addLogEntry('success', message, source || 'dbt');

            // Refresh dbt models
            loadDbtModels();

            // Delay clearing sync status and refreshing to ensure user sees "syncing" state
            // Always cleanup activeSyncs, even if source matching fails
            setTimeout(() => {
                if (triggeredSource) {
                    activeSyncs.delete(triggeredSource);
                    console.log('🟢 [WS] [DELAYED] Cleared activeSyncs for:', triggeredSource);
                }
                updateSyncCounter();

                // Refresh sources list
                if (!isLoadingSources) {
                    console.log('🟢 [WS] [DELAYED] Calling loadSources() after user saw syncing state');
                    loadSources();
                }
            }, 500);  // Wait 500ms so user sees the syncing badge
            break;

        case 'dbt_run_failed':
            console.log('❌ [WS] dbt_run_failed - Individual model:', source);
            addLogEntry('error', message, source);
            // Don't show toast - reduces spam, error visible in activity log
            // Refresh to show error status (only if no active file operations)
            const totalFileOpsRunFailed = getTotalFileOperations();
            if (totalFileOpsRunFailed === 0) {
                loadDbtModels();
            } else {
                console.log('❌ [WS] Skipping loadDbtModels() - active file operations:', totalFileOpsRunFailed);
            }
            break;

        case 'dbt_run_all_failed':
            console.log('🔴 [WS] dbt_run_all_failed - Clearing flag and refreshing');
            console.log('🔴 [WS] dbtRunStartTime before clear:', dbtRunStartTime);
            dbtRunStartTime = null;

            // Extract source name if this was triggered by a source operation
            const sourceMatchFailed = source?.match(/triggered by (\w+)/);
            if (sourceMatchFailed) {
                const triggeredSource = sourceMatchFailed[1];
                console.log('🔴 [WS] dbt failure was triggered by source:', triggeredSource);
                // DON'T clear file operations here - batch timeout will handle it
                // DO clear activeSyncs since the operation failed
                if (activeSyncs.has(triggeredSource)) {
                    activeSyncs.delete(triggeredSource);
                    updateSyncCounter();
                    console.log('🔴 [WS] Cleared activeSyncs for:', triggeredSource);
                }
            }

            console.log('🔴 [WS] Cleared flag. Now refreshing UI...');
            addLogEntry('error', message, source || 'dbt');
            // Don't show toast - error visible in activity log and status will show
            // Refresh both dbt models and sources (only if no active file operations)
            const totalFileOpsAllFailed = getTotalFileOperations();
            if (totalFileOpsAllFailed === 0) {
                loadDbtModels();
                if (!isLoadingSources) {
                    console.log('🔴 [WS] Also calling loadSources()');
                    loadSources();
                }
            } else {
                console.log('🔴 [WS] Skipping refresh - active file operations:', totalFileOpsAllFailed);
            }
            break;

        default:
            console.log('❓ [WS] Unknown WebSocket event:', event);
    }
}

function updateConnectionStatus(connected) {
    const indicator = document.getElementById('status-indicator');
    const text = document.getElementById('status-text');

    if (connected) {
        indicator.className = 'h-2 w-2 bg-green-500 rounded-full';
        text.textContent = 'Connected';
        text.className = 'text-xs sm:text-sm text-green-600';
    } else {
        indicator.className = 'h-2 w-2 bg-red-500 rounded-full animate-pulse';
        text.textContent = 'Disconnected';
        text.className = 'text-xs sm:text-sm text-red-600';
    }
}

function updateSyncCounter() {
    const count = activeSyncs.size;
    const text = document.getElementById('status-text');

    if (!text) return;

    // If syncing, show count
    if (count > 0) {
        const plural = count === 1 ? 'source' : 'sources';
        text.textContent = `Syncing ${count} ${plural}...`;
        text.className = 'text-xs sm:text-sm text-blue-600 font-medium animate-pulse-slow';
    } else if (ws && ws.readyState === WebSocket.OPEN) {
        // Connected and not syncing
        text.textContent = 'Connected';
        text.className = 'text-xs sm:text-sm text-green-600';
    }
}

// ============================================================================
// API Calls
// ============================================================================

async function apiCall(endpoint, method = 'GET', body = null, timeoutMs = 5000) {
    // Create abort controller for timeout
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

    const options = {
        method,
        headers: {
            'Content-Type': 'application/json',
        },
        signal: controller.signal,
    };

    if (body) {
        options.body = JSON.stringify(body);
    }

    try {
        const response = await fetch(endpoint, options);
        clearTimeout(timeoutId);

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }

        return await response.json();
    } catch (error) {
        clearTimeout(timeoutId);
        if (error.name === 'AbortError') {
            console.warn(`API call timed out after ${timeoutMs}ms: ${endpoint}`);
            throw new Error(`Request timed out (database may be busy)`);
        }
        console.error(`API call failed: ${endpoint}`, error);
        throw error;
    }
}

async function loadServiceStatus() {
    if (isLoadingStatus) return;

    // Skip polling if syncs are active (DuckDB locked, will timeout)
    const totalFileOps = getTotalFileOperations();
    if (activeSyncs.size > 0 || dbtRunStartTime !== null || totalFileOps > 0) {
        console.log('⏸️ [Status Poll] Skipping - active operations:', {
            syncs: activeSyncs.size,
            dbt: dbtRunStartTime !== null,
            fileOps: totalFileOps
        });
        return;
    }

    isLoadingStatus = true;

    try {
        // Longer timeout for service status check (Windows Docker Desktop is slower)
        const status = await apiCall('/api/status', 'GET', null, 15000);

        // Update service indicators
        updateServiceIndicator('service-api', status.services.api);
        updateServiceIndicator('service-duckdb', status.services.duckdb);
        updateServiceIndicator('service-metabase', status.services.metabase);
        updateServiceIndicator('service-dbt', status.services.dbt_docs);
    } catch (error) {
        console.error('Error loading service status:', error);
        showToast('Failed to load service status', 'error');

        // Set all services to unknown state
        ['service-api', 'service-duckdb', 'service-metabase', 'service-dbt'].forEach(id => {
            updateServiceIndicator(id, 'unknown');
        });
    } finally {
        isLoadingStatus = false;
    }
}

async function loadSources() {
    if (isLoadingSources) return;

    // Cancel any pending retry from previous call
    if (loadSourcesRetryTimeout) {
        console.log('⏸️ [loadSources] Cancelling pending retry');
        clearTimeout(loadSourcesRetryTimeout);
        loadSourcesRetryTimeout = null;
    }

    isLoadingSources = true;
    showSourcesLoading();

    try {
        // Longer timeout for sources (includes DuckDB queries for row counts)
        sources = await apiCall('/api/sources', 'GET', null, 15000);
        renderSourcesTable();
    } catch (error) {
        console.log('⏸️ [loadSources] Error (expected during sync):', error.message);

        // If timeout (database busy), show a helpful message and retry
        if (error.message.includes('timed out')) {
            const tbody = document.getElementById('sources-table-body');

            // Only show loading message if we actually have active operations
            // This prevents flickering when timeout happens at the tail end
            const totalFileOps = getTotalFileOperations();
            if (activeSyncs.size > 0 || totalFileOps > 0 || dbtRunStartTime !== null) {
                tbody.innerHTML = `
                    <tr>
                        <td colspan="6" class="px-6 py-8 text-center text-gray-500">
                            <div class="flex flex-col items-center space-y-3">
                                <div class="spinner"></div>
                                <p>Processing files...</p>
                                <p class="text-sm text-gray-400">${totalFileOps > 0 ? 'Syncing and transforming data' : 'Please wait'}</p>
                            </div>
                        </td>
                    </tr>
                `;
            }

            // DON'T retry if there are active syncs, dbt runs, or file operations
            // This prevents cascading retry attempts when multiple files are uploading
            if (activeSyncs.size > 0 || dbtRunStartTime !== null || totalFileOps > 0) {
                console.log('⏸️ [loadSources] Skipping retry - active operations:', {
                    syncs: activeSyncs.size,
                    dbt: dbtRunStartTime !== null,
                    fileOps: totalFileOps
                });
                isLoadingSources = false;
                return;
            }

            // Only retry if no active operations (might be a stale lock)
            isLoadingSources = false;
            console.log('⏸️ [loadSources] Scheduling retry in 3s (no active operations)');
            loadSourcesRetryTimeout = setTimeout(() => {
                loadSourcesRetryTimeout = null;
                loadSources();
            }, 3000);
            return;
        }

        showToast('Failed to load data sources', 'error');
        showSourcesError();
    } finally {
        isLoadingSources = false;
    }
}

async function loadActivityLogs() {
    try {
        // Load up to 50 most recent logs from backend for quick scanning
        const logs = await apiCall('/api/logs?limit=50');

        // Convert backend log format to frontend format - preserve all fields
        activityLog = logs.map(log => ({
            timestamp: new Date(log.timestamp).toLocaleTimeString(),
            level: log.level || 'info',
            source: log.source || 'system',
            message: log.message || ''
        }));

        renderActivityLog();
        console.log(`Loaded ${activityLog.length} activity log entries`);
    } catch (error) {
        console.error('Error loading activity logs:', error);
        // Don't show toast - non-critical error, logs will populate from WebSocket
    }
}

async function triggerSync(sourceName) {
    // Prevent duplicate sync requests
    if (activeSyncs.has(sourceName)) {
        showToast(`${sourceName} is already syncing`, 'warning');
        return;
    }

    // Prevent sync if there are active file operations for this source
    if (activeFileOperations.has(sourceName)) {
        const count = activeFileOperations.get(sourceName).size;
        console.log(`⏸️ [triggerSync] Blocking sync for ${sourceName} - ${count} file operation(s) in progress`);
        showToast(`${sourceName} has ${count} file operation(s) in progress, please wait`, 'warning');
        return;
    }

    try {
        // Optimistically update UI
        activeSyncs.set(sourceName, Date.now());
        updateSourceStatus(sourceName, 'syncing');
        addLogEntry('info', `Triggering sync`, sourceName);

        const response = await apiCall(`/api/sources/${sourceName}/sync`, 'POST', {
            full_refresh: false
        }, 30000);  // 30s timeout for sync operations

        if (response.success) {
            showToast(`Sync started for ${sourceName}`, 'info');
        } else {
            // Handle non-successful response
            throw new Error(response.message || 'Unknown error');
        }
    } catch (error) {
        console.error('Error triggering sync:', error);
        showToast(`Failed to start sync for ${sourceName}`, 'error');
        addLogEntry('error', `Failed to trigger sync: ${error.message}`, sourceName);

        // Revert optimistic update
        activeSyncs.delete(sourceName);
        updateSourceStatus(sourceName, 'failed');
    }
}

// ============================================================================
// UI Updates
// ============================================================================

function updateServiceIndicator(elementId, status) {
    const element = document.getElementById(elementId);
    if (!element) return;

    const indicator = element.querySelector('.rounded-full');
    if (!indicator) return;

    // Remove existing classes
    indicator.className = 'inline-block h-2 w-2 rounded-full';

    // Add status-specific class
    switch (status) {
        case 'running':
        case 'healthy':
            indicator.classList.add('bg-green-500');
            break;
        case 'not_initialized':
            indicator.classList.add('bg-yellow-500');
            break;
        case 'stopped':
        case 'not_found':
            indicator.classList.add('bg-red-500');
            break;
        default:
            indicator.classList.add('bg-gray-400');
    }
}

async function fetchPlatformHealth() {
    try {
        // Longer timeout for platform health (includes DuckDB queries and disk checks)
        const health = await apiCall('/api/health/platform', 'GET', null, 15000);
        updateHealthWidget(health);
    } catch (error) {
        console.error('Error fetching platform health:', error);
        // Silently fail - health widget is optional
    }
}

function updateHealthWidget(health) {
    const widget = document.getElementById('health-widget');
    const icon = document.getElementById('health-icon');
    const statusText = document.getElementById('health-status-text');

    if (!widget || !icon || !statusText) return;

    // Remove existing border colors
    widget.classList.remove('border-green-500', 'border-yellow-500', 'border-red-500', 'border-gray-400');

    // Update based on status
    if (health.status === 'healthy') {
        widget.classList.add('border-green-500');
        icon.textContent = '✅';
        statusText.textContent = 'All Systems Healthy';
        statusText.className = 'text-lg font-semibold text-green-800';
    } else if (health.status === 'warning') {
        widget.classList.add('border-yellow-500');
        icon.textContent = '⚠️';
        const warningCount = health.warnings.length;
        statusText.textContent = `${warningCount} Warning${warningCount > 1 ? 's' : ''}`;
        statusText.className = 'text-lg font-semibold text-yellow-800';
    } else if (health.status === 'critical') {
        widget.classList.add('border-red-500');
        icon.textContent = '❌';
        const issueCount = health.critical_issues.length + health.warnings.length;
        statusText.textContent = `${issueCount} Issue${issueCount > 1 ? 's' : ''} Detected`;
        statusText.className = 'text-lg font-semibold text-red-800';
    } else {
        widget.classList.add('border-gray-400');
        icon.textContent = '⏳';
        statusText.textContent = 'Unknown Status';
        statusText.className = 'text-lg font-semibold text-gray-800';
    }
}

function showSourcesLoading() {
    const tbody = document.getElementById('sources-table-body');
    tbody.innerHTML = `
        <tr>
            <td colspan="6" class="px-6 py-8 text-center text-gray-500">
                <div class="flex flex-col items-center space-y-3">
                    <div class="spinner"></div>
                    <p>Loading sources...</p>
                </div>
            </td>
        </tr>
    `;
}

function renderSourcesTable() {
    console.log('🎨 [renderSourcesTable] Called with sources:', sources.length, 'activeSyncs:', activeSyncs.size);
    const tbody = document.getElementById('sources-table-body');

    if (!sources || sources.length === 0) {
        console.log('🎨 [renderSourcesTable] No sources to render!');

        tbody.innerHTML = `
            <tr>
                <td colspan="6" class="px-6 py-8 text-center text-gray-500">
                    <div class="flex flex-col items-center space-y-2">
                        <svg class="w-12 h-12 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0v5a2 2 0 01-2 2H6a2 2 0 01-2-2v-5m16 0h-2.586a1 1 0 00-.707.293l-2.414 2.414a1 1 0 01-.707.293h-3.172a1 1 0 01-.707-.293l-2.414-2.414A1 1 0 006.586 13H4"></path>
                        </svg>
                        <p>No sources configured</p>
                        <p class="text-sm">Run <code class="bg-gray-100 px-2 py-1 rounded">dango source add</code> to add a source</p>
                    </div>
                </td>
            </tr>
        `;
        return;
    }

    tbody.innerHTML = sources.map(source => {
        const isSyncing = activeSyncs.has(source.name);
        const hasFileOps = activeFileOperations.has(source.name);
        const isDisabled = isSyncing || hasFileOps;

        console.log(`🎨 [renderSourcesTable] Source "${source.name}": isSyncing=${isSyncing}, hasFileOps=${hasFileOps}, status="${source.status}"`);

        const buttonDisabled = isDisabled ? 'disabled' : '';
        const buttonText = isSyncing ? 'Syncing...' : (hasFileOps ? 'Processing...' : 'Sync Now');

        // For CSV sources, clicking the row opens upload modal
        // For other sources, clicking the row opens detail modal
        const rowClickHandler = source.type === 'csv'
            ? `openCsvUploadModal('${source.name}')`
            : `openSourceDetail('${source.name}')`;

        const rowClickHelp = source.type === 'csv'
            ? 'Click to upload CSV files'
            : 'Click to view details';

        return `
        <tr id="source-${source.name}" class="hover:bg-gray-50 transition-colors duration-150 cursor-pointer"
            onclick="${rowClickHandler}" title="${rowClickHelp}">
            <td class="px-6 py-4 whitespace-nowrap">
                <div class="flex items-center">
                    <div class="text-sm font-medium text-gray-900">${source.name}</div>
                </div>
            </td>
            <td class="px-6 py-4 whitespace-nowrap">
                <span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-gray-100 text-gray-800">
                    ${source.type}
                </span>
            </td>
            <td class="px-6 py-4 whitespace-nowrap">
                ${renderStatusPill(source, isSyncing, hasFileOps)}
            </td>
            <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                ${renderRowCount(source)}
            </td>
            <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                ${formatRelativeTime(source.last_sync)}
            </td>
            <td class="px-6 py-4 whitespace-nowrap text-right text-sm font-medium">
                <button
                    onclick="event.stopPropagation(); triggerSync('${source.name}')"
                    class="text-blue-600 hover:text-blue-900 disabled:text-gray-400 disabled:cursor-not-allowed transition-colors duration-150"
                    id="sync-btn-${source.name}"
                    ${buttonDisabled}
                >
                    ${buttonText}
                </button>
            </td>
        </tr>
        `;
    }).join('');
}

function getStatusBadge(status) {
    const badges = {
        'synced': '<span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-green-100 text-green-800">Synced</span>',
        'syncing': '<span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-blue-100 text-blue-800 animate-pulse">Syncing...</span>',
        'processing': '<span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-purple-100 text-purple-800 animate-pulse">Processing...</span>',
        'empty': '<span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-yellow-100 text-yellow-800">Empty</span>',
        'not_synced': '<span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-gray-100 text-gray-800">Not Synced</span>',
        'failed': '<span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-red-100 text-red-800">Failed</span>',
    };

    return badges[status] || badges['not_synced'];
}

/**
 * Render row count with table breakdown for multi-resource sources
 *
 * @param {object} source - Source object with row_count and optional tables array
 * @returns {string} HTML for row count display
 */
function renderRowCount(source) {
    if (source.row_count === null || source.row_count === undefined) {
        return '-';
    }

    // If no tables breakdown or single table, show simple count
    if (!source.tables || source.tables.length <= 1) {
        return source.row_count.toLocaleString();
    }

    // Multi-resource source: show breakdown with clean bullets (no monospace needed)
    const tablesList = source.tables
        .map(t => `<div class="text-xs text-gray-600 pl-4 py-0.5">• <span class="font-medium">${t.name}</span>: ${t.row_count.toLocaleString()} rows</div>`)
        .join('');

    return `
        <div class="flex flex-col">
            <div class="font-medium text-sm">${source.tables.length} tables, ${source.row_count.toLocaleString()} total rows</div>
            <div class="mt-1">${tablesList}</div>
        </div>
    `;
}

/**
 * Render status pill with freshness information
 *
 * @param {object} source - Source object with freshness data
 * @param {boolean} isSyncing - Whether source is currently syncing
 * @param {boolean} hasFileOps - Whether source has file operations in progress
 * @returns {string} HTML for status pill
 */
function renderStatusPill(source, isSyncing, hasFileOps) {
    // Handle active states first
    if (isSyncing) {
        return '<span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-blue-100 text-blue-800 animate-pulse">⏳ Syncing...</span>';
    }

    if (hasFileOps) {
        return '<span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-purple-100 text-purple-800 animate-pulse">⚙️ Processing...</span>';
    }

    // Use freshness data if available
    const freshness = source.freshness;

    if (!freshness || freshness.status === 'never_synced') {
        return '<span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-gray-100 text-gray-800">⚪ Never Synced</span>';
    }

    if (freshness.status === 'failed') {
        return `<span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-red-100 text-red-800">❌ Failed</span>`;
    }

    if (freshness.status === 'empty') {
        return '<span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-yellow-100 text-yellow-800">⚪ No Data</span>';
    }

    // All successful syncs show uniform status (timestamp shown in Last Sync column)
    if (freshness.status === 'synced') {
        return '<span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-green-100 text-green-800">✓ Synced</span>';
    }

    // Fallback for any other status
    return '<span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-gray-100 text-gray-800">✓ Synced</span>';
}

function updateSourceStatus(sourceName, newStatus) {
    console.log(`🔄 [updateSourceStatus] Setting ${sourceName} to "${newStatus}"`);

    // Update the sources array if it exists (for consistency)
    const source = sources.find(s => s.name === sourceName);
    if (source) {
        source.status = newStatus;
        console.log(`🔄 [updateSourceStatus] Updated sources array for ${sourceName}`);
    }

    // ALWAYS update the DOM directly, regardless of array state
    // This ensures status updates work even before sources array is loaded
    const row = document.getElementById(`source-${sourceName}`);
    console.log(`🔄 [updateSourceStatus] Row element found:`, !!row);

    if (row) {
        const statusCell = row.querySelector('td:nth-child(3)');
        if (statusCell) {
            statusCell.innerHTML = getStatusBadge(newStatus);
        }

        // Disable/enable sync button
        const syncBtn = document.getElementById(`sync-btn-${sourceName}`);
        if (syncBtn) {
            syncBtn.disabled = newStatus === 'syncing';
            syncBtn.textContent = newStatus === 'syncing' ? 'Syncing...' : 'Sync Now';
        }
    } else {
        // Row doesn't exist yet - table hasn't been rendered
        // Force a re-render of the table if we have sources data
        if (sources && sources.length > 0) {
            renderSourcesTable();
        }
    }
}

function showSourcesError() {
    const tbody = document.getElementById('sources-table-body');
    tbody.innerHTML = `
        <tr>
            <td colspan="6" class="px-6 py-8 text-center text-red-500">
                <div class="flex flex-col items-center space-y-2">
                    <svg class="w-12 h-12" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path>
                    </svg>
                    <p>Error loading sources</p>
                    <button onclick="loadSources()" class="text-sm text-blue-600 hover:text-blue-800">Retry</button>
                </div>
            </td>
        </tr>
    `;
}

// ============================================================================
// Activity Log
// ============================================================================

function addLogEntry(level, message, source = 'system') {
    const timestamp = new Date().toLocaleTimeString();
    const entry = { timestamp, level, source, message };

    activityLog.unshift(entry);

    // Limit log size
    if (activityLog.length > MAX_LOG_ENTRIES) {
        activityLog = activityLog.slice(0, MAX_LOG_ENTRIES);
    }

    renderActivityLog();
}

function renderActivityLog() {
    const logContainer = document.getElementById('activity-log');

    if (activityLog.length === 0) {
        logContainer.innerHTML = '<tr><td colspan="4" class="px-6 py-8 text-center text-gray-500 text-sm">No recent activity</td></tr>';
        return;
    }

    logContainer.innerHTML = activityLog.map(entry => {
        const levelBadge = getLevelBadge(entry.level);
        const sourceDisplay = entry.source || 'system';
        // Keep original formatting - don't trim whitespace, preserve newlines
        const formattedMessage = entry.message || '';

        return `
            <tr class="hover:bg-gray-50 transition-colors duration-150">
                <td class="px-4 py-3 whitespace-nowrap">${levelBadge}</td>
                <td class="px-4 py-3 whitespace-nowrap text-sm text-gray-600">${sourceDisplay}</td>
                <td class="px-4 py-3 whitespace-nowrap text-xs text-gray-500">${entry.timestamp}</td>
                <td class="px-4 py-3 text-sm text-gray-900"><pre class="log-message">${formattedMessage}</pre></td>
            </tr>
        `;
    }).join('');
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

function clearLog() {
    activityLog = [];
    renderActivityLog();
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

    // Trigger animation
    setTimeout(() => {
        toast.classList.remove('opacity-0', 'translate-x-full');
    }, 10);

    // Remove after 4 seconds
    setTimeout(() => {
        toast.classList.add('opacity-0', 'translate-x-full');
        setTimeout(() => {
            container.removeChild(toast);
        }, 300);
    }, 4000);
}

// ============================================================================
// Global Functions (called from HTML)
// ============================================================================

function refreshSources() {
    showToast('Refreshing sources...', 'info');
    loadSources();
}

// ============================================================================
// CSV Upload Modal
// ============================================================================

async function openCsvUploadModal(sourceName) {
    // Find the source
    const source = sources.find(s => s.name === sourceName);
    if (!source || source.type !== 'csv') {
        showToast('Invalid CSV source', 'error');
        return;
    }

    // Show modal
    document.getElementById('upload-modal').classList.remove('hidden');

    // Set source name
    document.getElementById('upload-source-name').textContent = sourceName;

    // Reset form
    document.getElementById('csv-upload-form').reset();

    // Populate configuration from source details
    try {
        const details = await apiCall(`/api/sources/${sourceName}/details`);
        const csvConfig = details.config.csv || {};

        // Display directory
        document.getElementById('upload-directory').textContent = csvConfig.directory || 'data';

        // Display file pattern
        document.getElementById('upload-file-pattern').textContent = csvConfig.file_pattern || '*.csv';

        // Display regeneration notes (if present)
        const notesContainer = document.getElementById('upload-notes-container');
        const notesSpan = document.getElementById('upload-notes');
        if (csvConfig.notes && csvConfig.notes.trim()) {
            notesSpan.textContent = csvConfig.notes;
            notesContainer.style.display = 'block';
        } else {
            notesContainer.style.display = 'none';
        }

    } catch (error) {
        console.error('Error loading source config:', error);
        showToast('Failed to load source configuration', 'error');
    }

    // Load CSV files
    loadCsvFilesList(sourceName);
}

async function loadCsvFilesList(sourceName) {
    const container = document.getElementById('csv-files-list');

    try {
        const data = await apiCall(`/api/sources/${sourceName}/csv-files`);

        if (!data.files || data.files.length === 0) {
            container.innerHTML = `
                <p class="text-gray-500 text-center py-2">No CSV files found</p>
            `;
            return;
        }

        // Group files by status
        const filesHtml = data.files.map(file => {
            let statusBadge = '';
            let statusColor = '';
            let statusIcon = '';

            if (file.on_disk && file.loaded) {
                statusBadge = 'Loaded';
                statusColor = 'bg-green-100 text-green-800';
                statusIcon = '✓';
            } else if (file.on_disk && !file.loaded) {
                statusBadge = 'Not loaded';
                statusColor = 'bg-yellow-100 text-yellow-800';
                statusIcon = '◉';
            } else if (!file.on_disk && file.loaded) {
                statusBadge = 'Deleted';
                statusColor = 'bg-red-100 text-red-800';
                statusIcon = '⚠';
            }

            const sizeDisplay = file.size ? `${(file.size / 1024).toFixed(1)} KB` : '-';
            const rowsDisplay = file.rows_loaded ? `${file.rows_loaded.toLocaleString()} rows` : '';

            // Add delete button for files on disk
            const safeFilename = file.filename.replace(/[^a-zA-Z0-9]/g, '_');
            const deleteButton = file.on_disk ?
                `<button
                    id="delete-btn-${safeFilename}"
                    onclick="handleFileDelete('${sourceName}', '${file.path}', '${file.filename}', '${safeFilename}')"
                    class="ml-2 text-red-600 hover:text-red-800 text-xs font-medium"
                    title="Delete file">
                    Delete
                </button>` : '';

            return `
                <div class="flex items-center justify-between py-2 border-b border-blue-100 last:border-0">
                    <div class="flex-1 min-w-0">
                        <div class="font-medium text-gray-900 truncate">${file.filename}</div>
                        <div class="text-xs text-gray-500">${sizeDisplay}${rowsDisplay ? ' • ' + rowsDisplay : ''}</div>
                    </div>
                    <div class="flex items-center gap-2">
                        <span class="inline-flex items-center px-2 py-1 rounded text-xs font-medium ${statusColor}">
                            ${statusIcon} ${statusBadge}
                        </span>
                        ${deleteButton}
                    </div>
                </div>
            `;
        }).join('');

        container.innerHTML = `
            <div class="max-h-48 overflow-y-auto space-y-1">
                ${filesHtml}
            </div>
            <div class="mt-2 pt-2 border-t border-blue-200 text-xs text-gray-500">
                ${data.files_on_disk} file(s) on disk • ${data.files_loaded} file(s) loaded
            </div>
        `;

    } catch (error) {
        console.error('Error loading CSV files:', error);
        container.innerHTML = `
            <p class="text-red-500 text-center py-2">Failed to load file list</p>
        `;
    }
}

async function handleFileDelete(sourceName, filePath, filename, safeFilename) {
    // Confirm deletion
    const confirmed = confirm(`Are you sure you want to delete "${filename}"?\n\nThis will immediately:\n• Remove the file from disk\n• Remove all associated data from the database\n\nThis action cannot be undone.`);

    if (!confirmed) {
        return;
    }

    // Track this delete operation to prevent premature loadSources() retries
    const operationId = `delete-${Date.now()}`;
    addFileOperation(sourceName, operationId);

    // Close modal immediately to prevent user from clicking other actions
    showToast(`Deleting ${filename}...`, 'info');
    closeUploadModal();
    addLogEntry('info', `Deleting file: ${filename}`, sourceName);

    // OPTIMISTIC UPDATE: Show syncing BEFORE delete starts
    console.log('🗑️ [Delete] Setting optimistic syncing state BEFORE fetch');
    activeSyncs.set(sourceName, Date.now());
    updateSyncCounter();
    renderSourcesTable();

    try {
        // Call DELETE endpoint
        const response = await fetch(`/api/sources/${sourceName}/csv-files?file_path=${encodeURIComponent(filePath)}`, {
            method: 'DELETE'
        });

        const result = await response.json();

        if (response.ok && result.success) {
            showToast(result.message, 'success');
            addLogEntry('success', `Deleted file: ${filename}`, sourceName);
            // Syncing state already set optimistically before delete
            // WebSocket events will take over and eventually clear it

            // Don't call loadSources immediately - let WebSocket events handle updates
            // This prevents page freeze from querying locked DuckDB during sync
        } else {
            throw new Error(result.detail || 'Delete failed');
        }
    } catch (error) {
        console.error('Error deleting file:', error);
        showToast(`Failed to delete file: ${error.message}`, 'error');
        addLogEntry('error', `Failed to delete file: ${filename} - ${error.message}`, sourceName);
        // Revert optimistic update on error
        activeSyncs.delete(sourceName);
        updateSyncCounter();
        renderSourcesTable();
    } finally {
        // Note: Don't remove the operation here - it will be cleared by the WebSocket
        // event handler when dbt_run_all_completed fires for this source
        // This ensures we don't trigger premature loadSources() retries
    }
}

// Make function available globally
window.handleFileDelete = handleFileDelete;

function closeUploadModal() {
    document.getElementById('upload-modal').classList.add('hidden');
    document.getElementById('upload-progress').classList.add('hidden');

    // Clear selected files display
    const filesList = document.getElementById('selected-files');
    const filesListContainer = document.getElementById('selected-files-list');
    if (filesList) filesList.innerHTML = '';
    if (filesListContainer) filesListContainer.classList.add('hidden');
}

function syncSourceFromModal() {
    const sourceName = document.getElementById('upload-source-name').textContent;
    if (sourceName) {
        // Close modal first
        closeUploadModal();
        // Trigger sync
        triggerSync(sourceName);
    }
}

window.openCsvUploadModal = openCsvUploadModal;
window.syncSourceFromModal = syncSourceFromModal;

async function handleCsvUpload() {
    const fileInput = document.getElementById('csv-file');
    const sourceName = document.getElementById('upload-source-name').textContent;

    if (!fileInput.files || fileInput.files.length === 0) {
        showToast('Please select at least one CSV file', 'error');
        return;
    }

    const files = Array.from(fileInput.files);
    const fileNames = files.map(f => f.name).join(', ');

    // Track this upload batch to prevent premature loadSources() retries
    // Files are uploaded to disk first, then ONE sync runs for all files at the end
    const operationId = `upload-batch-${Date.now()}`;
    const fileCount = files.length;
    addFileOperation(sourceName, operationId);
    console.log(`📤 [Upload] Tracking batch of ${fileCount} files under operation ${operationId}`);

    // Close modal immediately to prevent user from clicking other actions
    showToast(`Uploading ${files.length} file(s) to disk...`, 'info');
    closeUploadModal();

    // Declare at function scope so setTimeout can access it
    let successCount = 0;
    let failCount = 0;
    let uploadComplete = false;

    try {
        // Upload each file sequentially
        for (const file of files) {
            const formData = new FormData();
            formData.append('file', file);

            try {
                const response = await fetch(`/api/sources/${sourceName}/upload-csv`, {
                    method: 'POST',
                    body: formData
                });

                const result = await response.json();

                if (response.ok && result.success) {
                    successCount++;
                    addLogEntry('success', `CSV uploaded: ${file.name}`, sourceName);
                } else {
                    failCount++;
                    addLogEntry('error', `CSV upload failed: ${file.name} - ${result.detail || 'Unknown error'}`, sourceName);
                }
            } catch (error) {
                failCount++;
                addLogEntry('error', `CSV upload failed: ${file.name} - ${error.message}`, sourceName);
            }
        }

        // Show result summary
        if (successCount > 0) {
            const fileWord = successCount === 1 ? 'file' : 'files';
            showToast(`Successfully uploaded ${successCount} ${fileWord} to disk. Syncing...`, 'success');

            // Now trigger ONE sync for ALL uploaded files
            console.log(`📤 [Upload] Triggering single sync for ${successCount} uploaded files`);
            try {
                // Call the sync endpoint to process all uploaded files in one batch
                // Use longer timeout (30s) as sync may take time to respond even though it runs in background
                const syncResponse = await apiCall(`/api/sources/${sourceName}/sync`, 'POST', {
                    full_refresh: false
                }, 30000);
                console.log('📤 [Upload] Sync triggered successfully, WebSocket events will track progress');
                // WebSocket events will handle activeSyncs state and UI updates
            } catch (syncError) {
                console.error('Error triggering sync:', syncError);
                showToast(`Files uploaded but sync failed: ${syncError.message}`, 'error');
                // Clean up operation tracking on sync failure
                removeFileOperation(sourceName, operationId);
            }
        }

        if (failCount > 0) {
            if (successCount > 0) {
                showToast(`Uploaded ${successCount} file(s), ${failCount} failed`, 'warning');
            } else {
                showToast(`All ${failCount} file(s) failed to upload`, 'error');
                // Clean up operation tracking if all failed
                removeFileOperation(sourceName, operationId);
            }
        }

        // Mark upload as complete so finally block knows state
        uploadComplete = true;
    } catch (error) {
        console.error('Error uploading CSV:', error);
        showToast(`Upload failed: ${error.message}`, 'error');
        addLogEntry('error', `Upload failed: ${error.message}`, sourceName);
        // Clear operation on error
        removeFileOperation(sourceName, operationId);
    } finally {
        // Clean up file input
        fileInput.value = '';

        // Clear the displayed selected files list
        const filesList = document.getElementById('selected-files');
        const filesListContainer = document.getElementById('selected-files-list');
        if (filesList) filesList.innerHTML = '';
        if (filesListContainer) filesListContainer.classList.add('hidden');
    }
}

// Handle CSV upload form submission - set up on page load
document.addEventListener('DOMContentLoaded', () => {
    const uploadForm = document.getElementById('csv-upload-form');
    if (uploadForm) {
        uploadForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            await handleCsvUpload();
        });
    }

    // Handle file selection display
    const fileInput = document.getElementById('csv-file');
    if (fileInput) {
        fileInput.addEventListener('change', (e) => {
            const files = e.target.files;
            const filesList = document.getElementById('selected-files');
            const filesListContainer = document.getElementById('selected-files-list');

            if (files.length > 0) {
                // Clear previous list
                filesList.innerHTML = '';

                // Add each file to the list
                Array.from(files).forEach(file => {
                    const li = document.createElement('li');
                    li.textContent = file.name;
                    filesList.appendChild(li);
                });

                // Show the list
                filesListContainer.classList.remove('hidden');
            } else {
                // Hide the list if no files selected
                filesListContainer.classList.add('hidden');
            }
        });
    }
});

// ============================================================================
// Source Detail Modal
// ============================================================================

async function openSourceDetail(sourceName) {
    // Show modal and loading state
    document.getElementById('source-detail-modal').classList.remove('hidden');
    document.getElementById('detail-loading').classList.remove('hidden');
    document.getElementById('detail-content').classList.add('hidden');
    document.getElementById('detail-source-name').textContent = sourceName;

    try {
        // Fetch source details
        const details = await apiCall(`/api/sources/${sourceName}/details`);

        // Update stats with table breakdown if applicable
        const detailRowCountElement = document.getElementById('detail-row-count');
        if (details.row_count === null) {
            detailRowCountElement.textContent = '-';
        } else if (details.tables && details.tables.length > 1) {
            // Multi-resource source: show table breakdown
            const tablesList = details.tables
                .map(t => `<div class="text-xs text-gray-600 pl-4 py-0.5">• <span class="font-medium">${t.name}</span>: ${t.row_count.toLocaleString()} rows</div>`)
                .join('');

            detailRowCountElement.innerHTML = `
                <div class="flex flex-col">
                    <div class="font-medium text-sm">${details.tables.length} tables, ${details.row_count.toLocaleString()} total rows</div>
                    <div class="mt-1">${tablesList}</div>
                </div>
            `;
        } else {
            // Single resource or no breakdown: show simple count
            detailRowCountElement.textContent = details.row_count.toLocaleString();
        }

        // Display freshness information
        const freshnessElement = document.getElementById('detail-freshness');
        if (freshnessElement && details.freshness) {
            const freshness = details.freshness;
            let freshnessHTML = '';

            if (freshness.status === 'never_synced') {
                freshnessHTML = '<span class="px-3 py-1 text-sm font-semibold rounded-full bg-gray-100 text-gray-800">⚪ Never Synced</span>';
            } else if (freshness.status === 'failed') {
                const timeAgo = formatRelativeTime(freshness.last_sync_time);
                freshnessHTML = `<span class="px-3 py-1 text-sm font-semibold rounded-full bg-red-100 text-red-800">❌ Failed ${timeAgo}</span>`;
            } else {
                // All successful syncs show uniform status
                const hoursAgo = freshness.hours_since_sync;

                if (hoursAgo !== null) {
                    const hoursText = hoursAgo < 1 ? `${Math.round(hoursAgo * 60)}m` : `${Math.round(hoursAgo)}h`;
                    freshnessHTML = `<span class="px-3 py-1 text-sm font-semibold rounded-full bg-green-100 text-green-800">✓ Synced (${hoursText} ago)</span>`;
                } else {
                    freshnessHTML = '<span class="px-3 py-1 text-sm font-semibold rounded-full bg-green-100 text-green-800">✓ Synced</span>';
                }
            }

            freshnessElement.innerHTML = freshnessHTML;
        }

        const history = details.history || [];
        document.getElementById('detail-sync-count').textContent = history.length;

        if (history.length > 0) {
            // Check if last sync failed and display error prominently
            if (history[0].status === 'failed' && history[0].error_message) {
                const errorAlert = document.getElementById('detail-error-alert');
                const errorMessage = document.getElementById('detail-error-message').querySelector('pre');
                errorAlert.classList.remove('hidden');
                errorMessage.textContent = history[0].error_message;
            } else {
                document.getElementById('detail-error-alert').classList.add('hidden');
            }
        } else {
            document.getElementById('detail-error-alert').classList.add('hidden');
        }

        // Display configuration with custom rendering for different source types
        const configElement = document.getElementById('detail-config');

        if (details.config.type === 'stripe' && details.config.stripe) {
            // Clean rendering for Stripe sources - only show user-relevant info
            const stripeConfig = details.config.stripe;
            const endpoints = stripeConfig.endpoints || [];
            const startDate = stripeConfig.start_date ? new Date(stripeConfig.start_date).toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' }) : 'Not specified';

            configElement.innerHTML = `<div class="space-y-3">
<div><span class="font-semibold text-gray-700">Data Sources:</span> <span class="text-gray-900">${endpoints.join(', ')}</span></div>
<div><span class="font-semibold text-gray-700">Syncing From:</span> <span class="text-gray-900">${startDate}</span></div>
${details.config.description ? `<div><span class="font-semibold text-gray-700">Notes:</span> <span class="text-gray-900">${details.config.description}</span></div>` : ''}
</div>`;
        } else if (details.config.type === 'csv' && details.config.csv) {
            // Clean rendering for CSV sources - only show user-relevant info
            const csvConfig = details.config.csv;

            configElement.innerHTML = `<div class="space-y-3">
<div><span class="font-semibold text-gray-700">Upload Location:</span> <span class="text-gray-900">${csvConfig.directory || 'data'}</span></div>
<div><span class="font-semibold text-gray-700">File Pattern:</span> <span class="text-gray-900">${csvConfig.file_pattern || '*.csv'}</span></div>
${details.config.description ? `<div><span class="font-semibold text-gray-700">Notes:</span> <span class="text-gray-900">${details.config.description}</span></div>` : ''}
</div>`;
        } else {
            // Fallback: formatted JSON for other source types
            configElement.textContent = JSON.stringify(details.config, null, 2);
        }

        // Render history table
        renderSourceHistory(history);

        // Show content, hide loading
        document.getElementById('detail-loading').classList.add('hidden');
        document.getElementById('detail-content').classList.remove('hidden');

    } catch (error) {
        console.error('Error loading source details:', error);
        showToast(`Failed to load details for ${sourceName}`, 'error');
        closeSourceDetail();
    }
}

function closeSourceDetail() {
    document.getElementById('source-detail-modal').classList.add('hidden');
}

function renderSourceHistory(history) {
    const tbody = document.getElementById('detail-history');
    const noHistory = document.getElementById('detail-no-history');

    if (!history || history.length === 0) {
        tbody.innerHTML = '';
        noHistory.classList.remove('hidden');
        return;
    }

    noHistory.classList.add('hidden');

    tbody.innerHTML = history.map(entry => {
        const timestamp = new Date(entry.timestamp).toLocaleString();
        const statusBadge = entry.status === 'success'
            ? '<span class="px-2 py-1 text-xs rounded-full bg-green-100 text-green-800">Success</span>'
            : '<span class="px-2 py-1 text-xs rounded-full bg-red-100 text-red-800">Failed</span>';

        const duration = entry.duration_seconds
            ? `${entry.duration_seconds}s`
            : '-';

        const syncType = entry.full_refresh ? 'Full Refresh' : 'Incremental';

        let rowContent = `
            <tr class="hover:bg-gray-50">
                <td class="px-4 py-3 text-sm text-gray-900">${timestamp}</td>
                <td class="px-4 py-3">${statusBadge}</td>
                <td class="px-4 py-3 text-sm text-gray-900">${duration}</td>
                <td class="px-4 py-3 text-sm text-gray-600">${syncType}</td>
            </tr>
        `;

        // Add error row if there's an error message
        if (entry.error_message) {
            rowContent += `
                <tr class="bg-red-50">
                    <td colspan="4" class="px-4 py-2 text-sm text-red-700">
                        <strong>Error:</strong>
                        <pre class="log-message mt-1">${entry.error_message}</pre>
                    </td>
                </tr>
            `;
        }

        return rowContent;
    }).join('');
}

// ============================================================================
// dbt Models
// ============================================================================

let dbtModels = [];
// Track if dbt is currently running (timestamp when started, or null)
let dbtRunStartTime = null;
// Track individual models that are currently running (for per-model status)
let runningModels = new Set();

async function loadDbtModels(retryCount = 0) {
    try {
        const response = await apiCall('/api/dbt/models');
        dbtModels = response.models || [];
        renderDbtModelsTable();
        return dbtModels;  // Return the models array for WebSocket handler
    } catch (error) {
        console.error('Error loading dbt models:', error);

        // If timeout (database busy) and haven't retried too many times, retry
        if (error.message.includes('timed out') && retryCount < 3) {
            console.log(`Database busy, retrying dbt models load (attempt ${retryCount + 1}/3)...`);
            // Wait a bit before retrying (exponential backoff)
            await new Promise(resolve => setTimeout(resolve, 1000 * (retryCount + 1)));
            return loadDbtModels(retryCount + 1);
        }

        showDbtModelsError();
        return [];  // Return empty array on error
    }
}

function renderDbtModelsTable() {
    const tbody = document.getElementById('dbt-models-table-body');

    // If dbt is running but we don't have models data yet, show running state
    if (dbtRunStartTime !== null && (!dbtModels || dbtModels.length === 0)) {
        tbody.innerHTML = `
            <tr>
                <td colspan="7" class="px-6 py-8 text-center text-gray-500">
                    <p class="text-blue-600">dbt is running...</p>
                    <p class="text-sm mt-2">Loading model information</p>
                </td>
            </tr>
        `;
        return;
    }

    if (!dbtModels || dbtModels.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="7" class="px-6 py-8 text-center text-gray-500">
                    <p>No dbt models found</p>
                    <p class="text-sm mt-2">Run <code class="bg-gray-100 px-2 py-1 rounded">dbt compile</code> to generate manifest</p>
                </td>
            </tr>
        `;
        return;
    }

    tbody.innerHTML = dbtModels.map(model => {
        // Disable ALL buttons if ANY model is running (prevents concurrent runs and DuckDB locking)
        const anyModelRunning = runningModels.size > 0 || dbtRunStartTime !== null;
        const buttonDisabled = anyModelRunning ? 'disabled' : '';
        const buttonText = anyModelRunning ? 'Running...' : 'Run';

        // Only show "Running" badge for the specific model(s) that are actually running
        const isThisModelRunning = runningModels.has(model.name) || dbtRunStartTime !== null;

        // Format row count
        const rowCount = model.row_count !== null && model.row_count !== undefined
            ? model.row_count.toLocaleString()
            : '-';

        // Format last run timestamp
        let lastRun = '-';
        if (model.last_run) {
            try {
                const runDate = new Date(model.last_run);
                const now = new Date();
                const diffMs = now - runDate;
                const diffMins = Math.floor(diffMs / 60000);
                const diffHours = Math.floor(diffMs / 3600000);
                const diffDays = Math.floor(diffMs / 86400000);

                if (diffMins < 1) {
                    lastRun = 'Just now';
                } else if (diffMins < 60) {
                    lastRun = `${diffMins}m ago`;
                } else if (diffHours < 24) {
                    lastRun = `${diffHours}h ago`;
                } else if (diffDays < 7) {
                    lastRun = `${diffDays}d ago`;
                } else {
                    lastRun = runDate.toLocaleDateString();
                }
            } catch (e) {
                lastRun = '-';
            }
        }

        // Status badge - use actual status from run_results.json
        let statusBadge;
        if (isThisModelRunning) {
            // Currently running via UI
            statusBadge = '<span class="px-2 py-1 text-xs rounded-full bg-yellow-100 text-yellow-800"><span class="inline-block animate-pulse mr-1">●</span>Running</span>';
        } else if (model.status === 'success') {
            // Last run succeeded
            statusBadge = '<span class="px-2 py-1 text-xs rounded-full bg-green-100 text-green-800">✓ Success</span>';
        } else if (model.status === 'error') {
            // Last run failed
            statusBadge = '<span class="px-2 py-1 text-xs rounded-full bg-red-100 text-red-800">✗ Error</span>';
        } else if (model.status === 'skipped') {
            // Was skipped in last run
            statusBadge = '<span class="px-2 py-1 text-xs rounded-full bg-gray-100 text-gray-600">⊘ Skipped</span>';
        } else {
            // Never run or status unknown
            statusBadge = '<span class="px-2 py-1 text-xs rounded-full bg-gray-100 text-gray-600">Not Run</span>';
        }

        return `
            <tr class="hover:bg-gray-50 transition-colors duration-150">
                <td class="px-6 py-4 whitespace-nowrap">
                    <div class="text-sm font-medium text-gray-900">${model.name}</div>
                </td>
                <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-600">
                    ${model.schema || '-'}
                </td>
                <td class="px-6 py-4 whitespace-nowrap">
                    <span class="px-2 py-1 text-xs rounded-full bg-blue-100 text-blue-800">
                        ${model.materialization || 'view'}
                    </span>
                </td>
                <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-600">
                    ${rowCount}
                </td>
                <td class="px-6 py-4 whitespace-nowrap">
                    ${statusBadge}
                </td>
                <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-600">
                    ${lastRun}
                </td>
                <td class="px-6 py-4 whitespace-nowrap text-right text-sm font-medium">
                    <button
                        onclick="runDbtModel('${model.name}', false)"
                        class="text-blue-600 hover:text-blue-900 disabled:text-gray-400 disabled:cursor-not-allowed mr-3"
                        id="dbt-btn-${model.name}"
                        ${buttonDisabled}
                    >
                        ${buttonText}
                    </button>
                    <button
                        onclick="runDbtModel('${model.name}', true)"
                        class="text-green-600 hover:text-green-900 disabled:text-gray-400 disabled:cursor-not-allowed mr-3"
                        ${buttonDisabled}
                        title="Run with downstream models"
                    >
                        Run+
                    </button>
                    <a
                        href="/dbt-docs#!/model/${model.unique_id}"
                        target="_blank"
                        class="text-blue-600 hover:text-blue-900"
                        title="View documentation"
                    >
                        📖
                    </a>
                </td>
            </tr>
        `;
    }).join('');
}

async function runDbtModel(modelName, cascade) {
    // Server-side locking will prevent concurrent runs
    // No client-side blocking needed - server returns error if locked
    try {
        // Optimistically disable button
        updateDbtModelStatus(modelName, true);
        addLogEntry('info', `Triggering model run`, `dbt:${modelName}`);

        // Use longer timeout for dbt operations (30s) - actual status updates come via WebSocket
        const response = await apiCall(`/api/dbt/models/${modelName}/run?cascade=${cascade}`, 'POST', null, 30000);

        if (response.success) {
            const action = cascade ? 'with cascade' : '';
            showToast(`dbt model ${modelName} started ${action}`, 'info');
            addLogEntry('info', `Running model ${action}`, `dbt:${modelName}`);
            // WebSocket events will update the UI when model completes
        } else {
            throw new Error(response.message || 'Unknown error');
        }
    } catch (error) {
        console.error('Error running dbt model:', error);
        showToast(`Failed to run ${modelName}`, 'error');
        addLogEntry('error', `Failed to run: ${error.message}`, `dbt:${modelName}`);
        // Revert optimistic update
        updateDbtModelStatus(modelName, false);
    }
}

function updateDbtModelStatus(modelName, running) {
    // Update button state
    const btn = document.getElementById(`dbt-btn-${modelName}`);
    if (btn) {
        btn.disabled = running;
        btn.textContent = running ? 'Running...' : 'Run';

        // Update status badge in the same row
        const row = btn.closest('tr');
        if (row) {
            const statusCell = row.querySelector('td:nth-child(5)'); // 5th column is status
            if (statusCell && running) {
                // Show running badge with pulsing dot
                statusCell.innerHTML = '<span class="px-2 py-1 text-xs rounded-full bg-yellow-100 text-yellow-800"><span class="inline-block animate-pulse mr-1">●</span>Running</span>';
            }
            // If not running, leave badge as-is (will be updated by full reload)
        }
    }

    // Track running state
    if (running) {
        runningModels.add(modelName);
    } else {
        runningModels.delete(modelName);
    }
}

function showDbtModelsError() {
    const tbody = document.getElementById('dbt-models-table-body');
    tbody.innerHTML = `
        <tr>
            <td colspan="5" class="px-6 py-8 text-center text-red-500">
                <p>Error loading dbt models</p>
                <button onclick="loadDbtModels()" class="text-sm text-blue-600 hover:text-blue-800 mt-2">Retry</button>
            </td>
        </tr>
    `;
}

function refreshDbtModels() {
    showToast('Refreshing dbt models...', 'info');
    loadDbtModels();
}

// ============================================================================
// Tab Navigation
// ============================================================================

function switchTab(tabName, updateHash = true) {
    // Valid tabs
    const validTabs = ['sources', 'models', 'activity'];
    if (!validTabs.includes(tabName)) {
        tabName = 'sources';
    }

    // Hide all tabs
    document.querySelectorAll('.tab-content').forEach(tab => {
        tab.classList.add('hidden');
    });

    // Remove active state from all buttons
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.classList.remove('active', 'border-blue-600', 'text-blue-600');
        btn.classList.add('border-transparent', 'text-gray-600');
    });

    // Show selected tab
    const selectedTab = document.getElementById(`tab-${tabName}`);
    if (selectedTab) {
        selectedTab.classList.remove('hidden');
    }

    // Activate selected button
    const selectedBtn = document.getElementById(`tab-btn-${tabName}`);
    if (selectedBtn) {
        selectedBtn.classList.add('active', 'border-blue-600', 'text-blue-600');
        selectedBtn.classList.remove('border-transparent', 'text-gray-600');
    }

    // Update URL hash if requested (for tab persistence on refresh)
    if (updateHash) {
        window.history.replaceState(null, null, `#${tabName}`);
    }

    // Trigger data load if needed
    if (tabName === 'sources' && sources.length === 0) {
        loadSources();
    } else if (tabName === 'models' && dbtModels.length === 0) {
        loadDbtModels();
    }
}

// ============================================================================
// Metabase Credentials Helper
// ============================================================================

let metabaseCredentials = null;

async function loadMetabaseCredentials() {
    try {
        metabaseCredentials = await apiCall('/api/metabase/credentials');

        document.getElementById('metabase-email').textContent = metabaseCredentials.email;
        document.getElementById('metabase-password').textContent = metabaseCredentials.password;

        // Show banner if not previously dismissed
        const dismissed = localStorage.getItem('metabase-credentials-dismissed');
        if (!dismissed) {
            document.getElementById('metabase-credentials-banner').classList.remove('hidden');
        }
    } catch (error) {
        console.error('Failed to load Metabase credentials:', error);
        // Don't show banner if credentials can't be loaded
    }
}

function openMetabase() {
    if (!metabaseCredentials) {
        // Credentials not loaded yet, just open Metabase
        window.open('http://localhost:3000', '_blank');
        return;
    }

    // Open new window and auto-submit login form
    const metabaseWindow = window.open('', '_blank');

    const autoLoginHtml = `
        <!DOCTYPE html>
        <html>
        <head>
            <title>Logging in to Metabase...</title>
            <style>
                body {
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    height: 100vh;
                    margin: 0;
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                }
                .container {
                    background: white;
                    border-radius: 16px;
                    padding: 48px;
                    text-align: center;
                    box-shadow: 0 20px 60px rgba(0,0,0,0.3);
                }
                .spinner {
                    border: 4px solid #f3f4f6;
                    border-top: 4px solid #667eea;
                    border-radius: 50%;
                    width: 40px;
                    height: 40px;
                    animation: spin 1s linear infinite;
                    margin: 0 auto 20px;
                }
                @keyframes spin {
                    0% { transform: rotate(0deg); }
                    100% { transform: rotate(360deg); }
                }
                h2 { color: #1f2937; margin: 0 0 8px 0; }
                p { color: #6b7280; margin: 0; }
            </style>
        </head>
        <body>
            <div class="container">
                <div class="spinner"></div>
                <h2>Logging in to Metabase...</h2>
                <p>You'll be redirected in a moment</p>
            </div>
            <form id="loginForm" method="POST" action="http://localhost:3000/auth/login" style="display:none;">
                <input type="text" name="username" value="${metabaseCredentials.email}">
                <input type="password" name="password" value="${metabaseCredentials.password}">
            </form>
            <script>
                // Auto-submit after brief delay
                setTimeout(() => {
                    document.getElementById('loginForm').submit();
                }, 500);
            </script>
        </body>
        </html>
    `;

    metabaseWindow.document.write(autoLoginHtml);
    metabaseWindow.document.close();
}

function copyMetabaseField(field) {
    const element = document.getElementById(`metabase-${field}`);
    const text = element.textContent;

    navigator.clipboard.writeText(text).then(() => {
        const originalText = element.nextElementSibling.textContent;
        element.nextElementSibling.textContent = 'Copied!';
        element.nextElementSibling.classList.add('text-green-600');

        setTimeout(() => {
            element.nextElementSibling.textContent = originalText;
            element.nextElementSibling.classList.remove('text-green-600');
        }, 2000);
    });
}

function dismissMetabaseBanner() {
    document.getElementById('metabase-credentials-banner').classList.add('hidden');
    localStorage.setItem('metabase-credentials-dismissed', 'true');
}

// Make functions available globally
window.triggerSync = triggerSync;
window.refreshSources = refreshSources;
window.clearLog = clearLog;
window.closeUploadModal = closeUploadModal;
window.openSourceDetail = openSourceDetail;
window.closeSourceDetail = closeSourceDetail;
window.runDbtModel = runDbtModel;
window.refreshDbtModels = refreshDbtModels;
window.switchTab = switchTab;
window.openMetabase = openMetabase;
window.copyMetabaseField = copyMetabaseField;
window.dismissMetabaseBanner = dismissMetabaseBanner;
