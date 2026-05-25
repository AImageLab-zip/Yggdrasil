(function () {
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

    function clamp(value, min, max) {
        return Math.max(min, Math.min(max, value));
    }

    function dataUrlToBlob(dataUrl) {
        const parts = dataUrl.split(',');
        const mime = parts[0].match(/:(.*?);/)[1];
        const bstr = atob(parts[1]);
        let n = bstr.length;
        const u8arr = new Uint8Array(n);
        while (n--) u8arr[n] = bstr.charCodeAt(n);
        return new Blob([u8arr], { type: mime });
    }

    function imageToDataUrl(imageEl) {
        const canvas = document.createElement('canvas');
        canvas.width = imageEl.naturalWidth;
        canvas.height = imageEl.naturalHeight;
        const ctx = canvas.getContext('2d');
        ctx.drawImage(imageEl, 0, 0);
        return canvas.toDataURL('image/png');
    }

    function createToolbar() {
        const bar = document.createElement('div');
        bar.className = 'rgb-edit-toolbar';
        bar.innerHTML = [
            '<button type="button" data-action="crop" title="Crop"><i class="fas fa-crop-alt"></i></button>',
            '<button type="button" data-action="flip-v" title="Mirror vertical"><i class="fas fa-arrows-alt-v"></i></button>',
            '<button type="button" data-action="flip-h" title="Mirror horizontal"><i class="fas fa-arrows-alt-h"></i></button>',
            '<button type="button" data-action="rotate-cw" title="Rotate 90° clockwise"><i class="fas fa-rotate-right"></i></button>',
            '<button type="button" data-action="reset" title="Reset edits"><i class="fas fa-delete-left"></i></button>',
            '<button type="button" data-action="confirm" class="confirm" title="Confirm edits"><i class="fas fa-check"></i></button>',
        ].join('');
        return bar;
    }

    function ensureCropUi(container) {
        let layer = container.querySelector('.rgb-crop-layer');
        let box = container.querySelector('.rgb-crop-box');
        if (layer && box) return { layer, box };

        layer = document.createElement('div');
        layer.className = 'rgb-crop-layer';
        box = document.createElement('div');
        box.className = 'rgb-crop-box';
        box.innerHTML = [
            '<span class="rgb-crop-handle tl" data-handle="tl"></span>',
            '<span class="rgb-crop-handle tr" data-handle="tr"></span>',
            '<span class="rgb-crop-handle bl" data-handle="bl"></span>',
            '<span class="rgb-crop-handle br" data-handle="br"></span>',
        ].join('');
        layer.appendChild(box);
        container.appendChild(layer);
        return { layer, box };
    }

    function destroyCropUi(container) {
        const layer = container.querySelector('.rgb-crop-layer');
        if (layer) layer.remove();
    }

    function cropFromDisplayedImage(imageEl, cropRectDisplay) {
        const rect = imageEl.getBoundingClientRect();
        if (!rect.width || !rect.height || !imageEl.naturalWidth || !imageEl.naturalHeight) return null;

        const sx = Math.round((cropRectDisplay.x / rect.width) * imageEl.naturalWidth);
        const sy = Math.round((cropRectDisplay.y / rect.height) * imageEl.naturalHeight);
        const sw = Math.round((cropRectDisplay.w / rect.width) * imageEl.naturalWidth);
        const sh = Math.round((cropRectDisplay.h / rect.height) * imageEl.naturalHeight);

        const safeW = Math.max(1, Math.min(imageEl.naturalWidth - sx, sw));
        const safeH = Math.max(1, Math.min(imageEl.naturalHeight - sy, sh));

        const canvas = document.createElement('canvas');
        canvas.width = safeW;
        canvas.height = safeH;
        const ctx = canvas.getContext('2d');
        ctx.drawImage(imageEl, sx, sy, safeW, safeH, 0, 0, safeW, safeH);
        return canvas.toDataURL('image/png');
    }

    function cropDisplayRectToImageRect(imageEl, cropRectDisplay) {
        const rect = imageEl.getBoundingClientRect();
        if (!rect.width || !rect.height || !imageEl.naturalWidth || !imageEl.naturalHeight) return null;
        const x = Math.round((cropRectDisplay.x / rect.width) * imageEl.naturalWidth);
        const y = Math.round((cropRectDisplay.y / rect.height) * imageEl.naturalHeight);
        const width = Math.round((cropRectDisplay.w / rect.width) * imageEl.naturalWidth);
        const height = Math.round((cropRectDisplay.h / rect.height) * imageEl.naturalHeight);
        return {
            x: clamp(x, 0, imageEl.naturalWidth),
            y: clamp(y, 0, imageEl.naturalHeight),
            width: clamp(width, 1, imageEl.naturalWidth),
            height: clamp(height, 1, imageEl.naturalHeight),
        };
    }

    function flipDisplayedImage(imageEl, horizontal, vertical) {
        const w = imageEl.naturalWidth;
        const h = imageEl.naturalHeight;
        if (!w || !h) return null;
        const canvas = document.createElement('canvas');
        canvas.width = w;
        canvas.height = h;
        const ctx = canvas.getContext('2d');
        ctx.save();
        ctx.translate(horizontal ? w : 0, vertical ? h : 0);
        ctx.scale(horizontal ? -1 : 1, vertical ? -1 : 1);
        ctx.drawImage(imageEl, 0, 0, w, h);
        ctx.restore();
        return canvas.toDataURL('image/png');
    }

    function rotateArbitraryAndCrop(source, W, H, degrees) {
        if (!W || !H) return null;

        const θ = ((degrees % 360) + 360) % 360 * Math.PI / 180;
        const abscos = Math.abs(Math.cos(θ));
        const abssin = Math.abs(Math.sin(θ));

        const bbW = Math.ceil(W * abscos + H * abssin);
        const bbH = Math.ceil(W * abssin + H * abscos);

        const rotCanvas = document.createElement('canvas');
        rotCanvas.width = bbW;
        rotCanvas.height = bbH;
        const rotCtx = rotCanvas.getContext('2d');
        rotCtx.translate(bbW / 2, bbH / 2);
        rotCtx.rotate(θ);
        rotCtx.drawImage(source, -W / 2, -H / 2);

        // Largest axis-aligned W:H inscribed rectangle inside the rotated image
        let α = ((degrees % 360) + 360) % 360;
        if (α > 180) α = 360 - α;
        if (α > 90) α = 180 - α;
        const αRad = α * Math.PI / 180;
        const cosA = Math.cos(αRad);
        const sinA = Math.sin(αRad);
        const inscribedW = Math.min(
            (W * W) / (W * cosA + H * sinA),
            (W * H) / (W * sinA + H * cosA)
        );
        const inscribedH = inscribedW * H / W;

        const cropX = Math.round((bbW - inscribedW) / 2);
        const cropY = Math.round((bbH - inscribedH) / 2);
        const cropW = Math.round(inscribedW);
        const cropH = Math.round(inscribedH);

        const resultCanvas = document.createElement('canvas');
        resultCanvas.width = cropW;
        resultCanvas.height = cropH;
        resultCanvas.getContext('2d').drawImage(rotCanvas, cropX, cropY, cropW, cropH, 0, 0, cropW, cropH);

        return { dataUrl: resultCanvas.toDataURL('image/png'), resultCanvas, cropX, cropY, cropW, cropH, bbW, bbH };
    }

    function rotateDisplayedImage(imageEl) {
        const w = imageEl.naturalWidth;
        const h = imageEl.naturalHeight;
        if (!w || !h) return null;
        const canvas = document.createElement('canvas');
        canvas.width = h;
        canvas.height = w;
        const ctx = canvas.getContext('2d');
        ctx.translate(h, 0);
        ctx.rotate(Math.PI / 2);
        ctx.drawImage(imageEl, 0, 0);
        return canvas.toDataURL('image/png');
    }

    function isInteractiveFormElement(target) {
        return !!(target && target.closest('button, input, textarea, select, a, [contenteditable="true"]'));
    }

    window.RGBImageEditor = {
        attachToImage: function (img, options) {
            if (!img || img.dataset.rgbEditorMounted === 'true') return;
            img.dataset.rgbEditorMounted = 'true';

            const container = options.container || img.parentElement;
            if (!container) return;
            if (getComputedStyle(container).position === 'static') container.style.position = 'relative';

            const state = {
                sourceUrl: (options.initialSession && options.initialSession.sourceUrl) || img.src,
                rawUrl: options.rawUrl || (options.initialSession && options.initialSession.rawUrl) || (options.initialSession && options.initialSession.sourceUrl) || img.src,
                edited: false,
                dirty: false,
                cropMode: false,
                cropRect: null,
                drag: null,
                imgRect: null,
                baseWidth: (options.initialSession && options.initialSession.baseWidth) || img.naturalWidth || 0,
                baseHeight: (options.initialSession && options.initialSession.baseHeight) || img.naturalHeight || 0,
                currentWidth: (options.initialSession && options.initialSession.currentWidth) || img.naturalWidth || 0,
                currentHeight: (options.initialSession && options.initialSession.currentHeight) || img.naturalHeight || 0,
                operations: Array.isArray(options.initialSession && options.initialSession.operations)
                    ? options.initialSession.operations.map(operation => ({ ...operation }))
                    : [],
                history: Array.isArray(options.initialSession && options.initialSession.history) && options.initialSession.history.length
                    ? options.initialSession.history.map((entry) => ({
                        url: entry.url,
                        operations: Array.isArray(entry.operations) ? entry.operations.map(operation => ({ ...operation })) : [],
                    }))
                    : [{ url: img.src, operations: [] }],
                loading: false,
            };

            const toolbar = createToolbar();
            container.appendChild(toolbar);
            container.classList.add('rgb-edit-host');
            syncToolbarState();

            function buildEditMeta() {
                return {
                    mode: 'inline-crop-flip',
                    input_width: state.baseWidth || img.naturalWidth || 0,
                    input_height: state.baseHeight || img.naturalHeight || 0,
                    output_width: state.currentWidth || img.naturalWidth || 0,
                    output_height: state.currentHeight || img.naturalHeight || 0,
                    operations: state.operations.map(operation => ({ ...operation })),
                };
            }

            function buildEditorSession() {
                return {
                    sourceUrl: state.sourceUrl,
                    rawUrl: state.rawUrl,
                    baseWidth: state.baseWidth,
                    baseHeight: state.baseHeight,
                    currentWidth: state.currentWidth,
                    currentHeight: state.currentHeight,
                    operations: state.operations.map(operation => ({ ...operation })),
                    history: state.history.map((entry) => ({
                        url: entry.url,
                        operations: Array.isArray(entry.operations) ? entry.operations.map(operation => ({ ...operation })) : [],
                    })),
                };
            }

            function syncCurrentDimensions() {
                state.currentWidth = img.naturalWidth || state.currentWidth;
                state.currentHeight = img.naturalHeight || state.currentHeight;
            }

            function syncToolbarState() {
                const undoBtn = toolbar.querySelector('[data-action="reset"]');
                const confirmBtn = toolbar.querySelector('[data-action="confirm"]');
                if (undoBtn) {
                    undoBtn.disabled = state.loading || !state.dirty;
                }
                if (confirmBtn) {
                    confirmBtn.disabled = state.loading || !state.dirty;
                }
            }

            function loadImageUrl(nextUrl, done) {
                state.loading = true;
                syncToolbarState();
                let finished = false;
                const handleLoad = function () {
                    if (finished) return;
                    finished = true;
                    syncCurrentDimensions();
                    state.loading = false;
                    if (typeof done === 'function') done();
                    syncToolbarState();
                };
                const handleError = function () {
                    if (finished) return;
                    finished = true;
                    state.loading = false;
                    syncToolbarState();
                };
                img.addEventListener('load', handleLoad, { once: true });
                img.addEventListener('error', handleError, { once: true });
                img.src = nextUrl;
                if (img.complete && img.naturalWidth) {
                    handleLoad();
                }
            }

            function applyHistoryState(entry) {
                state.operations = Array.isArray(entry.operations) ? entry.operations.map(operation => ({ ...operation })) : [];
                state.edited = state.operations.length > 0;
                state.cropMode = false;
                state.cropRect = null;
                destroyCropUi(container);
                loadImageUrl(entry.url, function () {
                    if (typeof options.onPreview === 'function') {
                        options.onPreview(entry.url, buildEditMeta(), buildEditorSession());
                    }
                });
            }

            function pushHistoryState(dataUrl, operation) {
                const operations = state.operations.concat(operation ? [{ ...operation }] : []);
                const entry = { url: dataUrl, operations };
                state.history.push(entry);
                state.dirty = true;
                applyHistoryState(entry);
            }

            function resetToInitialImage() {
                const targetUrl = state.rawUrl || state.sourceUrl;
                state.cropMode = false;
                state.cropRect = null;
                destroyCropUi(container);
                state.sourceUrl = targetUrl;
                state.currentWidth = state.baseWidth;
                state.currentHeight = state.baseHeight;
                state.history = [{ url: targetUrl, operations: [] }];
                state.dirty = false;
                applyHistoryState(state.history[0]);
                if (typeof options.onReset === 'function') {
                    options.onReset(targetUrl, buildEditorSession());
                }
            }

            function exitCropMode() {
                state.cropMode = false;
                state.cropRect = null;
                state.drag = null;
                destroyCropUi(container);
            }

            function startCropMode() {
                state.cropMode = true;
                const { layer, box } = ensureCropUi(container);
                const rect = img.getBoundingClientRect();
                const parent = container.getBoundingClientRect();
                const offsetX = rect.left - parent.left;
                const offsetY = rect.top - parent.top;
                state.imgRect = { x: offsetX, y: offsetY, w: rect.width, h: rect.height };

                const initW = Math.max(40, rect.width * 0.7);
                const initH = Math.max(40, rect.height * 0.7);
                const initX = (rect.width - initW) / 2;
                const initY = (rect.height - initH) / 2;

                state.cropRect = {
                    x: initX,
                    y: initY,
                    w: initW,
                    h: initH,
                };

                function renderBox() {
                    if (!state.cropRect) return;
                    box.style.left = `${state.imgRect.x + state.cropRect.x}px`;
                    box.style.top = `${state.imgRect.y + state.cropRect.y}px`;
                    box.style.width = `${state.cropRect.w}px`;
                    box.style.height = `${state.cropRect.h}px`;
                }

                function pointerPos(e) {
                    const p = container.getBoundingClientRect();
                    return { x: e.clientX - p.left, y: e.clientY - p.top };
                }

                box.onmousedown = function (e) {
                    if (e.target.closest('.rgb-crop-handle')) return;
                    e.preventDefault();
                    const p = pointerPos(e);
                    state.drag = {
                        mode: 'move',
                        sx: p.x,
                        sy: p.y,
                        x: state.cropRect.x,
                        y: state.cropRect.y,
                    };
                };

                box.querySelectorAll('.rgb-crop-handle').forEach((handle) => {
                    handle.onmousedown = function (e) {
                        e.preventDefault();
                        e.stopPropagation();
                        const p = pointerPos(e);
                        state.drag = {
                            mode: 'resize',
                            handle: handle.dataset.handle,
                            sx: p.x,
                            sy: p.y,
                            x: state.cropRect.x,
                            y: state.cropRect.y,
                            w: state.cropRect.w,
                            h: state.cropRect.h,
                        };
                    };
                });

                layer.onmousemove = function (e) {
                    if (!state.drag || !state.cropRect) return;
                    const p = pointerPos(e);
                    if (state.drag.mode === 'move') {
                        const nx = state.drag.x + (p.x - state.drag.sx);
                        const ny = state.drag.y + (p.y - state.drag.sy);
                        state.cropRect.x = clamp(nx, 0, state.imgRect.w - state.cropRect.w);
                        state.cropRect.y = clamp(ny, 0, state.imgRect.h - state.cropRect.h);
                    } else {
                        const dx = p.x - state.drag.sx;
                        const dy = p.y - state.drag.sy;
                        const minSize = 30;
                        if (state.drag.handle === 'br') {
                            state.cropRect.w = clamp(state.drag.w + dx, minSize, state.imgRect.w - state.drag.x);
                            state.cropRect.h = clamp(state.drag.h + dy, minSize, state.imgRect.h - state.drag.y);
                        } else if (state.drag.handle === 'bl') {
                            const nx = clamp(state.drag.x + dx, 0, state.drag.x + state.drag.w - minSize);
                            const nw = state.drag.w + (state.drag.x - nx);
                            state.cropRect.x = nx;
                            state.cropRect.w = clamp(nw, minSize, state.imgRect.w - nx);
                            state.cropRect.h = clamp(state.drag.h + dy, minSize, state.imgRect.h - state.drag.y);
                        } else if (state.drag.handle === 'tr') {
                            const ny = clamp(state.drag.y + dy, 0, state.drag.y + state.drag.h - minSize);
                            const nh = state.drag.h + (state.drag.y - ny);
                            state.cropRect.y = ny;
                            state.cropRect.h = clamp(nh, minSize, state.imgRect.h - ny);
                            state.cropRect.w = clamp(state.drag.w + dx, minSize, state.imgRect.w - state.drag.x);
                        } else if (state.drag.handle === 'tl') {
                            const nx = clamp(state.drag.x + dx, 0, state.drag.x + state.drag.w - minSize);
                            const ny = clamp(state.drag.y + dy, 0, state.drag.y + state.drag.h - minSize);
                            const nw = state.drag.w + (state.drag.x - nx);
                            const nh = state.drag.h + (state.drag.y - ny);
                            state.cropRect.x = nx;
                            state.cropRect.y = ny;
                            state.cropRect.w = clamp(nw, minSize, state.imgRect.w - nx);
                            state.cropRect.h = clamp(nh, minSize, state.imgRect.h - ny);
                        }
                    }
                    renderBox();
                };

                layer.onmouseup = function () {
                    state.drag = null;
                };

                renderBox();
            }

            function handleKeydown(e) {
                if (!container.contains(img)) return;
                if (e.key === 'Escape' && state.cropMode) {
                    e.preventDefault();
                    e.stopPropagation();
                    exitCropMode();
                    return;
                }
                if (e.key === 'Enter' && state.cropMode) {
                    e.preventDefault();
                    e.stopPropagation();
                    applyCrop();
                    return;
                }
                if (e.key === 'Enter' && (toolbar.contains(e.target) || !isInteractiveFormElement(e.target))) {
                    e.preventDefault();
                    e.stopPropagation();
                }
            }

            container.addEventListener('keydown', handleKeydown, true);
            img.tabIndex = -1;

            function applyCrop() {
                if (!state.cropMode || !state.cropRect || state.loading) return;
                const cropRectDisplay = {
                    x: state.cropRect.x,
                    y: state.cropRect.y,
                    w: state.cropRect.w,
                    h: state.cropRect.h,
                };
                const currentWidth = state.currentWidth || img.naturalWidth;
                const currentHeight = state.currentHeight || img.naturalHeight;
                const cropRectImage = cropDisplayRectToImageRect(img, cropRectDisplay);
                const dataUrl = cropFromDisplayedImage(img, cropRectDisplay);
                if (dataUrl && cropRectImage) {
                    pushHistoryState(dataUrl, {
                        type: 'crop',
                        x: cropRectImage.x,
                        y: cropRectImage.y,
                        width: cropRectImage.width,
                        height: cropRectImage.height,
                        input_width: currentWidth,
                        input_height: currentHeight,
                        output_width: cropRectImage.width,
                        output_height: cropRectImage.height,
                    });
                }
            }

            function showRotatePopup(anchorEl) {
                const existing = document.querySelector('.rgb-rotate-popup');
                if (existing) existing.remove();

                // Snapshot current image so preview always rotates from the same base
                const snapW = img.naturalWidth || state.currentWidth;
                const snapH = img.naturalHeight || state.currentHeight;
                const snapCanvas = document.createElement('canvas');
                snapCanvas.width = snapW;
                snapCanvas.height = snapH;
                snapCanvas.getContext('2d').drawImage(img, 0, 0);
                const originalSrc = img.src;
                const snapDataUrl = snapCanvas.toDataURL('image/png');

                let lastResult = null;
                let lastDegrees = 0;
                let rafPending = null;

                const popup = document.createElement('div');
                popup.className = 'rgb-rotate-popup';
                popup.innerHTML =
                    '<label>Rotation: <strong class="rgb-rotate-value">0.0</strong>°</label>' +
                    '<input type="range" min="-360" max="360" step="0.1" value="0">' +
                    '<div class="rgb-rotate-actions">' +
                    '<button type="button" class="cancel" title="Cancel"><i class="fas fa-times"></i></button>' +
                    '<button type="button" class="confirm" title="Apply"><i class="fas fa-check"></i></button>' +
                    '</div>';
                document.body.appendChild(popup);

                const slider = popup.querySelector('input[type="range"]');
                const valueEl = popup.querySelector('.rgb-rotate-value');

                const anchorRect = anchorEl.getBoundingClientRect();
                let top = anchorRect.bottom + 6;
                let left = anchorRect.left;
                if (left + 210 > window.innerWidth) left = window.innerWidth - 218;
                if (top + 110 > window.innerHeight) top = anchorRect.top - 110;
                popup.style.top = top + 'px';
                popup.style.left = left + 'px';

                function applyPreviewFrame(degrees) {
                    rafPending = null;
                    const norm = ((degrees % 360) + 360) % 360;
                    let dataUrl, previewCanvas;
                    if (norm < 0.01 || norm > 359.99) {
                        lastResult = null;
                        dataUrl = snapDataUrl;
                        previewCanvas = snapCanvas;
                    } else {
                        lastResult = rotateArbitraryAndCrop(snapCanvas, snapW, snapH, degrees);
                        dataUrl = lastResult ? lastResult.dataUrl : snapDataUrl;
                        previewCanvas = lastResult ? lastResult.resultCanvas : snapCanvas;
                    }
                    img.src = dataUrl;
                    if (typeof options.onLivePreview === 'function') options.onLivePreview(dataUrl, previewCanvas);
                }

                slider.addEventListener('input', () => {
                    const degrees = Number(slider.value);
                    lastDegrees = degrees;
                    valueEl.textContent = degrees.toFixed(1);
                    if (rafPending) cancelAnimationFrame(rafPending);
                    rafPending = requestAnimationFrame(() => applyPreviewFrame(degrees));
                });

                function applyArbitraryRotation() {
                    if (rafPending) { cancelAnimationFrame(rafPending); rafPending = null; }
                    const degrees = lastDegrees;
                    const norm = ((degrees % 360) + 360) % 360;
                    closePopup();
                    if (norm < 0.01 || norm > 359.99) {
                        img.src = originalSrc;
                        if (typeof options.onLivePreview === 'function') options.onLivePreview(snapDataUrl, snapCanvas);
                        return;
                    }
                    const result = rotateArbitraryAndCrop(snapCanvas, snapW, snapH, degrees);
                    if (!result) {
                        img.src = originalSrc;
                        if (typeof options.onLivePreview === 'function') options.onLivePreview(snapDataUrl, snapCanvas);
                        return;
                    }
                    pushHistoryState(result.dataUrl, {
                        type: 'rotate-arbitrary',
                        angle: degrees,
                        input_width: snapW,
                        input_height: snapH,
                        output_width: result.cropW,
                        output_height: result.cropH,
                        bb_width: result.bbW,
                        bb_height: result.bbH,
                        crop_x: result.cropX,
                        crop_y: result.cropY,
                    });
                }

                function cancelAndRestore() {
                    if (rafPending) { cancelAnimationFrame(rafPending); rafPending = null; }
                    img.src = originalSrc;
                    if (typeof options.onLivePreview === 'function') options.onLivePreview(snapDataUrl, snapCanvas);
                    closePopup();
                }

                function closePopup() {
                    popup.remove();
                    document.removeEventListener('mousedown', handleOutsideClick);
                    document.removeEventListener('keydown', handlePopupKey);
                }

                function handleOutsideClick(e) {
                    if (!popup.contains(e.target)) cancelAndRestore();
                }

                function handlePopupKey(e) {
                    if (e.key === 'Enter') { e.preventDefault(); applyArbitraryRotation(); }
                    if (e.key === 'Escape') { e.preventDefault(); cancelAndRestore(); }
                }

                popup.querySelector('.confirm').addEventListener('click', applyArbitraryRotation);
                popup.querySelector('.cancel').addEventListener('click', cancelAndRestore);

                setTimeout(() => {
                    document.addEventListener('mousedown', handleOutsideClick);
                    document.addEventListener('keydown', handlePopupKey);
                    slider.focus();
                }, 0);
            }

            toolbar.addEventListener('contextmenu', (e) => {
                e.preventDefault();
                e.stopPropagation();
                const btn = e.target.closest('button');
                if (btn && btn.dataset.action === 'rotate-cw' && !state.loading) {
                    showRotatePopup(btn);
                }
            });

            toolbar.addEventListener('click', (e) => {
                e.stopPropagation();
                const btn = e.target.closest('button');
                if (!btn) return;
                const action = btn.dataset.action;
                if (!action) return;

                if (action === 'crop') {
                    if (state.loading) return;
                    if (!state.cropMode) {
                        startCropMode();
                    } else {
                        applyCrop();
                    }
                    return;
                }

                if (action === 'flip-h') {
                    if (state.loading) return;
                    const currentWidth = state.currentWidth || img.naturalWidth;
                    const currentHeight = state.currentHeight || img.naturalHeight;
                    const dataUrl = flipDisplayedImage(img, true, false);
                    if (dataUrl) {
                        pushHistoryState(dataUrl, {
                            type: 'flip-h',
                            input_width: currentWidth,
                            input_height: currentHeight,
                            output_width: currentWidth,
                            output_height: currentHeight,
                        });
                    }
                    return;
                }

                if (action === 'flip-v') {
                    if (state.loading) return;
                    const currentWidth = state.currentWidth || img.naturalWidth;
                    const currentHeight = state.currentHeight || img.naturalHeight;
                    const dataUrl = flipDisplayedImage(img, false, true);
                    if (dataUrl) {
                        pushHistoryState(dataUrl, {
                            type: 'flip-v',
                            input_width: currentWidth,
                            input_height: currentHeight,
                            output_width: currentWidth,
                            output_height: currentHeight,
                        });
                    }
                    return;
                }

                if (action === 'rotate-cw') {
                    if (state.loading) return;
                    const currentWidth = state.currentWidth || img.naturalWidth;
                    const currentHeight = state.currentHeight || img.naturalHeight;
                    const dataUrl = rotateDisplayedImage(img);
                    if (dataUrl) {
                        pushHistoryState(dataUrl, {
                            type: 'rotate-cw',
                            input_width: currentWidth,
                            input_height: currentHeight,
                            output_width: currentHeight,
                            output_height: currentWidth,
                        });
                    }
                    return;
                }

                if (action === 'reset') {
                    if (state.loading) return;
                    resetToInitialImage();
                    return;
                }

                if (action === 'confirm') {
                    if (state.loading || !state.dirty) return;
                    const editMeta = buildEditMeta();
                    const dataUrl = imageToDataUrl(img);
                    const formData = new FormData();
                    formData.append('modality_slug', options.modalitySlug);
                    formData.append('source_file_id', String(options.sourceFileId));
                    formData.append('edit_meta', JSON.stringify(editMeta));
                    formData.append('image', dataUrlToBlob(dataUrl), 'edited.png');
                    state.loading = true;
                    syncToolbarState();
                    const namespace = window.projectNamespace || 'maxillo';
                    fetch(`/${namespace}/api/patient/${options.patientId}/rgb-edit/save/`, {
                        method: 'POST',
                        headers: { 'X-CSRFToken': csrfToken() },
                        body: formData,
                    })
                        .then(r => r.json())
                        .then((data) => {
                            state.loading = false;
                            if (!data.success) {
                                syncToolbarState();
                                if (typeof window.showNotification === 'function') {
                                    window.showNotification('error', data.error || 'Failed to save edits');
                                } else {
                                    alert(data.error || 'Failed to save edits');
                                }
                                return;
                            }
                            const cacheBust = data.url + (data.url.includes('?') ? '&' : '?') + 'v=' + Date.now();
                            state.sourceUrl = cacheBust;
                            state.baseWidth = editMeta.output_width || img.naturalWidth || state.baseWidth;
                            state.baseHeight = editMeta.output_height || img.naturalHeight || state.baseHeight;
                            state.currentWidth = state.baseWidth;
                            state.currentHeight = state.baseHeight;
                            state.operations = [];
                            state.history = [{ url: cacheBust, operations: [] }];
                            state.edited = false;
                            state.dirty = false;
                            state.cropMode = false;
                            state.cropRect = null;
                            destroyCropUi(container);
                            loadImageUrl(cacheBust, function () {
                                if (typeof window.showNotification === 'function') {
                                    window.showNotification('success', 'Image edits saved successfully');
                                }
                                if (typeof options.onSaved === 'function') {
                                    options.onSaved(data, editMeta, buildEditorSession());
                                }
                            });
                        })
                        .catch(() => {
                            state.loading = false;
                            syncToolbarState();
                            if (typeof window.showNotification === 'function') {
                                window.showNotification('error', 'Failed to save edits');
                            } else {
                                alert('Failed to save edits');
                            }
                        });
                }
            });
        },
    };
})();
