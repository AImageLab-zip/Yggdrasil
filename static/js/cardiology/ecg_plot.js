/**
 * Client-side ECG plotter. Ports the clinical-grid layout of the reference
 * `mcd_rppg_plot.py` script to <canvas>: same 25 mm/s time scale, same
 * 10 mm/mV amplitude scale, same minor/major grid spacing (0.04s/0.1mV and
 * 0.2s/0.5mV), one stacked row per lead, PQ/QT/ST text under the last row.
 *
 * The full recording is drawn once onto an off-DOM buffer canvas; only a
 * window of it (sized to whatever the device's screen actually offers) is
 * ever painted into the visible canvas, panned with a <input type="range">
 * slider or by dragging directly on the plot. That's what makes a 30s / 12-lead
 * recording (a couple thousand px wide at clinical scale) usable on a phone
 * without either shrinking the clinical scale or fighting a giant scrollbar.
 *
 * No server round-trip beyond fetching the raw JSON: parsing and drawing
 * ~15000 samples x 12 leads is a handful of milliseconds on <canvas>, well
 * within "fast" for a JSON this size.
 */
(function () {
    'use strict';

    var PX_PER_MM = 3;
    var MM_PER_SEC = 25;
    var MM_PER_MV = 10;
    var ROW_HEIGHT_MM = 30;
    var LABEL_WIDTH_PX = 56;
    var MIN_VIEWPORT_PX = 240;

    // Mirrors maxillo's browser-panoramic contract (frontend/imaging/panoramic/savePayload.js):
    // every exit path announces an outcome, both as a DOM event (for any future
    // in-page listener) and, when running inside an iframe, via postMessage to
    // the parent -- that second half is what static/js/cardiology/ecg_warmup.js
    // listens for when it drives this page unattended.
    var ANNOUNCE_TYPE = 'ecg-plot';
    var ANNOUNCE_EVENT = 'ecgplotdefault';

    function announce(outcome, detail) {
        try {
            document.dispatchEvent(new CustomEvent(ANNOUNCE_EVENT, { detail: { outcome: outcome, detail: detail } }));
        } catch (e) { /* CustomEvent unsupported: nothing else depends on this today */ }
        if (window.parent && window.parent !== window) {
            window.parent.postMessage({ type: ANNOUNCE_TYPE, outcome: outcome, detail: detail }, window.location.origin);
        }
    }

    function loadEcg(container) {
        var url = container.dataset.ecgUrl;
        var filename = container.dataset.ecgFilename || 'ecg';
        if (!url) return;

        fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
            .then(function (response) {
                if (!response.ok) throw new Error('Failed to load ECG (' + response.status + ')');
                return response.json();
            })
            .then(function (ecg) { renderEcg(container, ecg, filename); })
            .catch(function (err) {
                container.innerHTML = '<p class="text-danger">Could not load ECG: ' + escapeHtml(err.message) + '</p>';
                announce('failed', err.message);
            });
    }

    function escapeHtml(text) {
        var div = document.createElement('div');
        div.textContent = String(text);
        return div.innerHTML;
    }

    function renderEcg(container, ecg, filename) {
        var fs = ecg.frequency;
        var time = ecg.dataX || [];
        var seg = ecg.segmentsData || {};
        var rawLeads = ecg.data || [];

        var minLen = time.length;
        rawLeads.forEach(function (lead) { minLen = Math.min(minLen, (lead.values || []).length); });
        time = time.slice(0, minLen);

        var leads = [];
        rawLeads.forEach(function (lead) {
            var values = (lead.values || []).slice(0, minLen);
            if (values.length <= 100) return;
            var allZero = values.every(function (v) { return v === 0; });
            if (allZero) return;
            leads.push({ title: lead.title, values: values });
        });

        if (!leads.length || !time.length) {
            container.innerHTML = '<p class="text-muted">This recording has no plottable leads.</p>';
            announce('skipped', 'no plottable leads');
            return;
        }

        var durationSec = time[time.length - 1] - time[0];
        var pxPerSec = MM_PER_SEC * PX_PER_MM;
        var pxPerMv = MM_PER_MV * PX_PER_MM;
        var rowHeightPx = ROW_HEIGHT_MM * PX_PER_MM;

        var fullWidth = Math.ceil(durationSec * pxPerSec) + 20;
        var fullHeight = leads.length * rowHeightPx;

        // Off-DOM: the source of truth for both the panned viewport and the
        // full-resolution PNG download. Never attached to the page itself.
        var fullCanvas = document.createElement('canvas');
        fullCanvas.width = fullWidth;
        fullCanvas.height = fullHeight;
        var fullCtx = fullCanvas.getContext('2d');
        fullCtx.fillStyle = '#ffffff';
        fullCtx.fillRect(0, 0, fullWidth, fullHeight);

        leads.forEach(function (lead, i) {
            var rowTop = i * rowHeightPx;
            drawGrid(fullCtx, 0, rowTop, fullWidth, rowHeightPx, pxPerSec, pxPerMv);
            drawWaveform(fullCtx, lead.values, time, rowTop, rowHeightPx, pxPerSec, pxPerMv);
        });

        // Frozen label column, drawn once -- stays put while the data area
        // pans, so an annotator never loses track of which row is which lead.
        var labelCanvas = document.createElement('canvas');
        labelCanvas.width = LABEL_WIDTH_PX;
        labelCanvas.height = fullHeight;
        var labelCtx = labelCanvas.getContext('2d');
        labelCtx.fillStyle = '#ffffff';
        labelCtx.fillRect(0, 0, LABEL_WIDTH_PX, fullHeight);
        leads.forEach(function (lead, i) {
            var rowTop = i * rowHeightPx;
            labelCtx.fillStyle = '#fff8f0';
            labelCtx.fillRect(0, rowTop, LABEL_WIDTH_PX, rowHeightPx);
            labelCtx.fillStyle = '#1a1a2e';
            labelCtx.font = 'bold 12px sans-serif';
            labelCtx.fillText(lead.title, 4, rowTop + rowHeightPx / 2 + 4);
        });

        container.innerHTML = '';

        var summary = document.createElement('p');
        summary.className = 'text-muted small mb-1';
        summary.textContent = 'fs = ' + fs + ' Hz, ' + MM_PER_SEC + ' mm/s, duration = ' + durationSec.toFixed(1) + ' s';
        container.appendChild(summary);

        var frame = document.createElement('div');
        frame.className = 'ecg-plot-frame';
        frame.style.display = 'flex';
        frame.style.border = '1px solid var(--ygg-border, #ddd)';
        frame.style.overflow = 'hidden';

        var labelWrap = document.createElement('div');
        labelWrap.appendChild(labelCanvas);
        frame.appendChild(labelWrap);

        var viewWrap = document.createElement('div');
        viewWrap.style.flex = '1 1 auto';
        viewWrap.style.minWidth = '0';
        var viewCanvas = document.createElement('canvas');
        viewCanvas.height = fullHeight;
        viewCanvas.style.display = 'block';
        viewCanvas.style.touchAction = 'pan-y';
        viewCanvas.style.cursor = 'grab';
        viewWrap.appendChild(viewCanvas);
        frame.appendChild(viewWrap);
        container.appendChild(frame);

        var slider = document.createElement('input');
        slider.type = 'range';
        slider.className = 'form-range mt-2';
        slider.style.width = '100%';
        slider.min = '0';
        slider.value = '0';
        container.appendChild(slider);

        var viewCtx = viewCanvas.getContext('2d');
        var offset = 0;
        var viewportWidth = 0;
        var maxOffset = 0;

        function layout() {
            var available = Math.max(viewWrap.clientWidth || 0, MIN_VIEWPORT_PX);
            viewportWidth = Math.min(fullWidth, available);
            viewCanvas.width = viewportWidth;
            maxOffset = Math.max(0, fullWidth - viewportWidth);
            slider.max = String(maxOffset);
            slider.disabled = maxOffset === 0;
            setOffset(offset);
        }

        function setOffset(next) {
            offset = Math.min(Math.max(0, next), maxOffset);
            slider.value = String(offset);
            viewCtx.clearRect(0, 0, viewportWidth, fullHeight);
            viewCtx.drawImage(
                fullCanvas,
                offset, 0, viewportWidth, fullHeight,
                0, 0, viewportWidth, fullHeight
            );
        }

        slider.addEventListener('input', function () { setOffset(parseInt(slider.value, 10) || 0); });

        // Direct drag-to-pan on the plot itself -- the slider covers the same
        // ground, but dragging the waveform is the more natural gesture on a
        // touchscreen. `touch-action: pan-y` above leaves vertical page
        // scroll to the browser and hands only horizontal movement to us.
        var dragStartX = null;
        var dragStartOffset = 0;
        viewCanvas.addEventListener('pointerdown', function (event) {
            dragStartX = event.clientX;
            dragStartOffset = offset;
            viewCanvas.style.cursor = 'grabbing';
            viewCanvas.setPointerCapture(event.pointerId);
        });
        viewCanvas.addEventListener('pointermove', function (event) {
            if (dragStartX === null) return;
            setOffset(dragStartOffset - (event.clientX - dragStartX));
        });
        function endDrag() { dragStartX = null; viewCanvas.style.cursor = 'grab'; }
        viewCanvas.addEventListener('pointerup', endDrag);
        viewCanvas.addEventListener('pointercancel', endDrag);

        var resizeTimer = null;
        window.addEventListener('resize', function () {
            window.clearTimeout(resizeTimer);
            resizeTimer = window.setTimeout(layout, 150);
        });

        layout();

        var annotationParts = [];
        if (seg.pqAverageValue) annotationParts.push('PQ = ' + Math.round(seg.pqAverageValue * 1000) + ' ms');
        if (seg.qtAverageValue) annotationParts.push('QT = ' + Math.round(seg.qtAverageValue * 1000) + ' ms');
        if (seg.stAverageValue) annotationParts.push('ST = ' + Math.round(seg.stAverageValue * 1000) + ' ms');
        if (annotationParts.length) {
            var annotation = document.createElement('p');
            annotation.className = 'text-muted small fst-italic mt-1 mb-0';
            annotation.textContent = annotationParts.join('   ');
            container.appendChild(annotation);
        }

        var downloadBtn = document.createElement('button');
        downloadBtn.type = 'button';
        downloadBtn.className = 'btn btn-outline-secondary btn-sm mt-2';
        downloadBtn.innerHTML = '<i class="fas fa-download me-1"></i>Download PNG';
        downloadBtn.addEventListener('click', function () {
            var link = document.createElement('a');
            link.download = filename.replace(/\.json$/i, '') + '.png';
            link.href = fullCanvas.toDataURL('image/png');
            link.click();
        });
        container.appendChild(downloadBtn);

        maybeSaveGeneratedPlot(container, fullCanvas);
    }

    function uuid4() {
        if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
        return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
            var r = (Math.random() * 16) | 0;
            var v = c === 'x' ? r : (r & 0x3) | 0x8;
            return v.toString(16);
        });
    }

    /**
     * Silently generate-and-save the plot once, the first time anyone opens a
     * patient with no processed artifact yet -- exactly maxillo's "generate a
     * default panoramic on first open" behavior (frontend/imaging/panoramic/
     * bootstrap.js), minus the arch/geometry machinery ECG has no equivalent of.
     * Always announces (see `announce` above), which is what lets
     * ecg_warmup.js drive this unattended from a hidden iframe.
     */
    function maybeSaveGeneratedPlot(container, fullCanvas) {
        var saveUrl = container.dataset.ecgSaveUrl;
        var canSave = container.dataset.ecgCanSave === 'true';
        var alreadyProcessed = container.dataset.ecgHasProcessed === 'true';

        if (!saveUrl || !canSave) {
            announce('skipped', !saveUrl ? 'no save endpoint' : 'no write permission');
            return;
        }
        if (alreadyProcessed) {
            announce('existing');
            return;
        }

        fullCanvas.toBlob(function (blob) {
            if (!blob) {
                announce('failed', 'canvas could not be exported to PNG');
                return;
            }
            var body = new FormData();
            body.append('plot_png', blob, 'ecg_plot.png');
            body.append('generation_uuid', uuid4());

            fetch(saveUrl, {
                method: 'POST',
                headers: { 'X-CSRFToken': window.yggCsrfToken ? window.yggCsrfToken() : '', 'X-Requested-With': 'XMLHttpRequest' },
                body: body
            })
                .then(function (response) { return response.json().then(function (data) { return { ok: response.ok, data: data }; }); })
                .then(function (result) {
                    if (!result.ok) { announce('failed', result.data.error || 'save failed'); return; }
                    announce(result.data.outcome || (result.data.idempotent ? 'existing' : 'created'));
                })
                .catch(function (err) { announce('failed', err.message); });
        }, 'image/png');
    }

    function drawGrid(ctx, left, top, width, rowHeight, pxPerSec, pxPerMv) {
        ctx.fillStyle = '#fff8f0';
        ctx.fillRect(left, top, width, rowHeight);

        var minorSec = 0.04, majorSec = 0.20;
        var minorMv = 0.1, majorMv = 0.5;

        ctx.lineWidth = 0.4;
        ctx.strokeStyle = '#f0b0a0';
        ctx.beginPath();
        for (var x = 0; x <= width; x += minorSec * pxPerSec) {
            ctx.moveTo(left + x, top);
            ctx.lineTo(left + x, top + rowHeight);
        }
        for (var y = 0; y <= rowHeight; y += minorMv * pxPerMv) {
            ctx.moveTo(left, top + y);
            ctx.lineTo(left + width, top + y);
        }
        ctx.stroke();

        ctx.lineWidth = 0.8;
        ctx.strokeStyle = '#e08070';
        ctx.beginPath();
        for (var xm = 0; xm <= width; xm += majorSec * pxPerSec) {
            ctx.moveTo(left + xm, top);
            ctx.lineTo(left + xm, top + rowHeight);
        }
        for (var ym = 0; ym <= rowHeight; ym += majorMv * pxPerMv) {
            ctx.moveTo(left, top + ym);
            ctx.lineTo(left + width, top + ym);
        }
        ctx.stroke();
    }

    function drawWaveform(ctx, values, time, rowTop, rowHeight, pxPerSec, pxPerMv) {
        var t0 = time[0];
        var midY = rowTop + rowHeight / 2;

        ctx.strokeStyle = '#1a1a2e';
        ctx.lineWidth = 0.9;
        ctx.beginPath();
        for (var i = 0; i < values.length; i++) {
            var x = (time[i] - t0) * pxPerSec;
            var y = midY - values[i] * pxPerMv;
            if (i === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
        }
        ctx.stroke();
    }

    document.addEventListener('DOMContentLoaded', function () {
        var containers = document.querySelectorAll('[data-ecg-url]');
        containers.forEach(loadEcg);
    });
})();
