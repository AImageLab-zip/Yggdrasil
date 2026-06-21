/**
 * laparoscopy_annotator.js
 *
 * Video frame-by-frame annotation tool using Konva.js.
 *
 * ── Zoom / pan ──────────────────────────────────────────────────────────────
 * Zoom is CSS transform (translate + scale) on a shared inner wrapper (wrapEl)
 * that holds both the <video> and the Konva canvas container.  Konva is NOT
 * scaled internally for zoom; only the layer scale (video-px → display-px) is
 * applied to each layer.  _pointerPos() divides Konva's pointer position only
 * by the layer scale — NOT by the zoom factor, which Konva already corrects.
 *
 * ── Event pipeline ──────────────────────────────────────────────────────────
 * Three non-overlapping layers, each with a single responsibility:
 *
 *   Konva stage  mousedown / mousemove / mouseleave
 *                  → start drawing, continue stroke, stop stroke on canvas-exit
 *   outerEl      mousedown / wheel
 *                  → start pan, zoom
 *   window       mousemove / mouseup
 *                  → continue pan, universal stop (drawing + pan)
 *
 * Using stage.on('mouseleave') rather than outerEl mouseleave means the stop
 * fires exactly at the canvas boundary, not the outer container boundary.
 * window mouseup guarantees drawing/pan always terminates even when the mouse
 * button is released outside the browser window.
 *
 * ── Per-frame annotations ───────────────────────────────────────────────────
 * Every shape stores frameTime = video.currentTime at draw time.
 * _updateShapeVisibility() shows/hides shapes based on FRAME_TOLERANCE.
 */
