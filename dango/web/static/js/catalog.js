/**
 * catalog.js — Search debounce and code highlighting helpers for the
 * data catalog page.
 */
(function () {
    "use strict";

    window.DangoCatalog = {
        _searchTimer: null,

        /**
         * Debounce a search callback.
         * @param {string} query - The search string.
         * @param {function} callback - Called with (query) after delay.
         * @param {number} [delay=300] - Debounce milliseconds.
         */
        debounceSearch: function (query, callback, delay) {
            if (this._searchTimer) clearTimeout(this._searchTimer);
            if (!query || query.trim().length < 2) {
                callback(null);
                return;
            }
            this._searchTimer = setTimeout(function () {
                callback(query.trim());
            }, delay || 300);
        },

        /**
         * Apply syntax highlighting to a <code> element via highlight.js.
         * No-op if hljs is not loaded.
         * @param {string} elementId - DOM id of the <code> element.
         */
        highlightCode: function (elementId) {
            if (typeof hljs === "undefined") return;
            var el = document.getElementById(elementId);
            if (el) hljs.highlightElement(el);
        },

        /**
         * Highlight all <code> blocks with class "catalog-sql".
         */
        highlightAll: function () {
            if (typeof hljs === "undefined") return;
            document.querySelectorAll("code.catalog-sql").forEach(function (el) {
                hljs.highlightElement(el);
            });
        },
    };
})();
