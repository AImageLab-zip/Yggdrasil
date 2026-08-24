/**
 * Batch default-panoramic generation (administrators).
 *
 * Drives the normal patient view in a hidden frame, one patient at a time:
 * cbct_panorex_editor.js runs its unattended pass there and posts the outcome
 * back, so there is no second copy of the reconstruction to keep in sync.
 *
 * Strictly sequential - each patient loads a whole CBCT volume into memory.
 */
(function () {
    'use strict';

    // A CBCT volume download plus reconstruction; generous, but bounded so one
    // stuck patient cannot stall the whole folder.
    var PATIENT_TIMEOUT_MS = 300000;

    var state = { queue: [], index: 0, running: false, stopped: false, current: null, timer: null };

    function element(id) { return document.getElementById(id); }

    function patientUrl(patientId) {
        var template = (window.panoramicWarmup || {}).patientUrlTemplate || '';
        return template.replace(/0\/?$/, patientId + '/');
    }

    function log(message, level) {
        var list = element('warmupLog');
        if (!list) return;
        var row = document.createElement('li');
        row.className = 'text-' + (level || 'muted');
        row.textContent = message;
        list.insertBefore(row, list.firstChild);
        while (list.childElementCount > 200) list.removeChild(list.lastChild);
    }

    function setProgress() {
        var total = state.queue.length;
        var count = element('warmupCount');
        var bar = element('warmupBar');
        if (count) count.textContent = state.index + '/' + total;
        if (bar) bar.style.width = (total ? Math.round((state.index / total) * 100) : 0) + '%';
    }

    function setStatus(text) {
        var status = element('warmupStatus');
        if (status) status.textContent = text;
    }

    function finish(message) {
        state.running = false;
        state.current = null;
        window.clearTimeout(state.timer);
        element('warmupFrame').src = 'about:blank';
        element('warmupStop').hidden = true;
        element('warmupStart').disabled = state.queue.length === 0;
        setStatus(message);
    }

    function advance(outcome, detail) {
        if (!state.running) return;
        window.clearTimeout(state.timer);
        var patient = state.current;
        if (patient) {
            var levels = {
                created: 'success', existing: 'muted', skipped: 'muted',
                failed: 'danger', timeout: 'warning'
            };
            var texts = {
                created: 'generated',
                existing: 'already had one',
                skipped: 'skipped' + (detail ? ': ' + detail : ''),
                failed: 'failed' + (detail ? ': ' + detail : ''),
                timeout: 'timed out'
            };
            log(
                'Patient ' + patient.id + ' (' + patient.name + '): ' +
                (texts[outcome] || outcome),
                levels[outcome] || 'muted'
            );
        }
        state.index += 1;
        setProgress();
        next();
    }

    function next() {
        if (state.stopped) {
            finish('Stopped after ' + state.index + ' patient(s).');
            return;
        }
        if (state.index >= state.queue.length) {
            finish('Finished ' + state.index + ' patient(s).');
            return;
        }
        var patient = state.queue[state.index];
        state.current = patient;
        setStatus('Generating for patient ' + patient.id + ' (' + patient.name + ')');
        element('warmupFrame').src = patientUrl(patient.id);
        state.timer = window.setTimeout(function () { advance('timeout'); }, PATIENT_TIMEOUT_MS);
    }

    function scan() {
        var folderSelect = element('warmupFolder');
        var folderId = folderSelect && folderSelect.value;
        if (!folderId) return;

        element('warmupStatusCard').hidden = false;
        element('warmupStart').disabled = true;
        setStatus('Looking for patients without a panoramic...');

        var url = (window.panoramicWarmup || {}).pendingUrl + '?folder=' + encodeURIComponent(folderId);
        fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
            .then(function (response) {
                return response.json().then(function (data) {
                    if (!response.ok) throw new Error(data.error || 'Could not list patients.');
                    return data;
                });
            })
            .then(function (data) {
                state.queue = data.patients || [];
                state.index = 0;
                setProgress();
                if (!state.queue.length) {
                    setStatus('Every patient in ' + data.folder.name + ' already has a panoramic.');
                    return;
                }
                var message = state.queue.length + ' patient(s) in ' + data.folder.name + ' need one';
                if (data.truncated) message += ' (of ' + data.total + '; run again for the rest)';
                setStatus(message + '.');
                element('warmupStart').disabled = false;
            })
            .catch(function (error) {
                setStatus(error.message || 'Could not list patients.');
            });
    }

    function start() {
        if (state.running || !state.queue.length) return;
        state.running = true;
        state.stopped = false;
        state.index = 0;
        setProgress();
        element('warmupStart').disabled = true;
        element('warmupStop').hidden = false;
        next();
    }

    function init() {
        if (!element('warmupFrame')) return;

        element('warmupScan').addEventListener('click', scan);
        element('warmupStart').addEventListener('click', start);
        element('warmupStop').addEventListener('click', function () {
            state.stopped = true;
            setStatus('Stopping after the current patient...');
        });

        window.addEventListener('message', function (event) {
            if (event.origin !== window.location.origin) return;
            var payload = event.data || {};
            if (payload.type !== 'panoramic-default') return;
            advance(payload.outcome, payload.detail);
        });
    }

    document.addEventListener('DOMContentLoaded', init);
}());
