(function () {
    'use strict';

    var U = window.LaparoscopyAnnotatorUtils;

    var MIN_AUTO_MASK_AREA_PIXELS   = 64;
    var MIN_AUTO_MASK_AREA_RATIO    = 0.00005;
    var MIN_AUTO_MASK_RELATIVE_AREA = 0.015;

    /* ===================================================================== */
    /* Private module-level helpers                                             */
    /* ===================================================================== */

    function _autoMaskMinComponentArea(pixelCount, largestSize) {
        var minPixels = Number(window.magicMinMaskAreaPixels);
        if (!isFinite(minPixels) || minPixels < 0) minPixels = MIN_AUTO_MASK_AREA_PIXELS;
        var ratio = Number(window.magicMinMaskAreaRatio);
        if (!isFinite(ratio) || ratio < 0) ratio = MIN_AUTO_MASK_AREA_RATIO;
        var relativeRatio = Number(window.magicMinMaskRelativeAreaRatio);
        if (!isFinite(relativeRatio) || relativeRatio < 0) relativeRatio = MIN_AUTO_MASK_RELATIVE_AREA;

        var minArea = Math.max(minPixels, Math.floor(Number(pixelCount || 0) * ratio));
        if (isFinite(largestSize) && largestSize > 0) {
            minArea = Math.max(minArea, Math.floor(largestSize * relativeRatio));
        }
        return Math.max(1, minArea);
    }

    function _traceComponentOuterContour(componentMap, component, w, h) {
        var compId = component.id;
        var minx = component.minx, maxx = component.maxx;
        var miny = component.miny, maxy = component.maxy;
        var w1 = w + 1;
        var maxEdges = 50000, edgeCount = 0, overflow = false;
        var outgoing = new Map();

        function edgeToken(a, b) { return String(a) + '>' + String(b); }
        function pointId(x, y)   { return y * w1 + x; }
        function directionIndex(fromId, toId) {
            var fy = (fromId / w1) | 0, fx = fromId - fy * w1;
            var ty = (toId   / w1) | 0, tx = toId   - ty * w1;
            if (tx > fx) return 0;
            if (ty > fy) return 1;
            if (tx < fx) return 2;
            return 3;
        }
        function addEdge(x1, y1, x2, y2) {
            if (overflow) return;
            if (++edgeCount > maxEdges) { overflow = true; return; }
            var a = pointId(x1, y1), b = pointId(x2, y2);
            var arr = outgoing.get(a);
            if (!arr) { arr = []; outgoing.set(a, arr); }
            arr.push(b);
        }

        for (var y = miny; y <= maxy; y++) {
            var rowBase = y * w;
            for (var x = minx; x <= maxx; x++) {
                var idx = rowBase + x;
                if (componentMap[idx] !== compId) continue;
                var topSame    = y > 0     && componentMap[idx - w] === compId;
                var bottomSame = y < h - 1 && componentMap[idx + w] === compId;
                var leftSame   = x > 0     && componentMap[idx - 1] === compId;
                var rightSame  = x < w - 1 && componentMap[idx + 1] === compId;
                if (!topSame)    addEdge(x,   y,   x+1, y);
                if (!bottomSame) addEdge(x+1, y+1, x,   y+1);
                if (!leftSame)   addEdge(x,   y+1, x,   y);
                if (!rightSame)  addEdge(x+1, y,   x+1, y+1);
            }
        }

        if (overflow || !outgoing.size) return [];

        var chains = [];
        var visited = new Set();
        outgoing.forEach(function (nexts, startId) {
            if (visited.has(startId)) return;
            var chain = [startId];
            visited.add(startId);
            var current = startId;
            var prevDir = -1;
            for (var step = 0; step < maxEdges; step++) {
                var candidates = outgoing.get(current);
                if (!candidates || !candidates.length) break;
                var next = -1;
                if (candidates.length === 1) {
                    next = candidates[0];
                } else {
                    var bestDir = Infinity;
                    var turnDir = (prevDir + 2) % 4;
                    for (var ci = 0; ci < candidates.length; ci++) {
                        var dir = directionIndex(current, candidates[ci]);
                        var diff = (dir - turnDir + 4) % 4;
                        if (diff < bestDir) { bestDir = diff; next = candidates[ci]; }
                    }
                }
                if (next === startId) break;
                if (visited.has(next)) break;
                chain.push(next);
                visited.add(next);
                prevDir = directionIndex(current, next);
                current = next;
            }
            if (chain.length >= 4) chains.push(chain);
        });

        return chains.map(function (chain) {
            var flat = [];
            chain.forEach(function (pid) {
                var py = (pid / w1) | 0;
                flat.push(pid - py * w1, py);
            });
            return flat;
        });
    }

    function _extractComponentContours(grid, w, h) {
        var n = w * h;
        var componentMap = new Int32Array(n);
        var components = [];
        var nextId = 1;

        for (var start = 0; start < n; start++) {
            if (!grid[start] || componentMap[start]) continue;
            var compId = nextId++;
            var minx = w, maxx = 0, miny = h, maxy = 0;
            var queue = [start];
            componentMap[start] = compId;
            var head = 0;
            while (head < queue.length) {
                var cur  = queue[head++];
                var cy2  = (cur / w) | 0, cx2 = cur - cy2 * w;
                if (cx2 < minx) minx = cx2; if (cx2 > maxx) maxx = cx2;
                if (cy2 < miny) miny = cy2; if (cy2 > maxy) maxy = cy2;
                var neighbors = [];
                if (cx2 > 0)     neighbors.push(cur - 1);
                if (cx2 < w - 1) neighbors.push(cur + 1);
                if (cy2 > 0)     neighbors.push(cur - w);
                if (cy2 < h - 1) neighbors.push(cur + w);
                for (var ni = 0; ni < neighbors.length; ni++) {
                    var nb = neighbors[ni];
                    if (grid[nb] && !componentMap[nb]) {
                        componentMap[nb] = compId;
                        queue.push(nb);
                    }
                }
            }
            var area = queue.length;
            components.push({ id: compId, area: area, minx: minx, maxx: maxx, miny: miny, maxy: maxy });
        }

        if (!components.length) return [];
        components.sort(function (a, b) { return b.area - a.area; });
        var minArea = _autoMaskMinComponentArea(n, components[0].area);
        components = components.filter(function (component) {
            return Number(component.area || 0) >= minArea;
        });
        if (!components.length) return [];
        if (components.length > 18) components = components.slice(0, 18);

        var contours = [];
        components.forEach(function (comp) {
            var chains = _traceComponentOuterContour(componentMap, comp, w, h);
            if (!chains.length) return;
            chains.sort(function (a, b) { return b.length - a.length; });
            var longest = chains[0];
            if (longest.length >= 6) contours.push(longest);
        });

        return contours;
    }

    /* ===================================================================== */
    /* Mask mixin                                                               */
    /* ===================================================================== */

    window.LaparoscopyAnnotatorMixins = window.LaparoscopyAnnotatorMixins || {};

    window.LaparoscopyAnnotatorMixins.mask = function (VideoAnnotator) {

        VideoAnnotator.prototype._discardMaskFrames = function (predicate) {
            this._maskFrameCache = this._maskFrameCache.filter(function (e) {
                return !predicate(e);
            });
            this._lastRenderedMaskKey = null;
        };

        VideoAnnotator.prototype._findMaskFrameBySeq = function (cacheSeq) {
            if (cacheSeq == null) return null;
            for (var i = this._maskFrameCache.length - 1; i >= 0; i--) {
                if (Number(this._maskFrameCache[i].cache_seq) === Number(cacheSeq)) {
                    return this._maskFrameCache[i];
                }
            }
            return null;
        };

        // ── Mask display ───────────────────────────────────────────────────

        VideoAnnotator.prototype._maskOverlaySizeKey = function () {
            var c = this._maskOverlayCanvas;
            return c ? (String(c.width) + 'x' + String(c.height)) : null;
        };

        VideoAnnotator.prototype._maskCacheKey = function (frameOrFrames) {
            var frames = Array.isArray(frameOrFrames) ? frameOrFrames : [frameOrFrames];
            return frames.map(function (e) {
                if (!e) return '';
                var cc = Array.isArray(e.prepared_contours) ? e.prepared_contours.length : 0;
                return String(e.cache_seq) + ':' + String(e.timestamp) + ':' +
                       String(e.region_id || '') + ':' + String(cc);
            }).join('|');
        };

        VideoAnnotator.prototype._pickMaskFrames = function (t) {
            var key = this._frameKey(t);
            var byRegion = {};
            for (var i = this._maskFrameCache.length - 1; i >= 0; i--) {
                var entry = this._maskFrameCache[i];
                if (entry._accepted) continue;
                if (entry.frame_key !== key) continue;
                var regionId = entry.region_id != null ? String(entry.region_id) : '';
                if (byRegion[regionId]) continue;
                if (!entry.mask_b64 || !Array.isArray(entry.mask_shape)) continue;
                if (!this._isMaskFrameRegionVisible(entry)) continue;
                byRegion[regionId] = entry;
            }
            return Object.keys(byRegion).sort().map(function (k) { return byRegion[k]; });
        };

        VideoAnnotator.prototype._isMaskFrameRegionVisible = function (maskFrame) {
            if (!maskFrame || maskFrame.region_id == null) return true;
            var regionId = String(maskFrame.region_id);
            var region = (this.regions || []).find(function (r) {
                return String(r.id) === regionId;
            });
            return region ? region.visible !== false : true;
        };

        VideoAnnotator.prototype._resolveMaskRegion = function (maskFrame, fallbackRegionId) {
            var regionId = maskFrame && maskFrame.region_id ? String(maskFrame.region_id) : null;
            if (!regionId && fallbackRegionId) regionId = String(fallbackRegionId);
            var region = (this.regions || []).find(function (r) {
                return String(r.id) === String(regionId);
            });
            return region || null;
        };

        VideoAnnotator.prototype._ensureMaskOverlay = function () {
            var videoEl = this.videoEl;
            if (!videoEl) return null;

            var dw = videoEl.clientWidth, dh = videoEl.clientHeight;
            var vw = videoEl.videoWidth,  vh = videoEl.videoHeight;
            var contentW, contentH, offsetX, offsetY;

            if (vw > 0 && vh > 0 && dw > 0 && dh > 0) {
                var videoAspect = vw / vh, boxAspect = dw / dh;
                if (videoAspect >= boxAspect) {
                    contentW = dw; contentH = Math.round(dw / videoAspect);
                    offsetX = 0;   offsetY  = Math.round((dh - contentH) / 2);
                } else {
                    contentH = dh; contentW = Math.round(dh * videoAspect);
                    offsetX  = Math.round((dw - contentW) / 2); offsetY = 0;
                }
            } else {
                contentW = dw || 1; contentH = dh || 1; offsetX = 0; offsetY = 0;
            }

            if (!this._maskOverlayCanvas) {
                this._maskOverlayCanvas = document.createElement('canvas');
                this._maskOverlayCanvas.style.cssText =
                    'position:absolute;pointer-events:none;z-index:15;';
                this.wrapEl.appendChild(this._maskOverlayCanvas);
                this._maskOverlayCtx = this._maskOverlayCanvas.getContext('2d');
            }

            var s = this._maskOverlayCanvas.style;
            s.left = offsetX + 'px'; s.top = offsetY + 'px';
            s.width = contentW + 'px'; s.height = contentH + 'px';
            if (this._maskOverlayCanvas.width  !== contentW) this._maskOverlayCanvas.width  = contentW;
            if (this._maskOverlayCanvas.height !== contentH) this._maskOverlayCanvas.height = contentH;

            return { canvas: this._maskOverlayCanvas, ctx: this._maskOverlayCtx };
        };

        VideoAnnotator.prototype._clearMaskOverlay = function () {
            if (this._maskOverlayCtx && this._maskOverlayCanvas) {
                this._maskOverlayCtx.clearRect(
                    0, 0, this._maskOverlayCanvas.width, this._maskOverlayCanvas.height);
            }
            this._lastRenderedMaskKey = null;
            this._lastMaskOverlaySizeKey = null;
            this._currentMaskFrames = [];
            this._currentMaskFrame = null;
            this._maskHoverCacheSeq = null;
            this._maskHoverComponentIndex = null;
            this._updateMagicAcceptButton();
        };

        VideoAnnotator.prototype._drawMaskOverlay = function (frameResults) {
            var frames = Array.isArray(frameResults) ? frameResults
                : (frameResults ? [frameResults] : []);
            if (!frames.length) { this._clearMaskOverlay(); return; }

            var overlay = this._ensureMaskOverlay();
            if (!overlay) return;
            var ctx = overlay.ctx;
            var cw = overlay.canvas.width, ch = overlay.canvas.height;
            ctx.clearRect(0, 0, cw, ch);

            var hoverSeq  = this._maskHoverCacheSeq;
            var hoverComp = this._maskHoverComponentIndex;
            var hasHover  = false;
            if (hoverSeq != null && hoverComp != null) {
                for (var hfi = 0; hfi < frames.length; hfi++) {
                    var hf = frames[hfi];
                    if (!hf || Number(hf.cache_seq) !== Number(hoverSeq)) continue;
                    var hcs = Array.isArray(hf.prepared_contours) ? hf.prepared_contours : [];
                    if (Number(hoverComp) >= 0 && Number(hoverComp) < hcs.length) {
                        hasHover = true; break;
                    }
                }
                if (!hasHover) {
                    this._maskHoverCacheSeq = null;
                    this._maskHoverComponentIndex = null;
                }
            }

            var renderedFrames = [], renderedAny = false;

            for (var fi = 0; fi < frames.length; fi++) {
                var frameResult = frames[fi];
                if (!frameResult || !frameResult.mask_b64 || !Array.isArray(frameResult.mask_shape)) continue;
                if (!this._isMaskFrameRegionVisible(frameResult)) continue;

                var shape  = frameResult.mask_shape;
                var maskH  = Number(frameResult.prepared_mask_h || shape[shape.length - 2]);
                var maskW2 = Number(frameResult.prepared_mask_w || shape[shape.length - 1]);
                if (!isFinite(maskH) || !isFinite(maskW2) || maskH <= 0 || maskW2 <= 0) continue;

                var region = this._resolveMaskRegion(frameResult);
                var regionColor = region ? region.color : '#00dc50';
                var r = 0, g = 220, b = 80;
                if (regionColor && /^#[0-9a-fA-F]{6}$/.test(regionColor)) {
                    r = parseInt(regionColor.slice(1, 3), 16);
                    g = parseInt(regionColor.slice(3, 5), 16);
                    b = parseInt(regionColor.slice(5, 7), 16);
                }

                var contours = Array.isArray(frameResult.prepared_contours)
                    ? frameResult.prepared_contours : [];
                if (!contours.length) {
                    this._prepareMaskContours(frameResult);
                    contours = Array.isArray(frameResult.prepared_contours)
                        ? frameResult.prepared_contours : [];
                }
                renderedFrames.push(frameResult);

                if (contours.length) {
                    ctx.save();
                    ctx.lineJoin = 'round'; ctx.lineCap = 'round';
                    for (var ci = 0; ci < contours.length; ci++) {
                        var contour = contours[ci];
                        if (!contour || contour.length < 6) continue;

                        var isHovered = hasHover &&
                            Number(hoverSeq) === Number(frameResult.cache_seq) &&
                            Number(hoverComp) === Number(ci);

                        var alpha = 0.20 + Math.min(0.12, ci * 0.025);
                        var strokeAlpha = 0.95, strokeWidth = 1.4;
                        if (hasHover && !isHovered) { alpha = 0.08; strokeAlpha = 0.45; strokeWidth = 1.0; }
                        if (isHovered) { alpha = Math.max(0.45, alpha + 0.12); strokeAlpha = 1.0; strokeWidth = 2.8; }

                        ctx.fillStyle   = 'rgba(' + r + ',' + g + ',' + b + ',' + alpha.toFixed(3) + ')';
                        ctx.strokeStyle = isHovered
                            ? 'rgba(255,255,255,' + strokeAlpha.toFixed(2) + ')'
                            : 'rgba(' + r + ',' + g + ',' + b + ',' + strokeAlpha.toFixed(2) + ')';
                        ctx.lineWidth = strokeWidth;

                        ctx.beginPath();
                        ctx.moveTo((contour[0] / maskW2) * cw, (contour[1] / maskH) * ch);
                        for (var pi = 2; pi < contour.length; pi += 2) {
                            ctx.lineTo((contour[pi] / maskW2) * cw, (contour[pi+1] / maskH) * ch);
                        }
                        ctx.closePath(); ctx.fill(); ctx.stroke();
                        renderedAny = true;
                    }
                    ctx.restore();
                    continue;
                }

                var builtGrid = this._buildBinaryMaskGrid(frameResult);
                if (!builtGrid) continue;
                var imageData = new ImageData(maskW2, maskH);
                for (var pi2 = 0; pi2 < builtGrid.pixelCount; pi2++) {
                    if (builtGrid.grid[pi2]) {
                        var di = pi2 * 4;
                        imageData.data[di] = r; imageData.data[di+1] = g;
                        imageData.data[di+2] = b; imageData.data[di+3] = 100;
                        renderedAny = true;
                    }
                }
                var tmp = document.createElement('canvas');
                tmp.width = maskW2; tmp.height = maskH;
                var tmpCtx = tmp.getContext('2d');
                if (tmpCtx) { tmpCtx.putImageData(imageData, 0, 0); ctx.drawImage(tmp, 0, 0, cw, ch); }
            }

            if (!renderedFrames.length || !renderedAny) { this._clearMaskOverlay(); return; }

            this._lastRenderedMaskKey   = this._maskCacheKey(renderedFrames);
            this._lastMaskOverlaySizeKey = this._maskOverlaySizeKey();
            this._currentMaskFrames     = renderedFrames;
            this._currentMaskFrame      = renderedFrames[0] || null;
        };

        VideoAnnotator.prototype._syncMaskToCurrentVideoTime = function () {
            if (!this.annotationMode) { this._clearMaskOverlay(); return; }
            var t = this._currentVideoTime();
            var frames = this._pickMaskFrames(t);
            if (!frames.length) { this._clearMaskOverlay(); return; }
            var key = this._maskCacheKey(frames);
            var sizeKey = this._maskOverlaySizeKey();
            if (key === this._lastRenderedMaskKey && sizeKey && sizeKey === this._lastMaskOverlaySizeKey) {
                this._currentMaskFrames = frames;
                this._currentMaskFrame  = frames[0] || null;
                this._updateMagicAcceptButton();
                return;
            }
            this._drawMaskOverlay(frames);
            this._updateMagicAcceptButton();
        };

        VideoAnnotator.prototype._bindMaskSync = function () {
            if (this._maskSyncBound) return;
            this._maskSyncBound = true;
            var self = this;
            this.videoEl.addEventListener('timeupdate', function () { self._syncMaskToCurrentVideoTime(); });
            this.videoEl.addEventListener('seeked',     function () { self._syncMaskToCurrentVideoTime(); });
            this.videoEl.addEventListener('loadedmetadata', function () {
                self._lastRenderedMaskKey = null;
                // Re-size the Konva stage now that videoWidth/Height are known.
                // _syncStageSize may have been called earlier (in _enterAnnotationMode)
                // before metadata was available, producing wrong layer scales.
                if (typeof self._syncStageSize === 'function') self._syncStageSize();
                self._syncMaskToCurrentVideoTime();
                // Force the browser to decode and display frame 0. At loadedmetadata,
                // readyState is HAVE_METADATA but the first frame may not be rendered
                // yet, causing the mask overlay to appear misaligned with the video.
                // Setting currentTime = 0 triggers seeking → seeked, which forces decode.
                if (self.videoEl.currentTime === 0) self.videoEl.currentTime = 0;
            });
            window.addEventListener('resize', function () { self._syncMaskToCurrentVideoTime(); });
        };

        // ── Binary grid + contours ─────────────────────────────────────────

        VideoAnnotator.prototype._buildBinaryMaskGrid = function (maskFrame) {
            if (!maskFrame || !Array.isArray(maskFrame.mask_shape) || !maskFrame.mask_b64) return null;
            if (String(maskFrame.mask_encoding || '') !== 'bitpack_u1_v1') return null;
            var shape = maskFrame.mask_shape;
            var maskH = Number(shape[shape.length - 2]);
            var maskW = Number(shape[shape.length - 1]);
            if (!maskW || !maskH) return null;
            var bytes = this._decodeB64ToBytes(maskFrame.mask_b64);
            var pixelCount = maskW * maskH;
            if (bytes.length !== Math.ceil(pixelCount / 8)) return null;
            var grid = new Uint8Array(pixelCount);
            for (var pi = 0; pi < pixelCount; pi++) {
                grid[pi] = ((bytes[pi >> 3] >> (pi & 7)) & 1) ? 1 : 0;
            }
            return { grid: grid, maskW: maskW, maskH: maskH, pixelCount: pixelCount };
        };

        VideoAnnotator.prototype._decodeB64ToBytes = function (b64) {
            var raw = atob(b64 || '');
            var out = new Uint8Array(raw.length);
            for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
            return out;
        };

        VideoAnnotator.prototype._encodeBytesToB64 = function (bytes) {
            var binary = '';
            for (var i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
            return btoa(binary);
        };

        VideoAnnotator.prototype._prepareMaskContours = function (maskFrame) {
            var built = this._buildBinaryMaskGrid(maskFrame);
            if (!built) {
                maskFrame.prepared_contours = [];
                maskFrame.prepared_mask_w   = null;
                maskFrame.prepared_mask_h   = null;
                return;
            }
            var rawContours = _extractComponentContours(built.grid, built.maskW, built.maskH);
            var prepared = [];
            for (var ci = 0; ci < rawContours.length; ci++) {
                var contour = rawContours[ci];
                if (!contour || contour.length < 6) continue;
                if (contour.length > 1800 * 2) {
                    var stride = Math.max(1, Math.floor((contour.length / 2) / 1800));
                    var down = [];
                    for (var di = 0; di < contour.length; di += stride * 2) {
                        down.push(contour[di], contour[di+1]);
                    }
                    contour = down;
                }
                var simplified = U.rdpSimplify(contour, 1.5);
                if (simplified.length < 6) simplified = contour;
                if (simplified.length < 6) continue;
                prepared.push(simplified);
            }
            maskFrame.prepared_contours = prepared;
            maskFrame.prepared_mask_w   = built.maskW;
            maskFrame.prepared_mask_h   = built.maskH;
        };

    };
})();
