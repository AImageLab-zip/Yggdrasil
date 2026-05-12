(function () {
    'use strict';

    var TIMELINE_PIN_MERGE_TOLERANCE = 0.050;

    var U = window.LaparoscopyAnnotatorUtils;

    window.LaparoscopyAnnotatorMixins = window.LaparoscopyAnnotatorMixins || {};

    window.LaparoscopyAnnotatorMixins.timeline = function (VideoAnnotator) {
        VideoAnnotator.prototype._refreshTimelineVisuals = function () {
            this._renderTimelinePins();
            this._renderTimelineSegments();
            this._updateTemporalTimelineUI();
        };

        VideoAnnotator.prototype._refreshTimelineWithClasses = function () {
            this._renderTimelineClassList();
            this._renderTimelineAdminClassList();
            this._refreshTimelineVisuals();
        };

        VideoAnnotator.prototype._timelineEnabled = function () {
            return !!(
                this.timelineTrackWrapEl &&
                this.timelineSegmentsLayerEl &&
                this.timelinePinsLayerEl &&
                this.timelinePlayheadEl
            );
        };

        VideoAnnotator.prototype._videoDuration = function () {
            var d = this.videoEl ? this.videoEl.duration : 0;
            return (isFinite(d) && d > 0) ? d : 0;
        };

        VideoAnnotator.prototype._clampTimelineTime = function (t) {
            var duration = this._videoDuration();
            if (!isFinite(t) || t < 0) return 0;
            if (duration > 0) return Math.min(duration, t);
            return t;
        };

        VideoAnnotator.prototype._timelineTimeToPct = function (t) {
            var duration = this._videoDuration();
            if (!duration) return 0;
            var ratio = this._clampTimelineTime(t) / duration;
            return Math.max(0, Math.min(1, ratio));
        };

        VideoAnnotator.prototype._timelineTimeFromClientX = function (clientX) {
            if (!this.timelineTrackWrapEl) return 0;
            var rect = this.timelineTrackWrapEl.getBoundingClientRect();
            var left = rect.left + 8;
            var width = Math.max(1, rect.width - 16);
            var ratio = (clientX - left) / width;
            ratio = Math.max(0, Math.min(1, ratio));
            return this._clampTimelineTime(ratio * this._videoDuration());
        };

        VideoAnnotator.prototype._timelineClassById = function (classId) {
            return this.timelineClasses.find(function (c) { return c.id === classId; }) || null;
        };

        VideoAnnotator.prototype._activeTimelineClass = function () {
            var active = this._timelineClassById(this.activeTimelineClassId);
            if (active && active.visible) return active;

            var visible = this.timelineClasses.find(function (c) { return c.visible; }) || null;
            if (visible) {
                this.activeTimelineClassId = visible.id;
                return visible;
            }

            if (this.timelineClasses[0]) {
                this.activeTimelineClassId = this.timelineClasses[0].id;
                return this.timelineClasses[0];
            }
            return null;
        };

        VideoAnnotator.prototype._timelineClassColor = function (classObj) {
            return (classObj && classObj.color) ? classObj.color : '#6c757d';
        };

        VideoAnnotator.prototype._addDefaultTimelineClass = function () {
            if (this.timelineClasses.length === 0) {
                this.addTimelineClass('1');
            }
        };

        VideoAnnotator.prototype.addTimelineClass = function (name, color, dbId) {
            var actualColor;
            if (color) {
                actualColor = color;
            } else {
                actualColor = U.PALETTE[this._timelinePaletteIdx % U.PALETTE.length];
                this._timelinePaletteIdx++;
            }

            var id = 'timeline-class-' + Date.now() + '-' + Math.random().toString(36).slice(2);
            var cls = { id: id, dbId: dbId || null, name: name, color: actualColor, visible: true };
            this.timelineClasses.push(cls);

            if (!this.activeTimelineClassId) this.activeTimelineClassId = id;

            this._refreshTimelineWithClasses();
            return cls;
        };

        VideoAnnotator.prototype._startTimelineClassEdit = function (classId) {
            if (!this.isAdmin) return;
            this.activeTimelineClassId = classId;
            this._editingTimelineClassId = classId;
            this._refreshTimelineWithClasses();
        };

        VideoAnnotator.prototype._commitTimelineClassEdit = function (classId, nextValue) {
            if (!this.isAdmin) return;
            var cls = this._timelineClassById(classId);
            if (!cls) return;
            var trimmed = (nextValue || '').trim();
            if (trimmed) cls.name = trimmed;
            this._editingTimelineClassId = null;
            this._refreshTimelineWithClasses();
            if (this.isAdmin && cls.dbId && trimmed) {
                this._requestVoid('/laparoscopy/api/quadrant-types/' + cls.dbId + '/', {
                    method: 'PATCH',
                    headers: this._jsonHeaders(),
                    body: JSON.stringify({ name: trimmed }),
                });
            }
        };

        VideoAnnotator.prototype._cancelTimelineClassEdit = function () {
            this._editingTimelineClassId = null;
            this._refreshTimelineWithClasses();
        };

        VideoAnnotator.prototype._changeTimelineClassColor = function (classId, newColor) {
            var cls = this._timelineClassById(classId);
            if (!cls) return;
            cls.color = newColor;
            this._refreshTimelineWithClasses();
            if (cls.dbId) {
                this._requestVoid('/laparoscopy/api/quadrant-types/' + cls.dbId + '/', {
                    method: 'PATCH',
                    headers: this._jsonHeaders(),
                    body: JSON.stringify({ color: newColor }),
                });
            }
        };

        VideoAnnotator.prototype._removeTimelineClass = function (classId) {
            if (!this.isAdmin) return;
            if (this.timelineClasses.length <= 1) return;

            var target = this._timelineClassById(classId);
            if (!target) return;

            var replacement = this.timelineClasses.find(function (c) { return c.id !== classId; }) || null;
            if (!replacement) return;

            this.timelinePins.forEach(function (pin) {
                if (pin.classId === classId) pin.classId = replacement.id;
            });

            var deletedDbId = target.dbId;
            this.timelineClasses = this.timelineClasses.filter(function (c) { return c.id !== classId; });
            if (this.activeTimelineClassId === classId) this.activeTimelineClassId = replacement.id;
            if (this._editingTimelineClassId === classId) this._editingTimelineClassId = null;

            this._closeTimelinePinMenu();
            this._refreshTimelineWithClasses();
            this._scheduleTimelineMarkersSync();

            if (this.isAdmin && deletedDbId && replacement.dbId) {
                this._requestVoid('/laparoscopy/api/quadrant-types/' + deletedDbId + '/', {
                    method: 'DELETE',
                    headers: this._jsonHeaders(),
                    body: JSON.stringify({ replacement_id: replacement.dbId }),
                });
            }
        };

        VideoAnnotator.prototype._renderTimelineClassList = function () {
            var self = this;
            if (!this.timelineClassListEl) return;
            this.timelineClassListEl.innerHTML = '';

            this.timelineClasses.forEach(function (cls) {
                var li = document.createElement('li');
                var btn = document.createElement('button');
                btn.type = 'button';
                btn.className = 'dropdown-item d-flex align-items-center gap-2' +
                    (cls.id === self.activeTimelineClassId ? ' active' : '');

                var dot = document.createElement('span');
                dot.style.cssText = 'display:inline-block;width:12px;height:12px;border-radius:50%;flex-shrink:0;background:' + cls.color + ';cursor:pointer;';
                dot.title = 'Change color';
                dot.addEventListener('click', function (e) {
                    e.stopPropagation();
                    U.openColorPicker(cls.color, function (nextColor) {
                        self._changeTimelineClassColor(cls.id, nextColor);
                    });
                });
                btn.appendChild(dot);

                var nameLabel = document.createElement('span');
                nameLabel.className = 'small flex-grow-1';
                nameLabel.textContent = cls.name;
                btn.appendChild(nameLabel);

                btn.addEventListener('click', function () {
                    self.activeTimelineClassId = cls.id;
                    self._refreshTimelineWithClasses();
                    self._updateTemporalTimelineUI();
                });

                li.appendChild(btn);
                self.timelineClassListEl.appendChild(li);
            });

            var activeCls = this._timelineClassById(this.activeTimelineClassId);
            var swatchEl = document.getElementById('timeline-class-active-swatch');
            var labelEl  = document.getElementById('timeline-class-active-label');
            if (activeCls) {
                if (swatchEl) swatchEl.style.background = activeCls.color;
                if (labelEl)  labelEl.textContent = activeCls.name;
            }
        };

        VideoAnnotator.prototype._renderTimelineAdminClassList = function () {
            var self = this;
            if (!this.timelineClassAdminListEl) return;

            this.timelineClassAdminListEl.innerHTML = '';

            this.timelineClasses.forEach(function (cls) {
                var chip = document.createElement('div');
                chip.className = 'quadrant-chip' + (cls.id === self.activeTimelineClassId ? ' is-active' : '');
                chip.style.setProperty('--quadrant-color', cls.color);
                chip.setAttribute('role', 'button');
                chip.setAttribute('tabindex', '0');
                chip.title = 'Use ' + cls.name + ' for new markers';
                chip.addEventListener('click', function () {
                    if (self._editingTimelineClassId === cls.id) return;
                    self.activeTimelineClassId = cls.id;
                    self._refreshTimelineWithClasses();
                });
                chip.addEventListener('keydown', function (e) {
                    if (e.key !== 'Enter' && e.key !== ' ') return;
                    e.preventDefault();
                    if (self._editingTimelineClassId === cls.id) return;
                    self.activeTimelineClassId = cls.id;
                    self._refreshTimelineWithClasses();
                });

                var dot = document.createElement('span');
                dot.className = 'quadrant-chip-dot';
                dot.title = 'Change color';
                dot.addEventListener('click', function (e) {
                    e.stopPropagation();
                    U.openColorPicker(cls.color, function (nextColor) {
                        self._changeTimelineClassColor(cls.id, nextColor);
                    });
                });
                chip.appendChild(dot);

                if (self._editingTimelineClassId === cls.id) {
                    var nameInput = document.createElement('input');
                    nameInput.type = 'text';
                    nameInput.value = cls.name;
                    nameInput.className = 'form-control form-control-sm quadrant-chip-input';
                    nameInput.setAttribute('data-timeline-class-edit', cls.id);
                    nameInput.addEventListener('click', function (e) { e.stopPropagation(); });
                    nameInput.addEventListener('keydown', function (e) {
                        if (e.key === 'Enter') { e.preventDefault(); self._commitTimelineClassEdit(cls.id, this.value); }
                        if (e.key === 'Escape') { e.preventDefault(); self._cancelTimelineClassEdit(); }
                    });
                    chip.appendChild(nameInput);
                } else {
                    var nameLabel = document.createElement('span');
                    nameLabel.className = 'quadrant-chip-name fw-semibold';
                    nameLabel.textContent = cls.name;
                    chip.appendChild(nameLabel);
                }

                var actions = document.createElement('div');
                actions.className = 'quadrant-chip-actions';
                chip.appendChild(actions);

                if (self._editingTimelineClassId === cls.id) {
                    var saveBtn = document.createElement('button');
                    saveBtn.className = 'quadrant-chip-btn text-success';
                    saveBtn.innerHTML = '<i class="fas fa-check"></i>';
                    saveBtn.title = 'Save';
                    saveBtn.addEventListener('click', function (e) {
                        e.stopPropagation();
                        var inp = self.timelineClassAdminListEl.querySelector('input[data-timeline-class-edit="' + cls.id + '"]');
                        self._commitTimelineClassEdit(cls.id, inp ? inp.value : cls.name);
                    });
                    actions.appendChild(saveBtn);

                    var cancelBtn = document.createElement('button');
                    cancelBtn.className = 'quadrant-chip-btn';
                    cancelBtn.innerHTML = '<i class="fas fa-times"></i>';
                    cancelBtn.title = 'Cancel';
                    cancelBtn.addEventListener('click', function (e) {
                        e.stopPropagation();
                        self._cancelTimelineClassEdit();
                    });
                    actions.appendChild(cancelBtn);
                } else {
                    var editBtn = document.createElement('button');
                    editBtn.className = 'quadrant-chip-btn';
                    editBtn.innerHTML = '<i class="fas fa-pen"></i>';
                    editBtn.title = 'Rename';
                    editBtn.addEventListener('click', function (e) {
                        e.stopPropagation();
                        self._startTimelineClassEdit(cls.id);
                    });
                    actions.appendChild(editBtn);
                }

                if (self.timelineClasses.length > 1) {
                    var delBtn = document.createElement('button');
                    delBtn.className = 'quadrant-chip-btn text-danger';
                    delBtn.innerHTML = '<i class="fas fa-times"></i>';
                    delBtn.title = 'Remove quadrant';
                    delBtn.addEventListener('click', function (e) {
                        e.stopPropagation();
                        self._removeTimelineClass(cls.id);
                    });
                    actions.appendChild(delBtn);
                }

                self.timelineClassAdminListEl.appendChild(chip);
            });

            if (this._editingTimelineClassId) {
                var activeInput = this.timelineClassAdminListEl.querySelector(
                    'input[data-timeline-class-edit="' + this._editingTimelineClassId + '"]'
                );
                if (activeInput) {
                    activeInput.focus();
                    activeInput.select();
                }
            }
        };

        VideoAnnotator.prototype._selectedTimelinePin = function () {
            var selectedId = this._selectedTimelinePinId;
            return this.timelinePins.find(function (pin) { return pin.id === selectedId; }) || null;
        };

        VideoAnnotator.prototype._sortTimelinePins = function () {
            this.timelinePins.sort(function (a, b) { return a.time - b.time; });
        };

        VideoAnnotator.prototype._compactTimelinePins = function () {
            this._sortTimelinePins();
            var compacted = [];
            for (var i = 0; i < this.timelinePins.length; i++) {
                var pin = this.timelinePins[i];
                var prev = compacted[compacted.length - 1] || null;
                if (prev && prev.classId === pin.classId) {
                    if (this._selectedTimelinePinId === pin.id) {
                        this._selectedTimelinePinId = prev.id;
                    }
                    continue;
                }
                compacted.push(pin);
            }
            this.timelinePins = compacted;
        };

        VideoAnnotator.prototype._timelineClassAt = function (timeSeconds) {
            var t = this._clampTimelineTime(timeSeconds);
            var activeClass = null;

            for (var i = 0; i < this.timelinePins.length; i++) {
                var pin = this.timelinePins[i];
                var cls = this._timelineClassById(pin.classId);
                if (!cls || !cls.visible) continue;
                if (pin.time <= t + TIMELINE_PIN_MERGE_TOLERANCE) activeClass = cls;
                else break;
            }

            return activeClass;
        };

        VideoAnnotator.prototype._renderTimelineSegments = function () {
            if (!this.timelineSegmentsLayerEl) return;
            this.timelineSegmentsLayerEl.innerHTML = '';

            var duration = this._videoDuration();
            if (!duration) return;

            var sorted = this.timelinePins.slice().sort(function (a, b) { return a.time - b.time; });
            var cursor = 0;
            var activeClass = null;

            var self = this;
            function appendSegment(start, end, cls) {
                if (end <= start) return;
                var segment = document.createElement('div');
                segment.className = 'timeline-segment';
                segment.style.left = ((start / duration) * 100).toFixed(4) + '%';
                segment.style.width = (((end - start) / duration) * 100).toFixed(4) + '%';
                segment.style.setProperty('--segment-color', self._timelineClassColor(cls));
                if (!cls) segment.style.opacity = '0.35';
                self.timelineSegmentsLayerEl.appendChild(segment);
            }

            for (var i = 0; i < sorted.length; i++) {
                var pin = sorted[i];
                var pinClass = this._timelineClassById(pin.classId);
                if (!pinClass || !pinClass.visible) continue;

                appendSegment(cursor, pin.time, activeClass);
                cursor = pin.time;
                activeClass = pinClass;
            }
            appendSegment(cursor, duration, activeClass);
        };

        VideoAnnotator.prototype._renderTimelinePins = function () {
            if (!this._timelineEnabled()) return;
            var self = this;
            this.timelinePinsLayerEl.innerHTML = '';

            this._sortTimelinePins();

            this.timelinePins.forEach(function (pin) {
                var cls = self._timelineClassById(pin.classId);
                if (!cls || !cls.visible) return;

                var pinBtn = document.createElement('button');
                pinBtn.type = 'button';
                pinBtn.className = 'timeline-pin' + (pin.id === self._selectedTimelinePinId ? ' is-selected' : '');
                pinBtn.setAttribute('data-pin-id', pin.id);
                pinBtn.style.left = (self._timelineTimeToPct(pin.time) * 100).toFixed(4) + '%';
                pinBtn.style.setProperty('--pin-color', self._timelineClassColor(cls));
                pinBtn.title = cls.name + ' @ ' + U.fmtTime(pin.time);

                pinBtn.addEventListener('click', function (e) {
                    e.preventDefault();
                    e.stopPropagation();
                    self._selectedTimelinePinId = pin.id;
                    self._renderTimelinePins();
                    var freshPinBtn = self.timelinePinsLayerEl.querySelector('[data-pin-id="' + pin.id + '"]');
                    self._openTimelinePinMenu(pin.id, freshPinBtn);
                });

                pinBtn.addEventListener('contextmenu', function (e) {
                    e.preventDefault();
                    e.stopPropagation();
                    self._selectedTimelinePinId = pin.id;
                    self._renderTimelinePins();
                    var freshPinBtn = self.timelinePinsLayerEl.querySelector('[data-pin-id="' + pin.id + '"]');
                    self._openTimelinePinMenu(pin.id, freshPinBtn, e.clientX, e.clientY);
                });

                self.timelinePinsLayerEl.appendChild(pinBtn);
            });
        };

        VideoAnnotator.prototype._beginTimelineDrag = function (evt) {
            if (!this._timelineEnabled()) return;
            evt.preventDefault();
            evt.stopPropagation();

            this.videoEl.pause();
            this._timelineDrag = { kind: 'playhead' };

            this._dragTimelineToClientX(evt.clientX);
        };

        VideoAnnotator.prototype._dragTimelineToClientX = function (clientX) {
            if (!this._timelineDrag) return;
            var drag = this._timelineDrag;
            var targetTime = this._timelineTimeFromClientX(clientX);

            if (drag.kind === 'playhead') {
                this._seekPending = null;
                this._seekInFlight = false;
                this.videoEl.currentTime = targetTime;
                this._updateTemporalTimelineUI();
                return;
            }
        };

        VideoAnnotator.prototype._addTimelinePinAt = function (timeSeconds) {
            var activeClass = this._activeTimelineClass();
            if (!activeClass) return;
            var t = this._clampTimelineTime(
                (typeof timeSeconds === 'number') ? timeSeconds : (this.videoEl.currentTime || 0)
            );

            var merged = this.timelinePins.find(function (pin) {
                return Math.abs(pin.time - t) <= TIMELINE_PIN_MERGE_TOLERANCE;
            });

            if (merged) {
                merged.classId = activeClass.id;
                this._selectedTimelinePinId = merged.id;
            } else {
                var id = 'pin-' + Date.now() + '-' + Math.random().toString(36).slice(2);
                this.timelinePins.push({ id: id, dbId: null, time: t, classId: activeClass.id });
                this._selectedTimelinePinId = id;
            }

            this._compactTimelinePins();
            this._refreshTimelineVisuals();
            this._scheduleTimelineMarkersSync();
        };

        VideoAnnotator.prototype._deleteTimelinePin = function (pinId) {
            var idx = this.timelinePins.findIndex(function (pin) { return pin.id === pinId; });
            if (idx === -1) return;

            this.timelinePins.splice(idx, 1);
            if (this._selectedTimelinePinId === pinId) this._selectedTimelinePinId = null;
            this._closeTimelinePinMenu();

            this._compactTimelinePins();
            this._refreshTimelineVisuals();
            this._scheduleTimelineMarkersSync();
        };

        VideoAnnotator.prototype._setTimelinePinClass = function (pinId, classId) {
            var pin = this.timelinePins.find(function (p) { return p.id === pinId; });
            var cls = this._timelineClassById(classId);
            if (!pin || !cls) return;
            pin.classId = cls.id;
            this.activeTimelineClassId = cls.id;
            this._compactTimelinePins();
            this._refreshTimelineWithClasses();
            this._scheduleTimelineMarkersSync();
        };

        VideoAnnotator.prototype._moveTimelinePinToCurrentTime = function (pinId) {
            var pin = this.timelinePins.find(function (p) { return p.id === pinId; });
            if (!pin) return;

            var targetTime = this._clampTimelineTime(this.videoEl.currentTime || 0);
            var mergeTarget = this.timelinePins.find(function (p) {
                return p.id !== pinId && Math.abs(p.time - targetTime) <= TIMELINE_PIN_MERGE_TOLERANCE;
            });

            if (mergeTarget) {
                mergeTarget.classId = pin.classId;
                this.timelinePins = this.timelinePins.filter(function (p) { return p.id !== pinId; });
                this._selectedTimelinePinId = mergeTarget.id;
            } else {
                pin.time = targetTime;
                this._selectedTimelinePinId = pin.id;
            }

            this._compactTimelinePins();
            this._refreshTimelineVisuals();
            this._scheduleTimelineMarkersSync();
        };

        VideoAnnotator.prototype._closeTimelinePinMenu = function () {
            if (this._timelinePinMenuEl) {
                this._timelinePinMenuEl.remove();
                this._timelinePinMenuEl = null;
            }
            if (this._timelinePinMenuCloser) {
                document.removeEventListener('click', this._timelinePinMenuCloser);
                this._timelinePinMenuCloser = null;
            }
        };

        VideoAnnotator.prototype._openTimelinePinMenu = function (pinId, anchorEl, clientX, clientY) {
            var self = this;
            var pin = this.timelinePins.find(function (p) { return p.id === pinId; });
            if (!pin) return;

            this._closeTimelinePinMenu();

            var menu = document.createElement('div');
            menu.style.cssText = 'position:fixed;background:#fff;border:1px solid #ccc;border-radius:6px;' +
                'box-shadow:0 8px 24px rgba(0,0,0,0.18);z-index:1200;min-width:220px;padding:0.5rem;';

            var title = document.createElement('div');
            title.className = 'small fw-semibold mb-2';
            title.textContent = 'Marker @ ' + U.fmtTime(pin.time);
            menu.appendChild(title);

            var selectWrap = document.createElement('div');
            selectWrap.className = 'mb-2';
            menu.appendChild(selectWrap);

            var select = document.createElement('select');
            select.className = 'form-select form-select-sm';
            this.timelineClasses.forEach(function (cls) {
                var opt = document.createElement('option');
                opt.value = cls.id;
                opt.textContent = cls.name + (cls.visible ? '' : ' (hidden)');
                select.appendChild(opt);
            });
            select.value = pin.classId;
            select.addEventListener('change', function () {
                self._setTimelinePinClass(pinId, select.value);
                self._closeTimelinePinMenu();
            });
            selectWrap.appendChild(select);

            var moveBtn = document.createElement('button');
            moveBtn.className = 'btn btn-sm btn-outline-secondary w-100 mb-2';
            moveBtn.innerHTML = '<i class="fas fa-crosshairs me-1"></i>Move To Cursor';
            moveBtn.addEventListener('click', function () {
                self._moveTimelinePinToCurrentTime(pinId);
                self._closeTimelinePinMenu();
            });
            menu.appendChild(moveBtn);

            var deleteBtn = document.createElement('button');
            deleteBtn.className = 'btn btn-sm btn-outline-danger w-100';
            deleteBtn.innerHTML = '<i class="fas fa-trash-alt me-1"></i>Delete Marker';
            deleteBtn.addEventListener('click', function () {
                self._deleteTimelinePin(pinId);
                self._closeTimelinePinMenu();
            });
            menu.appendChild(deleteBtn);

            document.body.appendChild(menu);

            var left;
            var top;

            if (typeof clientX === 'number' && typeof clientY === 'number') {
                left = clientX + 8;
                top = clientY + 8;
            } else if (anchorEl) {
                var anchorRect = anchorEl.getBoundingClientRect();
                left = anchorRect.right + 8;
                top = anchorRect.top + (anchorRect.height / 2) - (menu.offsetHeight / 2);
            } else {
                left = 12;
                top = 12;
            }

            left = Math.max(8, Math.min(window.innerWidth - menu.offsetWidth - 8, left));
            top = Math.max(8, Math.min(window.innerHeight - menu.offsetHeight - 8, top));

            menu.style.left = left + 'px';
            menu.style.top = top + 'px';

            this._timelinePinMenuEl = menu;
            this._timelinePinMenuCloser = function (e) {
                if (!menu.contains(e.target)) self._closeTimelinePinMenu();
            };
            setTimeout(function () {
                document.addEventListener('click', self._timelinePinMenuCloser);
            }, 0);
        };

        VideoAnnotator.prototype._promptNewTimelineClass = function () {
            if (!this.isAdmin) return;
            var name = String(this.timelineClasses.length + 1);
            var cls = this.addTimelineClass(name);
            this.activeTimelineClassId = cls.id;
            this._startTimelineClassEdit(cls.id);
            this._persistTimelineClass(cls);
        };

        VideoAnnotator.prototype._updateTemporalTimelineUI = function () {
            if (!this._timelineEnabled()) return;

            var duration = this._videoDuration();
            var current = this._clampTimelineTime(this.videoEl.currentTime || 0);
            var pct = this._timelineTimeToPct(current);

            if (this.timelinePlayheadEl) {
                this.timelinePlayheadEl.style.left = 'calc(8px + (100% - 16px) * ' + pct.toFixed(6) + ')';
            }
            if (this.timelineCurrentTimeEl) {
                this.timelineCurrentTimeEl.textContent = U.fmtTime(current);
            }
            if (this.timelineDurationEl) {
                this.timelineDurationEl.textContent = U.fmtTime(duration);
            }

            if (this.timelineActiveClassEl) {
                var currentClass = this._timelineClassAt(current);
                var currentLabel = currentClass ? currentClass.name : '-';
                this.timelineActiveClassEl.textContent = 'Current: ' + currentLabel;
                this.timelineActiveClassEl.style.backgroundColor = this._timelineClassColor(currentClass);
                this.timelineActiveClassEl.style.color = '#fff';
            }
        };

        VideoAnnotator.prototype._initTemporalClassification = function () {
            if (!this._timelineEnabled()) return;
            var self = this;

            this.timelinePins = [];
            this._addDefaultTimelineClass();
            this._refreshTimelineWithClasses();

            if (this.timelineAddPinBtnEl) {
                this.timelineAddPinBtnEl.addEventListener('click', function () {
                    self._addTimelinePinAt(self.videoEl.currentTime || 0);
                });
            }

            if (this.timelineAddClassBtnEl) {
                this.timelineAddClassBtnEl.addEventListener('click', function () {
                    if (!self.isAdmin) return;
                    self._promptNewTimelineClass();
                });
            }

            if (this.timelinePlayheadEl) {
                this.timelinePlayheadEl.addEventListener('mousedown', function (e) {
                    if (e.button !== 0) return;
                    self._beginTimelineDrag(e);
                });
            }

            if (this.timelineTrackWrapEl) {
                this.timelineTrackWrapEl.addEventListener('mousedown', function (e) {
                    if (e.button !== 0) return;

                    var target = e.target;
                    var isPin = !!(target && target.closest && target.closest('.timeline-pin'));
                    var isPlayhead = target === self.timelinePlayheadEl;
                    if (isPin || isPlayhead) return;
                    e.preventDefault();

                    self._closeTimelinePinMenu();

                    self._beginTimelineDrag(e);
                });
            }

            var TL = this._timelineListeners;
            TL.winMove = function (e) {
                if (!self._timelineDrag) return;
                e.preventDefault();
                self._dragTimelineToClientX(e.clientX);
            };
            TL.winUp = function () {
                if (!self._timelineDrag) return;
                self._timelineDrag = null;
                self._updateTemporalTimelineUI();
            };
            window.addEventListener('mousemove', TL.winMove);
            window.addEventListener('mouseup', TL.winUp);
        };

        VideoAnnotator.prototype._destroyTimeline = function () {
            var TL = this._timelineListeners || {};
            if (TL.winMove) window.removeEventListener('mousemove', TL.winMove);
            if (TL.winUp)   window.removeEventListener('mouseup',   TL.winUp);
            this._timelineListeners = {};
        };
    };
})();