(function () {
    'use strict';

    /* ====================================================================== */
    /* Shared utilities                                                         */
    /* ====================================================================== */

    var U = window.LaparoscopyAnnotatorUtils;

    /* ====================================================================== */
    /* Constructor                                                              */
    /* ====================================================================== */

    /**
     * @param {Object}            cfg
     * @param {HTMLVideoElement}  cfg.videoEl
     * @param {HTMLElement}       cfg.wrapEl        inner zoom container
     * @param {HTMLElement}       cfg.outerEl        fixed outer container (overflow:hidden)
     * @param {HTMLElement}       cfg.toolbarEl
     * @param {HTMLElement}       cfg.regionListEl
     * @param {HTMLElement}       cfg.shapesListEl
     * @param {HTMLButtonElement} cfg.toggleBtn
     * @param {HTMLElement}       cfg.timestampEl
     * @param {HTMLInputElement}  cfg.brushSizeInput
     * @param {HTMLElement}       cfg.brushSizeLabel
     * @param {HTMLElement}       [cfg.polygonHintEl]  shown while drawing a polygon
     * @param {HTMLElement}       [cfg.timelineTrackWrapEl]
     * @param {HTMLElement}       [cfg.timelineTrackEl]
     * @param {HTMLElement}       [cfg.timelineSegmentsLayerEl]
     * @param {HTMLElement}       [cfg.timelinePinsLayerEl]
     * @param {HTMLButtonElement} [cfg.timelinePlayheadEl]
     * @param {HTMLElement}       [cfg.timelineClassListEl]
     * @param {HTMLElement}       [cfg.timelineClassAdminListEl]
     * @param {HTMLElement}       [cfg.timelineCurrentTimeEl]
     * @param {HTMLElement}       [cfg.timelineDurationEl]
     * @param {HTMLElement}       [cfg.timelineActiveClassEl]
     * @param {HTMLButtonElement} [cfg.timelineAddPinBtnEl]
     * @param {HTMLButtonElement} [cfg.timelineAddClassBtnEl]
     */
    function VideoAnnotator(cfg) {
        /* DOM references */
        this.videoEl        = cfg.videoEl;
        this.wrapEl         = cfg.wrapEl;
        this.outerEl        = cfg.outerEl;
        this.toolbarEl      = cfg.toolbarEl;
        this._toolbarPointToolBtnEl = cfg.toolbarPointToolBtnEl ||
            (this.toolbarEl ? this.toolbarEl.querySelector('[data-tool="point"]') : null);
        this.regionListEl   = cfg.regionListEl;
        this.shapesListEl   = cfg.shapesListEl;
        this.toggleBtn      = cfg.toggleBtn;
        this.timestampEl    = cfg.timestampEl;
        this.brushSizeInput = cfg.brushSizeInput;
        this.brushSizeLabel = cfg.brushSizeLabel;
        this.polygonHintEl  = cfg.polygonHintEl || null;

        /* Temporal classification timeline */
        this.timelineTrackWrapEl       = cfg.timelineTrackWrapEl || null;
        this.timelineTrackEl           = cfg.timelineTrackEl || null;
        this.timelineSegmentsLayerEl   = cfg.timelineSegmentsLayerEl || null;
        this.timelinePinsLayerEl       = cfg.timelinePinsLayerEl || null;
        this.timelinePlayheadEl        = cfg.timelinePlayheadEl || null;
        this.timelineClassListEl       = cfg.timelineClassListEl || null;
        this.timelineClassAdminListEl  = cfg.timelineClassAdminListEl || null;
        this.timelineCurrentTimeEl     = cfg.timelineCurrentTimeEl || null;
        this.timelineDurationEl        = cfg.timelineDurationEl || null;
        this.timelineActiveClassEl     = cfg.timelineActiveClassEl || null;
        this.timelineAddPinBtnEl       = cfg.timelineAddPinBtnEl || null;
        this.timelineAddClassBtnEl     = cfg.timelineAddClassBtnEl || null;

        /* Admin / API */
        this.isAdmin   = cfg.isAdmin   || false;
        this.csrfToken = cfg.csrfToken || '';
        this.patientId = cfg.patientId || null;

        /* Magic Toolbox */
        this._magicPanelEl = cfg.magicPanelEl || null;
        this._magicPointToolBtnEl = cfg.magicPointToolBtnEl || null;
        this._magicPointPositiveBtnEl = cfg.magicPointPositiveBtnEl || null;
        this._magicPointNegativeBtnEl = cfg.magicPointNegativeBtnEl || null;
        this._magicPromptsCountEl = cfg.magicPromptsCountEl || null;
        this._magicSendBtnEl = cfg.magicSendBtnEl || document.getElementById('magic-send-prompts-btn');
        this._magicPromptsListEl = cfg.magicPromptsListEl || document.getElementById('magic-prompts-list');
        this._magicWindowInputEl = cfg.magicWindowInputEl || document.getElementById('magic-window-seconds-input');
        this._magicOverlayEl = null;
        this._magicPointActive = false;
        this._magicPointLabel = 1;
        this._lastNonPointTool = 'brush';
        this._magicPrompts = [];
        this._magicStatusEl = null;
        this._maskDecisionLayerEl = null;
        this._ws = null;
        this._wsReconnectTimer = null;
        this._wsReconnectAttempts = 0;
        this._workerConnected = false;
        this._workerWsHost = cfg.workerWsHost || window.workerWsHost || 'zip-dgx.ing.unimore.it';
        this._workerVideoSource = cfg.workerVideoSource || window.workerVideoSourceRef || null;
        this._workerVideoId = cfg.workerVideoId || window.workerVideoId || null;
        if (!this._workerVideoId) {
            var workerFileId = window.workerVideoSourceFileId != null ? String(window.workerVideoSourceFileId) : '';
            this._workerVideoId = workerFileId ? 'lap-' + String(this.patientId) + '-' + workerFileId : 'lap-' + String(this.patientId);
        }
        this._maskOverlayCanvas = null;
        this._maskOverlayCtx = null;
        this._maskFrameCache = [];
        this._maskStoreSeq = 0;
        this._resizeListener = null;
        this._batchingShapes = false;
        this._maskSyncBound = false;
        this._currentMaskFrames = [];
        this._currentMaskFrame = null;
        this._lastRenderedMaskKey = null;
        this._lastMaskOverlaySizeKey = null;
        this._maskHoverCacheSeq = null;
        this._maskHoverComponentIndex = null;
        this._subsampledVideoFps = cfg.subsampledVideoFps || 1;
        this._lastPromptSigByScope = {};
        this._autoMaskTrackStateByGroup = {};
        this._autoShapeMetaById = {};
        this._autoShapeIdsByScope = {};
        this._autoShapeIdsByTrack = {};
        this._autoShapeIdByEntryComponent = {};
        this._suppressAutoMaskDeletion = false;
        this._rejectedTrackCutoffByKey = {};
        this._pendingUpdateScopesFIFO = [];
        this._pendingUpdateScopesByJob = {};
        window.__med = {
            patientId: this.patientId,
            videoSource: this._workerVideoSource,
            videoId: this._workerVideoId,
        };

        /* Tool state */
        this.annotationMode = false;
        this.currentTool    = 'brush';
        this.brushSize      = parseInt(cfg.brushSizeInput.value, 10) || 8;

        /* Regions */
        this.regions          = [];
        this.activeRegionId   = null;
        this.paletteIdx       = 0;
        this._editingRegionId = null;

        /* Shapes */
        this.shapes           = [];
        this._selectedShapeId = null;
        this._filterShapesCurrentFrame = true;

        /* In-progress polygon */
        this._polyPoints = [];
        this._polyLine   = null;
        this._polyGuide  = null;
        this._polyDots   = [];

        /* Polygon vertex handles (editing an existing polygon) */
        this._polyVertexHandles = [];
        this._polyVertexShapeId = null;
        this._draggingVertex    = false;

        /* Freehand drawing */
        this._drawing          = false;
        this._currentLine      = null;

        /* Zoom / pan */
        this._zoom         = 1.0;
        this._panX         = 0;
        this._panY         = 0;
        this._spaceDown    = false;
        this._ctrlDown     = false;
        this._middleMouseDown = false;
        this._isPanning    = false;
        this._panStartX    = 0;
        this._panStartY    = 0;
        this._panStartPanX = 0;
        this._panStartPanY = 0;

        /* Seek coalescing */
        this._seekPending  = null;
        this._seekInFlight = false;

        /* Temporal classification state */
        this.timelineClasses = [];
        this.timelinePins = [];
        this.activeTimelineClassId = null;
        this._editingTimelineClassId = null;
        this._timelinePaletteIdx = 0;
        this._selectedTimelinePinId = null;
        this._timelineDrag = null;
        this._timelinePreviewTime = null;
        this._timelineListeners = {};
        this._timelinePinMenuEl = null;
        this._timelinePinMenuCloser = null;
        this._timelineSyncTimer = null;
        this._timelineSyncInFlight = false;
        this._timelineSyncQueued = false;

        /* Bound event listeners (stored for cleanup) */
        this._L = {};

        /* Konva */
        this.stage       = null;
        this.cursorLayer = null;
        this._cursorCircle = null;

        /* Bootstrap */
        this.wrapEl.style.transformOrigin = '0 0';
        this.wrapEl.style.position        = 'relative';

        this._initKonva();
        this._bindToolbar();
        this._bindFrameNav();
        this._initTemporalClassification();
        this._bindKeyboard();
        this._bindToggle();
        this._addDefaultRegion();
        this._initMagicToolbox();

        var _self = this;
        this.videoEl.addEventListener('timeupdate', function () {
            _self._updateShapeVisibility();
            if (_self._filterShapesCurrentFrame) _self._renderShapesList();
            _self._updateTemporalTimelineUI();
        });
        this.videoEl.addEventListener('seeked', function () {
            _self._updateShapeVisibility();
            if (_self._filterShapesCurrentFrame) _self._renderShapesList();
            _self._updateTemporalTimelineUI();
        });
        this._updateTimestamp();
        this._updateTemporalTimelineUI();

        /* Load persisted types from DB (replaces defaults when API responds) */
        var self = this;
        var regionTypesPromise = this._loadRegionTypes();
        var quadrantTypesPromise = this._loadQuadrantTypes();
        if (regionTypesPromise && typeof regionTypesPromise.then === 'function') {
            regionTypesPromise
                .then(function () { self._loadRegionAnnotations(); })
                .catch(function () { self._loadRegionAnnotations(); });
        } else {
            this._loadRegionAnnotations();
        }

        if (quadrantTypesPromise && typeof quadrantTypesPromise.then === 'function') {
            quadrantTypesPromise
                .then(function () { self._loadTimelineMarkers(); })
                .catch(function () { self._loadTimelineMarkers(); });
        } else {
            this._loadTimelineMarkers();
        }

        window.__laparoscopyAnnotator = this;
    }

    /* ====================================================================== */
    /* Konva initialisation                                                     */
    /* ====================================================================== */

    VideoAnnotator.prototype._initKonva = function () {
        var container = document.createElement('div');
        container.id  = 'annotator-canvas-container';
        container.style.cssText = 'position:absolute;top:0;left:0;pointer-events:none;z-index:10;';
        this.wrapEl.appendChild(container);
        this._container = container;

        this.stage = new Konva.Stage({ container: container, width: 1, height: 1 });
        // One shared layer for all region annotation groups (keeps Konva layer count at 2).
        this._annotationLayer = new Konva.Layer();
        this.stage.add(this._annotationLayer);
        this.cursorLayer = new Konva.Layer();
        this.stage.add(this.cursorLayer);
    };

    VideoAnnotator.prototype._syncStageSize = function () {
        var dw = this.videoEl.clientWidth;
        var dh = this.videoEl.clientHeight;
        var vw = this.videoEl.videoWidth;
        var vh = this.videoEl.videoHeight;

        // Compute the actual video content rect (object-fit:contain letterboxing)
        var contentW, contentH, offsetX, offsetY;
        if (vw > 0 && vh > 0 && dw > 0 && dh > 0) {
            var videoAspect = vw / vh;
            var boxAspect   = dw / dh;
            if (videoAspect >= boxAspect) {
                contentW = dw;
                contentH = Math.round(dw / videoAspect);
                offsetX  = 0;
                offsetY  = Math.round((dh - contentH) / 2);
            } else {
                contentH = dh;
                contentW = Math.round(dh * videoAspect);
                offsetX  = Math.round((dw - contentW) / 2);
                offsetY  = 0;
            }
        } else {
            contentW = dw || 1;  contentH = dh || 1;
            offsetX  = 0;        offsetY  = 0;
        }

        this._videoContentW       = contentW;
        this._videoContentH       = contentH;
        this._videoContentOffsetX = offsetX;
        this._videoContentOffsetY = offsetY;

        this._container.style.left   = offsetX  + 'px';
        this._container.style.top    = offsetY  + 'px';
        this._container.style.width  = contentW + 'px';
        this._container.style.height = contentH + 'px';

        var scaleX = contentW / (vw || contentW);
        var scaleY = contentH / (vh || contentH);

        this.stage.width(contentW);
        this.stage.height(contentH);

        // Scale the single shared annotation layer; region groups inherit it automatically.
        this._annotationLayer.scaleX(scaleX);
        this._annotationLayer.scaleY(scaleY);
        this.cursorLayer.scaleX(scaleX);
        this.cursorLayer.scaleY(scaleY);
        this.stage.draw();
        this._resetZoom();
    };

    /* ====================================================================== */
    /* Zoom & pan (CSS-transform based)                                         */
    /* ====================================================================== */

    VideoAnnotator.prototype._applyZoom = function (delta, cx, cy) {
        var oldZoom = this._zoom;
        this._zoom  = Math.min(8, Math.max(1.0, oldZoom * delta));
        var factor  = this._zoom / oldZoom;
        if (cx !== undefined) {
            this._panX = cx - (cx - this._panX) * factor;
            this._panY = cy - (cy - this._panY) * factor;
        }
        this._applyTransform();
    };

    VideoAnnotator.prototype._resetZoom = function () {
        this._zoom = 1.0;
        this._panX = 0;
        this._panY = 0;
        this._applyTransform();
    };

    VideoAnnotator.prototype._applyTransform = function () {
        this.wrapEl.style.transform =
            'translate(' + this._panX + 'px,' + this._panY + 'px) scale(' + this._zoom + ')';
    };

    /* ====================================================================== */
    /* Coordinate conversion                                                    */
    /* ====================================================================== */

    /**
     * Convert Konva raw pointer → video-pixel drawing coordinates.
     * Konva's getPointerPosition() already corrects for CSS zoom (via
     * getBoundingClientRect), so we only divide by the layer scale here.
     */
    VideoAnnotator.prototype._pointerPos = function () {
        var raw = this.stage.getPointerPosition();
        if (!raw) return null;
        var sx = this.cursorLayer.scaleX() || 1;
        var sy = this.cursorLayer.scaleY() || 1;
        return { x: raw.x / sx, y: raw.y / sy };
    };

    /* ====================================================================== */
    /* Region management                                                        */
    /* ====================================================================== */

    VideoAnnotator.prototype._addDefaultRegion = function () {
        this.addRegion('Region 1');
    };

    VideoAnnotator.prototype.addRegion = function (name, color, dbId) {
        var actualColor;
        if (color) {
            actualColor = color;
        } else {
            actualColor = U.PALETTE[this.paletteIdx % U.PALETTE.length];
            this.paletteIdx++;
        }

        // Use a Group instead of a Layer so the stage only ever has 2 layers
        // (annotationLayer + cursorLayer).  Group.draw() is monkey-patched to
        // flush the parent layer so all existing region.layer.draw() call sites
        // continue to work without changes.
        var layer = new Konva.Group();
        this._annotationLayer.add(layer);
        layer.draw = function () {
            var parentLayer = this.getLayer();
            if (parentLayer) parentLayer.batchDraw();
            return this;
        };
        layer.batchDraw = layer.draw;

        var id = 'region-' + Date.now() + '-' + Math.random().toString(36).slice(2);
        var region = { id: id, dbId: dbId || null, name: name, color: actualColor, visible: true, layer: layer };
        this.regions.push(region);
        if (!this.activeRegionId) this.activeRegionId = id;

        this._renderRegionList();
        return region;
    };

    VideoAnnotator.prototype._activeRegion = function () {
        var id = this.activeRegionId;
        return this.regions.find(function (r) { return r.id === id; }) || null;
    };

    VideoAnnotator.prototype._startRegionEdit = function (regionId) {
        if (!this.isAdmin) return;
        this.activeRegionId   = regionId;
        this._editingRegionId = regionId;
        this._renderRegionList();
    };

    VideoAnnotator.prototype._commitRegionEdit = function (regionId, nextValue) {
        if (!this.isAdmin) return;
        var region = this.regions.find(function (r) { return r.id === regionId; });
        if (!region) return;
        var trimmed = (nextValue || '').trim();
        if (trimmed) region.name = trimmed;
        this._editingRegionId = null;
        this._renderRegionList();
        this._renderShapesList();
        if (this.isAdmin && region.dbId && trimmed) {
            this._requestVoid('/laparoscopy/api/region-types/' + region.dbId + '/', {
                method: 'PATCH',
                headers: this._jsonHeaders(),
                body: JSON.stringify({ name: trimmed }),
            });
        }
    };

    VideoAnnotator.prototype._cancelRegionEdit = function () {
        this._editingRegionId = null;
        this._renderRegionList();
    };

    VideoAnnotator.prototype._applyRegionStyleToShape = function (shape, region) {
        if (!shape || !region || !shape.konvaNode) return;

        if (shape.type === 'polygon') {
            shape.konvaNode.stroke(region.color);
            shape.konvaNode.fill(region.color + '55');
            return;
        }

        if (shape.type === 'eraser') {
            shape.konvaNode.stroke('rgba(0,0,0,1)');
            shape.konvaNode.globalCompositeOperation('destination-out');
            return;
        }

        shape.konvaNode.stroke(region.color);
    };

    VideoAnnotator.prototype._changeRegionColor = function (regionId, newColor) {
        var self = this;
        var region = this.regions.find(function (r) { return r.id === regionId; });
        if (!region) return;
        region.color = newColor;

        this.shapes.forEach(function (shape) {
            if (shape.regionId !== regionId) return;
            self._applyRegionStyleToShape(shape, region);
        });

        region.layer.draw();
        this._syncSelectedPolygonHandles();
        this._renderRegionList();
        this._renderShapesList();
        if (region.dbId) {
            this._requestVoid('/laparoscopy/api/region-types/' + region.dbId + '/', {
                method: 'PATCH',
                headers: this._jsonHeaders(),
                body: JSON.stringify({ color: newColor }),
            });
        }
    };

    VideoAnnotator.prototype._renderRegionList = function () {
        if (!this.regionListEl) return;
        var self = this;
        this.regionListEl.innerHTML = '';

        this.regions.forEach(function (r) {
            var li = document.createElement('li');
            li.className   = 'list-group-item py-1 px-2';
            li.style.cssText = 'cursor:pointer;flex:1 1 calc(50% - 0.25rem);min-width:0;' +
                'border-left:4px solid ' + (r.id === self.activeRegionId ? r.color : 'transparent') + ';' +
                (r.id === self.activeRegionId ? 'background:rgba(255,193,7,0.12);' : '');
            li.addEventListener('click', function () {
                if (self._editingRegionId === r.id) return;
                self.activeRegionId = r.id;
                self._renderRegionList();
            });

            var row = document.createElement('div');
            row.className = 'd-flex align-items-center gap-1';
            li.appendChild(row);

            /* colour dot — clickable to edit */
            var dot = document.createElement('span');
            dot.style.cssText = 'display:inline-block;width:11px;height:11px;border-radius:50%;flex-shrink:0;background:' + r.color + ';cursor:pointer;';
            dot.title = 'Click to change color';
            dot.addEventListener('click', function (e) {
                e.stopPropagation();
                U.openColorPicker(r.color, function (nextColor) {
                    self._changeRegionColor(r.id, nextColor);
                });
            });
            row.appendChild(dot);

            /* name or inline edit */
            var nameWrap = document.createElement('div');
            nameWrap.className = 'flex-grow-1';
            nameWrap.style.minWidth = '0';
            row.appendChild(nameWrap);

            if (self.isAdmin && self._editingRegionId === r.id) {
                var nameInput = document.createElement('input');
                nameInput.type      = 'text';
                nameInput.value     = r.name;
                nameInput.className = 'form-control form-control-sm';
                nameInput.style.cssText = 'padding:0.1rem 0.35rem;';
                nameInput.setAttribute('data-region-edit', r.id);
                nameInput.addEventListener('click', function (e) { e.stopPropagation(); });
                nameInput.addEventListener('keydown', function (e) {
                    if (e.key === 'Enter')  { e.preventDefault(); self._commitRegionEdit(r.id, this.value); }
                    if (e.key === 'Escape') { e.preventDefault(); self._cancelRegionEdit(); }
                });
                nameWrap.appendChild(nameInput);
            } else {
                var nameLabel = document.createElement('span');
                nameLabel.className   = 'small fw-semibold d-block text-truncate';
                nameLabel.style.lineHeight = '1.2';
                nameLabel.textContent = r.name;
                nameWrap.appendChild(nameLabel);
            }

            /* action buttons */
            var actions = document.createElement('div');
            actions.className = 'd-flex align-items-center gap-1 flex-shrink-0';
            row.appendChild(actions);

            var btnCss = 'padding:0.1rem 0.3rem;';

            if (self.isAdmin) {
                if (self._editingRegionId === r.id) {
                    var saveBtn = document.createElement('button');
                    saveBtn.className = 'btn btn-sm btn-outline-success';
                    saveBtn.style.cssText = btnCss;
                    saveBtn.innerHTML = '<i class="fas fa-check"></i>';
                    saveBtn.title     = 'Save';
                    saveBtn.addEventListener('click', function (e) {
                        e.stopPropagation();
                        var inp = self.regionListEl.querySelector('input[data-region-edit="' + r.id + '"]');
                        self._commitRegionEdit(r.id, inp ? inp.value : r.name);
                    });
                    actions.appendChild(saveBtn);

                    var cancelBtn = document.createElement('button');
                    cancelBtn.className = 'btn btn-sm btn-outline-secondary';
                    cancelBtn.style.cssText = btnCss;
                    cancelBtn.innerHTML = '<i class="fas fa-times"></i>';
                    cancelBtn.title     = 'Cancel';
                    cancelBtn.addEventListener('click', function (e) { e.stopPropagation(); self._cancelRegionEdit(); });
                    actions.appendChild(cancelBtn);
                } else {
                    var editBtn = document.createElement('button');
                    editBtn.className = 'btn btn-sm btn-outline-secondary';
                    editBtn.style.cssText = btnCss;
                    editBtn.innerHTML = '<i class="fas fa-pen"></i>';
                    editBtn.title     = 'Rename';
                    editBtn.addEventListener('click', function (e) { e.stopPropagation(); self._startRegionEdit(r.id); });
                    actions.appendChild(editBtn);
                }
            }

            var eyeBtn = document.createElement('button');
            eyeBtn.className = 'btn btn-sm btn-outline-secondary';
            eyeBtn.style.cssText = btnCss;
            eyeBtn.innerHTML = '<i class="fas fa-eye' + (r.visible ? '' : '-slash') + '"></i>';
            eyeBtn.title     = r.visible ? 'Hide' : 'Show';
            eyeBtn.addEventListener('click', function (e) {
                e.stopPropagation();
                r.visible = !r.visible;
                r.layer.visible(r.visible);
                r.layer.draw();
                self._syncSelectedPolygonHandles();
                self._renderRegionList();
            });
            actions.appendChild(eyeBtn);

            if (self.isAdmin && self.regions.length > 1) {
                var delBtn = document.createElement('button');
                delBtn.className = 'btn btn-sm btn-outline-danger';
                delBtn.style.cssText = btnCss;
                delBtn.innerHTML = '<i class="fas fa-times"></i>';
                delBtn.title     = 'Remove region';
                delBtn.addEventListener('click', function (e) {
                    e.stopPropagation();
                    var removedSelected = self.shapes.some(function (s) {
                        return s.regionId === r.id && s.id === self._selectedShapeId;
                    });
                    self.shapes = self.shapes.filter(function (s) {
                        if (s.regionId === r.id) { s.konvaNode.destroy(); return false; }
                        return true;
                    });
                    if (removedSelected) {
                        self._selectedShapeId = null;
                        self._clearPolygonVertexHandles();
                    }
                    if (self._editingRegionId === r.id) self._editingRegionId = null;
                    var deletedDbId = r.dbId;
                    r.layer.destroy();
                    if (self._annotationLayer) self._annotationLayer.batchDraw();
                    self.regions = self.regions.filter(function (x) { return x.id !== r.id; });
                    if (self.activeRegionId === r.id) {
                        self.activeRegionId = self.regions[0] ? self.regions[0].id : null;
                    }
                    self._renderRegionList();
                    self._renderShapesList();
                    if (self.isAdmin && deletedDbId) {
                        self._requestVoid('/laparoscopy/api/region-types/' + deletedDbId + '/', {
                            method: 'DELETE',
                            headers: self._csrfHeaders(),
                        });
                    }
                });
                actions.appendChild(delBtn);
            }

            self.regionListEl.appendChild(li);
        });

        if (this._editingRegionId) {
            var activeInput = this.regionListEl.querySelector(
                'input[data-region-edit="' + this._editingRegionId + '"]'
            );
            if (activeInput) { activeInput.focus(); activeInput.select(); }
        }
    };

    /* ====================================================================== */
    /* Magic Toolbox                                                            */
    /* ====================================================================== */

    VideoAnnotator.prototype._initMagicToolbox = function () {
        var self = this;

        this._magicOverlayEl = document.createElement('div');
        this._magicOverlayEl.id = 'magic-prompt-overlay';
        this._magicOverlayEl.style.cssText = 'position:absolute;inset:0;z-index:20;pointer-events:none;';
        this.wrapEl.appendChild(this._magicOverlayEl);

        this._maskDecisionLayerEl = document.createElement('div');
        this._maskDecisionLayerEl.id = 'magic-mask-decision-layer';
        this._maskDecisionLayerEl.style.cssText =
            'position:absolute;inset:0;z-index:26;pointer-events:none;display:none;';
        this.wrapEl.appendChild(this._maskDecisionLayerEl);

        if (this._magicPanelEl) {
            this._magicStatusEl = document.createElement('div');
            this._magicStatusEl.className = 'small text-muted mb-2';
            this._magicStatusEl.textContent = 'Magic Tool ready.';
            var body = this._magicPanelEl.querySelector('.card-body');
            if (body) body.insertBefore(this._magicStatusEl, body.firstChild);
        }

        if (this._magicPointToolBtnEl) {
            this._magicPointToolBtnEl.addEventListener('click', function () {
                self._setTool(self.currentTool === 'point' ? (self._lastNonPointTool || 'brush') : 'point');
            });
        }

        if (this._magicPointPositiveBtnEl) {
            this._magicPointPositiveBtnEl.addEventListener('click', function () {
                self._magicPointLabel = 1;
                self._syncMagicPointLabelButtons();
            });
        }

        if (this._magicPointNegativeBtnEl) {
            this._magicPointNegativeBtnEl.addEventListener('click', function () {
                self._magicPointLabel = 0;
                self._syncMagicPointLabelButtons();
            });
        }

        this._magicOverlayEl.addEventListener('click', function (e) {
            if (!self._magicPointActive || !self.annotationMode) return;
            var rect = self.videoEl.getBoundingClientRect();
            if (!rect.width || !rect.height) return;
            var region = self._activeRegion();
            if (!region) return;
            // Always snap to the nearest subsampled-fps boundary so that
            // frame_time matches what _renderMagicOverlay and _frameKey produce.
            var rawFt = Number(self._currentVideoTime());
            if (!isFinite(rawFt) || rawFt < 0) rawFt = 0;
            var ft = (typeof self._snapToSubsampledFrame === 'function')
                ? self._snapToSubsampledFrame(rawFt) : rawFt;

            self._magicPrompts.push({
                id: 'mp-' + Date.now() + '-' + Math.random().toString(36).slice(2),
                x: Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width)),
                y: Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height)),
                frame_time: ft,
                region_id: region.id,
                point_label: self._magicPointLabel,
            });
            delete self._lastPromptSigByScope[self._scopeKey(self._promptFrameKey(ft), region.id)];
            self._renderMagicOverlay();
            self._renderMagicPromptList();
            self._updateMagicCount();
        });

        var sendBtn = this._magicSendBtnEl || document.getElementById('magic-send-prompts-btn');
        if (sendBtn) {
            sendBtn.addEventListener('click', function () {
                self._sendMagicPromptsViaAnnotator();
            });
        }

        this.videoEl.addEventListener('timeupdate', function () { self._renderMagicOverlay(); self._renderMagicPromptList(); });
        this.videoEl.addEventListener('seeked', function () {
            self._timelinePreviewTime = null;
            self._renderMagicOverlay();
            self._renderMagicPromptList();
        });
        this.videoEl.addEventListener('play', function () {
            self._timelinePreviewTime = null;
            self._renderMagicOverlay();
            self._renderMagicPromptList();
        });
        this._syncMagicPointLabelButtons();
        this._setMagicPointActive(false);
        this._renderMagicPromptList();
        this._updateMagicCount();
        this._bindMaskSync();
        this._setMagicControlsEnabled(false);
        this._wsConnect();
    };

    VideoAnnotator.prototype._syncMagicPointLabelButtons = function () {
        var isPositive = this._magicPointLabel !== 0;
        if (this._magicPointPositiveBtnEl) {
            this._magicPointPositiveBtnEl.classList.toggle('active', isPositive);
            this._magicPointPositiveBtnEl.classList.toggle('btn-success', isPositive);
            this._magicPointPositiveBtnEl.classList.toggle('btn-outline-success', !isPositive);
        }
        if (this._magicPointNegativeBtnEl) {
            this._magicPointNegativeBtnEl.classList.toggle('active', !isPositive);
            this._magicPointNegativeBtnEl.classList.toggle('btn-danger', !isPositive);
            this._magicPointNegativeBtnEl.classList.toggle('btn-outline-danger', isPositive);
        }
    };

    VideoAnnotator.prototype._setMagicPointActive = function (active) {
        this._magicPointActive = !!active;
        if (this._magicOverlayEl) {
            this._magicOverlayEl.style.pointerEvents = this._magicPointActive ? 'auto' : 'none';
            this._magicOverlayEl.style.cursor = this._magicPointActive ? 'crosshair' : 'default';
        }
        if (this._magicPointToolBtnEl) {
            this._magicPointToolBtnEl.classList.toggle('active', this._magicPointActive);
            this._magicPointToolBtnEl.classList.toggle('btn-primary', this._magicPointActive);
            this._magicPointToolBtnEl.classList.toggle('btn-outline-primary', !this._magicPointActive);
        }
    };

    VideoAnnotator.prototype._renderMagicOverlay = function () {
        if (!this._magicOverlayEl) return;
        this._magicOverlayEl.innerHTML = '';
        if (!this.annotationMode) return;

        var currentTime = typeof this._snapToSubsampledFrame === 'function'
            ? this._snapToSubsampledFrame(this._currentVideoTime())
            : Math.round(Number(this._currentVideoTime()));
        var tolerance = 0.001;
        var regionById = {};
        this.regions.forEach(function (r) { regionById[r.id] = r; });
        var self = this;
        (this._magicPrompts || []).forEach(function (p) {
            if (Math.abs(Number(p.frame_time || 0) - currentTime) > tolerance) return;
            var region = regionById[p.region_id] || null;
            var color = region ? region.color : '#3498db';
            var isNegative = Number(p.point_label) === 0;
            var dot = document.createElement('div');
            dot.style.cssText = isNegative
                ? 'position:absolute;left:' + (p.x * 100) + '%;top:' + (p.y * 100) + '%;transform:translate(-50%,-50%);cursor:pointer;pointer-events:auto;color:' + color + ';font-size:16px;font-weight:700;text-shadow:0 0 4px rgba(0,0,0,.6);'
                : 'position:absolute;left:' + (p.x * 100) + '%;top:' + (p.y * 100) + '%;width:10px;height:10px;border-radius:50%;background:' + color + ';border:1.5px solid #fff;box-shadow:0 0 4px ' + color + ';transform:translate(-50%,-50%);cursor:pointer;pointer-events:auto;';
            if (isNegative) dot.textContent = 'x';
            dot.title = 'Click to remove prompt';
            dot.addEventListener('click', function (ev) {
                ev.stopPropagation();
                self._magicPrompts = (self._magicPrompts || []).filter(function (q) { return q.id !== p.id; });
                delete self._lastPromptSigByScope[self._scopeKey(self._promptFrameKey(p.frame_time), p.region_id)];
                self._renderMagicOverlay();
                self._renderMagicPromptList();
                self._updateMagicCount();
            });
            self._magicOverlayEl.appendChild(dot);
        });
    };

    VideoAnnotator.prototype._updateMagicCount = function () {
        if (this._magicPromptsCountEl) {
            this._magicPromptsCountEl.textContent = String((this._magicPrompts || []).length);
        }
    };

    /* ====================================================================== */
    /* Frame navigation                                                         */
    /* ====================================================================== */

    VideoAnnotator.prototype._bindFrameNav = function () {
        var self = this;

        U.on('frame-first',  'click', function () {
            self.videoEl.pause();
            self._seekPending = null; self._seekInFlight = false;
            self.videoEl.currentTime = 0;
        });
        U.on('frame-prev10', 'click', function () { self._stepBack(10); });
        U.on('frame-prev',   'click', function () { self._stepBack(1); });
        U.on('frame-play',   'click', function () { self._togglePlay(); });
        U.on('frame-next',   'click', function () { self._stepForward(1); });
        U.on('frame-next10', 'click', function () { self._stepForward(10); });
        U.on('frame-last',   'click', function () {
            self.videoEl.pause();
            self._seekPending = null; self._seekInFlight = false;
            if (isFinite(self.videoEl.duration)) self.videoEl.currentTime = self.videoEl.duration;
        });

        this.videoEl.addEventListener('timeupdate', function () { self._updateTimestamp(); });
        this.videoEl.addEventListener('seeked', function () {
            self._seekInFlight = false;
            self._flushSeek();
            self._updateTimestamp();
        });
        this.videoEl.addEventListener('play',  function () { self._updatePlayBtn(); });
        this.videoEl.addEventListener('pause', function () { self._updatePlayBtn(); });
    };

    /**
     * Seek coalescing: rapid clicks accumulate into _seekPending.
     * Only one seek is in-flight at a time; on 'seeked', any pending offset
     * is applied immediately — no serial drain of queued seeks.
     */
    VideoAnnotator.prototype._flushSeek = function () {
        if (this._seekInFlight || this._seekPending === null) return;
        this._seekInFlight = true;
        this.videoEl.currentTime = this._seekPending;
        this._seekPending = null;
    };

    VideoAnnotator.prototype._stepForward = function (frames) {
        this.videoEl.pause();
        var base = this._seekPending !== null ? this._seekPending : this.videoEl.currentTime;
        var max  = isFinite(this.videoEl.duration) ? this.videoEl.duration : Infinity;
        this._seekPending = Math.min(max, base + U.FRAME_STEP_S * frames);
        this._flushSeek();
    };

    VideoAnnotator.prototype._stepBack = function (frames) {
        this.videoEl.pause();
        var base = this._seekPending !== null ? this._seekPending : this.videoEl.currentTime;
        this._seekPending = Math.max(0, base - U.FRAME_STEP_S * frames);
        this._flushSeek();
    };

    VideoAnnotator.prototype._togglePlay = function () {
        if (this.annotationMode) return;
        if (this.videoEl.paused) { this.videoEl.play(); } else { this.videoEl.pause(); }
    };

    VideoAnnotator.prototype._updatePlayBtn = function () {
        var btn = U.el('frame-play');
        if (!btn) return;
        btn.innerHTML = this.videoEl.paused ? '<i class="fas fa-play"></i>' : '<i class="fas fa-pause"></i>';
    };

    VideoAnnotator.prototype._updateTimestamp = function () {
        if (!this.timestampEl) return;
        var t  = this.videoEl.currentTime || 0;
        var hh = Math.floor(t / 3600);
        var mm = Math.floor((t % 3600) / 60);
        var ss = Math.floor(t % 60);
        var ms = Math.floor((t % 1) * 1000);
        this.timestampEl.textContent =
            (hh ? String(hh).padStart(2, '0') + ':' : '') +
            String(mm).padStart(2, '0') + ':' +
            String(ss).padStart(2, '0') + '.' +
            String(ms).padStart(3, '0');
    };

    /* ====================================================================== */
    /* Keyboard shortcuts                                                        */
    /* ====================================================================== */

    VideoAnnotator.prototype._bindKeyboard = function () {
        var self = this;

        document.addEventListener('keydown', function (e) {
            if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;

            switch (e.key) {
                case ' ':
                    if (self.annotationMode) { e.preventDefault(); self._spaceDown = true; self._updateCursor(); }
                    break;
                case 'Control':
                    if (self.annotationMode) { e.preventDefault(); self._ctrlDown = true; self._updateCursor(); }
                    break;
                case 'ArrowLeft':
                    if (self.annotationMode) { e.preventDefault(); self._stepBack(e.shiftKey ? 10 : 1); }
                    break;
                case 'ArrowRight':
                    if (self.annotationMode) { e.preventDefault(); self._stepForward(e.shiftKey ? 10 : 1); }
                    break;
                case 'b': case 'B': if (self.annotationMode) self._setTool('brush');   break;
                case 'e': case 'E': if (self.annotationMode) self._setTool('eraser');  break;
                case 'p': case 'P': if (self.annotationMode) self._setTool('polygon'); break;
                case 'h': case 'H': if (self.annotationMode) self._setTool('pan');     break;
                case '[':
                    if (self.annotationMode) {
                        self.brushSize = Math.max(1, self.brushSize - 2);
                        if (self.brushSizeInput) self.brushSizeInput.value = self.brushSize;
                        if (self.brushSizeLabel) self.brushSizeLabel.textContent = self.brushSize;
                        if (self._cursorCircle)  self._cursorCircle.radius(self.brushSize / 2);
                    }
                    break;
                case ']':
                    if (self.annotationMode) {
                        self.brushSize = Math.min(100, self.brushSize + 2);
                        if (self.brushSizeInput) self.brushSizeInput.value = self.brushSize;
                        if (self.brushSizeLabel) self.brushSizeLabel.textContent = self.brushSize;
                        if (self._cursorCircle)  self._cursorCircle.radius(self.brushSize / 2);
                    }
                    break;
                case 'Escape':
                    if (self.annotationMode) self._cancelPolygon();
                    break;
                case 'Enter':
                    if (self.annotationMode && self.currentTool === 'polygon' && self._polyPoints.length >= 6) {
                        e.preventDefault();
                        self._polyClose();
                    }
                    break;
            }
        });

        document.addEventListener('keyup', function (e) {
            if (e.key === ' ')       { self._spaceDown = false; self._updateCursor(); }
            if (e.key === 'Control') { self._ctrlDown  = false; self._updateCursor(); }
        });
    };

    /* ====================================================================== */
    /* Export                                                                   */
    /* ====================================================================== */

    if (
        window.LaparoscopyAnnotatorMixins &&
        typeof window.LaparoscopyAnnotatorMixins.shapes === 'function'
    ) {
        window.LaparoscopyAnnotatorMixins.shapes(VideoAnnotator);
    }

    if (
        window.LaparoscopyAnnotatorMixins &&
        typeof window.LaparoscopyAnnotatorMixins.timeline === 'function'
    ) {
        window.LaparoscopyAnnotatorMixins.timeline(VideoAnnotator);
    }

    if (
        window.LaparoscopyAnnotatorMixins &&
        typeof window.LaparoscopyAnnotatorMixins.api === 'function'
    ) {
        window.LaparoscopyAnnotatorMixins.api(VideoAnnotator);
    }

    if (
        window.LaparoscopyAnnotatorMixins &&
        typeof window.LaparoscopyAnnotatorMixins.mask === 'function'
    ) {
        window.LaparoscopyAnnotatorMixins.mask(VideoAnnotator);
    }

    if (
        window.LaparoscopyAnnotatorMixins &&
        typeof window.LaparoscopyAnnotatorMixins.worker === 'function'
    ) {
        window.LaparoscopyAnnotatorMixins.worker(VideoAnnotator);
    }

    if (
        window.LaparoscopyAnnotatorMixins &&
        typeof window.LaparoscopyAnnotatorMixins.magic === 'function'
    ) {
        window.LaparoscopyAnnotatorMixins.magic(VideoAnnotator);
    }

    window.VideoAnnotator = VideoAnnotator;

})();
