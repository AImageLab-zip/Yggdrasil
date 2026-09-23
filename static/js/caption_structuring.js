/*
 * Filing a dictated caption into the report template.
 *
 * A separate file from vocal_caption.js (1400 lines and one class already) because this
 * is a different concern, and because keeping the decisions in pure functions is what
 * makes them testable — static/js/tests/caption_structuring.test.js exercises everything
 * on window.CaptionStructuring that does not touch the DOM.
 *
 * Not bundled: plain static/js is loaded directly by the template, so editing this file
 * needs no `npm run build` (only frontend/ does).
 *
 * The transport is POST + fetch + ReadableStream, not EventSource. EventSource is
 * GET-only (the caption id would ride in URLs and access logs), cannot send a CSRF token,
 * and reconnects automatically when the server closes the stream — which would silently
 * buy a second model call every time.
 */
(function () {
    'use strict';

    var STALL_TIMEOUT_MS = 30000;   // no data for this long -> the upstream is gone
    var HARD_TIMEOUT_MS = 180000;   // nothing legitimate takes this long

    // ---------------------------------------------------------------- pure helpers

    /**
     * Split a chunk of an SSE body into events, keeping whatever is incomplete.
     *
     * The reader hands over arbitrary byte boundaries, so an event routinely arrives in
     * two pieces; `rest` is what must be prepended to the next chunk. Comment lines
     * (": keep-alive") and malformed JSON are skipped rather than thrown, because one
     * bad frame must not abandon a report that is otherwise arriving fine.
     */
    function parseSseChunk(buffer) {
        var events = [];
        var parts = String(buffer || '').split('\n\n');
        var rest = parts.pop();
        parts.forEach(function (block) {
            block.split('\n').forEach(function (line) {
                if (!line || line.charAt(0) === ':') return;
                if (line.indexOf('data:') !== 0) return;
                var payload = line.slice(5).trim();
                if (!payload) return;
                try {
                    events.push(JSON.parse(payload));
                } catch (err) {
                    /* a truncated or malformed frame is not worth losing the rest over */
                }
            });
        });
        return { events: events, rest: rest };
    }

    /** Clinician-facing text for a failure code. Mirrors transcriptionCloseMessage(). */
    function structuringErrorMessage(code) {
        switch (code) {
            case 'disabled':
                return 'Report structuring is not enabled for this project.';
            case 'permission_denied':
                return 'You do not have permission to structure this caption.';
            case 'not_configured':
                return 'Report structuring is not configured on this server.';
            case 'no_template':
                return 'No report template is defined for this modality.';
            case 'too_short':
                return 'This caption is too short to structure.';
            case 'too_long':
                return 'This caption is too long to structure.';
            case 'in_flight':
                return 'This caption is already being structured.';
            case 'upstream_timeout':
                return 'The language model did not respond in time. Try again.';
            case 'rate_limited':
                return 'The language model is busy right now. Try again shortly.';
            case 'upstream_error':
                return 'The language model could not be reached. Try again.';
            case 'budget_exhausted':
                return 'The language model ran out of room before answering. '
                     + 'Ask an administrator to raise its token budget.';
            case 'malformed_response':
                return 'The language model returned something unreadable. Try again.';
            case 'stalled':
                return 'The response stopped arriving. Try again.';
            default:
                return 'Structuring failed unexpectedly.';
        }
    }

    /**
     * Whether the top-level Structure button should be clickable.
     *
     * Written as a pure function of a state object so the truth table is one testable
     * thing rather than a set of conditions scattered through event handlers.
     */
    function shouldEnableStructureButton(state) {
        if (!state) return false;
        if (state.isRecording) return false;
        if (state.inFlight) return false;
        if (!state.templateForModality) return false;
        var length = state.textLength || 0;
        var min = state.minLength || 10;
        if (length >= min) return true;
        // Nothing typed: the row buttons act on an existing caption instead.
        return length === 0 && !!state.hasCaption;
    }

    /** Does a template cover this modality? `["*"]` means one covers them all. */
    function templateAvailableFor(modalitySlug, availableSlugs) {
        if (!availableSlugs || !availableSlugs.length) return false;
        if (availableSlugs.indexOf('*') !== -1) return true;
        var slug = String(modalitySlug || '').trim();
        if (!slug) return false;
        if (availableSlugs.indexOf(slug) !== -1) return true;
        // Urology's modalities are "urology-mri" while its templates are filed under
        // "mri"; the server normalizes the same way (common/report_templates.py).
        var bare = slug.replace(/^[a-z]+-/, '');
        return availableSlugs.indexOf(bare) !== -1;
    }

    function escapeHtml(value) {
        return String(value === null || value === undefined ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    /**
     * The read-only block shown under a caption row once it has a report.
     *
     * Everything interpolated here came out of a language model, so every value goes
     * through escapeHtml. This is the one place in the feature where untrusted text
     * becomes markup.
     */
    function renderStructuredBlock(report) {
        if (!report || report.status !== 'completed') return '';
        var when = report.completed_at ? report.completed_at.slice(0, 16).replace('T', ' ') : '';
        var warningCount = (report.warnings || []).length;
        return '' +
            '<div class="caption-structured mt-1" data-structured-for="' + escapeHtml(report.id) + '">' +
                '<div class="d-flex align-items-center gap-2">' +
                    '<span class="badge-ygg badge-neutral">' +
                        '<i class="fas fa-wand-magic-sparkles me-1"></i>Structured' +
                    '</span>' +
                    '<small class="text-muted">' + escapeHtml(when) + '</small>' +
                    (report.attempt > 1
                        ? '<small class="text-muted">attempt ' + escapeHtml(report.attempt) + '</small>'
                        : '') +
                    (warningCount
                        ? '<small class="text-warning">' + escapeHtml(warningCount) + ' to review</small>'
                        : '') +
                '</div>' +
                '<div class="structured-text mt-1"><small class="text-dark" style="white-space:pre-wrap;">' +
                    escapeHtml(report.structured_text || '') +
                '</small></div>' +
            '</div>';
    }

    // ---------------------------------------------------------------- controller

    function CaptionStructuringController() {
        this.panel = null;
        this.inFlight = false;
        this.controller = null;
        this.stallTimer = null;
        this.hardTimer = null;
        this.currentCaptionId = null;
        this.availableModalities = [];
    }

    CaptionStructuringController.prototype.init = function () {
        this.panel = document.getElementById('structuredCaptionPanel');
        if (!this.panel) return;   // structuring is not available on this page

        var config = document.getElementById('caption-structuring-modalities');
        if (config) {
            try {
                this.availableModalities = JSON.parse(config.textContent || '[]');
            } catch (err) {
                this.availableModalities = [];
            }
        }

        this.bind();
        this.syncButtonState();
    };

    CaptionStructuringController.prototype.bind = function () {
        var self = this;

        var top = document.getElementById('structureCaption');
        if (top) {
            top.addEventListener('click', function () { self.structureFromTextarea(); });
        }

        // Delegated: caption rows are also inserted by vocal_caption.js after a save.
        document.addEventListener('click', function (event) {
            var button = event.target.closest && event.target.closest('.btn-structure-caption');
            if (!button) return;
            event.preventDefault();
            self.runStructuring(button.dataset.captionId, { modality: button.dataset.modality });
        });

        var stop = document.getElementById('structuredCaptionStop');
        if (stop) stop.addEventListener('click', function () { self.abort('stopped'); });

        var rerun = document.getElementById('structuredCaptionRerun');
        if (rerun) {
            rerun.addEventListener('click', function () {
                if (self.currentCaptionId) self.runStructuring(self.currentCaptionId, { rerun: true });
            });
        }

        var close = document.getElementById('structuredCaptionClose');
        if (close) {
            close.addEventListener('click', function () { self.panel.classList.add('d-none'); });
        }

        var textarea = document.getElementById('captionTextArea');
        if (textarea) {
            textarea.addEventListener('input', function () { self.syncButtonState(); });
        }

        document.addEventListener('caption:added', function () { self.syncButtonState(); });

        // A run in flight holds an open request and an unfinished row; leaving the page
        // without cancelling leaves both until they time out.
        window.addEventListener('beforeunload', function () {
            if (self.inFlight) self.abort('navigated');
        });
    };

    CaptionStructuringController.prototype.state = function () {
        var textarea = document.getElementById('captionTextArea');
        var recorder = window.recorder;
        var modality = this.selectedModality();
        return {
            isRecording: !!(recorder && recorder.isRecording),
            inFlight: this.inFlight,
            textLength: textarea ? textarea.value.trim().length : 0,
            minLength: 10,
            hasCaption: !!document.querySelector('.caption-item-compact'),
            templateForModality: templateAvailableFor(modality, this.availableModalities),
        };
    };

    CaptionStructuringController.prototype.selectedModality = function () {
        var checked = document.querySelector('#modalityToggleGroup input:checked');
        if (checked) return checked.value;
        var card = document.getElementById('captionUnifiedCard');
        return card ? (card.dataset.modality || '') : '';
    };

    CaptionStructuringController.prototype.syncButtonState = function () {
        var button = document.getElementById('structureCaption');
        if (!button) return;
        var state = this.state();
        button.disabled = !shouldEnableStructureButton(state);
        if (!state.templateForModality) {
            button.title = 'No report template is defined for this modality.';
        } else if (state.textLength > 0 && state.textLength < state.minLength) {
            button.title = 'At least ' + state.minLength + ' characters.';
        } else {
            button.title = 'Save this caption and file it into the report template';
        }
        var available = this.availableModalities;
        document.querySelectorAll('.btn-structure-caption').forEach(function (row) {
            // A row whose modality has no template can only ever produce a 409
            // no_template, and a control that can only fail is worse than no control
            // -- the same rule the top button and structuring_context.py already apply.
            var hasTemplate = templateAvailableFor(row.dataset.modality, available);
            row.disabled = state.inFlight || !hasTemplate;
            row.title = hasTemplate
                ? 'File this caption into the report template'
                : 'No report template is defined for this modality.';
        });
    };

    /**
     * One click: save whatever is in the textarea, then structure the row it became.
     *
     * Structuring operates on a saved caption — a rerun needs a stable identity, the
     * result must survive a reload, and the server re-checks permissions against a
     * concrete row. Making the clinician press Save first would just be that requirement
     * leaking into the UI.
     */
    CaptionStructuringController.prototype.structureFromTextarea = function () {
        var self = this;
        var textarea = document.getElementById('captionTextArea');
        var recorder = window.recorder;
        var text = textarea ? textarea.value.trim() : '';

        if (!text) {
            var newest = document.querySelector('.caption-item-compact');
            if (newest) this.runStructuring(newest.dataset.captionId, {});
            return;
        }
        if (!recorder || typeof recorder.saveTextCaption !== 'function') {
            this.fail('failed');
            return;
        }
        Promise.resolve(recorder.saveTextCaption()).then(function (caption) {
            if (caption && caption.id) {
                self.runStructuring(caption.id, {});
            } else {
                // The save reported a problem of its own and has already said so.
                self.syncButtonState();
            }
        }).catch(function () { self.fail('failed'); });
    };

    CaptionStructuringController.prototype.urlFor = function (which, captionId) {
        var attribute = which === 'report' ? 'reportUrlTemplate' : 'structureUrlTemplate';
        var template = this.panel ? this.panel.dataset[attribute] : '';
        // The template is reversed with caption_id=0; swapping the last path segment is
        // safer than string-building a URL the router may namespace differently.
        return String(template || '').replace(/\/0\/([a-z-]+)\/$/, '/' + captionId + '/$1/');
    };

    CaptionStructuringController.prototype.runStructuring = function (captionId, options) {
        if (!captionId || this.inFlight || !this.panel) return;
        options = options || {};

        var report = this.panel.querySelector('#structuredCaptionText');
        // The panel is server-rendered as one block, so a missing textarea means the
        // markup changed under us; bail rather than throw halfway through a run.
        if (!report) return;
        this.currentCaptionId = captionId;
        this.panel.dataset.captionId = captionId;
        this.panel.classList.remove('d-none');
        this.panel.scrollIntoView({ block: 'nearest' });

        var row = document.querySelector('.caption-item-compact[data-caption-id="' + captionId + '"]');
        var source = row ? row.querySelector('.caption-text-full small, .caption-text-preview small') : null;
        var sourceText = document.getElementById('structuredCaptionSourceText');
        if (sourceText) sourceText.textContent = source ? source.textContent.trim() : '';

        report.value = '';
        report.readOnly = true;
        this.setStatus('Structuring…', true);
        this.setWarnings([]);
        document.getElementById('structuredCaptionActions').classList.add('d-none');
        document.getElementById('structuredCaptionStop').classList.remove('d-none');

        this.inFlight = true;
        this.syncButtonState();
        this.controller = new AbortController();
        this.armTimers();

        var self = this;
        fetch(this.urlFor('structure', captionId), {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': window.yggCsrfToken(),
            },
            body: JSON.stringify({ generation_uuid: options.generationUuid || null }),
            signal: this.controller.signal,
        }).then(function (response) {
            var type = response.headers.get('content-type') || '';
            if (!response.ok && type.indexOf('application/json') !== -1) {
                return response.json().then(function (payload) {
                    self.fail(payload.code, payload.error);
                });
            }
            if (!response.body || !response.body.getReader) {
                // No streaming support: read it whole and render the final frame.
                return response.text().then(function (body) {
                    var parsed = parseSseChunk(body + '\n\n');
                    parsed.events.forEach(function (event) { self.handleEvent(event); });
                });
            }
            return self.consume(response.body.getReader());
        }).catch(function (error) {
            if (error && error.name === 'AbortError') return;   // we cancelled on purpose
            self.fail('upstream_error');
        }).then(function () {
            self.finish();
        });
    };

    CaptionStructuringController.prototype.consume = function (reader) {
        var self = this;
        var decoder = new TextDecoder();
        var buffer = '';

        function pump() {
            return reader.read().then(function (chunk) {
                if (chunk.done) return;
                self.armTimers();
                buffer += decoder.decode(chunk.value, { stream: true });
                var parsed = parseSseChunk(buffer);
                buffer = parsed.rest;
                parsed.events.forEach(function (event) { self.handleEvent(event); });
                return pump();
            });
        }
        return pump();
    };

    CaptionStructuringController.prototype.handleEvent = function (event) {
        var report = document.getElementById('structuredCaptionText');
        if (!event || !event.type) return;

        if (event.type === 'start') {
            this.setStatus(event.replayed ? 'Already structured' : 'Model working…', !event.replayed);
        } else if (event.type === 'delta') {
            report.value += event.text || '';
            report.scrollTop = report.scrollHeight;
        } else if (event.type === 'done') {
            this.complete(event.report);
        } else if (event.type === 'error') {
            this.fail(event.code, event.detail);
        }
    };

    CaptionStructuringController.prototype.complete = function (report) {
        var textarea = document.getElementById('structuredCaptionText');
        if (report && report.structured_text) textarea.value = report.structured_text;
        this.setStatus('Structured', false);
        this.setWarnings((report && report.warnings) || []);
        document.getElementById('structuredCaptionActions').classList.remove('d-none');

        var row = document.querySelector(
            '.caption-item-compact[data-caption-id="' + this.currentCaptionId + '"]'
        );
        if (row && report) {
            var existing = row.querySelector('.caption-structured');
            if (existing) existing.remove();
            var holder = row.querySelector('.caption-text-compact');
            if (holder) holder.insertAdjacentHTML('beforeend', renderStructuredBlock(report));
        }
    };

    CaptionStructuringController.prototype.setStatus = function (text, busy) {
        var status = document.getElementById('structuredCaptionStatus');
        if (!status) return;
        status.innerHTML = (busy ? '<i class="fas fa-spinner fa-spin me-1"></i>' : '')
            + escapeHtml(text);
    };

    CaptionStructuringController.prototype.setWarnings = function (warnings) {
        var holder = document.getElementById('structuredCaptionWarnings');
        if (!holder) return;
        if (!warnings || !warnings.length) {
            holder.innerHTML = '';
            return;
        }
        holder.innerHTML = warnings.map(function (warning) {
            return '<small class="text-warning d-block"><i class="fas fa-triangle-exclamation me-1"></i>'
                + escapeHtml(warning.detail || warning.code) + '</small>';
        }).join('');
    };

    CaptionStructuringController.prototype.fail = function (code, detail) {
        var message = structuringErrorMessage(code);
        this.setStatus('Failed', false);
        this.setWarnings([{ detail: detail && detail !== message ? message : message }]);
        if (window.appNotify) window.appNotify('error', message);
        document.getElementById('structuredCaptionActions').classList.remove('d-none');
    };

    CaptionStructuringController.prototype.armTimers = function () {
        var self = this;
        this.clearTimers(true);
        this.stallTimer = setTimeout(function () { self.abort('stalled'); }, STALL_TIMEOUT_MS);
        if (!this.hardTimer) {
            this.hardTimer = setTimeout(function () { self.abort('stalled'); }, HARD_TIMEOUT_MS);
        }
    };

    CaptionStructuringController.prototype.clearTimers = function (keepHard) {
        if (this.stallTimer) { clearTimeout(this.stallTimer); this.stallTimer = null; }
        if (!keepHard && this.hardTimer) { clearTimeout(this.hardTimer); this.hardTimer = null; }
    };

    CaptionStructuringController.prototype.abort = function (reason) {
        if (this.controller) this.controller.abort();
        if (reason === 'stalled') this.fail('stalled');
        else this.setStatus('Stopped', false);
        this.finish();
    };

    CaptionStructuringController.prototype.finish = function () {
        this.clearTimers();
        this.controller = null;
        this.inFlight = false;
        var stop = document.getElementById('structuredCaptionStop');
        if (stop) stop.classList.add('d-none');
        this.syncButtonState();
    };

    window.CaptionStructuring = {
        parseSseChunk: parseSseChunk,
        structuringErrorMessage: structuringErrorMessage,
        shouldEnableStructureButton: shouldEnableStructureButton,
        templateAvailableFor: templateAvailableFor,
        renderStructuredBlock: renderStructuredBlock,
        escapeHtml: escapeHtml,
        Controller: CaptionStructuringController,
        controller: null,
    };

    document.addEventListener('DOMContentLoaded', function () {
        window.CaptionStructuring.controller = new CaptionStructuringController();
        window.CaptionStructuring.controller.init();
    });
})();
