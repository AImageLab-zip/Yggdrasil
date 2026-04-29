(function () {
    const labelRoot = document.getElementById('intraoralSegmentationSection');
    if (!labelRoot || typeof window.Konva === 'undefined') {
        return;
    }

    const patientId = Number(labelRoot.dataset.patientId || 0);
    const canModify = labelRoot.dataset.canModify === 'true';
    const namespace = window.projectNamespace || 'maxillo';
    const segmentationBox = labelRoot.querySelector('[data-segmentation-root]');
    const teethGrid = document.getElementById('intraoralSegmentationTeethGrid');
    const confirmBtn = document.getElementById('segConfirmBtn');
    const onlySelectedBtn = document.getElementById('segOnlySelectedBtn');
    const resetViewBtn = document.getElementById('segResetViewBtn');
    const statusText = document.getElementById('segStatusText');
    const toothCodes = [
        '18', '17', '16', '15', '14', '13', '12', '11',
        '21', '22', '23', '24', '25', '26', '27', '28',
        '48', '47', '46', '45', '44', '43', '42', '41',
        '31', '32', '33', '34', '35', '36', '37', '38',
    ];
    const palette = ['#1E5BFF', '#00A9FF', '#00D4C7', '#38D66B', '#DCEB00', '#FFF066'];
    const curveTension = 0.35;
    const fillAlpha = {
        selected: 0.24,
        normal: 0.14,
    };
    const minProposalSegmentPx = 24;
    const toothIconCache = {};

    const state = {
        container: null,
        images: [],
        teethByFileId: {},
        selectedImage: null,
        selectedTooth: toothCodes[0],
        selectedPolygon: null,
        selectedVertex: null,
        drawing: false,
        draftPoints: [],
        stage: null,
        imageLayer: null,
        polygonLayer: null,
        handleLayer: null,
        imageObj: null,
        scale: 1,
        zoom: 1,
        panX: 0,
        panY: 0,
        isPanning: false,
        didPan: false,
        panStart: null,
        saveTimers: {},
        saveVersions: {},
        saveInFlight: {},
        savePending: {},
        resizeTimer: null,
        mountToken: 0,
        undoStack: [],
        redoStack: [],
        currentDraftId: null,
        nextDraftId: 1,
        onlySelectedTooth: false,
    };

    function setStatus(text) {
        if (statusText) statusText.textContent = text;
    }

    function imageStatusText(image = state.selectedImage, tooth = state.selectedTooth) {
        if (!image) return 'Select an image to start annotation.';
        if (image.is_confirmed) return 'Confirmed. Reopen to edit.';
        return `Tooth ${tooth}. Click image to add polygon points.`;
    }

    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text || '';
        return div.innerHTML;
    }

    function getCookie(name) {
        const cookies = document.cookie ? document.cookie.split(';') : [];
        for (let i = 0; i < cookies.length; i += 1) {
            const cookie = cookies[i].trim();
            if (cookie.startsWith(name + '=')) return decodeURIComponent(cookie.slice(name.length + 1));
        }
        return '';
    }

    function csrfToken() {
        const input = document.querySelector('input[name="csrfmiddlewaretoken"]');
        return (input && input.value) || getCookie('csrftoken');
    }

    function interpolateHex(a, b, t) {
        const ah = a.replace('#', '');
        const bh = b.replace('#', '');
        const av = [parseInt(ah.slice(0, 2), 16), parseInt(ah.slice(2, 4), 16), parseInt(ah.slice(4, 6), 16)];
        const bv = [parseInt(bh.slice(0, 2), 16), parseInt(bh.slice(2, 4), 16), parseInt(bh.slice(4, 6), 16)];
        const out = av.map((v, i) => Math.round(v + (bv[i] - v) * t));
        return `#${out.map(v => v.toString(16).padStart(2, '0')).join('')}`;
    }

    function gradientColor(index, total) {
        const t = total <= 1 ? 0 : index / (total - 1);
        const segment = t * (palette.length - 1);
        const left = Math.floor(segment);
        const right = Math.min(left + 1, palette.length - 1);
        return interpolateHex(palette[left], palette[right], segment - left);
    }

    function toothColor(code) {
        const n = Number(code);
        const q = Math.floor(n / 10);
        const t = n % 10;
        if ((q === 1 || q === 2) && t >= 1 && t <= 8) return gradientColor(q === 1 ? 8 - t : 8 + t - 1, 16);
        if ((q === 3 || q === 4) && t >= 1 && t <= 8) return gradientColor(q === 4 ? 8 - t : 8 + t - 1, 16);
        return palette[0];
    }

    function toothIconSource(code) {
        const q = code[0];
        const t = code[1];
        if (q === '2') return `1${t}`;
        if (q === '4') return t === '7' ? '36' : `3${t}`;
        if (code === '37') return '36';
        return code;
    }

    function toothIconMirrored(code) {
        return code[0] === '2' || code[0] === '4';
    }

    function normalizeToothSvg(svgText) {
        return svgText
            .replace(/<\?xml[\s\S]*?\?>/g, '')
            .replace(/<!DOCTYPE[\s\S]*?>/gi, '')
            .replace(/fill="#b2f2bb"/gi, 'fill="currentColor"')
            .replace(/<svg\b/i, '<svg aria-hidden="true" focusable="false"');
    }

    function loadToothSvg(source) {
        if (!toothIconCache[source]) {
            toothIconCache[source] = fetch(`/static/icons/teeth/${source}.svg`)
                .then(response => (response.ok ? response.text() : ''))
                .then(normalizeToothSvg)
                .catch(() => '');
        }
        return toothIconCache[source];
    }

    function hexToRgba(hex, alpha) {
        const h = hex.replace('#', '');
        const r = parseInt(h.slice(0, 2), 16);
        const g = parseInt(h.slice(2, 4), 16);
        const b = parseInt(h.slice(4, 6), 16);
        return `rgba(${r}, ${g}, ${b}, ${alpha})`;
    }

    function isPoint(value) {
        return Array.isArray(value) && value.length >= 2 && Number.isFinite(Number(value[0])) && Number.isFinite(Number(value[1]));
    }

    function normalizePolygon(points) {
        if (!Array.isArray(points)) return [];
        return points
            .filter(isPoint)
            .map(p => [Number(p[0]), Number(p[1])]);
    }

    function normalizeTeeth(raw) {
        const out = {};
        Object.keys(raw || {}).forEach((code) => {
            const value = raw[code];
            if (!Array.isArray(value) || !value.length) return;
            if (isPoint(value[0])) {
                const polygon = normalizePolygon(value);
                if (polygon.length >= 3) out[code] = [polygon];
                return;
            }
            const polygons = value.map(normalizePolygon).filter(p => p.length >= 3);
            if (polygons.length) out[code] = polygons;
        });
        return out;
    }

    function currentTeeth() {
        if (!state.selectedImage) return {};
        if (!state.teethByFileId[state.selectedImage.id]) state.teethByFileId[state.selectedImage.id] = {};
        return state.teethByFileId[state.selectedImage.id];
    }

    function canEditImage(image) {
        return canModify && image && !image.is_confirmed;
    }

    function canEditCurrentImage() {
        return canEditImage(state.selectedImage);
    }

    function allCurrentPolygonsFor(code) {
        const polygons = currentTeeth()[code];
        return Array.isArray(polygons) ? polygons : [];
    }

    function toDisplay(points) {
        return points.flatMap(p => [p[0] * state.scale, p[1] * state.scale]);
    }

    function curveMidpoint(points, idx) {
        const len = points.length;
        const p0 = points[(idx - 1 + len) % len];
        const p1 = points[idx];
        const p2 = points[(idx + 1) % len];
        const p3 = points[(idx + 2) % len];
        if (!curveTension) return [(p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2];

        const controls1 = tensionControls(p0, p1, p2);
        const controls2 = tensionControls(p1, p2, p3);
        return cubicPoint(p1, controls1.after, controls2.before, p2, 0.5);
    }

    function tensionControls(prev, point, next) {
        const d01 = Math.hypot(point[0] - prev[0], point[1] - prev[1]);
        const d12 = Math.hypot(next[0] - point[0], next[1] - point[1]);
        const total = d01 + d12;
        if (!total) return { before: clonePoint(point), after: clonePoint(point) };
        const fa = (curveTension * d01) / total;
        const fb = (curveTension * d12) / total;
        const dx = next[0] - prev[0];
        const dy = next[1] - prev[1];
        return {
            before: [point[0] - fa * dx, point[1] - fa * dy],
            after: [point[0] + fb * dx, point[1] + fb * dy],
        };
    }

    function cubicPoint(start, control1, control2, end, t) {
        const mt = 1 - t;
        const mt2 = mt * mt;
        const t2 = t * t;
        return [
            mt2 * mt * start[0] + 3 * mt2 * t * control1[0] + 3 * mt * t2 * control2[0] + t2 * t * end[0],
            mt2 * mt * start[1] + 3 * mt2 * t * control1[1] + 3 * mt * t2 * control2[1] + t2 * t * end[1],
        ];
    }

    function polygonCenter(points) {
        let twiceArea = 0;
        let cx = 0;
        let cy = 0;
        points.forEach((point, idx) => {
            const next = points[(idx + 1) % points.length];
            const cross = point[0] * next[1] - next[0] * point[1];
            twiceArea += cross;
            cx += (point[0] + next[0]) * cross;
            cy += (point[1] + next[1]) * cross;
        });
        if (Math.abs(twiceArea) > 0.001) return [cx / (3 * twiceArea), cy / (3 * twiceArea)];
        const sum = points.reduce((acc, point) => [acc[0] + point[0], acc[1] + point[1]], [0, 0]);
        return [sum[0] / points.length, sum[1] / points.length];
    }

    function polygonsBounds(polygons) {
        let minX = Infinity;
        let minY = Infinity;
        let maxX = -Infinity;
        let maxY = -Infinity;
        polygons.forEach((polygon) => {
            polygon.forEach((point) => {
                minX = Math.min(minX, point[0]);
                minY = Math.min(minY, point[1]);
                maxX = Math.max(maxX, point[0]);
                maxY = Math.max(maxY, point[1]);
            });
        });
        if (!Number.isFinite(minX)) return null;
        return { minX, minY, maxX, maxY };
    }

    function zoomToTooth(tooth) {
        if (!state.stage) return;
        const polygons = allCurrentPolygonsFor(tooth);
        const bounds = polygonsBounds(polygons);
        if (!bounds) return;
        const margin = 64;
        const boxWidth = Math.max((bounds.maxX - bounds.minX) * state.scale, 1);
        const boxHeight = Math.max((bounds.maxY - bounds.minY) * state.scale, 1);
        const nextZoom = Math.max(0.5, Math.min(8, Math.min(
            (state.stage.width() - margin * 2) / boxWidth,
            (state.stage.height() - margin * 2) / boxHeight,
        )));
        const centerX = ((bounds.minX + bounds.maxX) / 2) * state.scale;
        const centerY = ((bounds.minY + bounds.maxY) / 2) * state.scale;
        state.zoom = nextZoom;
        state.panX = state.stage.width() / 2 - centerX * nextZoom;
        state.panY = state.stage.height() / 2 - centerY * nextZoom;
        applyViewport();
    }

    function clampOriginalPoint(x, y) {
        if (!state.imageObj) return [Number(x), Number(y)];
        return [
            Number(Math.max(0, Math.min(state.imageObj.width, x)).toFixed(3)),
            Number(Math.max(0, Math.min(state.imageObj.height, y)).toFixed(3)),
        ];
    }

    function applyViewport() {
        applyFixedOverlayScale();
        [state.imageLayer, state.polygonLayer, state.handleLayer].forEach((layer) => {
            if (!layer) return;
            layer.position({ x: state.panX, y: state.panY });
            layer.scale({ x: state.zoom, y: state.zoom });
            layer.batchDraw();
        });
    }

    function applyFixedOverlayScale() {
        const fixedScale = 1 / Math.max(state.zoom, 0.001);
        [state.polygonLayer, state.handleLayer].forEach((layer) => {
            if (!layer) return;
            layer.getChildren().forEach((node) => {
                if (!node.getAttr('fixedScreenSize')) return;
                node.scale({ x: fixedScale, y: fixedScale });
            });
        });
    }

    function resetViewport() {
        state.zoom = 1;
        state.panX = 0;
        state.panY = 0;
        state.isPanning = false;
        state.didPan = false;
        state.panStart = null;
        applyViewport();
    }

    function pointerOriginal() {
        if (!state.stage || !state.imageObj) return null;
        const p = state.stage.getPointerPosition();
        if (!p) return null;
        return clampOriginalPoint(
            (p.x - state.panX) / (state.scale * state.zoom),
            (p.y - state.panY) / (state.scale * state.zoom),
        );
    }

    function currentImageById(fileId) {
        return state.images.find(image => image.id === Number(fileId));
    }

    function clonePoint(point) {
        return [Number(point[0]), Number(point[1])];
    }

    function clonePolygon(polygon) {
        return polygon.map(clonePoint);
    }

    function teethForFile(fileId) {
        if (!state.teethByFileId[fileId]) state.teethByFileId[fileId] = {};
        return state.teethByFileId[fileId];
    }

    function polygonsForFile(fileId, tooth) {
        const teeth = teethForFile(fileId);
        if (!Array.isArray(teeth[tooth])) teeth[tooth] = [];
        return teeth[tooth];
    }

    function cleanupTooth(fileId, tooth) {
        const teeth = teethForFile(fileId);
        if (Array.isArray(teeth[tooth]) && !teeth[tooth].length) delete teeth[tooth];
    }

    function refreshAfterHistory(fileId, save, message) {
        if (!state.selectedImage || state.selectedImage.id !== fileId) {
            const image = currentImageById(fileId);
            if (image && state.container) {
                state.selectedImage = image;
                renderFocused();
            }
        }
        if (state.selectedImage && state.selectedImage.id === fileId) {
            renderLabels();
            redrawStage();
        }
        if (save) scheduleSave(fileId);
        setStatus(message);
    }

    function removeDraftHistory(draftId) {
        if (!draftId) return;
        state.undoStack = state.undoStack.filter(action => action.draftId !== draftId);
        state.redoStack = state.redoStack.filter(action => action.draftId !== draftId);
    }

    function recordAction(action) {
        if (!canModify) return;
        state.undoStack.push(action);
        if (state.undoStack.length > 100) state.undoStack.shift();
        state.redoStack = [];
    }

    function insertPolygon(fileId, tooth, index, polygon) {
        const polygons = polygonsForFile(fileId, tooth);
        polygons.splice(Math.min(index, polygons.length), 0, clonePolygon(polygon));
    }

    function removePolygon(fileId, tooth, index) {
        const polygons = polygonsForFile(fileId, tooth);
        const removed = polygons.splice(index, 1)[0] || null;
        cleanupTooth(fileId, tooth);
        return removed;
    }

    function insertVertex(fileId, tooth, polygonIndex, pointIndex, point) {
        const polygon = polygonsForFile(fileId, tooth)[polygonIndex];
        if (!polygon) return;
        polygon.splice(Math.min(pointIndex, polygon.length), 0, clonePoint(point));
    }

    function removeVertex(fileId, tooth, polygonIndex, pointIndex) {
        const polygon = polygonsForFile(fileId, tooth)[polygonIndex];
        if (!polygon) return null;
        return polygon.splice(pointIndex, 1)[0] || null;
    }

    function setVertex(fileId, tooth, polygonIndex, pointIndex, point) {
        const polygon = polygonsForFile(fileId, tooth)[polygonIndex];
        if (!polygon || !polygon[pointIndex]) return;
        polygon[pointIndex] = clonePoint(point);
    }

    function applyHistoryAction(action, direction) {
        const undo = direction === 'undo';
        let save = true;
        if (action.type === 'polygon-create') {
            if (undo) {
                removePolygon(action.fileId, action.tooth, action.polygonIndex);
                state.selectedPolygon = null;
            } else {
                insertPolygon(action.fileId, action.tooth, action.polygonIndex, action.polygon);
                state.selectedPolygon = { tooth: action.tooth, index: action.polygonIndex };
            }
            state.selectedVertex = null;
        } else if (action.type === 'polygon-delete') {
            if (undo) {
                insertPolygon(action.fileId, action.tooth, action.polygonIndex, action.polygon);
                state.selectedPolygon = { tooth: action.tooth, index: action.polygonIndex };
            } else {
                removePolygon(action.fileId, action.tooth, action.polygonIndex);
                state.selectedPolygon = null;
            }
            state.selectedVertex = null;
        } else if (action.type === 'vertex-insert') {
            if (undo) {
                removeVertex(action.fileId, action.tooth, action.polygonIndex, action.pointIndex);
                state.selectedVertex = null;
            } else {
                insertVertex(action.fileId, action.tooth, action.polygonIndex, action.pointIndex, action.point);
                state.selectedVertex = { tooth: action.tooth, polygonIndex: action.polygonIndex, pointIndex: action.pointIndex };
            }
            state.selectedPolygon = { tooth: action.tooth, index: action.polygonIndex };
        } else if (action.type === 'vertex-delete') {
            if (undo) {
                insertVertex(action.fileId, action.tooth, action.polygonIndex, action.pointIndex, action.point);
                state.selectedVertex = { tooth: action.tooth, polygonIndex: action.polygonIndex, pointIndex: action.pointIndex };
            } else {
                removeVertex(action.fileId, action.tooth, action.polygonIndex, action.pointIndex);
                state.selectedVertex = null;
            }
            state.selectedPolygon = { tooth: action.tooth, index: action.polygonIndex };
        } else if (action.type === 'vertex-move') {
            setVertex(action.fileId, action.tooth, action.polygonIndex, action.pointIndex, undo ? action.from : action.to);
            state.selectedPolygon = { tooth: action.tooth, index: action.polygonIndex };
            state.selectedVertex = { tooth: action.tooth, polygonIndex: action.polygonIndex, pointIndex: action.pointIndex };
        } else if (action.type === 'draft-add') {
            save = false;
            if (undo) state.draftPoints.splice(action.pointIndex, 1);
            else state.draftPoints.splice(Math.min(action.pointIndex, state.draftPoints.length), 0, clonePoint(action.point));
            state.drawing = state.draftPoints.length > 0;
            state.selectedTooth = action.tooth;
            state.currentDraftId = action.draftId;
        } else if (action.type === 'draft-delete') {
            save = false;
            if (undo) state.draftPoints.splice(Math.min(action.pointIndex, state.draftPoints.length), 0, clonePoint(action.point));
            else state.draftPoints.splice(action.pointIndex, 1);
            state.drawing = state.draftPoints.length > 0;
            state.selectedTooth = action.tooth;
            state.currentDraftId = action.draftId;
        } else if (action.type === 'drawing-cancel') {
            save = false;
            state.draftPoints = undo ? clonePolygon(action.points) : [];
            state.drawing = undo && state.draftPoints.length > 0;
            state.selectedTooth = action.tooth;
            state.currentDraftId = action.draftId;
        }

        if (action.tooth) state.selectedTooth = action.tooth;
        refreshAfterHistory(action.fileId, save, undo ? 'Undone.' : 'Redone.');
    }

    function undoAction() {
        if (state.selectedImage && state.selectedImage.is_confirmed) return setStatus('Confirmed. Reopen to edit.');
        const action = state.undoStack.pop();
        if (!action) return setStatus('Nothing to undo.');
        applyHistoryAction(action, 'undo');
        state.redoStack.push(action);
    }

    function redoAction() {
        if (state.selectedImage && state.selectedImage.is_confirmed) return setStatus('Confirmed. Reopen to edit.');
        const action = state.redoStack.pop();
        if (!action) return setStatus('Nothing to redo.');
        applyHistoryAction(action, 'redo');
        state.undoStack.push(action);
    }

    function scheduleSave(fileId) {
        if (!canModify || !fileId) return;
        const key = String(fileId);
        state.saveVersions[key] = (state.saveVersions[key] || 0) + 1;
        window.clearTimeout(state.saveTimers[key]);
        setStatus('Saving...');
        state.saveTimers[key] = window.setTimeout(() => saveImage(fileId), 250);
    }

    async function saveImage(fileId) {
        if (!canModify) return;
        const key = String(fileId);
        if (state.saveInFlight[key]) {
            state.savePending[key] = true;
            return;
        }

        const image = currentImageById(fileId);
        if (!image) return;

        const version = state.saveVersions[key] || 0;
        const payload = {
            images: [{
                file_id: image.id,
                updated_at: image.updated_at || null,
                is_confirmed: !!image.is_confirmed,
                teeth: state.teethByFileId[image.id] || {},
            }],
        };

        state.saveInFlight[key] = true;
        try {
            const response = await fetch(`/${namespace}/api/patient/${patientId}/intraoral-segmentation/update/`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
                body: JSON.stringify(payload),
            });
            const data = await response.json();
            if (response.ok) {
                if (Array.isArray(data.images)) {
                    data.images.forEach((savedImage) => {
                        const target = currentImageById(savedImage.file_id);
                        if (target) {
                            target.updated_at = savedImage.updated_at || null;
                            target.is_confirmed = !!savedImage.is_confirmed;
                            target.confirmed_at = savedImage.confirmed_at || null;
                            target.confirmed_by = savedImage.confirmed_by || null;
                        }
                    });
                }
                if ((state.saveVersions[key] || 0) === version && !state.savePending[key]) {
                    setStatus('Saved.');
                }
            } else if (response.status === 409) {
                setStatus(data.error || 'Segmentation changed elsewhere. Reload before editing.');
            } else {
                setStatus(data.error || 'Save failed.');
            }
        } catch (error) {
            setStatus('Save failed.');
        } finally {
            state.saveInFlight[key] = false;
            if (state.savePending[key] || (state.saveVersions[key] || 0) !== version) {
                state.savePending[key] = false;
                saveImage(fileId);
            }
        }
    }

    function polygonCountForCurrentImage(code) {
        if (!state.selectedImage) return 0;
        return allCurrentPolygonsFor(code).length;
    }

    function renderLabels() {
        if (segmentationBox) segmentationBox.classList.toggle('has-selected-image', !!state.selectedImage);
        if (!teethGrid) return;
        teethGrid.innerHTML = '';
        toothCodes.forEach((code) => {
            const count = polygonCountForCurrentImage(code);
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'seg-tooth-btn';
            btn.classList.toggle('selected', code === state.selectedTooth);
            btn.classList.toggle('no-mask', count === 0);
            btn.dataset.tooth = code;
            btn.style.setProperty('--tooth-color', toothColor(code));
            btn.setAttribute('aria-label', `Tooth ${code}`);

            const icon = document.createElement('span');
            icon.className = 'seg-tooth-icon';
            icon.classList.toggle('mirrored', toothIconMirrored(code));
            icon.setAttribute('aria-hidden', 'true');
            btn.appendChild(icon);
            loadToothSvg(toothIconSource(code)).then((svg) => {
                if (icon.isConnected) icon.innerHTML = svg;
            });

            const codeText = document.createElement('span');
            codeText.className = 'seg-tooth-code';
            codeText.textContent = code;
            btn.appendChild(codeText);
            if (count > 0) {
                const badge = document.createElement('span');
                badge.className = 'seg-count';
                badge.textContent = String(count);
                btn.appendChild(badge);
            }
            btn.addEventListener('click', () => {
                if (state.drawing && state.draftPoints.length && state.selectedImage) {
                    recordAction({
                        type: 'drawing-cancel',
                        fileId: state.selectedImage.id,
                        tooth: state.selectedTooth,
                        draftId: state.currentDraftId,
                        points: clonePolygon(state.draftPoints),
                    });
                }
                state.selectedTooth = code;
                state.selectedPolygon = null;
                state.selectedVertex = null;
                state.drawing = false;
                state.draftPoints = [];
                state.currentDraftId = null;
                renderLabels();
                redrawStage();
                setStatus(imageStatusText(state.selectedImage, code));
            });
            btn.addEventListener('dblclick', (evt) => {
                evt.preventDefault();
                if (!state.selectedImage || !polygonCountForCurrentImage(code)) return;
                state.selectedTooth = code;
                zoomToTooth(code);
                renderLabels();
                redrawStage();
            });
            teethGrid.appendChild(btn);
        });
        renderOnlySelectedButton();
        renderConfirmButton();
    }

    function renderOnlySelectedButton() {
        if (!onlySelectedBtn) return;
        onlySelectedBtn.classList.toggle('active', state.onlySelectedTooth);
        onlySelectedBtn.textContent = state.onlySelectedTooth ? 'Show all' : 'Only selected';
        onlySelectedBtn.disabled = !state.selectedImage;
        if (resetViewBtn) resetViewBtn.disabled = !state.selectedImage;
    }

    function renderConfirmButton() {
        if (!confirmBtn) return;
        const confirmed = !!(state.selectedImage && state.selectedImage.is_confirmed);
        confirmBtn.classList.toggle('confirmed', confirmed);
        confirmBtn.textContent = confirmed ? 'Reopen' : 'Mark done';
        confirmBtn.disabled = !state.selectedImage || !canModify;
    }

    function toggleCurrentConfirmation() {
        if (!state.selectedImage || !canModify) return;
        state.selectedImage.is_confirmed = !state.selectedImage.is_confirmed;
        if (state.selectedImage.is_confirmed) {
            state.selectedVertex = null;
            state.drawing = false;
            state.draftPoints = [];
            state.currentDraftId = null;
        }
        renderLabels();
        updateConfirmationBadges();
        redrawStage();
        scheduleSave(state.selectedImage.id);
        setStatus(state.selectedImage.is_confirmed ? 'Confirmed. Reopen to edit.' : 'Reopened. Editing enabled.');
    }

    function selectPolygon(tooth, index) {
        if (state.drawing && state.draftPoints.length && state.selectedImage) {
            recordAction({
                type: 'drawing-cancel',
                fileId: state.selectedImage.id,
                tooth: state.selectedTooth,
                draftId: state.currentDraftId,
                points: clonePolygon(state.draftPoints),
            });
        }
        state.selectedTooth = tooth;
        state.selectedPolygon = { tooth, index };
        state.selectedVertex = null;
        state.drawing = false;
        state.draftPoints = [];
        state.currentDraftId = null;
        renderLabels();
        redrawStage();
        setStatus(canEditCurrentImage() ? `Editing tooth ${tooth} polygon ${index + 1}. Drag points or click midpoint.` : 'Confirmed. Reopen to edit.');
    }

    function insertPoint(tooth, polygonIndex, afterIndex, point) {
        const polygons = allCurrentPolygonsFor(tooth);
        if (!polygons[polygonIndex]) return;
        const pointIndex = afterIndex + 1;
        polygons[polygonIndex].splice(afterIndex + 1, 0, point);
        state.selectedPolygon = { tooth, index: polygonIndex };
        state.selectedVertex = { tooth, polygonIndex, pointIndex };
        recordAction({
            type: 'vertex-insert',
            fileId: state.selectedImage.id,
            tooth,
            polygonIndex,
            pointIndex,
            point: clonePoint(point),
        });
        redrawStage();
        scheduleSave(state.selectedImage.id);
    }

    function updatePoint(tooth, polygonIndex, pointIndex, point, line) {
        const polygon = allCurrentPolygonsFor(tooth)[polygonIndex];
        if (!polygon || !polygon[pointIndex]) return;
        const next = clampOriginalPoint(point[0], point[1]);
        polygon[pointIndex] = next;
        if (line) {
            line.points(toDisplay(polygon));
            state.polygonLayer.batchDraw();
        }
    }

    function updateProposalHandles(tooth, polygonIndex, polygon) {
        if (!state.handleLayer) return;
        state.handleLayer.getChildren().forEach((node) => {
            if (node.getAttr('proposalTooth') !== tooth || node.getAttr('proposalPolygonIndex') !== polygonIndex) return;
            const edgeIndex = node.getAttr('proposalEdgeIndex');
            if (!Number.isInteger(edgeIndex) || edgeIndex >= polygon.length) return;
            node.visible(isProposalSegmentLongEnough(polygon, edgeIndex));
            if (!node.visible()) return;
            const mid = curveMidpoint(polygon, edgeIndex);
            node.position({ x: mid[0] * state.scale, y: mid[1] * state.scale });
        });
        state.handleLayer.batchDraw();
    }

    function isProposalSegmentLongEnough(polygon, idx) {
        const start = polygon[idx];
        const end = polygon[(idx + 1) % polygon.length];
        if (!start || !end) return false;
        return Math.hypot(end[0] - start[0], end[1] - start[1]) * state.scale * state.zoom >= minProposalSegmentPx;
    }

    function displayPointerOriginal(node) {
        return clampOriginalPoint(
            node.x() / state.scale,
            node.y() / state.scale,
        );
    }

    function renderHandles(tooth, polygonIndex, polygon, line) {
        polygon.forEach((point, idx) => {
            const selectedVertex = state.selectedVertex
                && state.selectedVertex.tooth === tooth
                && state.selectedVertex.polygonIndex === polygonIndex
                && state.selectedVertex.pointIndex === idx;
            const handle = new window.Konva.Circle({
                x: point[0] * state.scale,
                y: point[1] * state.scale,
                radius: selectedVertex ? 7 : 5,
                fill: selectedVertex ? '#ffc107' : '#fff',
                stroke: '#0B5ED7',
                strokeWidth: 2,
                draggable: canEditCurrentImage(),
                fixedScreenSize: true,
            });
            let dragStartPoint = null;
            handle.on('mousedown touchstart', evt => { evt.cancelBubble = true; });
            handle.on('dragstart', () => {
                dragStartPoint = clonePoint(polygon[idx]);
            });
            handle.on('click tap', (evt) => {
                evt.cancelBubble = true;
                state.selectedVertex = { tooth, polygonIndex, pointIndex: idx };
                redrawStage();
                setStatus(`Point ${idx + 1} selected. Drag it, or press Delete to remove it.`);
            });
            handle.on('dragmove', () => {
                const next = displayPointerOriginal(handle);
                updatePoint(tooth, polygonIndex, idx, next, line);
                handle.position({ x: next[0] * state.scale, y: next[1] * state.scale });
                updateProposalHandles(tooth, polygonIndex, polygon);
            });
            handle.on('dragend', () => {
                state.selectedVertex = { tooth, polygonIndex, pointIndex: idx };
                const dragEndPoint = clonePoint(polygon[idx]);
                if (dragStartPoint && (dragStartPoint[0] !== dragEndPoint[0] || dragStartPoint[1] !== dragEndPoint[1])) {
                    recordAction({
                        type: 'vertex-move',
                        fileId: state.selectedImage.id,
                        tooth,
                        polygonIndex,
                        pointIndex: idx,
                        from: dragStartPoint,
                        to: dragEndPoint,
                    });
                }
                dragStartPoint = null;
                redrawStage();
                scheduleSave(state.selectedImage.id);
            });
            state.handleLayer.add(handle);
        });

        polygon.forEach((_point, idx) => {
            if (!isProposalSegmentLongEnough(polygon, idx)) return;
            const mid = curveMidpoint(polygon, idx);
            let insertedIndex = null;
            let didDrag = false;
            let recordedInsert = false;
            function ensureInserted() {
                if (insertedIndex !== null) return insertedIndex;
                insertedIndex = idx + 1;
                const currentMid = curveMidpoint(polygon, idx);
                polygon.splice(insertedIndex, 0, [Number(currentMid[0].toFixed(3)), Number(currentMid[1].toFixed(3))]);
                state.selectedPolygon = { tooth, index: polygonIndex };
                state.selectedVertex = { tooth, polygonIndex, pointIndex: insertedIndex };
                line.points(toDisplay(polygon));
                state.polygonLayer.batchDraw();
                setStatus(`Point ${insertedIndex + 1} added. Keep dragging to position it.`);
                return insertedIndex;
            }
            function recordInsertedPoint() {
                if (insertedIndex === null || recordedInsert) return;
                recordedInsert = true;
                recordAction({
                    type: 'vertex-insert',
                    fileId: state.selectedImage.id,
                    tooth,
                    polygonIndex,
                    pointIndex: insertedIndex,
                    point: clonePoint(polygon[insertedIndex]),
                });
            }
            const proposal = new window.Konva.Circle({
                x: mid[0] * state.scale,
                y: mid[1] * state.scale,
                radius: 3.5,
                fill: '#8f98a3',
                stroke: '#ffffff',
                strokeWidth: 1,
                draggable: canEditCurrentImage(),
                proposalTooth: tooth,
                proposalPolygonIndex: polygonIndex,
                proposalEdgeIndex: idx,
                fixedScreenSize: true,
            });
            proposal.on('mousedown touchstart', (evt) => {
                evt.cancelBubble = true;
                ensureInserted();
            });
            proposal.on('dragstart', (evt) => {
                evt.cancelBubble = true;
                didDrag = true;
                ensureInserted();
            });
            proposal.on('dragmove', (evt) => {
                evt.cancelBubble = true;
                didDrag = true;
                const pointIndex = ensureInserted();
                const nextPoint = displayPointerOriginal(proposal);
                proposal.position({ x: nextPoint[0] * state.scale, y: nextPoint[1] * state.scale });
                updatePoint(tooth, polygonIndex, pointIndex, nextPoint, line);
            });
            proposal.on('dragend', (evt) => {
                evt.cancelBubble = true;
                recordInsertedPoint();
                redrawStage();
                scheduleSave(state.selectedImage.id);
            });
            proposal.on('mouseup touchend', (evt) => {
                evt.cancelBubble = true;
                if (insertedIndex !== null && !didDrag) {
                    recordInsertedPoint();
                    redrawStage();
                    scheduleSave(state.selectedImage.id);
                }
            });
            proposal.on('click tap', (evt) => {
                evt.cancelBubble = true;
                if (insertedIndex === null) {
                    const currentMid = curveMidpoint(polygon, idx);
                    insertPoint(tooth, polygonIndex, idx, [Number(currentMid[0].toFixed(3)), Number(currentMid[1].toFixed(3))]);
                }
            });
            state.handleLayer.add(proposal);
        });
    }

    function redrawStage() {
        if (!state.stage || !state.polygonLayer || !state.handleLayer) return;
        state.polygonLayer.destroyChildren();
        state.handleLayer.destroyChildren();
        const teeth = currentTeeth();

        Object.keys(teeth).forEach((tooth) => {
            if (state.onlySelectedTooth && tooth !== state.selectedTooth) return;
            const polygons = Array.isArray(teeth[tooth]) ? teeth[tooth] : [];
            polygons.forEach((polygon, index) => {
                if (!Array.isArray(polygon) || polygon.length < 3) return;
                const selected = state.selectedPolygon && state.selectedPolygon.tooth === tooth && state.selectedPolygon.index === index;
                const color = toothColor(tooth);
                const line = new window.Konva.Line({
                    points: toDisplay(polygon),
                    closed: true,
                    tension: curveTension,
                    fill: hexToRgba(color, selected ? fillAlpha.selected : fillAlpha.normal),
                    stroke: hexToRgba(color, selected ? 1 : 0.95),
                    strokeWidth: selected ? 2.6 : 1.6,
                    strokeScaleEnabled: false,
                });
                line.on('click tap', (evt) => {
                    evt.cancelBubble = true;
                    if (state.didPan) return;
                    selectPolygon(tooth, index);
                });
                state.polygonLayer.add(line);
                if (!(selected && canEditCurrentImage())) {
                    const center = polygonCenter(polygon);
                    const label = new window.Konva.Text({
                        x: center[0] * state.scale,
                        y: center[1] * state.scale,
                        text: tooth,
                        fontSize: 13,
                        fontStyle: 'bold',
                        fill: '#ffffff',
                        stroke: '#111827',
                        strokeWidth: 3,
                        fillAfterStrokeEnabled: true,
                        listening: false,
                        fixedScreenSize: true,
                    });
                    label.offsetX(label.width() / 2);
                    label.offsetY(label.height() / 2);
                    state.polygonLayer.add(label);
                }
                if (selected && canEditCurrentImage()) renderHandles(tooth, index, polygon, line);
            });
        });

        if (state.drawing && state.draftPoints.length) {
            state.handleLayer.add(new window.Konva.Line({
                points: toDisplay(state.draftPoints),
                stroke: '#0B5ED7',
                strokeWidth: 2,
                strokeScaleEnabled: false,
                tension: curveTension,
                dash: [6, 4],
            }));
            state.draftPoints.forEach((point, idx) => {
                const draftHandle = new window.Konva.Circle({
                    x: point[0] * state.scale,
                    y: point[1] * state.scale,
                    radius: idx === 0 ? 5 : 4,
                    fill: idx === 0 ? '#198754' : '#0B5ED7',
                    stroke: '#fff',
                    strokeWidth: 1.5,
                    fixedScreenSize: true,
                });
                if (idx === 0 && state.draftPoints.length >= 3) {
                    draftHandle.on('click tap', (evt) => {
                        evt.cancelBubble = true;
                        finishDrawing();
                    });
                }
                state.handleLayer.add(draftHandle);
            });
        }

        applyFixedOverlayScale();
        state.polygonLayer.draw();
        state.handleLayer.draw();
    }

    function finishDrawing() {
        if (!state.drawing || !canEditCurrentImage()) return;
        if (state.draftPoints.length < 3) {
            setStatus('Need at least 3 points.');
            return;
        }
        const teeth = currentTeeth();
        if (!Array.isArray(teeth[state.selectedTooth])) teeth[state.selectedTooth] = [];
        const polygon = state.draftPoints.slice();
        const draftId = state.currentDraftId;
        teeth[state.selectedTooth].push(polygon);
        state.selectedPolygon = { tooth: state.selectedTooth, index: teeth[state.selectedTooth].length - 1 };
        state.selectedVertex = null;
        state.drawing = false;
        state.draftPoints = [];
        state.currentDraftId = null;
        removeDraftHistory(draftId);
        recordAction({
            type: 'polygon-create',
            fileId: state.selectedImage.id,
            tooth: state.selectedTooth,
            polygonIndex: state.selectedPolygon.index,
            polygon: clonePolygon(polygon),
        });
        renderLabels();
        redrawStage();
        scheduleSave(state.selectedImage.id);
    }

    function startOrContinueDrawing(point) {
        if (!canEditCurrentImage()) return;
        if (!state.drawing) {
            state.selectedPolygon = null;
            state.selectedVertex = null;
            state.drawing = true;
            state.draftPoints = [];
            state.currentDraftId = state.nextDraftId;
            state.nextDraftId += 1;
        }
        if (state.draftPoints.length >= 3) {
            const first = state.draftPoints[0];
            if (Math.hypot(point[0] - first[0], point[1] - first[1]) <= 10 / Math.max(state.scale, 0.01)) {
                finishDrawing();
                return;
            }
        }
        const pointIndex = state.draftPoints.length;
        state.draftPoints.push(point);
        recordAction({
            type: 'draft-add',
            fileId: state.selectedImage.id,
            tooth: state.selectedTooth,
            draftId: state.currentDraftId,
            pointIndex,
            point: clonePoint(point),
        });
        redrawStage();
        setStatus(`Drawing tooth ${state.selectedTooth}: ${state.draftPoints.length} points.`);
    }

    function deleteSelectedVertex() {
        if (!canEditCurrentImage() || !state.selectedVertex) return false;
        const { tooth, polygonIndex, pointIndex } = state.selectedVertex;
        const polygon = allCurrentPolygonsFor(tooth)[polygonIndex];
        if (!polygon) return false;
        if (polygon.length <= 3) {
            setStatus('A polygon needs at least 3 points. Delete the polygon instead.');
            return true;
        }
        const point = clonePoint(polygon[pointIndex]);
        polygon.splice(pointIndex, 1);
        state.selectedPolygon = { tooth, index: polygonIndex };
        state.selectedVertex = null;
        recordAction({
            type: 'vertex-delete',
            fileId: state.selectedImage.id,
            tooth,
            polygonIndex,
            pointIndex,
            point,
        });
        redrawStage();
        scheduleSave(state.selectedImage.id);
        setStatus(`Point removed from tooth ${tooth}.`);
        return true;
    }

    function deleteSelected() {
        if (state.selectedImage && state.selectedImage.is_confirmed) {
            setStatus('Confirmed. Reopen to edit.');
            return false;
        }
        if (deleteSelectedVertex()) return true;
        if (deleteSelectedPolygon()) return true;
        if (state.drawing && state.draftPoints.length) {
            const pointIndex = state.draftPoints.length - 1;
            const point = clonePoint(state.draftPoints[pointIndex]);
            state.draftPoints.pop();
            recordAction({
                type: 'draft-delete',
                fileId: state.selectedImage.id,
                tooth: state.selectedTooth,
                draftId: state.currentDraftId,
                pointIndex,
                point,
            });
            if (!state.draftPoints.length) state.drawing = false;
            redrawStage();
            setStatus(state.drawing ? `Drawing tooth ${state.selectedTooth}: ${state.draftPoints.length} points.` : 'Draft removed.');
            return true;
        }
        setStatus('Select a polygon or point to delete.');
        return false;
    }

    function deleteSelectedPolygon() {
        if (!canEditCurrentImage() || !state.selectedPolygon) return false;
        const { tooth, index } = state.selectedPolygon;
        const teeth = currentTeeth();
        const polygons = Array.isArray(teeth[tooth]) ? teeth[tooth] : [];
        if (!polygons[index]) return false;
        const polygon = clonePolygon(polygons[index]);
        polygons.splice(index, 1);
        if (!polygons.length) delete teeth[tooth];
        state.selectedPolygon = null;
        state.selectedVertex = null;
        recordAction({
            type: 'polygon-delete',
            fileId: state.selectedImage.id,
            tooth,
            polygonIndex: index,
            polygon,
        });
        renderLabels();
        redrawStage();
        scheduleSave(state.selectedImage.id);
        setStatus(`Tooth ${tooth} polygon deleted.`);
        return true;
    }

    function resetToGrid() {
        if (state.drawing && state.draftPoints.length && state.selectedImage) {
            recordAction({
                type: 'drawing-cancel',
                fileId: state.selectedImage.id,
                tooth: state.selectedTooth,
                draftId: state.currentDraftId,
                points: clonePolygon(state.draftPoints),
            });
        }
        state.selectedImage = null;
        state.selectedPolygon = null;
        state.selectedVertex = null;
        state.drawing = false;
        state.draftPoints = [];
        state.currentDraftId = null;
        destroyStage();
        if (state.container) state.container.classList.remove('is-focused');
        renderLabels();
        setStatus('Select an image to start annotation.');
    }

    function selectImage(image) {
        if (state.drawing && state.draftPoints.length && state.selectedImage) {
            recordAction({
                type: 'drawing-cancel',
                fileId: state.selectedImage.id,
                tooth: state.selectedTooth,
                draftId: state.currentDraftId,
                points: clonePolygon(state.draftPoints),
            });
        }
        state.selectedImage = image;
        state.selectedPolygon = null;
        state.selectedVertex = null;
        state.drawing = false;
        state.draftPoints = [];
        state.currentDraftId = null;
        renderFocused();
        renderLabels();
    }

    function destroyStage() {
        state.mountToken += 1;
        if (state.stage) state.stage.destroy();
        state.stage = null;
        state.imageLayer = null;
        state.polygonLayer = null;
        state.handleLayer = null;
        state.imageObj = null;
        state.scale = 1;
        state.zoom = 1;
        state.panX = 0;
        state.panY = 0;
        state.isPanning = false;
        state.didPan = false;
        state.panStart = null;
    }

    function zoomStage(evt) {
        evt.evt.preventDefault();
        const pointer = state.stage.getPointerPosition();
        if (!pointer) return;
        const oldZoom = state.zoom;
        const direction = evt.evt.deltaY > 0 ? -1 : 1;
        const nextZoom = Math.max(0.5, Math.min(8, oldZoom * (direction > 0 ? 1.08 : 1 / 1.08)));
        const baseX = (pointer.x - state.panX) / oldZoom;
        const baseY = (pointer.y - state.panY) / oldZoom;
        state.zoom = nextZoom;
        state.panX = pointer.x - baseX * nextZoom;
        state.panY = pointer.y - baseY * nextZoom;
        applyViewport();
    }

    function startPan(evt) {
        const source = evt.evt;
        const middleButton = source.button === 1;
        const altLeftButton = source.button === 0 && source.altKey;
        if (!middleButton && !altLeftButton) return;
        source.preventDefault();
        const pointer = state.stage.getPointerPosition();
        if (!pointer) return;
        state.isPanning = true;
        state.didPan = true;
        state.panStart = {
            pointer,
            panX: state.panX,
            panY: state.panY,
        };
        state.stage.container().style.cursor = 'grabbing';
    }

    function movePan(evt) {
        if (!state.isPanning || !state.panStart) return;
        evt.evt.preventDefault();
        const pointer = state.stage.getPointerPosition();
        if (!pointer) return;
        state.panX = state.panStart.panX + pointer.x - state.panStart.pointer.x;
        state.panY = state.panStart.panY + pointer.y - state.panStart.pointer.y;
        applyViewport();
    }

    function stopPan() {
        if (!state.isPanning) return;
        state.isPanning = false;
        state.panStart = null;
        if (state.stage) state.stage.container().style.cursor = '';
        window.setTimeout(() => { state.didPan = false; }, 0);
    }

    function mountStage(host, image) {
        destroyStage();
        const token = ++state.mountToken;
        host.innerHTML = '';
        const img = new window.Image();
        img.onload = () => {
            if (token !== state.mountToken) return;
            state.imageObj = img;
            const shell = host.closest('.intraoral-seg-stage-shell');
            const maxWidth = Math.max(320, (shell ? shell.clientWidth : host.clientWidth) - 2);
            state.scale = maxWidth / img.width;
            const width = Math.round(img.width * state.scale);
            const height = Math.round(img.height * state.scale);

            state.stage = new window.Konva.Stage({ container: host, width, height });
            state.imageLayer = new window.Konva.Layer();
            state.polygonLayer = new window.Konva.Layer();
            state.handleLayer = new window.Konva.Layer();
            state.stage.add(state.imageLayer);
            state.stage.add(state.polygonLayer);
            state.stage.add(state.handleLayer);
            state.imageLayer.add(new window.Konva.Image({ image: img, width, height, listening: false }));
            state.imageLayer.draw();
            resetViewport();
            host.addEventListener('auxclick', (evt) => {
                if (evt.button === 1) evt.preventDefault();
            });
            state.stage.on('wheel', zoomStage);
            state.stage.on('mousedown', startPan);
            state.stage.on('mousemove', movePan);
            state.stage.on('mouseup mouseleave', stopPan);
            state.stage.on('click tap', (evt) => {
                if (state.didPan) {
                    state.didPan = false;
                    return;
                }
                if (evt.target !== state.stage) return;
                if (!canEditCurrentImage()) {
                    setStatus(state.selectedImage && state.selectedImage.is_confirmed ? 'Confirmed. Reopen to edit.' : imageStatusText());
                    return;
                }
                const point = pointerOriginal();
                if (point) startOrContinueDrawing(point);
            });
            state.stage.on('dblclick dbltap', finishDrawing);
            redrawStage();
            setStatus(state.selectedImage.is_confirmed ? 'Confirmed. Reopen to edit.' : `Image ${image.index}. Tooth ${state.selectedTooth}. Click image to add polygon points.`);
        };
        img.onerror = () => {
            if (token === state.mountToken) setStatus('Image failed to load.');
        };
        img.src = image.url;
    }

    function updateConfirmationBadges() {
        if (!state.container) return;
        state.container.querySelectorAll('.intraoral-seg-thumb[data-image-id]').forEach((thumb) => {
            const image = currentImageById(thumb.dataset.imageId);
            let badge = thumb.querySelector('.seg-confirmed-badge');
            if (image && image.is_confirmed) {
                if (!badge) {
                    badge = document.createElement('span');
                    badge.className = 'seg-confirmed-badge';
                    badge.title = 'Segmentation confirmed';
                    thumb.appendChild(badge);
                }
            } else if (badge) {
                badge.remove();
            }
        });
    }

    function renderFocused() {
        if (!state.container || !state.selectedImage) return;
        state.container.classList.add('is-focused');
        const selected = state.container.querySelector('[data-selected-view]');
        if (!selected) return;
        selected.innerHTML = '';

        const strip = document.createElement('div');
        strip.className = 'intraoral-seg-strip';
        state.images.forEach((image) => {
            const thumb = document.createElement('button');
            thumb.type = 'button';
            thumb.className = 'intraoral-seg-thumb';
            thumb.dataset.imageId = String(image.id);
            thumb.classList.toggle('active', image.id === state.selectedImage.id);
            thumb.innerHTML = `<img src="${image.url}" alt=""><span class="visually-hidden">${escapeHtml(image.original_filename || String(image.index))}</span>`;
            if (image.is_confirmed) {
                const badge = document.createElement('span');
                badge.className = 'seg-confirmed-badge';
                badge.title = 'Segmentation confirmed';
                thumb.appendChild(badge);
            }
            thumb.addEventListener('click', () => {
                if (image.id === state.selectedImage.id) resetToGrid();
                else selectImage(image);
            });
            strip.appendChild(thumb);
        });

        const shell = document.createElement('div');
        shell.className = 'intraoral-seg-stage-shell';
        const stageHost = document.createElement('div');
        stageHost.className = 'intraoral-seg-stage';
        shell.appendChild(stageHost);

        selected.appendChild(strip);
        selected.appendChild(shell);
        mountStage(stageHost, state.selectedImage);
    }

    function renderGrid() {
        if (!state.container) return;
        state.container.innerHTML = `
            <div class="intraoral-seg-grid" data-image-grid></div>
            <div class="intraoral-seg-selected" data-selected-view></div>
        `;
        const grid = state.container.querySelector('[data-image-grid]');
        state.images.forEach((image) => {
            const card = document.createElement('button');
            card.type = 'button';
            card.className = 'intraoral-seg-card';
            const label = image.original_filename || `Image ${image.index}`;
            card.innerHTML = `
                <img src="${image.url}" alt="${escapeHtml(label)}">
                <div class="intraoral-seg-caption">${escapeHtml(label)}</div>
            `;
            card.addEventListener('click', () => selectImage(image));
            grid.appendChild(card);
        });
        resetToGrid();
    }

    async function loadSegmentation() {
        const response = await fetch(`/${namespace}/api/patient/${patientId}/intraoral-segmentation/`);
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'Failed to load segmentation.');
        const byId = new Map((Array.isArray(data.images) ? data.images : []).map(image => [image.id, image]));
        state.images = state.images.map((image) => {
            const loaded = byId.get(image.id) || image;
            return {
                ...loaded,
                is_confirmed: !!loaded.is_confirmed,
                confirmed_at: loaded.confirmed_at || null,
                confirmed_by: loaded.confirmed_by || null,
            };
        });
        state.teethByFileId = {};
        state.images.forEach((image) => {
            state.teethByFileId[image.id] = normalizeTeeth(image.teeth || {});
        });
    }

    async function mount(container, images) {
        state.container = container;
        state.container.className = 'intraoral-seg-workspace';
        state.images = Array.isArray(images) ? images.slice() : [];
        if (!state.images.length) return;
        setStatus('Loading segmentation...');
        try {
            await loadSegmentation();
            renderGrid();
        } catch (error) {
            state.teethByFileId = {};
            state.images = state.images.map(image => ({ ...image, is_confirmed: false, confirmed_at: null, confirmed_by: null }));
            state.images.forEach(image => { state.teethByFileId[image.id] = {}; });
            renderGrid();
            setStatus(error.message || 'Segmentation unavailable.');
        }
    }

    document.addEventListener('keydown', (evt) => {
        const target = evt.target;
        const isTextInput = target && (
            target.tagName === 'INPUT'
            || target.tagName === 'TEXTAREA'
            || target.isContentEditable
        );
        if (isTextInput) return;

        const key = evt.key.toLowerCase();
        const command = evt.ctrlKey || evt.metaKey;
        if (command && key === 'z' && !evt.shiftKey) {
            evt.preventDefault();
            undoAction();
            return;
        }
        if (command && (key === 'y' || (key === 'z' && evt.shiftKey))) {
            evt.preventDefault();
            redoAction();
            return;
        }

        if (evt.key === 'Escape' && state.selectedImage) {
            if (state.drawing) {
                if (state.draftPoints.length) {
                    recordAction({
                        type: 'drawing-cancel',
                        fileId: state.selectedImage.id,
                        tooth: state.selectedTooth,
                        draftId: state.currentDraftId,
                        points: clonePolygon(state.draftPoints),
                    });
                }
                state.drawing = false;
                state.draftPoints = [];
                redrawStage();
                setStatus('Drawing canceled.');
            } else if (state.selectedPolygon || state.selectedVertex) {
                state.selectedPolygon = null;
                state.selectedVertex = null;
                redrawStage();
                setStatus(`Image ${state.selectedImage.index}. Tooth ${state.selectedTooth}. Click image to add polygon points.`);
            }
        }
        if (evt.key === 'Enter' && state.drawing) {
            evt.preventDefault();
            finishDrawing();
        }
        if ((evt.key === 'Delete' || evt.key === 'Backspace') && state.selectedImage) {
            evt.preventDefault();
            deleteSelected();
        }
    }, true);

    window.addEventListener('resize', () => {
        if (!state.selectedImage) return;
        window.clearTimeout(state.resizeTimer);
        state.resizeTimer = window.setTimeout(renderFocused, 140);
    });

    if (onlySelectedBtn) {
        onlySelectedBtn.addEventListener('click', () => {
            state.onlySelectedTooth = !state.onlySelectedTooth;
            renderLabels();
            redrawStage();
        });
    }

    if (confirmBtn) {
        confirmBtn.addEventListener('click', toggleCurrentConfirmation);
    }

    if (resetViewBtn) {
        resetViewBtn.addEventListener('click', () => {
            resetViewport();
        });
    }

    renderLabels();
    window.IntraoralSegmentation = { mount };
    window.IntraoralSegmentationDebug = {
        state,
        undo: undoAction,
        redo: redoAction,
    };
})();
