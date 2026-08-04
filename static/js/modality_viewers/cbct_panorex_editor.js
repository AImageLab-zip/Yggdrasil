(function() {
    'use strict';

    var WORKER_URL = '/static/js/worker/seg2pano_worker.js';
    var root;
    var source;
    var worker;
    var raw;
    var stage;
    var imageLayer;
    var overlayLayer;
    var controlGroups = [];
    var resizeObserver;
    var geometryRequest = 0;
    var generationToken = 0;
    var initStarted = false;
    var controlsBound = false;
    var draggingControlPoint = false;
    var geometryPending = false;
    var workerReady = false;
    var generationRequested = false;
    var restoreSavedOnInit = false;
    var volumeReadyBound = false;
    var MIN_CONTROL_POINT_X_SEPARATION = 1;
    var ALGORITHM_VERSION = 'panorex-js-v2-mip';
    var state = {
        autoZ: null,
        z: 0,
        flipZ: false,
        geometry: null,
        mask: null,
        outputs: null,
        outputCanvases: null,
        mode: 'mip',
        busy: false,
        generationUuid: null
    };

    function element(id) { return document.getElementById(id); }

    function setStatus(text) {
        var status = element('panorexEditorStatus');
        if (status) status.textContent = text;
    }

    function setError(message, retryable) {
        var error = element('panorexEditorError');
        var messageElement = element('panorexEditorErrorMessage');
        var retry = element('panorexRetry');
        if (messageElement) messageElement.textContent = message || '';
        else if (error) error.textContent = message || '';
        if (error) error.hidden = !message;
        if (retry) retry.hidden = !message || !retryable;
        if (message) setStatus('Generation failed');
    }

    function setProgress(value, label) {
        var progress = element('panorexProgress');
        var bar = element('panorexProgressBar');
        if (progress) progress.hidden = value === null;
        if (bar && value !== null) bar.style.width = Math.round(Math.max(0, Math.min(1, value)) * 100) + '%';
        if (label) setStatus(label);
    }

    function setBusy(busy) {
        state.busy = busy;
        if (overlayLayer) overlayLayer.listening(!busy && !geometryPending);
    }

    function updateZControls() {
        var slider = element('panorexZSlider');
        var output = element('panorexZValue');
        if (slider) slider.value = String(state.z);
        if (output) output.textContent = String(state.z);
    }

    function csrfToken() {
        var input = root && root.querySelector('input[name="csrfmiddlewaretoken"]');
        if (input && input.value) return input.value;
        var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : '';
    }

    function waitForRawDescriptor() {
        return new Promise(function(resolve, reject) {
            function check() {
                if (!window.ViewerGrid || typeof window.ViewerGrid.getNativeRawVolumeDescriptor !== 'function') return false;
                var descriptor = window.ViewerGrid.getNativeRawVolumeDescriptor();
                if (!descriptor) return false;
                resolve(descriptor);
                return true;
            }
            if (check()) return;
            var listener = function() {
                if (check()) window.removeEventListener('viewergridvolumeready', listener);
            };
            window.addEventListener('viewergridvolumeready', listener);
            var timer = window.setInterval(function() {
                if (check()) {
                    window.clearInterval(timer);
                    window.removeEventListener('viewergridvolumeready', listener);
                }
            }, 250);
        });
    }

    function initializeWorker(segmentationSource) {
        workerReady = false;
        worker = new Worker(WORKER_URL);
        worker.onmessage = handleWorkerMessage;
        worker.onerror = function(event) {
            initStarted = false;
            if (worker) worker.terminate();
            worker = null;
            workerReady = false;
            geometryPending = false;
            setBusy(false);
            if (overlayLayer) overlayLayer.listening(false);
            setProgress(null);
            setError(event.message || 'The panoramic geometry worker stopped unexpectedly.', true);
            if (root) root.hidden = false;
        };
        geometryRequest++;
        var workerBuffer = segmentationSource.arrayBuffer.slice(0);
        worker.postMessage({
            type: 'init',
            id: geometryRequest,
            buffer: workerBuffer,
            raw: {
                dimensions: raw.dimensions,
                affine: raw.affine,
                flipZ: raw.flipZ
            }
        }, [workerBuffer]);
    }

    function start() {
        if (initStarted) return;
        initStarted = true;
        setError('');
        setBusy(true);
        setProgress(0.02, 'Waiting for native CBCT voxels');
        Promise.all([
            waitForRawDescriptor(),
            window.ViewerGrid.getPanorexSegmentationSource()
        ]).then(function(results) {
            raw = results[0];
            var segmentationSource = results[1];
            var voxelCount = raw.dimensions.width * raw.dimensions.height * raw.dimensions.depth;
            if (!raw.data || raw.data.length < voxelCount) throw new Error('NiiVue did not expose a complete native CBCT array.');
            if (source.volumeFileId && String(source.volumeFileId) !== String(raw.source.fileId)) {
                throw new Error('The displayed CBCT does not match the paired panoramic source.');
            }
            initializeWorker(segmentationSource);
        }).catch(function(error) {
            initStarted = false;
            setBusy(false);
            if (overlayLayer) overlayLayer.listening(false);
            setProgress(null);
            setError(error.message || 'Unable to initialize the panoramic editor.', true);
            if (root) root.hidden = false;
        });
    }

    function handleWorkerMessage(event) {
        var message = event.data || {};
        if (message.type === 'progress') {
            if (!message.id || message.id === geometryRequest) setProgress(message.value * 0.3, 'Analyzing segmentation');
            return;
        }
        if (message.type === 'error') {
            if (message.id && message.id !== geometryRequest) return;
            var initFailure = !workerReady;
            geometryPending = false;
            if (initFailure) {
                initStarted = false;
                if (worker) worker.terminate();
                worker = null;
                if (root) root.hidden = false;
            }
            setBusy(false);
            if (overlayLayer) overlayLayer.listening(false);
            setProgress(null);
            setError(message.message, initFailure);
            return;
        }
        if (message.type === 'initialized') {
            if (message.id !== geometryRequest) return;
            workerReady = true;
            state.autoZ = message.autoZ;
            state.z = message.autoZ;
            state.flipZ = message.flipZ;
            var zSlider = element('panorexZSlider');
            if (zSlider) zSlider.max = String(message.dimensions.depth - 1);
            updateZControls();
            if (source.state && source.state.algorithmVersion !== ALGORITHM_VERSION) {
                source.revision = 0;
            }
            if (
                restoreSavedOnInit &&
                source.state &&
                source.state.algorithmVersion === ALGORITHM_VERSION &&
                Array.isArray(source.state.spline) &&
                source.state.spline.length >= 4 &&
                Array.isArray(source.state.volumeShape) &&
                source.state.volumeShape[0] === message.dimensions.width &&
                source.state.volumeShape[1] === message.dimensions.height &&
                source.state.volumeShape[2] === message.dimensions.depth
            ) {
                restoreSavedOnInit = false;
                state.z = source.state.axialSlice;
                state.mode = source.state.defaultMode === 'raysum' ? 'raysum' : 'mip';
                updateZControls();
                requestGeometry(source.state.geometrySource === 'auto' ? null : source.state.spline);
            }
            return;
        }
        if (message.type !== 'geometry' || message.id !== geometryRequest) return;

        state.z = message.z;
        state.geometry = {
            source: message.source,
            polynomial: message.polynomial,
            start: message.start,
            end: message.end,
            controlPoints: message.controlPoints,
            spline: message.spline,
            centerline: message.centerline,
            slab: message.slab
        };
        state.mask = message.mask;
        geometryPending = false;
        state.outputs = null;
        state.outputCanvases = null;
        var saveButton = element('panorexSave');
        if (saveButton) saveButton.disabled = true;
        updateZControls();
        drawAxialEditor();
        setBusy(false);
        if (root) root.hidden = false;
        var savedViewer = element('cbctInlinePanoramic');
        if (savedViewer) savedViewer.hidden = true;
        if (generationRequested) {
            generationRequested = false;
            generatePanoramics();
        } else {
            setProgress(null);
            setStatus('Ready. Select Reset auto or adjust the axial arch.');
        }
    }

    function requestGeometry(controlPoints) {
        generationToken++;
        geometryRequest++;
        geometryPending = true;
        if (overlayLayer) overlayLayer.listening(false);
        state.geometry = null;
        state.mask = null;
        state.outputs = null;
        state.outputCanvases = null;
        var saveButton = element('panorexSave');
        if (saveButton) saveButton.disabled = true;
        setError('');
        if (!worker) {
            setBusy(false);
            geometryPending = false;
            setError('The panoramic geometry worker is not available.', true);
            return;
        }
        setBusy(true);
        setProgress(0.08, 'Fitting dental arch');
        worker.postMessage({
            type: 'geometry',
            id: geometryRequest,
            z: state.z,
            controlPoints: controlPoints || null
        });
    }

    function rawSliceCanvas() {
        var width = raw.dimensions.width;
        var height = raw.dimensions.height;
        var nativeZ = window.Seg2PanoCore.canonicalZToNative(state.z, raw.dimensions.depth, state.flipZ);
        var start = nativeZ * width * height;
        var min = Infinity;
        var max = -Infinity;
        var slope = raw.slope || 1;
        var intercept = raw.intercept || 0;
        for (var i = 0; i < width * height; i++) {
            var value = raw.data[start + i] * slope + intercept;
            if (value < min) min = value;
            if (value > max) max = value;
        }
        var canvas = document.createElement('canvas');
        canvas.width = width;
        canvas.height = height;
        var context = canvas.getContext('2d');
        var pixels = context.createImageData(width, height);
        var factor = max > min ? 255 / (max - min) : 0;
        for (var pixel = 0; pixel < width * height; pixel++) {
            var gray = Math.max(0, Math.min(255, Math.round((raw.data[start + pixel] * slope + intercept - min) * factor)));
            var offset = pixel * 4;
            pixels.data[offset] = gray;
            pixels.data[offset + 1] = gray;
            pixels.data[offset + 2] = gray;
            pixels.data[offset + 3] = 255;
        }
        context.putImageData(pixels, 0, 0);
        return canvas;
    }

    function maskCanvas() {
        var canvas = document.createElement('canvas');
        canvas.width = raw.dimensions.width;
        canvas.height = raw.dimensions.height;
        var context = canvas.getContext('2d');
        var pixels = context.createImageData(canvas.width, canvas.height);
        for (var i = 0; i < state.mask.length; i++) {
            if (!state.mask[i]) continue;
            var offset = i * 4;
            pixels.data[offset] = 91;
            pixels.data[offset + 1] = 141;
            pixels.data[offset + 2] = 239;
            pixels.data[offset + 3] = 100;
        }
        context.putImageData(pixels, 0, 0);
        return canvas;
    }

    function stageTransform() {
        var host = element('panorexAxialStage');
        if (!host) return null;
        var width = Math.max(1, host.clientWidth || 280);
        var height = Math.max(250, host.clientHeight);
        var scale = Math.min(width / raw.dimensions.width, height / raw.dimensions.height);
        return {
            width: width,
            height: height,
            scale: scale,
            x: (width - raw.dimensions.width * scale) / 2,
            y: (height - raw.dimensions.height * scale) / 2
        };
    }

    function flattenPoints(points, transform) {
        var flattened = [];
        for (var i = 0; i < points.length; i++) {
            flattened.push(transform.x + points[i][0] * transform.scale, transform.y + points[i][1] * transform.scale);
        }
        return flattened;
    }

    function drawAxialEditor() {
        if (!raw || !state.geometry || !window.Konva) return;
        var host = element('panorexAxialStage');
        var transform = stageTransform();
        if (!host || !transform) return;
        controlGroups = [];
        if (!stage) {
            stage = new window.Konva.Stage({ container: host, width: transform.width, height: transform.height });
            imageLayer = new window.Konva.Layer();
            overlayLayer = new window.Konva.Layer();
            stage.add(imageLayer);
            stage.add(overlayLayer);
        } else {
            stage.size({ width: transform.width, height: transform.height });
            imageLayer.destroyChildren();
            overlayLayer.destroyChildren();
        }

        imageLayer.add(new window.Konva.Image({
            image: rawSliceCanvas(), x: transform.x, y: transform.y,
            width: raw.dimensions.width * transform.scale,
            height: raw.dimensions.height * transform.scale
        }));
        imageLayer.add(new window.Konva.Image({
            image: maskCanvas(), x: transform.x, y: transform.y,
            width: raw.dimensions.width * transform.scale,
            height: raw.dimensions.height * transform.scale
        }));

        var line = new window.Konva.Line({
            points: flattenPoints(state.geometry.spline, transform),
            stroke: '#7bdcc7', strokeWidth: 2.5, lineCap: 'round', lineJoin: 'round', listening: false
        });
        overlayLayer.add(line);

        state.geometry.controlPoints.forEach(function(point, pointIndex) {
            var currentIndex = pointIndex;
            var group = new window.Konva.Group({
                name: 'panorex-control',
                x: transform.x + point[0] * transform.scale,
                y: transform.y + point[1] * transform.scale,
                draggable: true
            });
            group.add(new window.Konva.Circle({
                radius: 14,
                fill: 'rgba(0, 0, 0, 0.001)'
            }));
            group.add(new window.Konva.Circle({
                radius: 8,
                fill: '#7aa5ff',
                stroke: '#ffffff',
                strokeWidth: 1.5,
                listening: false
            }));
            group.on('mouseenter', function() {
                if (geometryPending) return;
                stage.container().style.cursor = 'grab';
            });
            group.on('mouseleave', function() {
                if (!draggingControlPoint) stage.container().style.cursor = 'default';
            });
            group.on('dragstart', function() {
                draggingControlPoint = true;
                stage.container().style.cursor = 'grabbing';
            });
            group.on('dragmove', function() {
                var minX = currentIndex > 0
                    ? state.geometry.controlPoints[currentIndex - 1][0] + MIN_CONTROL_POINT_X_SEPARATION
                    : 0;
                var maxX = currentIndex + 1 < state.geometry.controlPoints.length
                    ? state.geometry.controlPoints[currentIndex + 1][0] - MIN_CONTROL_POINT_X_SEPARATION
                    : raw.dimensions.width - 1;
                var x = window.Seg2PanoCore.clamp((group.x() - transform.x) / transform.scale, minX, maxX);
                var y = window.Seg2PanoCore.clamp((group.y() - transform.y) / transform.scale, 0, raw.dimensions.height - 1);
                group.position({ x: transform.x + x * transform.scale, y: transform.y + y * transform.scale });
                var movedPoint = [x, y];
                state.geometry.controlPoints[currentIndex] = movedPoint;
                var preview = window.Seg2PanoCore.catmullRomChain(state.geometry.controlPoints);
                line.points(flattenPoints(preview, transform));
                overlayLayer.batchDraw();
            });
            group.on('dragend', function() {
                draggingControlPoint = false;
                stage.container().style.cursor = 'default';
                generationRequested = true;
                requestGeometry(state.geometry.controlPoints.map(function(cp) { return cp.slice(); }));
            });
            controlGroups.push(group);
            overlayLayer.add(group);
        });

        function nearestControl(pointer) {
            var nearest = null;
            controlGroups.forEach(function(group) {
                var dx = group.x() - pointer.x;
                var dy = group.y() - pointer.y;
                var distance = Math.sqrt(dx * dx + dy * dy);
                if (distance <= 26 && (!nearest || distance < nearest.distance)) {
                    nearest = { group: group, distance: distance };
                }
            });
            return nearest;
        }

        stage.off('.panorexControl');
        stage.on('mousemove.panorexControl', function() {
            if (draggingControlPoint || geometryPending || state.busy) return;
            var pointer = stage.getPointerPosition();
            stage.container().style.cursor = pointer && nearestControl(pointer) ? 'grab' : 'default';
        });
        stage.on('mousedown.panorexControl touchstart.panorexControl', function(event) {
            if (geometryPending || state.busy) return;
            var pointer = stage.getPointerPosition();
            var nearest = pointer && nearestControl(pointer);
            if (!nearest) return;
            controlGroups.forEach(function(group) {
                if (group !== nearest.group && group.isDragging()) group.stopDrag();
            });
            if (!nearest.group.isDragging()) nearest.group.startDrag(event.evt);
        });
        stage.on('mouseleave.panorexControl', function() {
            if (!draggingControlPoint) stage.container().style.cursor = 'default';
        });
        imageLayer.draw();
        overlayLayer.draw();
    }

    function makeImageCanvas(values, width, height) {
        var normalized = window.Seg2PanoCore.normalizeOpenCV(values);
        var canvas = document.createElement('canvas');
        canvas.width = width;
        canvas.height = height;
        var context = canvas.getContext('2d');
        var image = context.createImageData(width, height);
        for (var i = 0; i < normalized.length; i++) {
            var offset = i * 4;
            image.data[offset] = normalized[i];
            image.data[offset + 1] = normalized[i];
            image.data[offset + 2] = normalized[i];
            image.data[offset + 3] = 255;
        }
        context.putImageData(image, 0, 0);
        return canvas;
    }

    function showSelectedOutput() {
        if (!state.outputCanvases) return;
        var sourceCanvas = state.outputCanvases[state.mode];
        var canvas = element('panorexResultCanvas');
        if (!sourceCanvas || !canvas) return;
        canvas.width = sourceCanvas.width;
        canvas.height = sourceCanvas.height;
        var context = canvas.getContext('2d');
        context.drawImage(sourceCanvas, 0, 0);
        var locatorY = Math.max(
            0.5,
            Math.min(canvas.height - 0.5, state.z / sourceCanvas.height * canvas.height + 0.5)
        );
        context.save();
        context.strokeStyle = '#f6b84a';
        context.lineWidth = 1;
        context.beginPath();
        context.moveTo(0, locatorY);
        context.lineTo(canvas.width, locatorY);
        context.stroke();
        context.restore();
        var empty = element('panorexEmptyResult');
        if (empty) empty.hidden = true;
        if (!root) return;
        root.querySelectorAll('[data-panorex-mode]').forEach(function(button) {
            var active = button.dataset.panorexMode === state.mode;
            button.classList.toggle('is-active', active);
            button.setAttribute('aria-pressed', active ? 'true' : 'false');
        });
    }

    function generatePanoramics() {
        if (!raw || !state.geometry || !state.geometry.slab.length) return;
        var token = ++generationToken;
        var slab = state.geometry.slab;
        var width = slab.length;
        var height = raw.dimensions.depth;
        var mip = new Float32Array(width * height);
        var raysum = new Float32Array(width * height);
        var column = 0;
        var columnsPerChunk = 4;
        setError('');
        setProgress(0.32, 'Generating both projections');

        function chunk() {
            if (token !== generationToken) return;
            var end = Math.min(width, column + columnsPerChunk);
            for (; column < end; column++) {
                window.Seg2PanoCore.projectColumnPair(
                    raw.data, raw.dimensions, slab, column, mip, raysum,
                    state.flipZ, raw.slope, raw.intercept
                );
            }
            setProgress(0.32 + 0.62 * column / width, 'Generating both projections');
            if (column < width) {
                window.setTimeout(chunk, 0);
                return;
            }
            if (token !== generationToken) return;
            state.outputs = { mip: mip, raysum: raysum };
            state.outputCanvases = {
                mip: makeImageCanvas(mip, width, height),
                raysum: makeImageCanvas(raysum, width, height)
            };
            state.generationUuid = createUuid();
            showSelectedOutput();
            setBusy(false);
            setProgress(null);
            setStatus('Ready | Z ' + state.z + ' | ' + width + ' columns | 41-ray slab');
            var saveButton = element('panorexSave');
            if (saveButton) saveButton.disabled = false;
        }
        window.setTimeout(chunk, 0);
    }

    function canvasBlob(canvas) {
        return new Promise(function(resolve, reject) {
            canvas.toBlob(function(blob) {
                if (blob) resolve(blob);
                else reject(new Error('The browser could not encode a panoramic PNG.'));
            }, 'image/png');
        });
    }

    function createUuid() {
        if (window.crypto && typeof window.crypto.randomUUID === 'function') return window.crypto.randomUUID();
        var bytes = new Uint8Array(16);
        window.crypto.getRandomValues(bytes);
        bytes[6] = (bytes[6] & 15) | 64;
        bytes[8] = (bytes[8] & 63) | 128;
        return Array.from(bytes, function(value) { return value.toString(16).padStart(2, '0'); }).join('').replace(
            /^(........)(....)(....)(....)(............)$/,
            '$1-$2-$3-$4-$5'
        );
    }

    function save() {
        if (!state.outputCanvases || state.busy) return;
        var csrf = csrfToken();
        if (!csrf) {
            setError('The security token is missing. Reload the page and try again.');
            return;
        }
        setBusy(true);
        var saveButton = element('panorexSave');
        if (saveButton) saveButton.disabled = true;
        setProgress(0.2, 'Encoding panoramic PNGs');
        Promise.all([
            canvasBlob(state.outputCanvases.mip),
            canvasBlob(state.outputCanvases.raysum)
        ]).then(function(blobs) {
            var payload = {
                source: {
                    job_id: source.jobId,
                    file_id: source.volumeFileId,
                    file_key: source.volumeFileKey,
                    file_hash: source.volumeFileHash,
                    segmentation_file_id: source.segmentationFileId,
                    segmentation_file_key: source.segmentationFileKey,
                    segmentation_file_hash: source.segmentationFileHash
                },
                volume_shape: [raw.dimensions.width, raw.dimensions.height, raw.dimensions.depth],
                axial_slice: state.z,
                spline: state.geometry.controlPoints.map(function(point) { return [point[0], point[1]]; }),
                geometry_source: state.geometry.source,
                default_mode: state.mode,
                algorithm_version: ALGORITHM_VERSION,
                generation_uuid: state.generationUuid,
                base_revision: Number(source.revision) || 0
            };
            var form = new FormData();
            form.append('state', JSON.stringify(payload));
            form.append('mip_png', blobs[0], 'panoramic-mip.png');
            form.append('raysum_png', blobs[1], 'panoramic-raysum.png');
            setProgress(0.65, 'Saving projection');
            return fetch('/maxillo/api/patient/' + window.scanId + '/panoramic/generated/', {
                method: 'POST',
                headers: { 'X-CSRFToken': csrf },
                body: form
            });
        }).then(function(response) {
            return response.json().catch(function() { return {}; }).then(function(data) {
                if (!response.ok) throw new Error(data.error || data.detail || ('Save failed (HTTP ' + response.status + ').'));
                return data;
            });
        }).then(function(data) {
            if (data.revision !== undefined) source.revision = data.revision;
            setBusy(false);
            setProgress(null);
            setStatus('Saved');
            var savedButton = element('panorexSave');
            if (savedButton) savedButton.disabled = true;
            if (window.PanoramicViewer && typeof window.PanoramicViewer.refreshAfterSave === 'function') {
                window.PanoramicViewer.refreshAfterSave(data);
            }
            var savedViewer = element('cbctInlinePanoramic');
            if (savedViewer) savedViewer.hidden = false;
            if (root) root.hidden = true;
        }).catch(function(error) {
            setBusy(false);
            setProgress(null);
            setError(error.message || 'Unable to save the generated panoramic images.');
            var failedButton = element('panorexSave');
            if (failedButton) failedButton.disabled = false;
        });
    }

    function setZ(nextZ) {
        var clamped = Math.max(0, Math.min(raw.dimensions.depth - 1, Math.trunc(nextZ)));
        if (clamped === state.z) return;
        generationRequested = true;
        state.z = clamped;
        updateZControls();
        requestGeometry(null);
    }

    function bindControls() {
        if (controlsBound) return;
        controlsBound = true;
        function bind(id, eventName, handler) {
            var target = element(id);
            if (target) target.addEventListener(eventName, handler);
        }
        bind('panorexZSlider', 'input', function(event) {
            var output = element('panorexZValue');
            if (output) output.textContent = event.target.value;
        });
        bind('panorexZSlider', 'change', function(event) { setZ(Number(event.target.value)); });
        bind('panorexPrevZ', 'click', function() { if (raw) setZ(state.z - 1); });
        bind('panorexNextZ', 'click', function() { if (raw) setZ(state.z + 1); });
        bind('panorexResetAuto', 'click', function() {
            if (state.autoZ === null) return;
            generationRequested = true;
            state.z = state.autoZ;
            updateZControls();
            requestGeometry(null);
        });
        bind('panorexRetry', 'click', function() {
            if (worker) worker.terminate();
            worker = null;
            workerReady = false;
            initStarted = false;
            start();
        });
        bind('panorexSave', 'click', save);
        root.querySelectorAll('[data-panorex-mode]').forEach(function(button) {
            button.addEventListener('click', function() {
                state.mode = button.dataset.panorexMode;
                showSelectedOutput();
                if (state.outputCanvases && !state.busy) {
                    state.generationUuid = createUuid();
                    var saveButton = element('panorexSave');
                    if (saveButton) saveButton.disabled = false;
                }
            });
        });
    }

    function activateEditor(restoreSaved) {
        if (!root || !source) return;
        var savedViewer = element('cbctInlinePanoramic');
        if (savedViewer) savedViewer.hidden = true;
        restoreSavedOnInit = Boolean(restoreSaved && source.state);

        bindControls();
        if (!resizeObserver && window.ResizeObserver) {
            resizeObserver = new ResizeObserver(function() {
                if (state.geometry && !draggingControlPoint) drawAxialEditor();
            });
            var stageElement = element('panorexAxialStage');
            if (stageElement) resizeObserver.observe(stageElement);
        }
        if (!volumeReadyBound) {
            window.addEventListener('viewergridvolumeready', start);
            volumeReadyBound = true;
        }
        if (state.geometry) {
            restoreSavedOnInit = false;
            root.hidden = false;
            drawAxialEditor();
            setStatus('Ready. Adjust the axial arch to replace the saved panoramic.');
            return;
        }
        root.hidden = false;
        start();
    }

    function init() {
        root = element('cbctPanorexEditor');
        if (!root || root.dataset.canEdit !== 'true' || !window.canEdit || !window.Worker || !window.Konva || !window.Seg2PanoCore) return;
        if (!window.ViewerGrid || typeof window.ViewerGrid.getPanorexSourceDescriptor !== 'function') return;
        source = window.ViewerGrid.getPanorexSourceDescriptor();
        if (!source || !source.volumeFileId || !source.volumeFileKey || !source.segmentationFileId || !source.segmentationFileKey) return;
        if (
            Number(source.revision) > 0 &&
            source.state &&
            source.state.algorithmVersion === ALGORITHM_VERSION
        ) {
            root.hidden = true;
            return;
        }

        activateEditor(false);
    }

    window.CBCTPanorexEditor = { enterEditMode: function() { activateEditor(true); } };

    document.addEventListener('DOMContentLoaded', init);
    window.addEventListener('pagehide', function(event) {
        if (event.persisted) return;
        if (worker) worker.terminate();
        worker = null;
        workerReady = false;
        if (resizeObserver) resizeObserver.disconnect();
    });
    window.addEventListener('pageshow', function(event) {
        if (!event.persisted || worker) return;
        initStarted = false;
        start();
    });
})();
