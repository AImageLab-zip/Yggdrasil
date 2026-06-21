(function () {
    'use strict';

    var U = window.LaparoscopyAnnotatorUtils;

    var MAX_MASK_CACHE = 300;

    window.LaparoscopyAnnotatorMixins = window.LaparoscopyAnnotatorMixins || {};

    window.LaparoscopyAnnotatorMixins.magic = function (VideoAnnotator) {

        VideoAnnotator.prototype._snapToSubsampledFrame = function (t) {
            var fps = this._subsampledVideoFps || 1;
            return Math.round(t * fps) / fps;
        };

        VideoAnnotator.prototype._frameKey = function (t) {
            return this._snapToSubsampledFrame(t).toFixed(6);
        };

        VideoAnnotator.prototype._currentVideoTime = function () {
            if (
                this.annotationMode &&
                this._timelineDrag &&
                this.videoEl && this.videoEl.paused &&
                isFinite(this._timelinePreviewTime)
            ) {
                return Number(this._timelinePreviewTime);
            }
            return (this.videoEl && isFinite(this.videoEl.currentTime))
                ? Number(this.videoEl.currentTime) : 0;
        };

        VideoAnnotator.prototype._regionGroupKey = function (entryOrRegionId, objectId) {
            if (entryOrRegionId && typeof entryOrRegionId === 'object') {
                var rid = entryOrRegionId.region_id != null ? entryOrRegionId.region_id : '';
                var oid = entryOrRegionId.object_id  != null ? entryOrRegionId.object_id  : '';
                return String(rid) + '::' + String(oid);
            }
            return String(entryOrRegionId != null ? entryOrRegionId : '') + '::' +
                   String(objectId != null ? objectId : '');
        };

        VideoAnnotator.prototype._autoScopeKey = function (frameKey, regionGroupKey) {
            return String(frameKey || '') + '::' + String(regionGroupKey || '');
        };

        VideoAnnotator.prototype._promptFrameKey = function (t) {
            return Number(t || 0).toFixed(6);
        };

        VideoAnnotator.prototype._scopeKey = function (frameKey, regionId) {
            return String(frameKey || '') + '::' + String(regionId || '');
        };

        VideoAnnotator.prototype._trackStorageKey = function (regionGroupKey, trackId) {
            return String(regionGroupKey || '') + '::' + String(trackId || '');
        };

        VideoAnnotator.prototype._onMaskFrame = function (frameResult) {
            var entry = this._storeMaskFrame(frameResult);
            this._syncMaskToCurrentVideoTime();
            if (!entry) {
                this._setMagicStatus('Mask update ignored (empty or rejected).', 'muted');
                return;
            }
            this._setMagicStatus('Mask received and saved for frame ' + String(entry.frame_index), 'success');
        };

        VideoAnnotator.prototype._storeMaskFrame = function (frameResult) {
            if (!frameResult || !frameResult.mask_b64 || !Array.isArray(frameResult.mask_shape)) return null;

            var ts = Number(frameResult.timestamp);
            if (!isFinite(ts) || ts < 0) ts = this._currentVideoTime();

            var regionId = frameResult.region_id != null ? String(frameResult.region_id) : null;
            var frameKey = this._frameKey(ts);

            var entry = {
                timestamp:    ts,
                frame_key:    frameKey,
                frame_index:  Number(frameResult.frame_index || -1),
                job_id:       frameResult.job_id != null ? String(frameResult.job_id) : null,
                mask_b64:     frameResult.mask_b64,
                mask_shape:   frameResult.mask_shape,
                mask_encoding: frameResult.mask_encoding || null,
                cache_seq:    ++this._maskStoreSeq,
                region_id:    regionId,
                object_id:    frameResult.object_id != null ? String(frameResult.object_id) : null,
                class_name:   frameResult.class_name || null,
                class_id:     frameResult.class_id   || null,
                prompt_points: Array.isArray(frameResult.prompt_points) ? frameResult.prompt_points : [],
            };

            this._prepareMaskContours(entry);

            var pendingScope = this._findPendingScopeForEntry(entry);
            if (pendingScope && !pendingScope.replaced_frame_keys) pendingScope.replaced_frame_keys = {};
            if (pendingScope && !pendingScope.replaced_frame_keys[entry.frame_key]) {
                this._clearPriorCanonicalStateForFrameRegion(
                    entry.frame_key,
                    entry.region_id,
                    Number(pendingScope.start_seq || 0),
                    !!pendingScope.replace_manual_shapes
                );
                pendingScope.replaced_frame_keys[entry.frame_key] = true;
            }
            if (
                pendingScope &&
                pendingScope.prompt_frame_key &&
                String(pendingScope.prompt_frame_key) === Number(entry.timestamp || 0).toFixed(6)
            ) {
                entry.prompt_points = this._normalizePromptPoints(pendingScope.prompt_points);
            } else if (pendingScope && String(pendingScope.frame_key || '') === String(entry.frame_key || '')) {
                entry.prompt_points = this._normalizePromptPoints(pendingScope.prompt_points);
            }
            if (!entry.prompt_points.length && pendingScope && pendingScope.prompt_frame_key && entry.region_id != null) {
                entry.prompt_points = this._collectPromptPointsForPromptFrameRegion(
                    pendingScope.prompt_frame_key,
                    entry.region_id
                );
            }
            if (!entry.prompt_points.length && entry.region_id != null) {
                entry.prompt_points = this._collectPromptPointsForFrameRegion(entry.frame_key, entry.region_id);
            }

            var seedEntry = this._findCachedEntryByFrameRegion(entry.frame_key, entry.region_id, entry.object_id) ||
                this._findSeedEntryForEntry(entry);
            this._assignComponentTrackIds(entry, seedEntry);
            this._filterRejectedComponentsInEntry(entry);
            this._markPendingScopeFrame(entry);

            this._maskFrameCache = this._maskFrameCache.filter(function (existing) {
                return !(
                    String(existing.frame_key || '') === String(entry.frame_key || '') &&
                    String(existing.region_id || '') === String(entry.region_id || '')
                );
            }, this);

            if (!Array.isArray(entry.prepared_contours) || !entry.prepared_contours.length) return null;

            this._maskFrameCache.push(entry);
            if (this._maskFrameCache.length > MAX_MASK_CACHE) {
                this._maskFrameCache = this._maskFrameCache.slice(
                    this._maskFrameCache.length - MAX_MASK_CACHE);
            }
            this._syncAutoAcceptedShapesForEntry(entry);
            return entry;
        };

        VideoAnnotator.prototype._findSeedEntryForEntry = function (entry) {
            if (!entry) return null;
            var regionGroup = this._regionGroupKey(entry);
            for (var i = this._maskFrameCache.length - 1; i >= 0; i--) {
                var e = this._maskFrameCache[i];
                if (this._regionGroupKey(e) !== regionGroup) continue;
                return e;
            }
            return null;
        };

        VideoAnnotator.prototype._acceptMask = function (regionIdOverride, componentIndex, targetCacheSeq) {
            var maskFrame = this._findMaskFrameBySeq(targetCacheSeq);
            var currentFrames = Array.isArray(this._currentMaskFrames) ? this._currentMaskFrames : [];
            if (!maskFrame && regionIdOverride != null) {
                for (var cf = 0; cf < currentFrames.length; cf++) {
                    if (String(currentFrames[cf].region_id || '') === String(regionIdOverride)) {
                        maskFrame = currentFrames[cf]; break;
                    }
                }
            }
            if (!maskFrame) maskFrame = this._currentMaskFrame || (currentFrames.length ? currentFrames[0] : null);
            if (!maskFrame) return;

            var region = this._resolveMaskRegion(maskFrame, regionIdOverride);
            if (!region) { console.warn('[Magic] accept: no active region'); return; }

            var contours = Array.isArray(maskFrame.prepared_contours) ? maskFrame.prepared_contours : [];
            var maskW    = Number(maskFrame.prepared_mask_w);
            var maskH    = Number(maskFrame.prepared_mask_h);
            if (!contours.length || !maskW || !maskH) {
                this._prepareMaskContours(maskFrame);
                contours = Array.isArray(maskFrame.prepared_contours) ? maskFrame.prepared_contours : [];
                maskW    = Number(maskFrame.prepared_mask_w);
                maskH    = Number(maskFrame.prepared_mask_h);
            }
            if (!contours.length) { console.warn('[Magic] accept: no contour'); return; }

            var frameTime = isFinite(maskFrame.timestamp)
                ? Number(maskFrame.timestamp)
                : Number(this._currentVideoTime());
            if (!isFinite(frameTime) || frameTime < 0) frameTime = 0;
            var frameKey    = maskFrame.frame_key || this._frameKey(frameTime);
            var promptPoints = this._normalizePromptPoints(maskFrame.prompt_points);
            if (!promptPoints.length) {
                promptPoints = this._collectPromptPointsForFrameRegion(frameKey, region.id);
            }

            var selectedContours = [], selectedIndexes = [];
            if (typeof componentIndex === 'number') {
                if (componentIndex >= 0 && componentIndex < contours.length) {
                    selectedContours.push(contours[componentIndex]);
                    selectedIndexes.push(componentIndex);
                }
            } else {
                selectedContours = contours.slice();
                for (var si = 0; si < contours.length; si++) selectedIndexes.push(si);
            }
            if (!selectedContours.length) return;

            var videoW = this.videoEl.videoWidth || this.stage.width();
            var videoH = this.videoEl.videoHeight || this.stage.height();
            var scaleX = videoW / maskW, scaleY = videoH / maskH;
            var created = 0;

            for (var ci = 0; ci < selectedContours.length; ci++) {
                var contour = selectedContours[ci];
                if (!contour || contour.length < 6) continue;
                var scaledPoints = U.rdpSimplify(contour.map(function (v, i) {
                    return i % 2 === 0 ? v * scaleX : v * scaleY;
                }), 6.0);
                if (scaledPoints.length < 6) continue;
                var konvaNode = new Konva.Line({
                    points:      scaledPoints,
                    fill:        region.color + '55',
                    stroke:      region.color,
                    strokeWidth: 2,
                    closed:      true,
                    listening:   false,
                });
                region.layer.add(konvaNode);
                this._registerShape('polygon', konvaNode, {
                    regionId:     region.id,
                    frameTime:    frameTime,
                    promptPoints: promptPoints,
                });
                created++;
            }
            if (!created) return;
            region.layer.draw();

            selectedIndexes.sort(function (a, b) { return b - a; });
            selectedIndexes.forEach(function (idx) {
                if (idx >= 0 && idx < contours.length) contours.splice(idx, 1);
            });
            maskFrame.prepared_contours = contours;

            if (!contours.length) {
                var seqToRemove = Number(maskFrame.cache_seq);
                this._discardMaskFrames(function (e) {
                    return Number(e.cache_seq) === seqToRemove;
                });
            }

            this._syncMaskToCurrentVideoTime();
            this._setMagicStatus(
                'Accepted ' + String(created) + ' polygon' + (created === 1 ? '' : 's') + '.',
                'success'
            );
        };

        VideoAnnotator.prototype._updateMagicAcceptButton = function () {
            if (!this.annotationMode) { this._hideMagicAcceptBtn(); return; }
            var hasMask = Array.isArray(this._currentMaskFrames) && this._currentMaskFrames.length > 0;
            if (!hasMask) { this._hideMagicAcceptBtn(); return; }
            this._showMaskDecisionBox();
        };

        VideoAnnotator.prototype._hideMagicAcceptBtn = function () {
            this._hideMaskDecisionBox();
            this._currentMaskFrames = [];
            this._currentMaskFrame  = null;
        };

        VideoAnnotator.prototype._hideMaskDecisionBox = function () {
            if (this._maskDecisionPanelEl) {
                this._maskDecisionPanelEl.remove();
                this._maskDecisionPanelEl = null;
            }
            if (this._maskDecisionLayerEl) {
                this._maskDecisionLayerEl.innerHTML = '';
                this._maskDecisionLayerEl.style.display = 'none';
            }
            if (this._maskHoverCacheSeq != null || this._maskHoverComponentIndex != null) {
                this._maskHoverCacheSeq = null;
                this._maskHoverComponentIndex = null;
                if (Array.isArray(this._currentMaskFrames) && this._currentMaskFrames.length) {
                    this._drawMaskOverlay(this._currentMaskFrames);
                }
            }
        };

        VideoAnnotator.prototype._setMaskHoverTarget = function (cacheSeq, componentIndex) {
            var nextSeq  = cacheSeq       != null ? Number(cacheSeq)       : null;
            var nextComp = componentIndex != null ? Number(componentIndex) : null;
            if (this._maskHoverCacheSeq === nextSeq && this._maskHoverComponentIndex === nextComp) return;
            this._maskHoverCacheSeq = nextSeq;
            this._maskHoverComponentIndex = nextComp;
            if (Array.isArray(this._currentMaskFrames) && this._currentMaskFrames.length) {
                this._drawMaskOverlay(this._currentMaskFrames);
            }
        };

        VideoAnnotator.prototype._clearMaskHoverTarget = function () {
            if (this._maskHoverCacheSeq == null && this._maskHoverComponentIndex == null) return;
            this._maskHoverCacheSeq = null;
            this._maskHoverComponentIndex = null;
            if (Array.isArray(this._currentMaskFrames) && this._currentMaskFrames.length) {
                this._drawMaskOverlay(this._currentMaskFrames);
            }
        };

        VideoAnnotator.prototype._showMaskDecisionBox = function () {
            this._hideMaskDecisionBox();
            var layerEl = this._maskDecisionLayerEl;
            if (!layerEl) return;
            var frames = Array.isArray(this._currentMaskFrames) ? this._currentMaskFrames : [];
            if (!frames.length) return;

            layerEl.style.display = '';
            var self = this;
            var panel = document.createElement('div');
            panel.style.cssText =
                'position:absolute;bottom:12px;left:50%;transform:translateX(-50%);' +
                'background:rgba(20,20,20,0.82);border-radius:8px;padding:8px 12px;' +
                'display:flex;gap:8px;align-items:center;pointer-events:auto;z-index:5;';

            if (frames.length === 1 &&
                Array.isArray(frames[0].prepared_contours) &&
                frames[0].prepared_contours.length > 1) {
                var frame0 = frames[0];
                var n = frame0.prepared_contours.length;
                for (var ci = 0; ci < n; ci++) {
                    (function (idx, fr) {
                        var cBtn = document.createElement('button');
                        cBtn.textContent = 'Comp ' + (idx + 1);
                        cBtn.className = 'btn btn-sm btn-outline-light';
                        cBtn.style.fontSize = '11px';
                        cBtn.addEventListener('click', function () {
                            self._acceptMask(
                                fr.region_id != null ? String(fr.region_id) : null,
                                idx,
                                fr.cache_seq
                            );
                        });
                        panel.appendChild(cBtn);
                    })(ci, frame0);
                }
            }

            var acceptAllBtn = document.createElement('button');
            acceptAllBtn.textContent = 'Accept all';
            acceptAllBtn.className = 'btn btn-sm btn-success';
            acceptAllBtn.addEventListener('click', function () {
                self._acceptMask(null, null, null);
            });
            panel.appendChild(acceptAllBtn);

            var rejectBtn = document.createElement('button');
            rejectBtn.textContent = 'Reject';
            rejectBtn.className = 'btn btn-sm btn-danger';
            rejectBtn.addEventListener('click', function () {
                self._rejectMask(null, null);
            });
            panel.appendChild(rejectBtn);

            layerEl.appendChild(panel);
            this._maskDecisionPanelEl = panel;
        };

        VideoAnnotator.prototype._rejectMask = function (componentIndex, targetCacheSeq) {
            var maskFrame   = this._findMaskFrameBySeq(targetCacheSeq);
            var currentFrames = Array.isArray(this._currentMaskFrames) ? this._currentMaskFrames : [];
            if (!maskFrame) maskFrame = this._currentMaskFrame || (currentFrames.length ? currentFrames[0] : null);
            if (!maskFrame) return;

            var regionId = maskFrame.region_id != null ? String(maskFrame.region_id) : null;
            var cutoffTs = isFinite(maskFrame.timestamp) ? Number(maskFrame.timestamp)
                : this._snapToSubsampledFrame(this._currentVideoTime());

            if (!regionId) {
                this._setMagicStatus('Cannot reject: no region id on mask.', 'warning'); return;
            }

            var regionGroup = this._regionGroupKey(maskFrame);
            var trackIds    = Array.isArray(maskFrame.component_track_ids) ? maskFrame.component_track_ids : [];

            if (typeof componentIndex === 'number' && isFinite(componentIndex)) {
                if (componentIndex < 0 || componentIndex >= trackIds.length) {
                    this._setMagicStatus('Invalid component index.', 'warning'); return;
                }
                var trackId  = Number(trackIds[componentIndex]);
                if (!isFinite(trackId)) {
                    this._setMagicStatus('Missing lineage id.', 'warning'); return;
                }
                var trackKey = String(regionGroup || '') + '::' + String(trackId || '');
                this._rejectedTrackCutoffByKey[trackKey] = cutoffTs;
                this._removeTrackFromMaskCache(regionGroup, trackId, cutoffTs);
                this._removeAutoShapesForTrackFromTimestamp(trackKey, cutoffTs);
                delete this._lastPromptSigByScope[String(maskFrame.frame_key || this._frameKey(cutoffTs) || '') + '::' + String(regionId || '')];
                this._syncMaskToCurrentVideoTime();
                this._setMagicStatus('Component rejected from this frame onward.', 'warning');
                return;
            }

            var rid = String(regionId);
            this._cancelPendingScopesForRegion(rid, cutoffTs);
            this._discardMaskFrames(function (e) {
                if (String(e.region_id || '') !== rid) return false;
                if (Number(e.timestamp) >= cutoffTs - 1e-6) {
                    this._removeAutoShapesForScope(this._autoScopeKey(e.frame_key, this._regionGroupKey(e)));
                }
                return Number(e.timestamp) >= cutoffTs - 1e-6;
            }.bind(this));
            delete this._lastPromptSigByScope[String(maskFrame.frame_key || this._frameKey(cutoffTs) || '') + '::' + String(rid || '')];
            this._syncMaskToCurrentVideoTime();
            this._setMagicStatus('Region rejected from this frame onward.', 'warning');
        };

        VideoAnnotator.prototype._normalizePromptPoints = function (rawPoints) {
            if (!Array.isArray(rawPoints)) return [];
            var out = [];
            for (var i = 0; i < rawPoints.length; i++) {
                var p = rawPoints[i];
                if (!p || typeof p !== 'object') continue;
                var x = Number(p.x), y = Number(p.y);
                if (!isFinite(x) || !isFinite(y) || x < 0 || x > 1 || y < 0 || y > 1) continue;
                out.push({ x: x, y: y, label: Number(p.label) === 0 ? 0 : 1 });
            }
            return out;
        };

        VideoAnnotator.prototype._collectPromptPointsForFrameRegion = function (frameKey, regionId) {
            var targetKey    = String(frameKey || '');
            var targetRegion = String(regionId || '');
            if (!targetKey || !targetRegion) return [];
            var seen = {}, collected = [];
            var prompts = Array.isArray(this._magicPrompts) ? this._magicPrompts : [];
            for (var i = 0; i < prompts.length; i++) {
                var p = prompts[i];
                if (!p) continue;
                if (this._frameKey(p.frame_time) !== targetKey) continue;
                if (String(p.region_id || '') !== targetRegion) continue;
                var norm = this._normalizePromptPoints([{ x: p.x, y: p.y, label: p.point_label }]);
                if (!norm.length) continue;
                var pt = norm[0];
                var key = pt.x.toFixed(6) + '::' + pt.y.toFixed(6) + '::' + String(pt.label);
                if (seen[key]) continue;
                seen[key] = true;
                collected.push(pt);
            }
            return collected;
        };

        VideoAnnotator.prototype._collectPromptPointsForPromptFrameRegion = function (promptFrameKey, regionId) {
            var targetKey    = String(promptFrameKey || '');
            var targetRegion = String(regionId || '');
            if (!targetKey || !targetRegion) return [];
            var seen = {}, collected = [];
            var prompts = Array.isArray(this._magicPrompts) ? this._magicPrompts : [];
            for (var i = 0; i < prompts.length; i++) {
                var p = prompts[i];
                if (!p) continue;
                if (Number(p.frame_time || 0).toFixed(6) !== targetKey) continue;
                if (String(p.region_id || '') !== targetRegion) continue;
                var norm = this._normalizePromptPoints([{ x: p.x, y: p.y, label: p.point_label }]);
                if (!norm.length) continue;
                var pt = norm[0];
                var key = pt.x.toFixed(6) + '::' + pt.y.toFixed(6) + '::' + String(pt.label);
                if (seen[key]) continue;
                seen[key] = true;
                collected.push(pt);
            }
            return collected;
        };

        VideoAnnotator.prototype._restoreMagicPromptsFromAnnotations = function (annotations) {
            if (!Array.isArray(annotations)) return;
            var merged = [], seen = {};
            var self = this;

            function addPrompt(frameTime, regionId, pt, stableId) {
                var key = [Number(frameTime || 0).toFixed(6), String(regionId || ''),
                           pt.x.toFixed(6), pt.y.toFixed(6), String(pt.label)].join('::');
                if (seen[key]) return;
                seen[key] = true;
                merged.push({ id: stableId, x: pt.x, y: pt.y,
                              frame_time: frameTime, region_id: regionId, point_label: pt.label });
            }

            var existing = Array.isArray(this._magicPrompts) ? this._magicPrompts : [];
            for (var ei = 0; ei < existing.length; ei++) {
                var e = existing[ei];
                if (!e) continue;
                var norm = this._normalizePromptPoints([{ x: e.x, y: e.y, label: e.point_label }]);
                if (!norm.length) continue;
                addPrompt(Number(e.frame_time || 0), e.region_id,
                          norm[0], e.id || ('mp-existing-' + String(ei)));
            }

            // For propagated masks, many annotations share the same prompt_points but at
            // different frame_times. We only restore prompts at the earliest frame_time for
            // each unique (region_id + prompt_points content) combination so that after reload
            // prompts appear only on the frame where the user originally clicked.
            var earliestByPromptSig = {};
            for (var ai = 0; ai < annotations.length; ai++) {
                var ann = annotations[ai];
                if (!ann || ann.region_type_id == null) continue;
                var ft = Number(ann.frame_time || 0);
                if (!isFinite(ft) || ft < 0) continue;
                var region = this._regionByDbId ? this._regionByDbId(ann.region_type_id) : null;
                if (!region) continue;
                var pts = this._normalizePromptPoints(ann.prompt_points);
                if (!pts.length) continue;
                var ptsSig = String(region.id) + '::' + pts.map(function (p) {
                    return p.x.toFixed(6) + ',' + p.y.toFixed(6) + ',' + String(p.label);
                }).join(';');
                var prev = earliestByPromptSig[ptsSig];
                if (!prev || ft < prev.ft) {
                    earliestByPromptSig[ptsSig] = { ft: ft, ann: ann, pts: pts, regionId: region.id };
                }
            }
            var sigKeys = Object.keys(earliestByPromptSig);
            for (var si = 0; si < sigKeys.length; si++) {
                var entry = earliestByPromptSig[sigKeys[si]];
                for (var pi = 0; pi < entry.pts.length; pi++) {
                    addPrompt(entry.ft, entry.regionId, entry.pts[pi],
                              'mp-db-' + String(entry.ann.id || si) + '-' + String(pi));
                }
            }

            this._magicPrompts = merged;
            this._renderMagicOverlay();
            this._renderMagicPromptList();
            this._updateMagicCount();
        };

        VideoAnnotator.prototype._renderMagicPromptList = function () {
            var el = this._magicPromptsListEl;
            if (!el) return;
            el.innerHTML = '';
            var prompts = Array.isArray(this._magicPrompts) ? this._magicPrompts : [];
            if (!prompts.length) {
                var empty = document.createElement('li');
                empty.className = 'list-group-item text-muted py-1 px-2';
                empty.textContent = 'No prompts yet.';
                el.appendChild(empty);
                return;
            }

            var self = this;
            var currentFt = Number(this._currentVideoTime());
            var tol = 0.02;
            var byFrame = {};
            prompts.forEach(function (p) {
                var key = Number(p.frame_time || 0).toFixed(6);
                if (!byFrame[key]) byFrame[key] = [];
                byFrame[key].push(p);
            });

            Object.keys(byFrame).sort(function (a, b) { return Number(a) - Number(b); }).forEach(function (ft) {
                var isCurrent = Math.abs(Number(ft) - currentFt) <= tol;
                var header = document.createElement('li');
                header.className = 'list-group-item py-1 px-2 small fw-bold' +
                    (isCurrent ? ' list-group-item-success' : ' text-muted');
                header.textContent = 'Frame ' + ft + 's' + (isCurrent ? ' current' : '');
                el.appendChild(header);

                byFrame[ft].forEach(function (p) {
                    var region = (self.regions || []).find(function (r) { return r.id === p.region_id; }) || null;
                    var isNegative = Number(p.point_label) === 0;
                    var item = document.createElement('li');
                    item.className = 'list-group-item py-1 px-3 small d-flex gap-2 align-items-center';
                    var dot = document.createElement('span');
                    dot.style.cssText = isNegative
                        ? 'width:10px;height:10px;flex-shrink:0;display:inline-flex;align-items:center;justify-content:center;color:' + (region ? region.color : '#888') + ';font-size:12px;font-weight:700;line-height:1;'
                        : 'width:8px;height:8px;border-radius:50%;flex-shrink:0;background:' + (region ? region.color : '#888');
                    if (isNegative) dot.textContent = 'x';
                    item.appendChild(dot);
                    var label = document.createElement('span');
                    label.className = 'flex-grow-1';
                    label.textContent = (isNegative ? '[-] ' : '[+] ') + (region ? region.name : '?') + '  ' +
                        Math.round(Number(p.x || 0) * 100) + '%, ' + Math.round(Number(p.y || 0) * 100) + '%';
                    item.appendChild(label);
                    el.appendChild(item);
                });
            });
        };

        VideoAnnotator.prototype._contourDescriptor = function (contour) {
            if (!Array.isArray(contour) || contour.length < 6) return null;
            var minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
            var sumX = 0, sumY = 0, n = contour.length / 2;
            for (var i = 0; i < contour.length; i += 2) {
                var x = Number(contour[i]), y = Number(contour[i+1]);
                if (!isFinite(x) || !isFinite(y)) continue;
                if (x < minx) minx = x; if (x > maxx) maxx = x;
                if (y < miny) miny = y; if (y > maxy) maxy = y;
                sumX += x; sumY += y;
            }
            if (!isFinite(minx)) return null;
            return { minx: minx, miny: miny, maxx: maxx, maxy: maxy,
                     cx: sumX/n, cy: sumY/n, area: Math.max(1, Math.abs(U.polygonArea(contour))) };
        };

        VideoAnnotator.prototype._bboxIoU = function (a, b) {
            var ix1 = Math.max(a.minx, b.minx), iy1 = Math.max(a.miny, b.miny);
            var ix2 = Math.min(a.maxx, b.maxx), iy2 = Math.min(a.maxy, b.maxy);
            var iw  = Math.max(0, ix2 - ix1), ih = Math.max(0, iy2 - iy1);
            var inter = iw * ih;
            if (inter <= 0) return 0;
            var areaA = Math.max(1, (a.maxx-a.minx) * (a.maxy-a.miny));
            var areaB = Math.max(1, (b.maxx-b.minx) * (b.maxy-b.miny));
            var union = areaA + areaB - inter;
            return union > 0 ? inter / union : 0;
        };

        VideoAnnotator.prototype._assignComponentTrackIds = function (entry, seedEntry) {
            if (!entry) return;
            var contours = Array.isArray(entry.prepared_contours) ? entry.prepared_contours : [];
            if (!contours.length) { entry.component_track_ids = []; return; }

            var regionGroup = this._regionGroupKey(entry);
            if (!this._autoMaskTrackStateByGroup[regionGroup]) {
                this._autoMaskTrackStateByGroup[regionGroup] = {
                    nextTrackId: 1, lastTimestamp: null, lastComponents: [],
                };
            }
            var state = this._autoMaskTrackStateByGroup[regionGroup];

            var prevComponents = [];
            if (seedEntry && Array.isArray(seedEntry.prepared_contours) &&
                Array.isArray(seedEntry.component_track_ids)) {
                for (var si2 = 0; si2 < seedEntry.prepared_contours.length; si2++) {
                    var desc2 = this._contourDescriptor(seedEntry.prepared_contours[si2]);
                    if (!desc2) continue;
                    desc2.track_id = seedEntry.component_track_ids[si2];
                    prevComponents.push(desc2);
                }
            }
            if (!prevComponents.length) prevComponents = (state.lastComponents || []).slice();

            var descriptors = contours.map(function (c) { return this._contourDescriptor(c); }.bind(this));
            var assigned    = new Array(contours.length);
            var usedPrev    = {};
            var diag = Math.sqrt(
                Math.pow(Number(entry.prepared_mask_w || 1), 2) +
                Math.pow(Number(entry.prepared_mask_h || 1), 2)
            );

            for (var ci = 0; ci < descriptors.length; ci++) {
                var current = descriptors[ci];
                if (!current) { assigned[ci] = ++state.nextTrackId; continue; }
                var bestIdx = -1, bestScore = -Infinity;
                for (var pi = 0; pi < prevComponents.length; pi++) {
                    var prev = prevComponents[pi];
                    if (!prev || prev.track_id == null || usedPrev[pi]) continue;
                    var iou = this._bboxIoU(current, prev);
                    var dx = current.cx - prev.cx, dy = current.cy - prev.cy;
                    var dist = Math.sqrt(dx*dx + dy*dy);
                    var distScore  = 1 - Math.min(1, dist / Math.max(1, diag * 0.35));
                    var areaRatio  = Math.min(current.area, prev.area) / Math.max(current.area, prev.area);
                    var score      = iou * 2.0 + distScore * 0.7 + areaRatio * 0.5;
                    if (score > bestScore) { bestScore = score; bestIdx = pi; }
                }
                if (bestIdx >= 0 && bestScore >= 0.42) {
                    assigned[ci] = Number(prevComponents[bestIdx].track_id);
                    usedPrev[bestIdx] = true;
                } else {
                    assigned[ci] = ++state.nextTrackId;
                }
            }

            entry.component_track_ids = assigned;

            var entryTs = Number(entry.timestamp || 0);
            if (state.lastTimestamp == null || entryTs >= Number(state.lastTimestamp) - 1e-6) {
                state.lastTimestamp = entryTs;
                state.lastComponents = descriptors.map(function (d, i) {
                    if (!d) return null;
                    return { track_id: assigned[i], minx: d.minx, miny: d.miny,
                             maxx: d.maxx, maxy: d.maxy, cx: d.cx, cy: d.cy, area: d.area };
                }).filter(Boolean);
            }
        };

        VideoAnnotator.prototype._filterRejectedComponentsInEntry = function (entry) {
            if (!entry) return;
            var contours = Array.isArray(entry.prepared_contours) ? entry.prepared_contours : [];
            var trackIds = Array.isArray(entry.component_track_ids) ? entry.component_track_ids : [];
            if (!contours.length || !trackIds.length) return;
            var regionGroup   = this._regionGroupKey(entry);
            var keptContours  = [], keptTrackIds = [];
            for (var i = 0; i < contours.length; i++) {
                var tid = trackIds[i];
                var tk = tid != null ? (String(regionGroup || '') + '::' + String(tid || '')) : null;
                if (tk && isFinite(this._rejectedTrackCutoffByKey[tk]) &&
                    Number(entry.timestamp) >= Number(this._rejectedTrackCutoffByKey[tk]) - 1e-6) continue;
                keptContours.push(contours[i]); keptTrackIds.push(tid);
            }
            entry.prepared_contours    = keptContours;
            entry.component_track_ids  = keptTrackIds;
        };

        VideoAnnotator.prototype._findCachedEntryByFrameRegion = function (frameKey, regionId, objectId) {
            var regionGroup = this._regionGroupKey(regionId, objectId);
            for (var i = this._maskFrameCache.length - 1; i >= 0; i--) {
                var entry = this._maskFrameCache[i];
                if (entry.frame_key !== frameKey) continue;
                if (this._regionGroupKey(entry) !== regionGroup) continue;
                return entry;
            }
            return null;
        };

        VideoAnnotator.prototype._entriesForFrameRegion = function (frameKey, regionId) {
            var rid = String(regionId || '');
            return (this._maskFrameCache || []).filter(function (entry) {
                return String(entry.frame_key || '') === String(frameKey || '') &&
                    String(entry.region_id || '') === rid;
            }).sort(function (a, b) {
                return Number(a.cache_seq || 0) - Number(b.cache_seq || 0);
            });
        };

        VideoAnnotator.prototype._forgetAutoShapeMeta = function (shapeId, providedMeta) {
            var sid = String(shapeId || '');
            if (!sid) return;
            var meta = providedMeta || this._autoShapeMetaById[sid] || null;
            delete this._autoShapeMetaById[sid];
            if (!meta) return;

            if (meta.scope_key) {
                var scopeList = this._autoShapeIdsByScope[meta.scope_key] || [];
                scopeList = scopeList.filter(function (id) { return String(id) !== sid; });
                if (scopeList.length) this._autoShapeIdsByScope[meta.scope_key] = scopeList;
                else delete this._autoShapeIdsByScope[meta.scope_key];
            }
            if (meta.track_key) {
                var trackList = this._autoShapeIdsByTrack[meta.track_key] || [];
                trackList = trackList.filter(function (id) { return String(id) !== sid; });
                if (trackList.length) this._autoShapeIdsByTrack[meta.track_key] = trackList;
                else delete this._autoShapeIdsByTrack[meta.track_key];
            }
            if (meta.entry_component_key && String(this._autoShapeIdByEntryComponent[meta.entry_component_key] || '') === sid) {
                delete this._autoShapeIdByEntryComponent[meta.entry_component_key];
            }
        };

        VideoAnnotator.prototype._removeShapesForFrameRegion = function (frameKey, regionId, includeManual) {
            var matching = [];
            var metaById = {};
            for (var i = 0; i < this.shapes.length; i++) {
                var shape = this.shapes[i];
                if (!shape) continue;
                if (this._frameKey(shape.frameTime || 0) !== String(frameKey || '')) continue;
                if (String(shape.regionId || '') !== String(regionId || '')) continue;
                var sid = String(shape.id || '');
                var meta = shape._autoMaskMeta || this._autoShapeMetaById[sid] || null;
                var isDbAutoMask = !meta && shape._isDbAutoMask;
                if (!includeManual && !meta && !isDbAutoMask) continue;
                matching.push(sid);
                metaById[sid] = meta || {};
            }
            if (!matching.length) return;

            var previous = this._suppressAutoMaskDeletion;
            this._suppressAutoMaskDeletion = true;
            try {
                for (var mi = 0; mi < matching.length; mi++) {
                    this._deleteShape(matching[mi]);
                    this._forgetAutoShapeMeta(matching[mi], metaById[matching[mi]]);
                }
            } finally {
                this._suppressAutoMaskDeletion = previous;
            }
        };

        VideoAnnotator.prototype._clearPriorCanonicalStateForFrameRegion = function (frameKey, regionId, maxCacheSeq, includeManualShapes) {
            var rid = String(regionId || '');
            var maxSeq = Number(maxCacheSeq);
            this._maskFrameCache = (this._maskFrameCache || []).filter(function (entry) {
                if (String(entry.frame_key || '') !== String(frameKey || '')) return true;
                if (String(entry.region_id || '') !== rid) return true;
                if (!isFinite(maxSeq)) return false;
                return Number(entry.cache_seq || 0) > maxSeq;
            });
            this._removeShapesForFrameRegion(frameKey, regionId, !!includeManualShapes);
            this._lastRenderedMaskKey = null;
        };

        VideoAnnotator.prototype._removeAutoShapeById = function (shapeId) {
            var sid = String(shapeId || '');
            if (!sid) return;
            var exists = this.shapes.some(function (s) { return String(s.id) === sid; });
            if (exists) this._deleteShape(sid);
            else this._forgetAutoShapeMeta(sid, this._autoShapeMetaById[sid] || null);
        };

        VideoAnnotator.prototype._removeAutoShapesForScope = function (scopeKey) {
            var ids = (this._autoShapeIdsByScope[scopeKey] || []).slice();
            delete this._autoShapeIdsByScope[scopeKey];
            for (var i = 0; i < ids.length; i++) this._removeAutoShapeById(ids[i]);
        };

        VideoAnnotator.prototype._removeAutoShapesForTrackFromTimestamp = function (trackKey, cutoffTs) {
            var ids = (this._autoShapeIdsByTrack[trackKey] || []).slice();
            for (var i = 0; i < ids.length; i++) {
                var sid = String(ids[i]);
                var meta = this._autoShapeMetaById[sid] || null;
                if (!meta) continue;
                if (Number(meta.timestamp || 0) + 1e-6 < Number(cutoffTs)) continue;
                this._removeAutoShapeById(sid);
            }
        };

        VideoAnnotator.prototype._clearMaskRegionWindow = function (regionId, startTs, endTs) {
            var rid = String(regionId || '');
            var scopeKeys = {};
            this._maskFrameCache = (this._maskFrameCache || []).filter(function (entry) {
                if (String(entry.region_id || '') !== rid) return true;
                var ts = Number(entry.timestamp || 0);
                if (ts + 1e-6 < Number(startTs) || ts - 1e-6 > Number(endTs)) return true;
                scopeKeys[this._autoScopeKey(entry.frame_key, this._regionGroupKey(entry))] = true;
                return false;
            }, this);
            Object.keys(scopeKeys).forEach(function (scopeKey) {
                this._removeAutoShapesForScope(scopeKey);
            }, this);
            this._lastRenderedMaskKey = null;
        };

        VideoAnnotator.prototype._syncAutoAcceptedShapesForEntry = function (entry) {
            if (!entry || !entry.frame_key || entry.region_id == null) return;
            var region = this._resolveMaskRegion(entry, entry.region_id);
            if (!region) return;

            var regionId = String(entry.region_id);
            var regionEntries = this._entriesForFrameRegion(entry.frame_key, regionId);
            this._removeShapesForFrameRegion(entry.frame_key, regionId);

            this._batchingShapes = true;
            for (var ei = 0; ei < regionEntries.length; ei++) {
                var currentEntry = regionEntries[ei];
                var contours = Array.isArray(currentEntry.prepared_contours) ? currentEntry.prepared_contours : [];
                var trackIds = Array.isArray(currentEntry.component_track_ids) ? currentEntry.component_track_ids : [];
                var maskW = Number(currentEntry.prepared_mask_w);
                var maskH = Number(currentEntry.prepared_mask_h);
                if (!contours.length || !trackIds.length || !maskW || !maskH) continue;

                var regionGroup = this._regionGroupKey(currentEntry);
                var scopeKey = this._autoScopeKey(currentEntry.frame_key, regionGroup);
                var frameTime = isFinite(currentEntry.timestamp)
                    ? Number(currentEntry.timestamp)
                    : Number(this._currentVideoTime());
                if (!isFinite(frameTime) || frameTime < 0) frameTime = 0;
                var promptPoints = this._normalizePromptPoints(currentEntry.prompt_points);
                if (!promptPoints.length) {
                    promptPoints = this._collectPromptPointsForFrameRegion(currentEntry.frame_key, currentEntry.region_id);
                }
                var videoW = this.videoEl.videoWidth || this.stage.width();
                var videoH = this.videoEl.videoHeight || this.stage.height();
                var scaleX = videoW / maskW;
                var scaleY = videoH / maskH;

                for (var ci = 0; ci < contours.length; ci++) {
                    var contour = contours[ci];
                    if (!contour || contour.length < 6) continue;
                    var trackId = trackIds[ci];
                    if (trackId == null) continue;

                    var scaledPoints = U.rdpSimplify(contour.map(function (v, i) {
                        return i % 2 === 0 ? v * scaleX : v * scaleY;
                    }), 6.0);
                    if (scaledPoints.length < 6) continue;

                    var konvaNode = new Konva.Line({
                        points: scaledPoints,
                        fill: region.color + '55',
                        stroke: region.color,
                        strokeWidth: 2,
                        closed: true,
                        listening: false,
                    });
                    region.layer.add(konvaNode);
                    var shape = this._registerShape('polygon', konvaNode, {
                        regionId: region.id,
                        frameTime: frameTime,
                        promptPoints: promptPoints,
                    });
                    if (!shape) continue;

                    var trackKey = String(regionGroup || '') + '::' + String(trackId || '');
                    var entryComponentKey = String(currentEntry.cache_seq) + '::' + String(ci);
                    var meta = {
                        scope_key: scopeKey,
                        track_key: trackKey,
                        entry_component_key: entryComponentKey,
                        timestamp: Number(currentEntry.timestamp || 0),
                        frame_key: currentEntry.frame_key,
                        region_group: regionGroup,
                        track_id: Number(trackId),
                        cache_seq: Number(currentEntry.cache_seq),
                        component_index: ci,
                    };
                    shape._autoMaskMeta = meta;
                    this._autoShapeMetaById[String(shape.id)] = meta;
                    if (!this._autoShapeIdsByScope[scopeKey]) this._autoShapeIdsByScope[scopeKey] = [];
                    this._autoShapeIdsByScope[scopeKey].push(shape.id);
                    if (!this._autoShapeIdsByTrack[trackKey]) this._autoShapeIdsByTrack[trackKey] = [];
                    this._autoShapeIdsByTrack[trackKey].push(shape.id);
                    this._autoShapeIdByEntryComponent[entryComponentKey] = shape.id;
                }
            }
            this._batchingShapes = false;
            region.layer.draw();
            this._updateShapeVisibility();
            this._renderShapesList();

            var acceptedEntries = this._entriesForFrameRegion(entry.frame_key, regionId);
            acceptedEntries.forEach(function (e) { e._accepted = true; });
        };

        VideoAnnotator.prototype._removeAutoMaskComponent = function (meta) {
            if (!meta) return;
            var targetCacheSeq = Number(meta.cache_seq);
            if (!isFinite(targetCacheSeq)) return;
            var targetTrackId = Number(meta.track_id);
            var hasTrackId = isFinite(targetTrackId);
            var targetIndex = Number(meta.component_index);
            var hasIndex = isFinite(targetIndex);
            var targetFrameKey = meta.frame_key != null ? String(meta.frame_key) : null;
            var removedAny = false;

            this._maskFrameCache = (this._maskFrameCache || []).filter(function (entry) {
                if (Number(entry.cache_seq) !== targetCacheSeq) return true;
                if (targetFrameKey !== null && String(entry.frame_key || '') !== targetFrameKey) return true;
                var contours = Array.isArray(entry.prepared_contours) ? entry.prepared_contours : [];
                var trackIds = Array.isArray(entry.component_track_ids) ? entry.component_track_ids : [];
                if (!contours.length || !trackIds.length) return true;
                var keptContours = [];
                var keptTrackIds = [];
                for (var i = 0; i < contours.length; i++) {
                    var match = hasTrackId ? Number(trackIds[i]) === targetTrackId : (hasIndex && i === targetIndex);
                    if (match) { removedAny = true; continue; }
                    keptContours.push(contours[i]);
                    keptTrackIds.push(trackIds[i]);
                }
                entry.prepared_contours = keptContours;
                entry.component_track_ids = keptTrackIds;
                return keptContours.length > 0;
            });
            if (removedAny) {
                this._lastRenderedMaskKey = null;
                this._syncMaskToCurrentVideoTime();
            }
        };

        VideoAnnotator.prototype._removeTrackFromMaskCache = function (regionGroupKey, trackId, cutoffTs) {
            var keepEntries = [];
            for (var i = 0; i < this._maskFrameCache.length; i++) {
                var entry = this._maskFrameCache[i];
                if (this._regionGroupKey(entry) !== regionGroupKey || Number(entry.timestamp || 0) + 1e-6 < Number(cutoffTs)) {
                    keepEntries.push(entry); continue;
                }
                var contours = Array.isArray(entry.prepared_contours) ? entry.prepared_contours : [];
                var trackIds = Array.isArray(entry.component_track_ids) ? entry.component_track_ids : [];
                if (!contours.length || !trackIds.length) continue;
                var keptContours = [], keptTrackIds = [];
                for (var ci = 0; ci < contours.length; ci++) {
                    if (Number(trackIds[ci]) === Number(trackId)) continue;
                    keptContours.push(contours[ci]); keptTrackIds.push(trackIds[ci]);
                }
                entry.prepared_contours = keptContours;
                entry.component_track_ids = keptTrackIds;
                if (keptContours.length) keepEntries.push(entry);
                else this._removeAutoShapesForScope(this._autoScopeKey(entry.frame_key, regionGroupKey));
            }
            this._maskFrameCache = keepEntries;
            this._lastRenderedMaskKey = null;
        };

        VideoAnnotator.prototype._isTrackRejected = function (regionGroupKey, trackId, timestamp) {
            if (trackId == null) return false;
            var trackKey = this._trackStorageKey(regionGroupKey, trackId);
            var cutoff   = this._rejectedTrackCutoffByKey[trackKey];
            if (!isFinite(cutoff)) return false;
            return Number(timestamp) >= Number(cutoff) - 1e-6;
        };

        VideoAnnotator.prototype._clearRejectedTracksForRegion = function (regionId) {
            var rid = String(regionId || '');
            var self = this;
            Object.keys(this._rejectedTrackCutoffByKey).forEach(function (key) {
                if (key.indexOf(rid + '::') === 0) delete self._rejectedTrackCutoffByKey[key];
            });
        };

        VideoAnnotator.prototype._clearAllMaskFrames = function () {
            var scopeKeys = Object.keys(this._autoShapeIdsByScope || {});
            for (var i = 0; i < scopeKeys.length; i++) this._removeAutoShapesForScope(scopeKeys[i]);
            this._maskFrameCache = [];
            this._currentMaskFrames = [];
            this._currentMaskFrame = null;
            this._lastRenderedMaskKey = null;
            this._autoMaskTrackStateByGroup = {};
            this._autoShapeMetaById = {};
            this._autoShapeIdsByScope = {};
            this._autoShapeIdsByTrack = {};
            this._autoShapeIdByEntryComponent = {};
            this._rejectedTrackCutoffByKey = {};
        };

        VideoAnnotator.prototype._registerPendingUpdateScopes = function (jobId, scopes) {
            if (!Array.isArray(scopes) || !scopes.length) return null;
            var group = { job_id: jobId || null, scopes: scopes, completed: false };
            if (jobId) this._pendingUpdateScopesByJob[String(jobId)] = group;
            this._pendingUpdateScopesFIFO.push(group);
            return group;
        };

        VideoAnnotator.prototype._cancelOverlappingPendingScopes = function (scopes) {
            if (!Array.isArray(scopes) || !scopes.length) return;

            function overlaps(scope, nextScope) {
                if (!scope || !nextScope) return false;
                if (String(scope.region_id || '') !== String(nextScope.region_id || '')) return false;
                if (Number(scope.end_ts || 0) + 1e-6 < Number(nextScope.start_ts || 0)) return false;
                if (Number(scope.start_ts || 0) - 1e-6 > Number(nextScope.end_ts || 0)) return false;
                return true;
            }

            function overlapsAny(scope) {
                for (var i = 0; i < scopes.length; i++) {
                    if (overlaps(scope, scopes[i])) return true;
                }
                return false;
            }

            Object.keys(this._pendingUpdateScopesByJob || {}).forEach(function (jobKey) {
                var group = this._pendingUpdateScopesByJob[jobKey];
                if (!group || !Array.isArray(group.scopes)) return;
                group.scopes = group.scopes.filter(function (scope) {
                    if (!overlapsAny(scope)) return true;
                    this._clearMaskRegionWindow(
                        scope.region_id,
                        scope.start_ts || 0,
                        scope.end_ts || scope.start_ts || 0
                    );
                    scope.completed = true;
                    return false;
                }, this);
                if (!group.scopes.length) {
                    group.completed = true;
                    delete this._pendingUpdateScopesByJob[jobKey];
                }
            }, this);

            this._pendingUpdateScopesFIFO = (this._pendingUpdateScopesFIFO || []).filter(function (group) {
                if (!group || !Array.isArray(group.scopes)) return false;
                group.scopes = group.scopes.filter(function (scope) {
                    if (!overlapsAny(scope)) return true;
                    this._clearMaskRegionWindow(
                        scope.region_id,
                        scope.start_ts || 0,
                        scope.end_ts || scope.start_ts || 0
                    );
                    scope.completed = true;
                    return false;
                }, this);
                if (!group.scopes.length) {
                    group.completed = true;
                    return false;
                }
                return true;
            }, this);
        };

        VideoAnnotator.prototype._cancelScopeGroup = function (scopeGroup) {
            if (!scopeGroup) return;
            scopeGroup.completed = true;
            this._pendingUpdateScopesFIFO = (this._pendingUpdateScopesFIFO || []).filter(function (g) {
                return g !== scopeGroup;
            });
            if (scopeGroup.job_id) delete this._pendingUpdateScopesByJob[String(scopeGroup.job_id)];
        };

        VideoAnnotator.prototype._findPendingScopeForEntry = function (entry) {
            if (!entry) return null;
            var regionId = String(entry.region_id || '');
            var ts = Number(entry.timestamp || 0);
            var jobId = entry.job_id != null ? String(entry.job_id) : null;
            if (jobId && this._pendingUpdateScopesByJob[jobId]) {
                var exactGroup = this._pendingUpdateScopesByJob[jobId];
                if (exactGroup && !exactGroup.completed && Array.isArray(exactGroup.scopes)) {
                    for (var esi = exactGroup.scopes.length - 1; esi >= 0; esi--) {
                        var exactScope = exactGroup.scopes[esi];
                        if (!exactScope || exactScope.completed) continue;
                        if (String(exactScope.region_id || '') !== regionId) continue;
                        if (ts + 1e-6 < Number(exactScope.start_ts || 0)) continue;
                        if (ts - 1e-6 > Number(exactScope.end_ts || 0)) continue;
                        return exactScope;
                    }
                }
            }
            for (var gi = this._pendingUpdateScopesFIFO.length - 1; gi >= 0; gi--) {
                var group = this._pendingUpdateScopesFIFO[gi];
                if (!group || group.completed || !Array.isArray(group.scopes)) continue;
                for (var si = group.scopes.length - 1; si >= 0; si--) {
                    var scope = group.scopes[si];
                    if (!scope || scope.completed) continue;
                    if (String(scope.region_id || '') !== regionId) continue;
                    if (ts + 1e-6 < Number(scope.start_ts || 0)) continue;
                    if (ts - 1e-6 > Number(scope.end_ts || 0)) continue;
                    return scope;
                }
            }
            return null;
        };

        VideoAnnotator.prototype._markPendingScopeFrame = function (entry) {
            if (!entry) return;
            var regionId = String(entry.region_id || '');
            var ts = Number(entry.timestamp || 0);
            var jobId = entry.job_id != null ? String(entry.job_id) : null;
            if (jobId && this._pendingUpdateScopesByJob[jobId]) {
                var exactGroup = this._pendingUpdateScopesByJob[jobId];
                if (exactGroup && !exactGroup.completed && Array.isArray(exactGroup.scopes)) {
                    for (var esi = exactGroup.scopes.length - 1; esi >= 0; esi--) {
                        var exactScope = exactGroup.scopes[esi];
                        if (!exactScope || exactScope.completed) continue;
                        if (String(exactScope.region_id || '') !== regionId) continue;
                        if (ts + 1e-6 < Number(exactScope.start_ts || 0)) continue;
                        if (ts - 1e-6 > Number(exactScope.end_ts || 0)) continue;
                        exactScope.touched[entry.frame_key] = true;
                        exactScope.latest_cache_seq = Math.max(
                            Number(exactScope.latest_cache_seq || 0), Number(entry.cache_seq || 0));
                        return;
                    }
                }
            }
            for (var gi = this._pendingUpdateScopesFIFO.length - 1; gi >= 0; gi--) {
                var group = this._pendingUpdateScopesFIFO[gi];
                if (!group || group.completed || !Array.isArray(group.scopes)) continue;
                for (var si = group.scopes.length - 1; si >= 0; si--) {
                    var scope = group.scopes[si];
                    if (!scope || scope.completed) continue;
                    if (String(scope.region_id || '') !== regionId) continue;
                    if (ts + 1e-6 < Number(scope.start_ts) || ts - 1e-6 > Number(scope.end_ts)) continue;
                    scope.touched[entry.frame_key] = true;
                    scope.latest_cache_seq = Math.max(
                        Number(scope.latest_cache_seq || 0), Number(entry.cache_seq || 0));
                    break;
                }
            }
        };

        VideoAnnotator.prototype._finalizePendingUpdateJob = function (jobId) {
            var group = null;
            if (jobId) {
                group = this._pendingUpdateScopesByJob[String(jobId)] || null;
                delete this._pendingUpdateScopesByJob[String(jobId)];
            }
            if (!group) {
                while (this._pendingUpdateScopesFIFO.length) {
                    var candidate = this._pendingUpdateScopesFIFO.shift();
                    if (!candidate || candidate.completed) continue;
                    group = candidate;
                    if (candidate.job_id) delete this._pendingUpdateScopesByJob[String(candidate.job_id)];
                    break;
                }
            } else {
                var self2 = this;
                this._pendingUpdateScopesFIFO = this._pendingUpdateScopesFIFO.filter(function (g) {
                    return g !== group;
                });
            }
            if (!group || !Array.isArray(group.scopes)) return;

            group.completed = true;
            for (var si = 0; si < group.scopes.length; si++) {
                var scope = group.scopes[si];
                if (!scope || scope.completed) continue;
                scope.completed = true;
                var touched   = scope.touched  || {};
                var regionId2 = String(scope.region_id || '');
                var startTs   = Number(scope.start_ts  || 0);
                var endTs     = Number(scope.end_ts    || startTs);
                this._discardMaskFrames(function (e) {
                    if (String(e.region_id || '') !== regionId2) return false;
                    var ets = Number(e.timestamp || 0);
                    if (ets + 1e-6 < startTs || ets - 1e-6 > endTs) return false;
                    return !touched[e.frame_key];
                });
            }
            this._syncMaskToCurrentVideoTime();
        };

        VideoAnnotator.prototype._cancelPendingScopesForRegion = function (regionId, cutoffTs) {
            var rid = String(regionId || '');
            var cutoff = Number(cutoffTs || 0);
            Object.keys(this._pendingUpdateScopesByJob || {}).forEach(function (jobKey) {
                var group = this._pendingUpdateScopesByJob[jobKey];
                if (!group || !Array.isArray(group.scopes)) return;
                group.scopes = group.scopes.filter(function (scope) {
                    if (String(scope.region_id || '') !== rid) return true;
                    return Number(scope.end_ts || 0) < cutoff;
                });
                if (!group.scopes.length) {
                    group.completed = true;
                    delete this._pendingUpdateScopesByJob[jobKey];
                }
            }, this);
            this._pendingUpdateScopesFIFO = (this._pendingUpdateScopesFIFO || []).filter(function (group) {
                if (!group || !Array.isArray(group.scopes)) return false;
                group.scopes = group.scopes.filter(function (scope) {
                    if (String(scope.region_id || '') !== rid) return true;
                    return Number(scope.end_ts || 0) < cutoff;
                });
                if (!group.scopes.length) { group.completed = true; return false; }
                return true;
            });
        };

        VideoAnnotator.prototype._onShapeDeleted = function (shape) {
            if (!shape || !shape.id || this._suppressAutoMaskDeletion) return;

            // Invalidate the prompt-dedup cache for this frame/region so the user
            // can resend the same prompts after deleting the resulting masks.
            if (typeof this._frameKey === 'function' && typeof this._scopeKey === 'function') {
                var fk = this._frameKey(shape.frameTime != null ? shape.frameTime : 0);
                var sk = this._scopeKey(fk, shape.regionId);
                delete this._lastPromptSigByScope[sk];
            }

            var sid = String(shape.id);
            var meta = shape._autoMaskMeta || this._autoShapeMetaById[sid] || null;
            if (!meta) return;
            this._removeAutoMaskComponent(meta);
            this._forgetAutoShapeMeta(sid, meta);
        };

    }; // end magic mixin
})();
