// Shared Dango frontend utilities. Loaded in base.html before page scripts.

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
