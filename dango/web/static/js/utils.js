// Shared Dango frontend utilities. Loaded in base.html before page scripts.

/**
 * Escape HTML special characters to prevent XSS.
 * @param {string} str
 * @returns {string}
 */
function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

/**
 * Format a source field for display. Coalesced dbt sources (containing '+ ')
 * are split into a bullet list; single sources pass through escapeHtml().
 * @param {string|null} source
 * @returns {string} HTML string
 */
function formatSource(source) {
    if (!source) return escapeHtml('system');
    if (source.includes('+ ')) {
        const parts = source.split('+ ').map(s => s.trim().replace(/\+$/, ''));
        const unique = [...new Set(parts)];
        return '<ul class="list-disc list-inside text-xs">' +
            unique.map(s => `<li>${escapeHtml(s)}</li>`).join('') +
            '</ul>';
    }
    return escapeHtml(source);
}

/**
 * Resolve a dot-delimited property path on an object.
 * Used by sortByProp and filterByText to access nested fields.
 * @param {Object} obj
 * @param {string} path - e.g., "freshness.status"
 * @returns {*} The resolved value, or undefined if any segment is null/undefined
 */
function resolveNested(obj, path) {
    if (!obj || !path) return undefined;
    return path.split('.').reduce((o, k) => (o != null ? o[k] : undefined), obj);
}

/**
 * Sort an array of objects by a property path or computed accessor.
 * Missing values sort last. Case-insensitive string comparison.
 * @param {Array} arr
 * @param {string|Function} accessor - Property path string or function(item) => value
 * @param {string} dir - "asc" or "desc"
 * @returns {Array} New sorted array (does not mutate input)
 */
function sortByProp(arr, accessor, dir) {
    if (!dir || dir === 'none' || !accessor) return [...arr];
    const copy = [...arr];
    const desc = dir === 'desc' ? -1 : 1;
    const get = typeof accessor === 'function' ? accessor : (item) => resolveNested(item, accessor);
    copy.sort((a, b) => {
        const va = get(a);
        const vb = get(b);
        if (va == null && vb == null) return 0;
        if (va == null) return 1;
        if (vb == null) return -1;
        if (typeof va === 'number' && typeof vb === 'number') {
            return (va - vb) * desc;
        }
        return String(va).toLowerCase().localeCompare(String(vb).toLowerCase()) * desc;
    });
    return copy;
}

/**
 * Filter an array of objects by case-insensitive substring match across named fields.
 * @param {Array} arr
 * @param {string} query - Filter text
 * @param {Array<string>} fields - Property paths to search
 * @returns {Array} Filtered copy (returns original if query is empty/whitespace)
 */
function filterByText(arr, query, fields) {
    if (!query || !query.trim()) return [...arr];
    const q = query.trim().toLowerCase();
    return arr.filter(item =>
        fields.some(field => {
            const val = resolveNested(item, field);
            return val != null && String(val).toLowerCase().includes(q);
        })
    );
}

/**
 * Format an ISO timestamp for display.
 * Thresholds: <60s "just now", <1h "X min ago", <24h "Xh ago",
 * <7d "Mon 15:39", >=7d "Jun 16" (with year if not current).
 *
 * @param {string} iso - ISO 8601 timestamp string
 * @returns {string} formatted relative time string
 */
function timeAgoIso(iso) {
    if (!iso) return '\u2014';  // em dash
    let ts = String(iso);
    if (!ts.endsWith('Z') && !/[+-]\d{2}:\d{2}$/.test(ts)) ts += 'Z';
    const date = new Date(ts);
    if (isNaN(date.getTime())) return '\u2014';
    const now = new Date();
    const diff = (now - date) / 1000;
    if (diff < 0) {
        const absDiff = Math.abs(diff);
        if (absDiff < 60) return 'in ' + Math.round(absDiff) + 's';
        if (absDiff < 3600) return 'in ' + Math.round(absDiff / 60) + ' min';
        if (absDiff < 86400) return 'in ' + Math.round(absDiff / 3600) + 'h';
        return 'in ' + Math.round(absDiff / 86400) + 'd';
    }
    const seconds = Math.floor(diff);
    if (seconds < 60) return 'just now';
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return minutes + ' min ago';
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return hours + 'h ago';
    const days = Math.floor(hours / 24);
    if (days < 7) {
        return date.toLocaleDateString(undefined, { weekday: 'short' }) + ' ' + date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
    }
    const monthDay = date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    if (date.getFullYear() !== now.getFullYear()) {
        return monthDay + ', ' + date.getFullYear();
    }
    return monthDay;
}
