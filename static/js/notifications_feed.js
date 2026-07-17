/*
 * Yggdrasil notifications bell (topbar / rail). Polls the notifications API,
 * shows an unread dot, renders the dropdown, and marks read. Distinct from
 * notifications.js (which renders transient toasts). Endpoints come from
 * window.YGG_NOTIF_API / window.YGG_NOTIF_MARK, injected by base.html.
 */
(function () {
    'use strict';

    var bell = document.querySelector('[data-notif-bell]');
    if (!bell || !window.YGG_NOTIF_API) return;

    var toggle = bell.querySelector('[data-notif-toggle]');
    var menu = bell.querySelector('[data-notif-menu]');
    var dot = bell.querySelector('[data-notif-dot]');
    var list = bell.querySelector('[data-notif-list]');
    var empty = bell.querySelector('[data-notif-empty]');
    var markAll = bell.querySelector('[data-notif-mark-all]');

    function csrf() {
        var m = document.cookie.match(/csrftoken=([^;]+)/);
        return m ? m[1] : '';
    }

    var LEVEL_ICON = { success: 'lc-circle-check', danger: 'lc-triangle-alert', warning: 'lc-triangle-alert', info: 'lc-info' };

    function render(data) {
        var items = (data && data.items) || [];
        if (dot) dot.hidden = !(data && data.unread > 0);
        if (!list) return;
        // Drop everything except the empty-state node, then repopulate.
        Array.prototype.slice.call(list.querySelectorAll('[data-notif-item]')).forEach(function (n) { n.remove(); });
        if (empty) empty.hidden = items.length > 0;
        var sprite = window.YGG_SPRITE || '';
        items.forEach(function (it) {
            var row = document.createElement(it.url ? 'a' : 'div');
            row.className = 'ygg-menu-item items-start' + (it.is_read ? ' opacity-60' : '');
            row.setAttribute('data-notif-item', '');
            row.setAttribute('data-id', it.id);
            if (it.url) row.href = it.url;
            var icon = LEVEL_ICON[it.level] || LEVEL_ICON.info;
            row.innerHTML =
                '<svg class="ygg-icon" aria-hidden="true"><use href="' + sprite + '#' + icon + '"></use></svg>' +
                '<span class="flex-1 min-w-0"><span class="block">' + escapeHtml(it.message) + '</span>' +
                '<span class="block text-xs text-content-muted mono">' + escapeHtml(it.created_at) + '</span></span>';
            row.addEventListener('click', function () { markRead(it.id); });
            list.appendChild(row);
        });
    }

    function escapeHtml(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
        });
    }

    function refresh() {
        fetch(window.YGG_NOTIF_API, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (d) { if (d) render(d); })
            .catch(function () {});
    }

    function markRead(id) {
        if (!window.YGG_NOTIF_MARK) return;
        fetch(window.YGG_NOTIF_MARK, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() },
            body: JSON.stringify(id ? { id: id } : {}),
        }).then(function () { refresh(); }).catch(function () {});
    }

    if (toggle) {
        toggle.addEventListener('click', function (e) {
            e.preventDefault();
            e.stopPropagation();
            if (menu) menu.hidden = !menu.hidden;
        });
    }
    if (markAll) {
        markAll.addEventListener('click', function (e) { e.preventDefault(); e.stopPropagation(); markRead(null); });
    }
    document.addEventListener('click', function (e) {
        if (menu && !menu.hidden && !bell.contains(e.target)) menu.hidden = true;
    });

    refresh();
    setInterval(refresh, 60000);
})();
