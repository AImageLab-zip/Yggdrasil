(function () {
    'use strict';

    function _jsonOrNull(response) {
        return (response && response.ok) ? response.json() : null;
    }

    function _normalizePromptPoints(rawPromptPoints) {
        if (!Array.isArray(rawPromptPoints)) return [];
        var normalized = [];
        for (var i = 0; i < rawPromptPoints.length; i++) {
            var rawPoint = rawPromptPoints[i];
            if (!rawPoint || typeof rawPoint !== 'object') continue;
            var x = Number(rawPoint.x);
            var y = Number(rawPoint.y);
            if (!isFinite(x) || !isFinite(y) || x < 0 || x > 1 || y < 0 || y > 1) continue;
            normalized.push({ x: x, y: y, label: Number(rawPoint.label) === 0 ? 0 : 1 });
        }
        return normalized;
    }

    window.LaparoscopyAnnotatorMixins = window.LaparoscopyAnnotatorMixins || {};

    window.LaparoscopyAnnotatorMixins.api = function (VideoAnnotator) {
        VideoAnnotator.prototype._jsonHeaders = function () {
            return { 'Content-Type': 'application/json', 'X-CSRFToken': this.csrfToken };
        };

        VideoAnnotator.prototype._csrfHeaders = function () {
            return { 'X-CSRFToken': this.csrfToken };
        };

        VideoAnnotator.prototype._requestJson = function (url, options) {
            return fetch(url, options)
                .then(_jsonOrNull)
                .catch(function () { return null; });
        };

        VideoAnnotator.prototype._requestVoid = function (url, options) {
            return fetch(url, options)
                .then(function () { return null; })
                .catch(function () { return null; });
        };

        VideoAnnotator.prototype._persistRegionType = function (region) {
            if (!this.isAdmin || !region) return Promise.resolve(null);
            return this._requestJson('/laparoscopy/api/region-types/', {
                method: 'POST',
                headers: this._jsonHeaders(),
                body: JSON.stringify({ name: region.name, color: region.color }),
            })
            .then(function (data) {
                if (data && data.id) region.dbId = data.id;
                return data;
            });
        };

        VideoAnnotator.prototype._loadRegionTypes = function () {
            var self = this;
            return this._requestJson('/laparoscopy/api/region-types/')
                .then(function (data) {
                    if (!data || !data.types) return;

                    if (data.types.length === 0) {
                        if (self.isAdmin) {
                            var defaultRegion = self.regions[0] || null;
                            if (defaultRegion && !defaultRegion.dbId) {
                                return self._persistRegionType(defaultRegion);
                            }
                        }
                        return;
                    }

                    self.regions.forEach(function (r) { r.layer.destroy(); });
                    self.regions = [];
                    self.activeRegionId = null;
                    data.types.forEach(function (t) {
                        self.addRegion(t.name, t.color, t.id);
                    });
                });
        };

        VideoAnnotator.prototype._regionByDbId = function (dbId) {
            return this.regions.find(function (r) { return r.dbId === dbId; }) || null;
        };

        VideoAnnotator.prototype._annotationCollectionUrl = function () {
            if (!this.patientId) return null;
            return '/laparoscopy/api/patient/' + this.patientId + '/annotations/';
        };

        VideoAnnotator.prototype._annotationDetailUrl = function (annotationId) {
            return '/laparoscopy/api/annotations/' + annotationId + '/';
        };

        VideoAnnotator.prototype._shapeToAnnotationPayload = function (shape) {
            if (!shape) return null;
            var region = this.regions.find(function (r) { return r.id === shape.regionId; });
            if (!region || !region.dbId) return null;
            if (!shape.konvaNode || typeof shape.konvaNode.points !== 'function') return null;

            var points = shape.konvaNode.points().slice();
            if (!points.length) return null;
            var strokeWidth = (typeof shape.konvaNode.strokeWidth === 'function')
                ? shape.konvaNode.strokeWidth()
                : this.brushSize;

            return {
                region_type_id: region.dbId,
                tool: shape.type,
                frame_time: shape.frameTime,
                points: points,
                stroke_width: strokeWidth,
                prompt_points: _normalizePromptPoints(shape.promptPoints),
            };
        };

        VideoAnnotator.prototype._deleteAnnotationById = function (annotationId) {
            if (!annotationId) return;
            this._requestVoid(this._annotationDetailUrl(annotationId), {
                method: 'DELETE',
                headers: this._csrfHeaders(),
            });
        };

        VideoAnnotator.prototype._createAnnotationForShape = function (shape) {
            var listUrl = this._annotationCollectionUrl();
            if (!listUrl || !shape || shape.dbId) return;

            var payload = this._shapeToAnnotationPayload(shape);
            if (!payload) return;

            var self = this;
            this._requestJson(listUrl, {
                method: 'POST',
                headers: this._jsonHeaders(),
                body: JSON.stringify(payload),
            })
            .then(function (data) {
                if (!data || !data.id) return;

                shape.dbId = data.id;
                var stillExists = self.shapes.some(function (s) { return s.id === shape.id; });
                if (!stillExists) {
                    self._deleteAnnotationById(data.id);
                    return;
                }

                var latestPayload = self._shapeToAnnotationPayload(shape) || payload;
                self._patchAnnotationForShape(shape, {
                    region_type_id: latestPayload.region_type_id,
                    points: shape.konvaNode.points().slice(),
                    stroke_width: latestPayload.stroke_width,
                    frame_time: shape.frameTime,
                    prompt_points: latestPayload.prompt_points,
                });
            });
        };

        VideoAnnotator.prototype._patchAnnotationForShape = function (shape, patchData) {
            if (!shape || !shape.dbId || !patchData) return;

            this._requestJson(this._annotationDetailUrl(shape.dbId), {
                method: 'PATCH',
                headers: this._jsonHeaders(),
                body: JSON.stringify(patchData),
            })
            .then(function () {});
        };

        VideoAnnotator.prototype._persistShapeGeometry = function (shape) {
            if (!shape || !shape.konvaNode || typeof shape.konvaNode.points !== 'function') return;
            this._patchAnnotationForShape(shape, {
                points: shape.konvaNode.points().slice(),
                stroke_width: (typeof shape.konvaNode.strokeWidth === 'function')
                    ? shape.konvaNode.strokeWidth()
                    : this.brushSize,
            });
        };

        VideoAnnotator.prototype._hydrateRegionAnnotation = function (annotation) {
            if (!annotation || !annotation.id) return;

            var region = this._regionByDbId(annotation.region_type_id);
            if (!region) return;

            var points = Array.isArray(annotation.points) ? annotation.points.slice() : [];
            if (points.length < 4 || (points.length % 2) !== 0) return;

            var tool = annotation.tool;
            if (['brush', 'eraser', 'polygon'].indexOf(tool) === -1) return;

            var shapeNode;
            if (tool === 'polygon') {
                if (points.length < 6) return;
                shapeNode = new Konva.Line({
                    points: points,
                    fill: region.color + '55',
                    stroke: region.color,
                    strokeWidth: 2,
                    closed: true,
                    listening: false,
                });
            } else {
                var isEraser = (tool === 'eraser');
                shapeNode = new Konva.Line({
                    points: points,
                    stroke: isEraser ? 'rgba(0,0,0,1)' : region.color,
                    strokeWidth: annotation.stroke_width || this.brushSize,
                    globalCompositeOperation: isEraser ? 'destination-out' : 'source-over',
                    lineCap: 'round',
                    lineJoin: 'round',
                    perfectDrawEnabled: !isEraser,
                    listening: false,
                });
            }

            region.layer.add(shapeNode);
            region.layer.draw();

            var frameTime = parseFloat(annotation.frame_time);
            if (!isFinite(frameTime) || frameTime < 0) frameTime = 0;

            var shape = this._registerShape(tool, shapeNode, {
                id: 'shape-db-' + annotation.id,
                dbId: annotation.id,
                regionId: region.id,
                frameTime: frameTime,
                promptPoints: _normalizePromptPoints(annotation.prompt_points),
                skipPersist: true,
            });
            if (shape && Array.isArray(annotation.prompt_points) && annotation.prompt_points.length) {
                shape._isDbAutoMask = true;
            }
        };

        VideoAnnotator.prototype._loadRegionAnnotations = function () {
            var listUrl = this._annotationCollectionUrl();
            if (!listUrl) return;

            var self = this;
            this._requestJson(listUrl)
                .then(function (data) {
                    if (!data || !Array.isArray(data.annotations)) return;

                    data.annotations.forEach(function (annotation) {
                        var alreadyLoaded = self.shapes.some(function (s) {
                            return s.dbId === annotation.id;
                        });
                        if (!alreadyLoaded) self._hydrateRegionAnnotation(annotation);
                    });

                    if (typeof self._restoreMagicPromptsFromAnnotations === 'function') {
                        self._restoreMagicPromptsFromAnnotations(data.annotations);
                    }

                    self._updateShapeVisibility();
                    self._renderShapesList();
                });
        };

        VideoAnnotator.prototype._persistTimelineClass = function (cls) {
            if (!this.isAdmin || !cls) return Promise.resolve(null);

            return this._requestJson('/laparoscopy/api/quadrant-types/', {
                method: 'POST',
                headers: this._jsonHeaders(),
                body: JSON.stringify({ name: cls.name, color: cls.color }),
            })
            .then(function (data) {
                if (data && data.id) cls.dbId = data.id;
                return data;
            });
        };

        VideoAnnotator.prototype._loadQuadrantTypes = function () {
            var self = this;
            return this._requestJson('/laparoscopy/api/quadrant-types/')
                .then(function (data) {
                    if (!data || !data.types) return;

                    if (data.types.length === 0) {
                        if (self.isAdmin) {
                            var defaultClass = self.timelineClasses[0] || null;
                            if (defaultClass && !defaultClass.dbId) {
                                return self._persistTimelineClass(defaultClass);
                            }
                        }
                        return;
                    }

                    self.timelineClasses = [];
                    self.timelinePins = [];
                    self.activeTimelineClassId = null;
                    self._editingTimelineClassId = null;
                    data.types.forEach(function (t) {
                        self.addTimelineClass(t.name, t.color, t.id);
                    });
                    self._refreshTimelineVisuals();
                });
        };

        VideoAnnotator.prototype._timelineMarkersCollectionUrl = function () {
            if (!this.patientId) return null;
            return '/laparoscopy/api/patient/' + this.patientId + '/quadrant-markers/';
        };

        VideoAnnotator.prototype._timelinePinsToPayload = function () {
            var self = this;
            var markers = this.timelinePins
                .map(function (pin) {
                    var cls = self._timelineClassById(pin.classId);
                    if (!cls || !cls.dbId) return null;

                    var payload = {
                        quadrant_type_id: cls.dbId,
                        time_ms: Math.max(0, Math.round(self._clampTimelineTime(pin.time) * 1000)),
                    };
                    if (pin.dbId) payload.id = pin.dbId;
                    return payload;
                })
                .filter(function (item) { return !!item; });

            return { markers: markers };
        };

        VideoAnnotator.prototype._applyTimelineMarkers = function (data) {
            if (!data || !Array.isArray(data.markers)) return;

            var selectedPin = this._selectedTimelinePin();
            var selectedDbId = selectedPin && selectedPin.dbId ? selectedPin.dbId : null;

            var previousByDbId = {};
            this.timelinePins.forEach(function (pin) {
                if (pin.dbId) previousByDbId[pin.dbId] = pin;
            });

            var self = this;
            var nextPins = [];
            data.markers.forEach(function (marker, idx) {
                var cls = self.timelineClasses.find(function (c) {
                    return c.dbId === marker.quadrant_type_id;
                });
                if (!cls) return;

                var markerId = parseInt(marker.id, 10);
                if (!isFinite(markerId) || markerId <= 0) return;

                var timeMs = parseInt(marker.time_ms, 10);
                if (!isFinite(timeMs) || timeMs < 0) timeMs = 0;

                var existing = previousByDbId[markerId] || null;
                nextPins.push({
                    id: existing ? existing.id : ('pin-db-' + markerId + '-' + idx),
                    dbId: markerId,
                    time: timeMs / 1000,
                    classId: cls.id,
                });
            });

            this.timelinePins = nextPins;

            if (selectedDbId) {
                var selectedAfter = this.timelinePins.find(function (pin) {
                    return pin.dbId === selectedDbId;
                });
                this._selectedTimelinePinId = selectedAfter ? selectedAfter.id : null;
            } else {
                this._selectedTimelinePinId = null;
            }

            this._refreshTimelineVisuals();
        };

        VideoAnnotator.prototype._loadTimelineMarkers = function () {
            var url = this._timelineMarkersCollectionUrl();
            if (!url) return Promise.resolve(null);

            var self = this;
            return this._requestJson(url)
                .then(function (data) {
                    self._applyTimelineMarkers(data);
                    return data;
                });
        };

        VideoAnnotator.prototype._scheduleTimelineMarkersSync = function () {
            var url = this._timelineMarkersCollectionUrl();
            if (!url) return;

            var self = this;
            if (this._timelineSyncTimer) clearTimeout(this._timelineSyncTimer);
            this._timelineSyncTimer = setTimeout(function () {
                self._timelineSyncTimer = null;
                self._flushTimelineMarkersSync();
            }, 250);
        };

        VideoAnnotator.prototype._flushTimelineMarkersSync = function () {
            var url = this._timelineMarkersCollectionUrl();
            if (!url) return;

            if (this._timelineSyncInFlight) {
                this._timelineSyncQueued = true;
                return;
            }

            this._timelineSyncInFlight = true;
            var payload = this._timelinePinsToPayload();
            var self = this;

            this._requestJson(url, {
                method: 'PUT',
                headers: this._jsonHeaders(),
                body: JSON.stringify(payload),
            })
            .then(function (data) {
                if (data) self._applyTimelineMarkers(data);
            })
            .finally(function () {
                self._timelineSyncInFlight = false;
                if (self._timelineSyncQueued) {
                    self._timelineSyncQueued = false;
                    self._flushTimelineMarkersSync();
                }
            });
        };
    };
})();
