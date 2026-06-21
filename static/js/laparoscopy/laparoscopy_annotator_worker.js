(function () {
    'use strict';

    window.LaparoscopyAnnotatorMixins = window.LaparoscopyAnnotatorMixins || {};

    window.LaparoscopyAnnotatorMixins.worker = function (VideoAnnotator) {

        // ── Worker WebSocket lifecycle ─────────────────────────────────────

        VideoAnnotator.prototype._wsConnect = function () {
            if (this._ws) return;
            if (!this._workerWsHost || !this._workerVideoId) {
                this._workerConnected = false;
                this._setMagicControlsEnabled(false);
                this._setMagicStatus('Worker unavailable: missing session config.', 'warning');
                return;
            }

            var wsProto = location.protocol === 'https:' ? 'wss' : 'ws';
            var url = wsProto + '://' + this._workerWsHost +
                '/ws/session/' + encodeURIComponent(this._workerVideoId) + '/';
            var self = this;
            this._setMagicControlsEnabled(false);
            this._setMagicStatus('Connecting to worker...', 'muted');
            this._ws = new WebSocket(url);
            window.__ws = this._ws;
            this._ws.onopen = function () { self._wsOnOpen(); };
            this._ws.onmessage = function (e) { self._wsOnMessage(e); };
            this._ws.onerror = function (e) { console.error('[WS] error', e); };
            this._ws.onclose = function (e) {
                console.log('[WS] close', e.code, e.reason);
                self._ws = null;
                self._workerConnected = false;
                self._setMagicControlsEnabled(false);
                self._setMagicStatus('Worker disconnected. Reconnecting...', 'warning');
                self._scheduleWsReconnect();
            };
        };

        VideoAnnotator.prototype._scheduleWsReconnect = function () {
            if (this._wsReconnectTimer) return;
            var self = this;
            var delay = Math.min(10000, 1000 * Math.pow(2, Math.min(this._wsReconnectAttempts || 0, 3)));
            this._wsReconnectAttempts = Number(this._wsReconnectAttempts || 0) + 1;
            this._wsReconnectTimer = setTimeout(function () {
                self._wsReconnectTimer = null;
                self._wsConnect();
            }, delay);
        };

        VideoAnnotator.prototype._wsOnOpen = function () {
            console.log('[WS] open');
            this._workerConnected = true;
            this._wsReconnectAttempts = 0;
            this._setMagicControlsEnabled(true);
            this._setMagicStatus('Worker connected.', 'success');

            if (!this._workerVideoSource) {
                this._setMagicStatus('Worker connected, but missing source video for SAM2.', 'warning');
                return;
            }

            var self = this;
            fetch('/laparoscopy/api/worker/session-ready/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'same-origin',
                body: JSON.stringify({
                    patientId: String(this.patientId),
                    video_source: this._workerVideoSource,
                    video_id: this._workerVideoId,
                }),
            })
            .then(function (resp) {
                return resp.text().then(function (text) {
                    if (!resp.ok) {
                        console.error('[WS] session ready failed', resp.status, text);
                        self._setMagicStatus('Worker ready failed (' + resp.status + ').', 'danger');
                        return;
                    }
                    self._setMagicStatus('Worker session ready.', 'success');
                });
            })
            .catch(function (err) {
                console.error('[WS] session ready error', err);
                self._setMagicStatus('Worker session setup error.', 'danger');
            });
        };

        VideoAnnotator.prototype._wsOnMessage = function (e) {
            try {
                var parsed = JSON.parse(e.data);
                if (!parsed || !parsed.type) return;
                if (parsed.type === 'inference_done') {
                    this._finalizePendingUpdateJob(parsed.job_id ? String(parsed.job_id) : null);
                    return;
                }
                if (parsed.type === 'frame_result') this._onMaskFrame(parsed);
            } catch (_err) {
                console.log('[WS raw]', e.data);
            }
        };

        VideoAnnotator.prototype._setMagicControlsEnabled = function (enabled) {
            var controls = [
                this._magicPointToolBtnEl,
                this._toolbarPointToolBtnEl,
                this._magicPointPositiveBtnEl,
                this._magicPointNegativeBtnEl,
                this._magicSendBtnEl,
                this._magicWindowInputEl,
            ];
            controls.forEach(function (el) { if (el) el.disabled = !enabled; });
            if (!enabled) this._hideMaskDecisionBox();
            if (!enabled && this.currentTool === 'point' && typeof this._setTool === 'function') {
                this._setTool(this._lastNonPointTool || 'brush');
            } else if (!enabled && this._magicPointActive) {
                this._setMagicPointActive(false);
            }
        };

        // ── Magic status (G6) ──────────────────────────────────────────────

        VideoAnnotator.prototype._setMagicStatus = function (message, tone) {
            if (!this._magicStatusEl) return;
            this._magicStatusEl.textContent = message;
            this._magicStatusEl.classList.remove('text-muted', 'text-success', 'text-danger', 'text-warning');
            if (tone === 'success') this._magicStatusEl.classList.add('text-success');
            else if (tone === 'danger')  this._magicStatusEl.classList.add('text-danger');
            else if (tone === 'warning') this._magicStatusEl.classList.add('text-warning');
            else this._magicStatusEl.classList.add('text-muted');
        };

        // ── Build per-region prompt signature (G7) ─────────────────────────

        VideoAnnotator.prototype._buildRegionPromptSignature = function (regionPayload, maskCacheSeq) {
            if (!regionPayload) return '';
            var pts    = Array.isArray(regionPayload.points)       ? regionPayload.points       : [];
            var labels = Array.isArray(regionPayload.point_labels) ? regionPayload.point_labels : [];
            var pairs  = [];
            for (var i = 0; i < pts.length; i++) {
                var p = pts[i] || [0, 0];
                pairs.push(Number(p[0]||0).toFixed(6) + ',' + Number(p[1]||0).toFixed(6) + ',' +
                           Number(labels[i]||0));
            }
            pairs.sort();
            var maskSig = 'none';
            if (regionPayload.mask_b64 && Array.isArray(regionPayload.mask_shape)) {
                maskSig = [String(regionPayload.mask_encoding || ''),
                           String(regionPayload.mask_shape[0] || ''),
                           String(regionPayload.mask_shape[1] || ''),
                           String(Number(maskCacheSeq || 0))].join(':');
            }
            return [String(regionPayload.region_id || ''), String(regionPayload.class_id || ''),
                    String(regionPayload.class_name || ''), pairs.join('|'), maskSig].join('::');
        };

        // ── Send magic prompts — per-region regions[] format (G2) ──────────

        VideoAnnotator.prototype._sendMagicPromptsViaAnnotator = function () {
            var self       = this;
            if (!this._workerConnected) {
                this._setMagicStatus('Worker is not connected. Wait for reconnect.', 'warning');
                return;
            }
            var rawFt = Number(this._currentVideoTime());
            if (!isFinite(rawFt) || rawFt < 0) rawFt = 0;
            // Snap so the frame key matches how prompts are stored (also snapped).
            var currentFt = (typeof this._snapToSubsampledFrame === 'function')
                ? this._snapToSubsampledFrame(rawFt) : rawFt;
            var currentKey = this._promptFrameKey(currentFt);
            var currentMaskKey = this._frameKey(currentFt);

            var framePoints = (this._magicPrompts || []).filter(function (p) {
                return self._promptFrameKey(p.frame_time) === currentKey;
            });

            if (!framePoints.length) {
                this._setMagicStatus('No prompts for current frame.', 'warning'); return;
            }

            var regionsById = {};
            framePoints.forEach(function (p) {
                var rid  = p.region_id != null ? String(p.region_id) : '1';
                var meta = (self.regions || []).find(function (r) { return String(r.id) === rid; }) || null;
                if (!regionsById[rid]) {
                    regionsById[rid] = {
                        region_id:    rid,
                        class_name:   meta ? meta.name : 'unknown',
                        points:       [],
                        point_labels: [],
                        normalized:   true,
                    };
                    if (meta && meta.dbId != null) regionsById[rid].class_id = String(meta.dbId);
                }
                regionsById[rid].points.push([p.x, p.y]);
                regionsById[rid].point_labels.push(Number(p.point_label) === 0 ? 0 : 1);
            });

            // Annotation-to-mask prompts (G8)
            var maskPrompts = this._collectAnnotationMaskPromptsForFrame(currentKey);
            Object.keys(maskPrompts).forEach(function (rid) {
                var mp = maskPrompts[rid];
                if (!mp) return;
                var meta = (self.regions || []).find(function (r) { return String(r.id) === rid; }) || null;
                var rp   = regionsById[rid];
                if (!rp) {
                    rp = { region_id: rid, class_name: meta ? meta.name : 'unknown', normalized: true };
                    if (meta && meta.dbId != null) rp.class_id = String(meta.dbId);
                    regionsById[rid] = rp;
                }
                rp.mask_b64 = mp.mask_b64;
                rp.mask_shape = mp.mask_shape;
                rp.mask_encoding = mp.mask_encoding;
            });

            var regionsPayload = Object.keys(regionsById).map(function (rid) { return regionsById[rid]; });
            var changedRegionsPayload = [];
            var pendingScopes = [];

            for (var ri = 0; ri < regionsPayload.length; ri++) {
                var rp = regionsPayload[ri];
                var regionId = String(rp.region_id || '');
                var scopeKey = this._scopeKey(currentKey, regionId);
                var sig = this._buildRegionPromptSignature(rp, Number(rp.cache_seq || 0));
                var hasMask = !!(rp.mask_b64 && Array.isArray(rp.mask_shape));
                if (!hasMask && this._lastPromptSigByScope[scopeKey] === sig) continue;

                var promptPoints = [];
                if (Array.isArray(rp.points) && Array.isArray(rp.point_labels)) {
                    var maxLen = Math.min(rp.points.length, rp.point_labels.length);
                    for (var ppi = 0; ppi < maxLen; ppi++) {
                        var pair = rp.points[ppi];
                        if (!Array.isArray(pair) || pair.length !== 2) continue;
                        promptPoints.push({ x: pair[0], y: pair[1], label: rp.point_labels[ppi] });
                    }
                }

                changedRegionsPayload.push(rp);
                pendingScopes.push({
                    frame_key: currentMaskKey,
                    prompt_frame_key: currentKey,
                    frame_ts: currentFt,
                    start_ts: currentFt,
                    end_ts: currentFt,
                    region_id: regionId,
                    signature: sig,
                    touched: {},
                    start_seq: Number(this._maskStoreSeq || 0),
                    latest_cache_seq: Number(this._maskStoreSeq || 0),
                    prompt_points: this._normalizePromptPoints(promptPoints),
                    replace_manual_shapes: hasMask,
                    completed: false,
                });
            }

            if (!changedRegionsPayload.length) {
                this._setMagicStatus('No prompt changes detected.', 'muted'); return;
            }

            var windowSeconds = 5.0;
            var windowEl = this._magicWindowInputEl || document.getElementById('magic-window-seconds-input');
            if (windowEl) { var pw = Number(windowEl.value); if (isFinite(pw) && pw > 0) windowSeconds = pw; }

            pendingScopes.forEach(function (scope) {
                scope.end_ts = scope.start_ts + windowSeconds + (0.5 / (self._subsampledVideoFps || 1));
                if (scope.region_id) self._clearRejectedTracksForRegion(scope.region_id);
            });
            this._cancelOverlappingPendingScopes(pendingScopes);
            var scopeGroup = this._registerPendingUpdateScopes(null, pendingScopes);

            var sendBtn = this._magicSendBtnEl || document.getElementById('magic-send-prompts-btn');
            if (sendBtn) sendBtn.disabled = true;
            this._setMagicStatus(
                'Sending prompts for ' + String(changedRegionsPayload.length) + ' region(s)...', 'muted');

            fetch('/laparoscopy/api/worker/session-prompt/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'same-origin',
                body: JSON.stringify({
                    patientId:       this.patientId,
                    video_id:        this._workerVideoId,
                    frame_timestamp: currentFt,
                    regions:         changedRegionsPayload,
                    window_seconds:  windowSeconds,
                    normalized:      true,
                }),
            })
            .then(function (resp) {
                return resp.text().then(function (t) {
                    if (!resp.ok) {
                        if (scopeGroup) self._cancelScopeGroup(scopeGroup);
                        self._setMagicStatus('Prompt request failed (' + resp.status + ').', 'danger');
                        return;
                    }
                    var parsed = {};
                    try { parsed = t ? JSON.parse(t) : {}; } catch (_err) { parsed = {}; }
                    var workerResponse = parsed && parsed.worker_response ? parsed.worker_response : {};
                    var jobId = workerResponse && workerResponse.job_id ? String(workerResponse.job_id) : null;
                    pendingScopes.forEach(function (scope) {
                        self._lastPromptSigByScope[self._scopeKey(scope.frame_key, scope.region_id)] = scope.signature;
                    });
                    if (scopeGroup && jobId) {
                        scopeGroup.job_id = jobId;
                        self._pendingUpdateScopesByJob[jobId] = scopeGroup;
                    }
                    self._setMagicStatus(
                        'Prompt sent for ' + String(changedRegionsPayload.length) +
                        ' region(s). Awaiting masks…', 'success');
                });
            })
            .catch(function () {
                if (scopeGroup) self._cancelScopeGroup(scopeGroup);
                self._setMagicStatus('Prompt request error.', 'danger');
            })
            .finally(function () { if (sendBtn) sendBtn.disabled = false; });
        };

        // ── Annotation-to-mask prompts (G8) ───────────────────────────────

        VideoAnnotator.prototype._collectAnnotationMaskPromptsForFrame = function (frameKey) {
            var maskW = Number(this.videoEl && this.videoEl.videoWidth  ? this.videoEl.videoWidth  : 0);
            var maskH = Number(this.videoEl && this.videoEl.videoHeight ? this.videoEl.videoHeight : 0);
            if (!maskW || !maskH) return {};

            var shapes  = Array.isArray(this.shapes) ? this.shapes : [];
            var grouped = {};
            for (var i = 0; i < shapes.length; i++) {
                var shape = shapes[i];
                if (!shape || this._frameKey(shape.frameTime || 0) !== frameKey) continue;
                var rid = shape.regionId != null ? String(shape.regionId) : '';
                if (!rid) continue;
                if (!grouped[rid]) grouped[rid] = [];
                grouped[rid].push(shape);
            }

            var result = {}, rids = Object.keys(grouped);
            for (var ri = 0; ri < rids.length; ri++) {
                var rid2   = rids[ri];
                var prompt = this._buildAnnotationMaskPrompt(grouped[rid2], maskW, maskH, rid2);
                if (prompt) result[rid2] = prompt;
            }
            return result;
        };

        VideoAnnotator.prototype._buildAnnotationMaskPrompt = function (annotationShapes, maskW, maskH, regionId) {
            if (!Array.isArray(annotationShapes) || !annotationShapes.length) return null;
            maskW = Math.round(maskW); maskH = Math.round(maskH);
            if (maskW <= 0 || maskH <= 0) return null;

            var canvas = document.createElement('canvas');
            canvas.width = maskW; canvas.height = maskH;
            var ctx = canvas.getContext('2d', { willReadFrequently: true });
            if (!ctx) return null;
            ctx.clearRect(0, 0, maskW, maskH);
            var drewAny = false;

            for (var si = 0; si < annotationShapes.length; si++) {
                var shape = annotationShapes[si];
                if (!shape || !shape.konvaNode || typeof shape.konvaNode.points !== 'function') continue;
                var pts = shape.konvaNode.points();
                if (!Array.isArray(pts) || pts.length < 4) continue;

                if (shape.type === 'polygon') {
                    if (pts.length < 6) continue;
                    ctx.save();
                    ctx.globalCompositeOperation = 'source-over';
                    ctx.fillStyle = '#ffffff';
                    ctx.beginPath();
                    ctx.moveTo(Number(pts[0]) || 0, Number(pts[1]) || 0);
                    for (var pi = 2; pi < pts.length; pi += 2) {
                        ctx.lineTo(Number(pts[pi]) || 0, Number(pts[pi+1]) || 0);
                    }
                    ctx.closePath(); ctx.fill(); ctx.restore();
                    drewAny = true;
                } else if (shape.type === 'brush' || shape.type === 'eraser') {
                    var sw = (typeof shape.konvaNode.strokeWidth === 'function')
                        ? Number(shape.konvaNode.strokeWidth()) : Number(this.brushSize || 1);
                    if (!isFinite(sw) || sw <= 0) sw = 1;
                    ctx.save();
                    ctx.globalCompositeOperation = shape.type === 'eraser' ? 'destination-out' : 'source-over';
                    ctx.strokeStyle = '#ffffff'; ctx.lineWidth = sw;
                    ctx.lineCap = 'round'; ctx.lineJoin = 'round';
                    ctx.beginPath();
                    ctx.moveTo(Number(pts[0]) || 0, Number(pts[1]) || 0);
                    for (var li = 2; li < pts.length; li += 2) {
                        ctx.lineTo(Number(pts[li]) || 0, Number(pts[li+1]) || 0);
                    }
                    ctx.stroke(); ctx.restore();
                    drewAny = true;
                }
            }

            if (!drewAny) return null;
            var imageData = ctx.getImageData(0, 0, maskW, maskH);
            var alpha     = imageData.data;
            var pixelCount = maskW * maskH;
            var packed    = new Uint8Array(Math.ceil(pixelCount / 8));
            var hasFG     = false;
            for (var pixel = 0, ai = 3; pixel < pixelCount; pixel++, ai += 4) {
                if (alpha[ai] <= 0) continue;
                hasFG = true;
                packed[pixel >> 3] |= (1 << (pixel & 7));
            }
            if (!hasFG) return null;

            return {
                mask_b64:      this._encodeBytesToB64(packed),
                mask_shape:    [maskH, maskW],
                mask_encoding: 'bitpack_u1_v1',
                cache_seq:     0,
                source:        'annotations',
                region_id:     regionId != null ? String(regionId) : null,
            };
        };

    };
})();
