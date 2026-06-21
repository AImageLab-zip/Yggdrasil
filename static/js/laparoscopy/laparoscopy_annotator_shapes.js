(function () {
    'use strict';

    var U = window.LaparoscopyAnnotatorUtils;

    window.LaparoscopyAnnotatorMixins = window.LaparoscopyAnnotatorMixins || {};

    window.LaparoscopyAnnotatorMixins.shapes = function (VideoAnnotator) {
        VideoAnnotator.prototype._registerShape = function (type, konvaNode, options) {
            options = options || {};

            var region = null;
            if (options.regionId) {
                region = this.regions.find(function (r) { return r.id === options.regionId; }) || null;
            }
            if (!region) region = this._activeRegion();

            var typeLabel = { brush: 'Brush', eraser: 'Eraser', polygon: 'Polygon' }[type] || type;
            var id = options.id || ('shape-' + Date.now() + '-' + Math.random().toString(36).slice(2));
            var shape = {
                id:        id,
                dbId:      options.dbId || null,
                type:      type,
                regionId:  region ? region.id : null,
                konvaNode: konvaNode,
                label:     typeLabel + ' \u2022 ' + (region ? region.name : '?'),
                frameTime: (typeof options.frameTime === 'number') ? options.frameTime : this.videoEl.currentTime,
                promptPoints: Array.isArray(options.promptPoints) ? options.promptPoints.map(function (p) {
                    return {
                        x: Number(p && p.x),
                        y: Number(p && p.y),
                        label: Number(p && p.label) === 0 ? 0 : 1,
                    };
                }).filter(function (p) {
                    return isFinite(p.x) && isFinite(p.y) && p.x >= 0 && p.x <= 1 && p.y >= 0 && p.y <= 1;
                }) : [],
            };
            this.shapes.push(shape);
            if (type === 'polygon') this._bindPolygonShape(shape);
            if (!options.skipPersist) this._createAnnotationForShape(shape);
            if (!this._batchingShapes) this._renderShapesList();
            return shape;
        };

        VideoAnnotator.prototype._shapeLabel = function (shape) {
            var typeLabel = { brush: 'Brush', eraser: 'Eraser', polygon: 'Polygon' }[shape.type] || shape.type;
            var region = this.regions.find(function (r) { return r.id === shape.regionId; });
            return typeLabel + ' \u2022 ' + (region ? region.name : '?');
        };

        VideoAnnotator.prototype._renderShapesList = function () {
            var self = this;
            if (!this.shapesListEl) return;
            this.shapesListEl.innerHTML = '';

            var panelEl = this.shapesListEl.closest('.card');
            if (panelEl) {
                var headerEl = panelEl.querySelector('.card-header');
                var existingBtn = headerEl ? headerEl.querySelector('[data-filter-btn]') : null;
                if (headerEl && !existingBtn) {
                    var filterBtn = document.createElement('button');
                    filterBtn.setAttribute('data-filter-btn', '1');
                    filterBtn.className = 'btn btn-sm btn-outline-secondary ms-2';
                    filterBtn.style.cssText = 'padding:0.1rem 0.3rem;';
                    filterBtn.innerHTML = '<i class="fas fa-filter"></i>';
                    filterBtn.title = 'Filter by current frame';
                    filterBtn.addEventListener('click', function () {
                        self._filterShapesCurrentFrame = !self._filterShapesCurrentFrame;
                        filterBtn.classList.toggle('active');
                        self._renderShapesList();
                    });
                    headerEl.appendChild(filterBtn);
                    if (this._filterShapesCurrentFrame) filterBtn.classList.add('active');
                }
            }

            var displayShapes = this.shapes.slice();
            if (this._filterShapesCurrentFrame) {
                var currentTime = this.videoEl.currentTime;
                displayShapes = displayShapes.filter(function (s) {
                    return Math.abs(s.frameTime - currentTime) <= U.FRAME_TOLERANCE;
                });
            }

            if (displayShapes.length === 0) {
                var empty = document.createElement('li');
                empty.className   = 'list-group-item text-muted small py-1 px-2';
                empty.textContent = this._filterShapesCurrentFrame ? 'No annotations on this frame.' : 'No annotations yet.';
                this.shapesListEl.appendChild(empty);
                return;
            }

            var iconFor = { brush: 'fa-paint-brush', eraser: 'fa-eraser', polygon: 'fa-draw-polygon' };

            displayShapes.slice().reverse().forEach(function (s) {
                var region = self.regions.find(function (r) { return r.id === s.regionId; });
                var color  = region ? region.color : '#888';

                var li = document.createElement('li');
                li.className  = 'list-group-item d-flex align-items-center gap-2 py-1 px-2';
                li.style.cursor = 'pointer';
                if (s.id === self._selectedShapeId) li.classList.add('bg-warning-subtle');

                var dot = document.createElement('span');
                dot.style.cssText = 'display:inline-block;width:10px;height:10px;border-radius:50%;flex-shrink:0;background:' + color;
                li.appendChild(dot);

                var icon = document.createElement('i');
                icon.className = 'fas ' + (iconFor[s.type] || 'fa-shapes') + ' small text-muted';
                li.appendChild(icon);

                var label = document.createElement('span');
                label.className   = 'flex-grow-1 small text-truncate';
                label.textContent = self._shapeLabel(s) + '  @' + U.fmtTime(s.frameTime);
                li.appendChild(label);
                li.title = 'Click to select, double-click to jump to frame';

                var regionBtn = document.createElement('button');
                regionBtn.className = 'btn btn-sm btn-outline-info py-0 px-2 flex-shrink-0';
                regionBtn.type      = 'button';
                regionBtn.innerHTML = '<i class="fas fa-exchange-alt"></i>';
                regionBtn.title     = 'Change region';
                regionBtn.addEventListener('click', function (e) {
                    e.stopPropagation();
                    self._showRegionSelector(s.id, e);
                });
                li.appendChild(regionBtn);

                var delBtn = document.createElement('button');
                delBtn.className = 'btn btn-sm btn-outline-danger py-0 px-2 flex-shrink-0';
                delBtn.type      = 'button';
                delBtn.innerHTML = '<i class="fas fa-trash-alt"></i>';
                delBtn.title     = 'Delete';
                delBtn.addEventListener('click', function (e) { e.stopPropagation(); self._deleteShape(s.id); });
                li.appendChild(delBtn);

                li.addEventListener('click', function () { self._selectShape(s.id); });
                li.addEventListener('dblclick', function (e) {
                    e.preventDefault();
                    e.stopPropagation();
                    self._jumpToShapeFrame(s.id);
                });
                self.shapesListEl.appendChild(li);
            });
        };

        VideoAnnotator.prototype._selectedShape = function () {
            var id = this._selectedShapeId;
            return this.shapes.find(function (s) { return s.id === id; }) || null;
        };

        VideoAnnotator.prototype._clearPolygonVertexHandles = function () {
            this._polyVertexHandles.forEach(function (h) { h.destroy(); });
            this._polyVertexHandles = [];
            this._polyVertexShapeId = null;
        };

        VideoAnnotator.prototype._syncSelectedPolygonHandles = function () {
            var shape  = this._selectedShape();
            var region = shape ? this.regions.find(function (r) { return r.id === shape.regionId; }) : null;
            var shouldShow = !!(
                this.annotationMode && shape && shape.type === 'polygon' &&
                region && shape.konvaNode.visible() && region.visible
            );

            if (!shouldShow) {
                this._clearPolygonVertexHandles();
                if (region) region.layer.draw();
                return;
            }

            var points = shape.konvaNode.points();
            var count  = points.length / 2;

            if (this._polyVertexShapeId !== shape.id || this._polyVertexHandles.length !== count) {
                this._clearPolygonVertexHandles();
                var self = this;

                for (var i = 0; i < points.length; i += 2) {
                    (function (vi) {
                        var handle = new Konva.Circle({
                            x: points[vi * 2], y: points[vi * 2 + 1],
                            radius: 6, fill: '#ffffff',
                            stroke: region.color, strokeWidth: 2,
                            draggable: true,
                        });

                        handle.on('mousedown touchstart', function (e) {
                            e.cancelBubble = true;
                            if (self._selectedShapeId !== shape.id) self._selectShape(shape.id);
                        });
                        handle.on('dblclick dbltap', function (e) {
                            e.cancelBubble = true;
                            self._removeVertexFromShape(shape, vi);
                        });
                        handle.on('dragstart', function (e) {
                            e.cancelBubble = true;
                            self._draggingVertex = true;
                        });
                        handle.on('dragmove', function (e) {
                            e.cancelBubble = true;
                            var pts = shape.konvaNode.points().slice();
                            pts[vi * 2]     = handle.x();
                            pts[vi * 2 + 1] = handle.y();
                            shape.konvaNode.points(pts);
                            region.layer.batchDraw();
                        });
                        handle.on('dragend', function (e) {
                            e.cancelBubble = true;
                            self._draggingVertex = false;
                            self._syncSelectedPolygonHandles();
                            self._persistShapeGeometry(shape);
                        });
                        handle.on('mouseenter', function () {
                            if (!self._inPanMode()) self._container.style.cursor = 'move';
                        });
                        handle.on('mouseleave', function () { self._updateCursor(); });

                        region.layer.add(handle);
                        self._polyVertexHandles.push(handle);
                    })(i / 2);
                }

                this._polyVertexShapeId = shape.id;
            }

            var cur = shape.konvaNode.points();
            this._polyVertexHandles.forEach(function (h, idx) {
                h.position({ x: cur[idx * 2], y: cur[idx * 2 + 1] });
                h.visible(true);
                h.moveToTop();
            });
            region.layer.draw();
        };

        VideoAnnotator.prototype._bindPolygonShape = function (shape) {
            var self = this;
            shape.konvaNode.listening(true);
            shape.konvaNode.on('mousedown touchstart', function (e) {
                if (!self.annotationMode || self.currentTool !== 'polygon' || self._inPanMode() || self._draggingVertex) return;
                if (self._selectedShapeId !== shape.id) {
                    self._selectShape(shape.id);
                    e.cancelBubble = true;
                    return;
                }
                var pos = self._pointerPos();
                if (!pos) return;
                self._insertVertexIntoShape(shape, pos);
                e.cancelBubble = true;
            });
        };

        VideoAnnotator.prototype._segmentDistanceSq = function (ax, ay, bx, by, px, py) {
            var dx = bx - ax, dy = by - ay;
            if (dx === 0 && dy === 0) {
                dx = px - ax; dy = py - ay;
                return dx * dx + dy * dy;
            }
            var t  = Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)));
            var cx = ax + t * dx, cy = ay + t * dy;
            dx = px - cx; dy = py - cy;
            return dx * dx + dy * dy;
        };

        VideoAnnotator.prototype._insertVertexIntoShape = function (shape, pos) {
            if (!shape || shape.type !== 'polygon') return;
            var points = shape.konvaNode.points().slice();
            if (points.length < 6) return;

            var bestAt = points.length, bestDist = Infinity;
            for (var i = 0; i < points.length; i += 2) {
                var next = (i + 2) % points.length;
                var d = this._segmentDistanceSq(
                    points[i], points[i + 1], points[next], points[next + 1], pos.x, pos.y
                );
                if (d < bestDist) { bestDist = d; bestAt = i + 2; }
            }
            points.splice(bestAt, 0, pos.x, pos.y);
            shape.konvaNode.points(points);
            this._syncSelectedPolygonHandles();
            this._persistShapeGeometry(shape);
        };

        VideoAnnotator.prototype._removeVertexFromShape = function (shape, vi) {
            if (!shape || shape.type !== 'polygon') return;
            var points = shape.konvaNode.points().slice();
            if (points.length / 2 <= 3) return;
            points.splice(vi * 2, 2);
            shape.konvaNode.points(points);
            this._draggingVertex = false;
            this._syncSelectedPolygonHandles();
            this._persistShapeGeometry(shape);
        };

        VideoAnnotator.prototype._deleteShape = function (id) {
            var idx = this.shapes.findIndex(function (s) { return s.id === id; });
            if (idx === -1) return;
            var s      = this.shapes[idx];
            var region = this.regions.find(function (r) { return r.id === s.regionId; });

            if (s.dbId) this._deleteAnnotationById(s.dbId);

            s.konvaNode.destroy();
            if (region) region.layer.draw();
            if (this._selectedShapeId === id) {
                this._selectedShapeId = null;
                this._clearPolygonVertexHandles();
            }
            this.shapes.splice(idx, 1);
            if (typeof this._onShapeDeleted === 'function') this._onShapeDeleted(s);
            this._renderShapesList();
        };

        VideoAnnotator.prototype._deleteLastShape = function () {
            if (!this.shapes.length) return;
            this._deleteShape(this.shapes[this.shapes.length - 1].id);
        };

        VideoAnnotator.prototype._selectShape = function (id) {
            var self = this;
            this.shapes.forEach(function (s) {
                s.konvaNode.shadowEnabled(false);
                var r = self.regions.find(function (r) { return r.id === s.regionId; });
                if (r) r.layer.draw();
            });

            this._selectedShapeId = (this._selectedShapeId === id) ? null : id;

            if (this._selectedShapeId) {
                var target = this.shapes.find(function (s) { return s.id === id; });
                if (target) {
                    target.konvaNode.shadowColor('#ffcc00');
                    target.konvaNode.shadowBlur(18);
                    target.konvaNode.shadowEnabled(true);
                    var r = this.regions.find(function (r) { return r.id === target.regionId; });
                    if (r) r.layer.draw();
                }
            }
            this._syncSelectedPolygonHandles();
            this._renderShapesList();
        };

        VideoAnnotator.prototype._jumpToShapeFrame = function (shapeId) {
            var shape = this.shapes.find(function (s) { return s.id === shapeId; });
            if (!shape) return;

            var targetTime = (typeof shape.frameTime === 'number' && isFinite(shape.frameTime))
                ? shape.frameTime
                : 0;
            if (targetTime < 0) targetTime = 0;

            var duration = this.videoEl.duration;
            if (isFinite(duration) && targetTime > duration) targetTime = duration;

            this.videoEl.pause();
            this._seekPending = null;
            this._seekInFlight = false;
            this.videoEl.currentTime = targetTime;

            if (this._selectedShapeId !== shapeId) this._selectShape(shapeId);
            this._updateTimestamp();
            this._updateShapeVisibility();
            this._updateTemporalTimelineUI();
        };

        VideoAnnotator.prototype._changeShapeRegion = function (shapeId, newRegionId) {
            var shape = this.shapes.find(function (s) { return s.id === shapeId; });
            if (!shape) return;
            var oldRegion = this.regions.find(function (r) { return r.id === shape.regionId; });
            var newRegion = this.regions.find(function (r) { return r.id === newRegionId; });
            if (!newRegion) return;

            if (oldRegion) {
                shape.konvaNode.remove();
            }
            newRegion.layer.add(shape.konvaNode);

            shape.regionId = newRegionId;
            this._applyRegionStyleToShape(shape, newRegion);

            if (oldRegion) oldRegion.layer.draw();
            newRegion.layer.draw();
            this._syncSelectedPolygonHandles();
            this._renderShapesList();

            if (newRegion.dbId) {
                this._patchAnnotationForShape(shape, { region_type_id: newRegion.dbId });
            }
        };

        VideoAnnotator.prototype._showRegionSelector = function (shapeId, clickEvent) {
            var self = this;
            var shape = this.shapes.find(function (s) { return s.id === shapeId; });
            if (!shape || this.regions.length <= 1) return;

            var menu = document.createElement('div');
            menu.style.cssText = 'position:fixed;background:#fff;border:1px solid #ccc;border-radius:4px;' +
                                  'box-shadow:0 2px 8px rgba(0,0,0,0.15);z-index:1000;min-width:150px;';

            this.regions.forEach(function (region) {
                var item = document.createElement('div');
                item.style.cssText = 'padding:0.5rem 1rem;cursor:pointer;border-bottom:1px solid #eee;';
                item.textContent = region.name;
                if (region.id === shape.regionId) {
                    item.style.background = '#f0f0f0';
                    item.textContent += ' \u2713';
                }
                item.addEventListener('mouseover', function () { item.style.background = '#f8f8f8'; });
                item.addEventListener('mouseout', function () {
                    if (region.id !== shape.regionId) item.style.background = '#fff';
                });
                item.addEventListener('click', function () {
                    self._changeShapeRegion(shapeId, region.id);
                    menu.remove();
                });
                menu.appendChild(item);
            });

            var x = ((clickEvent && clickEvent.clientX) || window.innerWidth / 2) - 50;
            var y = ((clickEvent && clickEvent.clientY) || 100) + 10;
            menu.style.left = Math.max(0, x) + 'px';
            menu.style.top = y + 'px';

            document.body.appendChild(menu);

            var closeMenu = function () {
                menu.remove();
                document.removeEventListener('click', closeMenu);
            };
            setTimeout(function () { document.addEventListener('click', closeMenu); }, 0);
        };

        VideoAnnotator.prototype._updateShapeVisibility = function () {
            var t = this.videoEl.currentTime;
            this.shapes.forEach(function (s) {
                s.konvaNode.visible(Math.abs(t - s.frameTime) <= U.FRAME_TOLERANCE);
            });
            this._syncSelectedPolygonHandles();
            this.regions.forEach(function (r) { r.layer.draw(); });
        };

        VideoAnnotator.prototype._bindToggle = function () {
            var self = this;
            this.toggleBtn.addEventListener('click', function () {
                if (self.annotationMode) { self._exitAnnotationMode(); }
                else                     { self._enterAnnotationMode(); }
            });
        };

        VideoAnnotator.prototype._enterAnnotationMode = function () {
            this.annotationMode = true;
            this.videoEl.pause();
            this.videoEl.controls = false;
            this.outerEl.style.outline = '3px solid #e74c3c';
            this._container.style.pointerEvents = 'auto';
            this.toolbarEl.classList.remove('d-none');
            this._syncStageSize();
            this._bindDrawing();
            this._bindResize();
            this._syncSelectedPolygonHandles();
            this.toggleBtn.textContent = 'Exit Annotation Mode';
            this.toggleBtn.classList.replace('btn-outline-warning', 'btn-warning');
            this._updateTimestamp();
            if (typeof this._renderMagicOverlay === 'function') this._renderMagicOverlay();
            if (typeof this._renderMagicPromptList === 'function') this._renderMagicPromptList();
        };

        VideoAnnotator.prototype._exitAnnotationMode = function () {
            this.annotationMode = false;
            this._timelinePreviewTime = null;
            this.videoEl.controls = true;
            this.outerEl.style.outline = '';
            this._container.style.pointerEvents = 'none';
            this._unbindDrawing();
            this._unbindResize();
            if (typeof this._destroyTimeline === 'function') this._destroyTimeline();
            this._cancelPolygon();
            this._clearPolygonVertexHandles();
            this._resetZoom();
            this.toolbarEl.classList.add('d-none');
            this.toggleBtn.textContent = 'Enter Annotation Mode';
            this.toggleBtn.classList.replace('btn-warning', 'btn-outline-warning');
            if (typeof this._renderMagicOverlay === 'function') this._renderMagicOverlay();
            if (typeof this._renderMagicPromptList === 'function') this._renderMagicPromptList();
        };

        VideoAnnotator.prototype._bindToolbar = function () {
            var self = this;

            this.toolbarEl.querySelectorAll('[data-tool]').forEach(function (btn) {
                btn.addEventListener('click', function () { self._setTool(btn.getAttribute('data-tool')); });
            });

            if (this.brushSizeInput) {
                this.brushSizeInput.addEventListener('input', function () {
                    self.brushSize = parseInt(this.value, 10);
                    if (self.brushSizeLabel) self.brushSizeLabel.textContent = self.brushSize;
                    if (self._cursorCircle)  self._cursorCircle.radius(self.brushSize / 2);
                });
            }

            U.on('add-region-btn', 'click', function () {
                if (!self.isAdmin) return;
                var name = 'Region ' + (self.regions.length + 1);
                var r = self.addRegion(name);
                self.activeRegionId = r.id;
                self._renderRegionList();
                self._persistRegionType(r);
            });

            U.on('toggle-regions-visibility-btn', 'click', function () {
                var anyVisible = self.regions.some(function (r) { return r.visible; });
                self.regions.forEach(function (r) {
                    r.visible = !anyVisible;
                    r.layer.visible(r.visible);
                    r.layer.draw();
                });
                self._syncSelectedPolygonHandles();
                self._renderRegionList();
                this.innerHTML = anyVisible
                    ? '<i class="fas fa-eye me-1"></i>Show all'
                    : '<i class="fas fa-eye-slash me-1"></i>Hide all';
                this.title = anyVisible ? 'Show all regions' : 'Hide all regions';
            });

            U.on('zoom-in-btn',    'click', function () {
                self._applyZoom(1.25, self.outerEl.clientWidth / 2, self.outerEl.clientHeight / 2);
            });
            U.on('zoom-out-btn',   'click', function () {
                self._applyZoom(0.8,  self.outerEl.clientWidth / 2, self.outerEl.clientHeight / 2);
            });
            U.on('zoom-reset-btn', 'click', function () { self._resetZoom(); });

            this.outerEl.addEventListener('wheel', function (e) {
                if (!self.annotationMode) return;
                e.preventDefault();
                var r  = self.outerEl.getBoundingClientRect();
                self._applyZoom(e.deltaY < 0 ? 1.1 : 0.909, e.clientX - r.left, e.clientY - r.top);
            }, { passive: false });
        };

        VideoAnnotator.prototype._setTool = function (tool) {
            if (tool !== 'point' && this.currentTool !== 'point') this._lastNonPointTool = tool;
            if (tool === 'point' && this.currentTool !== 'point') this._lastNonPointTool = this.currentTool || 'brush';
            this.currentTool = tool;
            this._cancelPolygon();
            this.toolbarEl.querySelectorAll('[data-tool]').forEach(function (btn) {
                btn.classList.toggle('active', btn.getAttribute('data-tool') === tool);
            });
            var brushSizeWrap = this.brushSizeInput && this.brushSizeInput.closest
                ? this.brushSizeInput.closest('.d-flex') : null;
            if (brushSizeWrap) brushSizeWrap.style.opacity = (tool === 'pan') ? '0.3' : '';
            if (typeof this._setMagicPointActive === 'function') this._setMagicPointActive(tool === 'point');
            this._updateCursor();
        };

        VideoAnnotator.prototype._inPanMode = function () {
            return this.currentTool === 'pan' || this._spaceDown || this._ctrlDown || this._middleMouseDown;
        };

        VideoAnnotator.prototype._updateCursor = function () {
            if (!this._container) return;
            if (this._inPanMode()) {
                this._container.style.cursor = this._isPanning ? 'grabbing' : 'grab';
            } else if (this.currentTool === 'point') {
                this._container.style.cursor = 'crosshair';
            } else {
                this._container.style.cursor = (this.currentTool === 'eraser') ? 'cell' : 'crosshair';
            }
        };

        VideoAnnotator.prototype._bindDrawing = function () {
            var self = this;
            var L    = this._L;

            L.stageDown = function (evt) {
                if (!self.annotationMode) return;
                var domEvt = evt && evt.evt ? evt.evt : null;
                if (domEvt && typeof domEvt.button === 'number' && domEvt.button !== 0) return;
                if (self.currentTool === 'point') return;
                if (self._inPanMode()) return;
                if (self._draggingVertex) return;
                if (evt.target && evt.target !== self.stage &&
                    typeof evt.target.draggable === 'function' && evt.target.draggable()) return;

                var pos = self._pointerPos();
                if (!pos) return;

                if (self.currentTool === 'polygon') {
                    self._polyAddVertex(pos);
                    return;
                }
                self._startDrawing(pos);
            };
            self.stage.on('mousedown touchstart', L.stageDown);

            L.stageMove = function () {
                if (self._isPanning || self._draggingVertex) return;
                var pos = self._pointerPos();
                if (!pos) return;

                if (self._cursorCircle) {
                    var isEraser = (self.currentTool === 'eraser');
                    self._cursorCircle.visible(isEraser);
                    if (isEraser) {
                        self._cursorCircle.position(pos);
                        self.cursorLayer.draw();
                    }
                }

                if (self.currentTool === 'polygon') {
                    self._polyUpdateGuide(pos);
                    return;
                }
                self._continueDrawing(pos);
            };
            self.stage.on('mousemove touchmove', L.stageMove);

            L.stageLeave = function () {
                self._finishDrawing();
                if (self._cursorCircle) {
                    self._cursorCircle.visible(false);
                    self.cursorLayer.draw();
                }
            };
            self.stage.on('mouseleave', L.stageLeave);

            L.outerDown = function (e) {
                var isMiddleButton = e.button === 1;
                if (isMiddleButton) {
                    e.preventDefault();
                    self._middleMouseDown = true;
                } else if (e.button !== 0 || !self._inPanMode()) {
                    return;
                }
                self._isPanning    = true;
                self._panStartX    = e.clientX;
                self._panStartY    = e.clientY;
                self._panStartPanX = self._panX;
                self._panStartPanY = self._panY;
                self._updateCursor();
            };
            self.outerEl.addEventListener('mousedown', L.outerDown);

            L.winMove = function (e) {
                if (!self._isPanning) return;
                self._panX = self._panStartPanX + (e.clientX - self._panStartX);
                self._panY = self._panStartPanY + (e.clientY - self._panStartY);
                self._applyTransform();
            };
            window.addEventListener('mousemove', L.winMove);

            L.winUp = function (e) {
                if (e && e.button === 1) {
                    self._middleMouseDown = false;
                }
                if (self._isPanning) {
                    if (self._middleMouseDown) return;
                    self._isPanning = false;
                    self._updateCursor();
                }
                self._finishDrawing();
            };
            window.addEventListener('mouseup', L.winUp);

            self._cursorCircle = new Konva.Circle({
                radius: self.brushSize / 2,
                stroke: '#fff', strokeWidth: 1, dash: [4, 2],
                listening: false, visible: false,
            });
            self.cursorLayer.add(self._cursorCircle);
            self.cursorLayer.draw();
            self._updateCursor();
        };

        VideoAnnotator.prototype._unbindDrawing = function () {
            var L = this._L;

            if (L.stageDown)  this.stage.off('mousedown touchstart', L.stageDown);
            if (L.stageMove)  this.stage.off('mousemove touchmove',  L.stageMove);
            if (L.stageLeave) this.stage.off('mouseleave',           L.stageLeave);
            if (L.outerDown)  this.outerEl.removeEventListener('mousedown', L.outerDown);
            if (L.winMove)    window.removeEventListener('mousemove', L.winMove);
            if (L.winUp)      window.removeEventListener('mouseup',   L.winUp);

            this._middleMouseDown = false;
            this._isPanning = false;
            this._L = {};

            if (this._cursorCircle) {
                this._cursorCircle.destroy();
                this._cursorCircle = null;
                this.cursorLayer.draw();
            }
        };

        VideoAnnotator.prototype._bindResize = function () {
            var self = this;
            var resizeTimeout = null;
            this._resizeListener = function () {
                clearTimeout(resizeTimeout);
                resizeTimeout = setTimeout(function () {
                    self._syncStageSize();
                }, 150);
            };
            window.addEventListener('resize', this._resizeListener);
        };

        VideoAnnotator.prototype._unbindResize = function () {
            if (this._resizeListener) {
                window.removeEventListener('resize', this._resizeListener);
                this._resizeListener = null;
            }
        };

        VideoAnnotator.prototype._startDrawing = function (pos) {
            var region = this._activeRegion();
            if (!region) return;
            this._drawing = true;
            var isEraser = (this.currentTool === 'eraser');
            var line = new Konva.Line({
                stroke: isEraser ? 'rgba(0,0,0,1)' : region.color,
                strokeWidth: this.brushSize,
                globalCompositeOperation: isEraser ? 'destination-out' : 'source-over',
                lineCap: 'round', lineJoin: 'round',
                perfectDrawEnabled: !isEraser,
                points: [pos.x, pos.y, pos.x, pos.y],
                listening: false,
            });
            region.layer.add(line);
            this._currentLine = line;
        };

        VideoAnnotator.prototype._continueDrawing = function (pos) {
            if (!this._drawing || !this._currentLine) return;
            var pts = this._currentLine.points();
            pts.push(pos.x, pos.y);
            this._currentLine.points(pts);
            this._currentLine.getLayer().draw();
        };

        VideoAnnotator.prototype._finishDrawing = function () {
            if (this._drawing && this._currentLine) {
                this._registerShape(this.currentTool, this._currentLine);
            }
            this._drawing     = false;
            this._currentLine = null;
        };

        VideoAnnotator.prototype._polyAddVertex = function (pos) {
            var region = this._activeRegion();
            if (!region) return;
            this._polyPoints.push(pos.x, pos.y);

            if (this._polyPoints.length === 2 && this.polygonHintEl) {
                this.polygonHintEl.classList.remove('d-none');
            }

            if (!this._polyLine) {
                this._polyLine = new Konva.Line({
                    points: this._polyPoints.slice(),
                    stroke: region.color, strokeWidth: 2, dash: [6, 3],
                    closed: false, listening: false,
                });
                region.layer.add(this._polyLine);

                this._polyGuide = new Konva.Line({
                    points: [pos.x, pos.y, pos.x, pos.y],
                    stroke: region.color, strokeWidth: 1, dash: [4, 2],
                    opacity: 0.5, listening: false,
                });
                region.layer.add(this._polyGuide);
            } else {
                this._polyLine.points(this._polyPoints.slice());
            }

            var dot = new Konva.Circle({ x: pos.x, y: pos.y, radius: 4, fill: region.color, listening: false });
            region.layer.add(dot);
            this._polyDots.push(dot);
            region.layer.draw();
        };

        VideoAnnotator.prototype._polyUpdateGuide = function (pos) {
            if (!this._polyGuide || this._polyPoints.length < 2) return;
            var last = this._polyPoints.slice(-2);
            this._polyGuide.points([last[0], last[1], pos.x, pos.y]);
            var region = this._activeRegion();
            if (region) region.layer.draw();
        };

        VideoAnnotator.prototype._polyClose = function () {
            if (this._polyPoints.length < 6) { this._cancelPolygon(); return; }
            var region = this._activeRegion();
            if (!region) { this._cancelPolygon(); return; }

            if (this._polyLine)  this._polyLine.destroy();
            if (this._polyGuide) this._polyGuide.destroy();
            this._polyDots.forEach(function (d) { d.destroy(); });

            var filled = new Konva.Line({
                points: this._polyPoints.slice(),
                fill: region.color + '55', stroke: region.color, strokeWidth: 2,
                closed: true, listening: false,
            });
            region.layer.add(filled);
            region.layer.draw();

            this._registerShape('polygon', filled);
            this._resetPolyState();
        };

        VideoAnnotator.prototype._cancelPolygon = function () {
            var region = this._activeRegion();
            if (this._polyLine)  this._polyLine.destroy();
            if (this._polyGuide) this._polyGuide.destroy();
            this._polyDots.forEach(function (d) { d.destroy(); });
            if (region) region.layer.draw();
            this._resetPolyState();
        };

        VideoAnnotator.prototype._resetPolyState = function () {
            this._polyPoints = [];
            this._polyLine   = null;
            this._polyGuide  = null;
            this._polyDots   = [];
            if (this.polygonHintEl) this.polygonHintEl.classList.add('d-none');
        };
    };
})();
