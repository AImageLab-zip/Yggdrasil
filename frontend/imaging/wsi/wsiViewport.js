/**
 * Multi-resolution tiled digital pathology WSI viewport.
 *
 * Coordinates are grounded in Level 0 slide pixels (`image_pixel`), enabling:
 * 1. Deep zoom across pyramid levels (macro 1.25x to 40x cellular resolution).
 * 2. High-performance tile culling and progressive Level of Detail (LOD) fetching.
 * 3. Durable measurement annotations compatible with annotations/adapters/cornerstone.py.
 */

import { wsiTileUrl, globalWsiTileCache } from './wsiLoader.js';

export function createWsiViewport({
    element,
    metadata,
    fileId,
    namespace = 'urology',
    onAnnotationsChanged = () => {},
    onSegmentationLoaded = () => {},
}) {
    element.innerHTML = '';
    element.style.position = 'relative';
    element.style.overflow = 'hidden';
    element.style.userSelect = 'none';
    element.style.backgroundColor = '#111827'; // Dark clinical backdrop

    // 1. Primary tile canvas
    const canvas = document.createElement('canvas');
    canvas.style.position = 'absolute';
    canvas.style.left = '0';
    canvas.style.top = '0';
    canvas.style.width = '100%';
    canvas.style.height = '100%';
    canvas.style.display = 'block';
    element.appendChild(canvas);
    const ctx = canvas.getContext('2d');

    // 2. Vector annotation & segmentation layers (SVG)
    const svgOverlay = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svgOverlay.style.position = 'absolute';
    svgOverlay.style.left = '0';
    svgOverlay.style.top = '0';
    svgOverlay.style.width = '100%';
    svgOverlay.style.height = '100%';
    svgOverlay.style.pointerEvents = 'none';
    element.appendChild(svgOverlay);

    const segmentationGroup = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    segmentationGroup.setAttribute('class', 'wsi-segmentation-layer');
    svgOverlay.appendChild(segmentationGroup);

    const annotationGroup = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    annotationGroup.setAttribute('class', 'wsi-annotation-layer');
    svgOverlay.appendChild(annotationGroup);

    // 3. Floating Minimap container
    const minimapContainer = document.createElement('div');
    minimapContainer.className = 'wsi-minimap-box';
    minimapContainer.style.position = 'absolute';
    minimapContainer.style.bottom = '16px';
    minimapContainer.style.right = '16px';
    minimapContainer.style.width = '160px';
    minimapContainer.style.height = '120px';
    minimapContainer.style.backgroundColor = 'rgba(17, 24, 39, 0.85)';
    minimapContainer.style.border = '1px solid rgba(255, 255, 255, 0.2)';
    minimapContainer.style.borderRadius = '6px';
    minimapContainer.style.overflow = 'hidden';
    minimapContainer.style.boxShadow = '0 4px 12px rgba(0,0,0,0.5)';
    minimapContainer.style.cursor = 'crosshair';
    minimapContainer.style.zIndex = '10';

    const minimapImg = document.createElement('img');
    minimapImg.src = `/${namespace}/api/wsi/${fileId}/thumbnail/`;
    minimapImg.style.width = '100%';
    minimapImg.style.height = '100%';
    minimapImg.style.objectFit = 'contain';
    minimapImg.style.display = 'block';
    minimapContainer.appendChild(minimapImg);

    const minimapRect = document.createElement('div');
    minimapRect.style.position = 'absolute';
    minimapRect.style.border = '2px solid #38bdf8';
    minimapRect.style.backgroundColor = 'rgba(56, 189, 248, 0.25)';
    minimapRect.style.pointerEvents = 'none';
    minimapContainer.appendChild(minimapRect);

    element.appendChild(minimapContainer);

    // 4. Scale bar overlay
    const scaleBar = document.createElement('div');
    scaleBar.className = 'wsi-scale-bar';
    scaleBar.style.position = 'absolute';
    scaleBar.style.bottom = '16px';
    scaleBar.style.left = '16px';
    scaleBar.style.padding = '4px 8px';
    scaleBar.style.backgroundColor = 'rgba(15, 23, 42, 0.75)';
    scaleBar.style.color = '#e2e8f0';
    scaleBar.style.fontSize = '12px';
    scaleBar.style.fontFamily = 'monospace';
    scaleBar.style.borderRadius = '4px';
    scaleBar.style.border = '1px solid rgba(255, 255, 255, 0.15)';
    scaleBar.style.pointerEvents = 'none';
    scaleBar.style.zIndex = '10';
    element.appendChild(scaleBar);

    // Slide state
    const slideWidth = metadata.width || 1000;
    const slideHeight = metadata.height || 1000;
    const tileSize = metadata.tile_size || metadata.tileSize || 256;
    const levels = metadata.levels || [{ level: 0, width: slideWidth, height: slideHeight, downsample: 1.0, cols: 1, rows: 1 }];
    const mppX = metadata.mpp_x || metadata.mpp || 0.25;

    // Viewport camera
    let centerX = slideWidth / 2;
    let centerY = slideHeight / 2;
    let zoom = 1.0; // screen pixels per slide Level 0 pixel
    let minZoom = 0.001;
    let maxZoom = 4.0;
    let hasFitted = false;

    // Annotations
    let annotations = [];
    let currentTool = 'Pan';
    let activeDrawing = null;
    let annotationsVisible = true;

    // Segmentation overlay
    let segmentationData = null;
    let segmentationVisible = true;
    let segmentationOpacity = 0.35;

    function resize() {
        const rect = element.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) return;
        const dpr = window.devicePixelRatio || 1;
        canvas.width = Math.max(1, Math.floor(rect.width * dpr));
        canvas.height = Math.max(1, Math.floor(rect.height * dpr));
        ctx.scale(dpr, dpr);
        if (!hasFitted) {
            fitToScreen();
        } else {
            render();
        }
    }

    function fitToScreen() {
        const rect = element.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) return;
        const scaleX = rect.width / slideWidth;
        const scaleY = rect.height / slideHeight;
        zoom = Math.min(scaleX, scaleY) * 0.95;
        minZoom = zoom * 0.2;
        maxZoom = Math.max(4.0, zoom * 80.0);
        centerX = slideWidth / 2;
        centerY = slideHeight / 2;
        hasFitted = true;
        render();
    }

    // Coordinate conversions (Level 0 slide pixel <-> Screen CSS pixel)
    function slideToScreen(sx, sy) {
        const rect = element.getBoundingClientRect();
        const screenX = (sx - centerX) * zoom + rect.width / 2;
        const screenY = (sy - centerY) * zoom + rect.height / 2;
        return [screenX, screenY];
    }

    function screenToSlide(px, py) {
        const rect = element.getBoundingClientRect();
        const slideX = (px - rect.width / 2) / zoom + centerX;
        const slideY = (py - rect.height / 2) / zoom + centerY;
        return [slideX, slideY];
    }

    // Select optimal pyramid level for current zoom
    function choosePyramidLevel() {
        // We want downsample approx 1 / zoom
        const targetDownsample = 1.0 / Math.max(zoom, 1e-6);
        let bestLevel = 0;
        let minDiff = Infinity;
        for (let i = 0; i < levels.length; i += 1) {
            const diff = Math.abs(levels[i].downsample - targetDownsample);
            if (diff < minDiff) {
                minDiff = diff;
                bestLevel = i;
            }
        }
        return levels[bestLevel];
    }

    // Tile rendering pass
    let renderPending = false;
    function render() {
        if (renderPending) return;
        renderPending = true;
        requestAnimationFrame(() => {
            renderPending = false;
            drawScene();
        });
    }

    function drawScene() {
        const rect = element.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) return;
        const dpr = window.devicePixelRatio || 1;
        ctx.save();
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.restore();

        // 1. Draw slide backdrop
        const [leftTopX, leftTopY] = slideToScreen(0, 0);
        const [rightBottomX, rightBottomY] = slideToScreen(slideWidth, slideHeight);
        const slideScreenW = rightBottomX - leftTopX;
        const slideScreenH = rightBottomY - leftTopY;

        ctx.save();
        ctx.fillStyle = '#ffffff';
        ctx.fillRect(leftTopX, leftTopY, slideScreenW, slideScreenH);

        // Strictly clip all tile rendering to the slide boundary so edge margins cannot bleed
        ctx.beginPath();
        ctx.rect(leftTopX, leftTopY, slideScreenW, slideScreenH);
        ctx.clip();

        // 2. Base overview layer (always rendered underneath to eliminate blank screens during zoom)
        const baseLvl = levels[levels.length - 1];
        const baseLvlDownsample = baseLvl.downsample;
        const baseLvlTileSlideSize = tileSize * baseLvlDownsample;
        for (let r = 0; r < baseLvl.rows; r += 1) {
            for (let c = 0; c < baseLvl.cols; c += 1) {
                const baseKey = `${fileId}:${baseLvl.level}:${c}_${r}`;
                const cachedBase = globalWsiTileCache.get(baseKey);
                if (cachedBase?.bitmap) {
                    const bx = c * baseLvlTileSlideSize;
                    const by = r * baseLvlTileSlideSize;
                    const [bsx, bsy] = slideToScreen(bx, by);
                    const tileSlideW = Math.min(slideWidth - bx, baseLvlTileSlideSize);
                    const tileSlideH = Math.min(slideHeight - by, baseLvlTileSlideSize);
                    const bw = tileSlideW * zoom;
                    const bh = tileSlideH * zoom;
                    ctx.drawImage(cachedBase.bitmap, bsx, bsy, bw, bh);
                } else {
                    scheduleFetchTile(baseKey, baseLvl.level, c, r, true);
                }
            }
        }

        // 3. Tile bounds for optimal resolution level
        const levelInfo = choosePyramidLevel();
        if (levelInfo.level !== baseLvl.level) {
            const lvlDownsample = levelInfo.downsample;
            const lvlTileSlideSize = tileSize * lvlDownsample;

            // Visible region in slide space
            const [visibleX0, visibleY0] = screenToSlide(0, 0);
            const [visibleX1, visibleY1] = screenToSlide(rect.width, rect.height);

            const colStart = Math.max(0, Math.floor(Math.max(0, visibleX0) / lvlTileSlideSize));
            const colEnd = Math.min(levelInfo.cols - 1, Math.floor(Math.min(slideWidth, visibleX1) / lvlTileSlideSize));
            const rowStart = Math.max(0, Math.floor(Math.max(0, visibleY0) / lvlTileSlideSize));
            const rowEnd = Math.min(levelInfo.rows - 1, Math.floor(Math.min(slideHeight, visibleY1) / lvlTileSlideSize));

            for (let row = rowStart; row <= rowEnd; row += 1) {
                for (let col = colStart; col <= colEnd; col += 1) {
                    const tileSlideX = col * lvlTileSlideSize;
                    const tileSlideY = row * lvlTileSlideSize;
                    const [screenX, screenY] = slideToScreen(tileSlideX, tileSlideY);

                    const tileSlideW = Math.min(slideWidth - tileSlideX, lvlTileSlideSize);
                    const tileSlideH = Math.min(slideHeight - tileSlideY, lvlTileSlideSize);
                    const tileScreenW = tileSlideW * zoom;
                    const tileScreenH = tileSlideH * zoom;

                    const tileKey = `${fileId}:${levelInfo.level}:${col}_${row}`;
                    const cached = globalWsiTileCache.get(tileKey);

                    if (cached?.bitmap) {
                        ctx.drawImage(cached.bitmap, screenX, screenY, tileScreenW, tileScreenH);
                    } else {
                        // Hierarchical LOD Fallback: immediately draw nearest cached parent level (e.g. 20x or 10x)
                        // scaled into this tile's exact footprint to eliminate blank/blurry stutter
                        for (let a = levelInfo.level + 1; a < levels.length; a += 1) {
                            const ancLvl = levels[a];
                            const ancTileSlideSize = tileSize * ancLvl.downsample;
                            const ancCol = Math.floor(tileSlideX / ancTileSlideSize);
                            const ancRow = Math.floor(tileSlideY / ancTileSlideSize);
                            const ancKey = `${fileId}:${ancLvl.level}:${ancCol}_${ancRow}`;
                            const cachedAnc = globalWsiTileCache.get(ancKey);
                            if (cachedAnc?.bitmap) {
                                const ancSlideX = ancCol * ancTileSlideSize;
                                const ancSlideY = ancRow * ancTileSlideSize;
                                const srcX = Math.max(0, ((tileSlideX - ancSlideX) / ancTileSlideSize) * tileSize);
                                const srcY = Math.max(0, ((tileSlideY - ancSlideY) / ancTileSlideSize) * tileSize);
                                const srcW = Math.min(tileSize - srcX, (tileSlideW / ancTileSlideSize) * tileSize);
                                const srcH = Math.min(tileSize - srcY, (tileSlideH / ancTileSlideSize) * tileSize);
                                ctx.drawImage(
                                    cachedAnc.bitmap,
                                    srcX, srcY, srcW, srcH,
                                    screenX, screenY, tileScreenW, tileScreenH
                                );
                                break;
                            }
                        }

                        // Schedule fetch with Euclidean distance priority to viewport center
                        const cX = tileSlideX + tileSlideW / 2;
                        const cY = tileSlideY + tileSlideH / 2;
                        const distToCenter = Math.hypot(cX - centerX, cY - centerY) / lvlTileSlideSize;
                        scheduleFetchTile(tileKey, levelInfo.level, col, row, false, distToCenter);
                    }
                }
            }

            // 1-tile prefetch margin around viewport at lower priority for seamless panning
            const marginColStart = Math.max(0, colStart - 1);
            const marginColEnd = Math.min(levelInfo.cols - 1, colEnd + 1);
            const marginRowStart = Math.max(0, rowStart - 1);
            const marginRowEnd = Math.min(levelInfo.rows - 1, rowEnd + 1);
            for (let mr = marginRowStart; mr <= marginRowEnd; mr += 1) {
                for (let mc = marginColStart; mc <= marginColEnd; mc += 1) {
                    if (mc >= colStart && mc <= colEnd && mr >= rowStart && mr <= rowEnd) continue;
                    const mKey = `${fileId}:${levelInfo.level}:${mc}_${mr}`;
                    if (!hasTile(mKey)) {
                        scheduleFetchTile(mKey, levelInfo.level, mc, mr, false, 9999);
                    }
                }
            }
        }

        ctx.restore();

        // Slide outer border
        ctx.strokeStyle = 'rgba(255, 255, 255, 0.2)';
        ctx.strokeRect(leftTopX, leftTopY, slideScreenW, slideScreenH);

        // 4. Update Minimap indicator
        updateMinimap(rect);

        // 5. Update Scale bar
        updateScaleBar();

        // 6. Draw annotations & segmentation
        renderAnnotations();
        renderSegmentation();
    }

    const MAX_CONCURRENT_FETCHES = 6;
    let activeFetchesCount = 0;
    const fetchQueue = []; // array of { tileKey, level, col, row, isBase, priority }
    const inFlightFetches = new Map(); // tileKey -> AbortController
    const pendingFetches = new Map(); // tileKey -> { tileKey, level, col, row, isBase, priority }
    let fetchDebounceTimer = null;

    function hasTile(tileKey) {
        return Boolean(globalWsiTileCache.has ? globalWsiTileCache.has(tileKey) : globalWsiTileCache.get(tileKey));
    }

    async function decodeBlobToBitmap(blob) {
        if (typeof createImageBitmap === 'function') {
            try {
                return await createImageBitmap(blob);
            } catch (e) {}
        }
        return new Promise((resolve, reject) => {
            const img = new Image();
            const url = URL.createObjectURL(blob);
            img.onload = () => {
                URL.revokeObjectURL(url);
                resolve(img);
            };
            img.onerror = (err) => {
                URL.revokeObjectURL(url);
                reject(err);
            };
            img.src = url;
        });
    }

    function scheduleFetchTile(tileKey, level, col, row, isBase = false, priority = 0) {
        if (hasTile(tileKey) || inFlightFetches.has(tileKey)) return;
        pendingFetches.set(tileKey, { tileKey, level, col, row, isBase, priority });
        if (isBase) {
            flushPendingFetches();
        } else if (!fetchDebounceTimer) {
            fetchDebounceTimer = setTimeout(flushPendingFetches, 30);
        }
    }

    function flushPendingFetches() {
        if (fetchDebounceTimer) {
            clearTimeout(fetchDebounceTimer);
            fetchDebounceTimer = null;
        }
        if (pendingFetches.size === 0) return;

        const levelInfo = choosePyramidLevel();
        const currentLevel = levelInfo.level;
        const baseLevel = levels[levels.length - 1].level;

        // Calculate visible bounding box with margin for panning culling
        const lvlDownsample = levelInfo.downsample;
        const lvlTileSlideSize = tileSize * lvlDownsample;
        const rect = element.getBoundingClientRect();
        const [visX0, visY0] = screenToSlide(0, 0);
        const [visX1, visY1] = screenToSlide(rect.width, rect.height);
        const cullMargin = 2;
        const colStart = Math.max(0, Math.floor(Math.max(0, visX0) / lvlTileSlideSize) - cullMargin);
        const colEnd = Math.min(levelInfo.cols - 1, Math.floor(Math.min(slideWidth, visX1) / lvlTileSlideSize) + cullMargin);
        const rowStart = Math.max(0, Math.floor(Math.max(0, visY0) / lvlTileSlideSize) - cullMargin);
        const rowEnd = Math.min(levelInfo.rows - 1, Math.floor(Math.min(slideHeight, visY1) / lvlTileSlideSize) + cullMargin);

        // Abort in-flight requests that are from other levels (unless base level) or out of viewport
        for (const [key, controller] of inFlightFetches.entries()) {
            const parts = key.split(':');
            const reqLevel = Number(parts[1]);
            const coords = parts[2] ? parts[2].split('_') : [0, 0];
            const reqCol = Number(coords[0]);
            const reqRow = Number(coords[1]);

            const isObsoleteLevel = reqLevel !== currentLevel && reqLevel !== baseLevel;
            const isOutOfView = reqLevel === currentLevel && (reqCol < colStart || reqCol > colEnd || reqRow < rowStart || reqRow > rowEnd);

            if (isObsoleteLevel || isOutOfView) {
                controller.abort();
                inFlightFetches.delete(key);
                activeFetchesCount = Math.max(0, activeFetchesCount - 1);
            }
        }

        // Prune obsolete level or out-of-view tiles from fetchQueue
        for (let i = fetchQueue.length - 1; i >= 0; i -= 1) {
            const item = fetchQueue[i];
            const isObsoleteLevel = item.level !== currentLevel && !item.isBase;
            const isOutOfView = item.level === currentLevel && (item.col < colStart || item.col > colEnd || item.row < rowStart || item.row > rowEnd);
            if (isObsoleteLevel || isOutOfView) {
                fetchQueue.splice(i, 1);
            }
        }

        // Add pending fetches to queue, avoiding duplicates
        for (const item of pendingFetches.values()) {
            if (!hasTile(item.tileKey) && !inFlightFetches.has(item.tileKey)) {
                const existingIdx = fetchQueue.findIndex((q) => q.tileKey === item.tileKey);
                if (existingIdx !== -1) {
                    fetchQueue[existingIdx].priority = item.priority;
                } else {
                    fetchQueue.push(item);
                }
            }
        }
        pendingFetches.clear();

        // Sort queue: base tiles first, then center-out priority ascending
        fetchQueue.sort((a, b) => {
            if (a.isBase !== b.isBase) return a.isBase ? -1 : 1;
            return a.priority - b.priority;
        });

        pumpFetchQueue();
    }

    function pumpFetchQueue() {
        while (activeFetchesCount < MAX_CONCURRENT_FETCHES && fetchQueue.length > 0) {
            const info = fetchQueue.shift();
            const { tileKey } = info;
            if (hasTile(tileKey) || inFlightFetches.has(tileKey)) continue;

            const controller = new AbortController();
            inFlightFetches.set(tileKey, controller);
            activeFetchesCount += 1;

            const url = wsiTileUrl({ fileId, level: info.level, col: info.col, row: info.row, namespace });

            fetch(url, { credentials: 'same-origin', signal: controller.signal })
                .then((res) => {
                    if (!res.ok) throw new Error(`HTTP ${res.status}`);
                    return res.blob();
                })
                .then((blob) => decodeBlobToBitmap(blob))
                .then((bitmap) => {
                    inFlightFetches.delete(tileKey);
                    activeFetchesCount = Math.max(0, activeFetchesCount - 1);
                    globalWsiTileCache.set(tileKey, { bitmap });
                    render();
                    pumpFetchQueue();
                })
                .catch((err) => {
                    inFlightFetches.delete(tileKey);
                    activeFetchesCount = Math.max(0, activeFetchesCount - 1);
                    if (err.name !== 'AbortError') {
                        console.error('Tile fetch failed:', url, err);
                    }
                    pumpFetchQueue();
                });
        }
    }

    function updateMinimap(rect) {
        const [visX0, visY0] = screenToSlide(0, 0);
        const [visX1, visY1] = screenToSlide(rect.width, rect.height);

        const mBoxW = 160;
        const mBoxH = 120;
        const aspect = slideWidth / slideHeight;
        let drawW = mBoxW;
        let drawH = mBoxW / aspect;
        if (drawH > mBoxH) {
            drawH = mBoxH;
            drawW = mBoxH * aspect;
        }
        const offsetX = (mBoxW - drawW) / 2;
        const offsetY = (mBoxH - drawH) / 2;

        const rx0 = Math.max(0, Math.min(1, visX0 / slideWidth)) * drawW + offsetX;
        const ry0 = Math.max(0, Math.min(1, visY0 / slideHeight)) * drawH + offsetY;
        const rx1 = Math.max(0, Math.min(1, visX1 / slideWidth)) * drawW + offsetX;
        const ry1 = Math.max(0, Math.min(1, visY1 / slideHeight)) * drawH + offsetY;

        minimapRect.style.left = `${Math.min(rx0, rx1)}px`;
        minimapRect.style.top = `${Math.min(ry0, ry1)}px`;
        minimapRect.style.width = `${Math.max(4, Math.abs(rx1 - rx0))}px`;
        minimapRect.style.height = `${Math.max(4, Math.abs(ry1 - ry0))}px`;
    }

    function updateScaleBar() {
        // Physical microns per screen pixel = mppX / zoom
        const umPerPx = mppX / zoom;
        const targetScreenPx = 100;
        const totalUm = targetScreenPx * umPerPx;

        let displayUnit = 'µm';
        let displayVal = Math.round(totalUm);
        if (totalUm >= 1000) {
            displayUnit = 'mm';
            displayVal = +(totalUm / 1000).toFixed(2);
        }
        const currentMag = (zoom * 40.0).toFixed(1);
        scaleBar.innerHTML = `<span style="border-bottom: 2px solid #38bdf8; display: inline-block; width: ${targetScreenPx}px; text-align: center; margin-bottom: 2px;">${displayVal} ${displayUnit}</span><div style="font-size: 10px; color: #94a3b8; text-align: center;">${currentMag}x</div>`;
    }

    // Minimap interaction
    minimapContainer.addEventListener('pointerdown', (e) => {
        const mRect = minimapContainer.getBoundingClientRect();
        const clickX = e.clientX - mRect.left;
        const clickY = e.clientY - mRect.top;
        const aspect = slideWidth / slideHeight;
        let drawW = 160;
        let drawH = 160 / aspect;
        if (drawH > 120) {
            drawH = 120;
            drawW = 120 * aspect;
        }
        const offsetX = (160 - drawW) / 2;
        const offsetY = (120 - drawH) / 2;

        const normX = Math.max(0, Math.min(1, (clickX - offsetX) / drawW));
        const normY = Math.max(0, Math.min(1, (clickY - offsetY) / drawH));
        centerX = normX * slideWidth;
        centerY = normY * slideHeight;
        render();
    });

    // Panning & Navigation
    let isDragging = false;
    let dragStartX = 0;
    let dragStartY = 0;
    let dragStartCenterX = 0;
    let dragStartCenterY = 0;

    element.addEventListener('pointerdown', (e) => {
        if (e.target.closest('.wsi-minimap-box')) return;
        if (currentTool === 'Pan' || e.button === 1 || e.button === 2) {
            isDragging = true;
            dragStartX = e.clientX;
            dragStartY = e.clientY;
            dragStartCenterX = centerX;
            dragStartCenterY = centerY;
            element.setPointerCapture(e.pointerId);
            return;
        }

        // Annotation creation
        const rect = element.getBoundingClientRect();
        const [slideX, slideY] = screenToSlide(e.clientX - rect.left, e.clientY - rect.top);

        if (currentTool === 'Length') {
            if (!activeDrawing) {
                activeDrawing = {
                    toolName: 'Length',
                    points: [[slideX, slideY], [slideX, slideY]],
                };
            } else {
                activeDrawing.points[1] = [slideX, slideY];
                finalizeActiveDrawing();
            }
        } else if (currentTool === 'RectangleROI') {
            if (!activeDrawing) {
                activeDrawing = {
                    toolName: 'RectangleROI',
                    points: [[slideX, slideY], [slideX, slideY]],
                };
            } else {
                const p0 = activeDrawing.points[0];
                const p1 = [slideX, slideY];
                // 4 corners: bottomLeft, bottomRight, topLeft, topRight
                activeDrawing.points = [
                    [Math.min(p0[0], p1[0]), Math.max(p0[1], p1[1])],
                    [Math.max(p0[0], p1[0]), Math.max(p0[1], p1[1])],
                    [Math.min(p0[0], p1[0]), Math.min(p0[1], p1[1])],
                    [Math.max(p0[0], p1[0]), Math.min(p0[1], p1[1])],
                ];
                finalizeActiveDrawing();
            }
        } else if (currentTool === 'CircleROI') {
            if (!activeDrawing) {
                activeDrawing = {
                    toolName: 'CircleROI',
                    points: [[slideX, slideY], [slideX, slideY]],
                };
            } else {
                activeDrawing.points[1] = [slideX, slideY];
                finalizeActiveDrawing();
            }
        } else if (currentTool === 'SplineROI') {
            if (!activeDrawing) {
                activeDrawing = {
                    toolName: 'SplineROI',
                    points: [[slideX, slideY]],
                };
            } else {
                activeDrawing.points.push([slideX, slideY]);
                render();
            }
        } else if (currentTool === 'Label') {
            const name = window.prompt('Enter annotation label / point name:', 'Region 1');
            if (name) {
                annotations.push({
                    metadata: { toolName: 'Label' },
                    data: {
                        label: name,
                        handles: { points: [[slideX, slideY]] },
                    },
                });
                onAnnotationsChanged(annotations);
                render();
            }
        }
    });

    element.addEventListener('pointermove', (e) => {
        if (isDragging) {
            const dx = e.clientX - dragStartX;
            const dy = e.clientY - dragStartY;
            centerX = dragStartCenterX - dx / zoom;
            centerY = dragStartCenterY - dy / zoom;
            render();
            return;
        }

        if (activeDrawing) {
            const rect = element.getBoundingClientRect();
            const [slideX, slideY] = screenToSlide(e.clientX - rect.left, e.clientY - rect.top);
            if (activeDrawing.toolName === 'Length' || activeDrawing.toolName === 'CircleROI') {
                activeDrawing.points[1] = [slideX, slideY];
                render();
            } else if (activeDrawing.toolName === 'RectangleROI') {
                const p0 = activeDrawing.points[0];
                activeDrawing.previewPoint = [slideX, slideY];
                render();
            }
        }
    });

    element.addEventListener('pointerup', (e) => {
        if (isDragging) {
            isDragging = false;
            try {
                element.releasePointerCapture(e.pointerId);
            } catch {}
        }
    });

    element.addEventListener('dblclick', (e) => {
        if (activeDrawing && activeDrawing.toolName === 'SplineROI') {
            finalizeActiveDrawing();
        }
    });

    element.addEventListener('contextmenu', (e) => e.preventDefault());

    // Wheel zoom
    element.addEventListener('wheel', (e) => {
        e.preventDefault();
        const rect = element.getBoundingClientRect();
        const mouseX = e.clientX - rect.left;
        const mouseY = e.clientY - rect.top;
        const [slideX, slideY] = screenToSlide(mouseX, mouseY);

        const zoomFactor = e.deltaY < 0 ? 1.25 : 0.8;
        const nextZoom = Math.max(minZoom, Math.min(maxZoom, zoom * zoomFactor));

        // Keep cursor position stable under zoom
        zoom = nextZoom;
        centerX = slideX - (mouseX - rect.width / 2) / zoom;
        centerY = slideY - (mouseY - rect.height / 2) / zoom;
        render();
    }, { passive: false });

    function finalizeActiveDrawing() {
        if (!activeDrawing) return;
        annotations.push({
            metadata: { toolName: activeDrawing.toolName },
            data: {
                handles: { points: activeDrawing.points },
            },
        });
        activeDrawing = null;
        onAnnotationsChanged(annotations);
        render();
    }

    function renderAnnotations() {
        annotationGroup.innerHTML = '';
        if (!annotationsVisible) return;

        const allToRender = [...annotations];
        if (activeDrawing) {
            allToRender.push({
                metadata: { toolName: activeDrawing.toolName },
                data: {
                    handles: {
                        points: activeDrawing.toolName === 'RectangleROI' && activeDrawing.previewPoint
                            ? [
                                  [Math.min(activeDrawing.points[0][0], activeDrawing.previewPoint[0]), Math.max(activeDrawing.points[0][1], activeDrawing.previewPoint[1])],
                                  [Math.max(activeDrawing.points[0][0], activeDrawing.previewPoint[0]), Math.max(activeDrawing.points[0][1], activeDrawing.previewPoint[1])],
                                  [Math.min(activeDrawing.points[0][0], activeDrawing.previewPoint[0]), Math.min(activeDrawing.points[0][1], activeDrawing.previewPoint[1])],
                                  [Math.max(activeDrawing.points[0][0], activeDrawing.previewPoint[0]), Math.min(activeDrawing.points[0][1], activeDrawing.previewPoint[1])],
                              ]
                            : activeDrawing.points,
                    },
                },
                isActive: true,
            });
        }

        for (const annot of allToRender) {
            const toolName = annot.metadata?.toolName;
            const points = annot.data?.handles?.points || [];
            if (!points.length) continue;

            const color = annot.isActive ? '#f59e0b' : '#38bdf8';

            if (toolName === 'Length' && points.length >= 2) {
                const [p0x, p0y] = slideToScreen(points[0][0], points[0][1]);
                const [p1x, p1y] = slideToScreen(points[1][0], points[1][1]);

                const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
                line.setAttribute('x1', p0x);
                line.setAttribute('y1', p0y);
                line.setAttribute('x2', p1x);
                line.setAttribute('y2', p1y);
                line.setAttribute('stroke', color);
                line.setAttribute('stroke-width', '2');
                annotationGroup.appendChild(line);

                // Calibrated length
                const distPx = Math.hypot(points[1][0] - points[0][0], points[1][1] - points[0][1]);
                const distUm = distPx * mppX;
                const labelText = distUm >= 1000 ? `${(distUm / 1000).toFixed(2)} mm` : `${Math.round(distUm)} µm`;

                const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                text.setAttribute('x', (p0x + p1x) / 2 + 6);
                text.setAttribute('y', (p0y + p1y) / 2 - 6);
                text.setAttribute('fill', '#ffffff');
                text.setAttribute('font-size', '12px');
                text.setAttribute('font-family', 'sans-serif');
                text.setAttribute('font-weight', 'bold');
                text.setAttribute('filter', 'drop-shadow(0 1px 2px rgba(0,0,0,0.8))');
                text.textContent = labelText;
                annotationGroup.appendChild(text);
            } else if (toolName === 'RectangleROI' && points.length >= 4) {
                // points: bottomLeft, bottomRight, topLeft, topRight
                const [tlX, tlY] = slideToScreen(points[2][0], points[2][1]);
                const [brX, brY] = slideToScreen(points[1][0], points[1][1]);
                const w = Math.abs(brX - tlX);
                const h = Math.abs(brY - tlY);

                const rectEl = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
                rectEl.setAttribute('x', Math.min(tlX, brX));
                rectEl.setAttribute('y', Math.min(tlY, brY));
                rectEl.setAttribute('width', w);
                rectEl.setAttribute('height', h);
                rectEl.setAttribute('stroke', color);
                rectEl.setAttribute('stroke-width', '2');
                rectEl.setAttribute('fill', annot.isActive ? 'rgba(245, 158, 11, 0.15)' : 'rgba(56, 189, 248, 0.15)');
                annotationGroup.appendChild(rectEl);

                // Area
                const slideW = Math.abs(points[1][0] - points[0][0]);
                const slideH = Math.abs(points[0][1] - points[2][1]);
                const areaUm2 = (slideW * mppX) * (slideH * mppX);
                const areaText = areaUm2 >= 1e6 ? `${(areaUm2 / 1e6).toFixed(2)} mm²` : `${Math.round(areaUm2)} µm²`;

                const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                text.setAttribute('x', Math.min(tlX, brX) + 4);
                text.setAttribute('y', Math.min(tlY, brY) - 4);
                text.setAttribute('fill', '#ffffff');
                text.setAttribute('font-size', '12px');
                text.setAttribute('font-family', 'sans-serif');
                text.setAttribute('font-weight', 'bold');
                text.setAttribute('filter', 'drop-shadow(0 1px 2px rgba(0,0,0,0.8))');
                text.textContent = areaText;
                annotationGroup.appendChild(text);
            } else if (toolName === 'CircleROI' && points.length >= 2) {
                const [c0x, c0y] = slideToScreen(points[0][0], points[0][1]);
                const [c1x, c1y] = slideToScreen(points[1][0], points[1][1]);
                const r = Math.hypot(c1x - c0x, c1y - c0y);

                const circleEl = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
                circleEl.setAttribute('cx', c0x);
                circleEl.setAttribute('cy', c0y);
                circleEl.setAttribute('r', r);
                circleEl.setAttribute('stroke', color);
                circleEl.setAttribute('stroke-width', '2');
                circleEl.setAttribute('fill', 'rgba(56, 189, 248, 0.15)');
                annotationGroup.appendChild(circleEl);
            } else if (toolName === 'SplineROI' && points.length >= 2) {
                const screenPoints = points.map((p) => slideToScreen(p[0], p[1]));
                const poly = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
                poly.setAttribute('points', screenPoints.map((sp) => `${sp[0]},${sp[1]}`).join(' '));
                poly.setAttribute('stroke', color);
                poly.setAttribute('stroke-width', '2');
                poly.setAttribute('fill', 'rgba(236, 72, 153, 0.2)');
                annotationGroup.appendChild(poly);
            } else if (toolName === 'Label' && points.length >= 1) {
                const [lx, ly] = slideToScreen(points[0][0], points[0][1]);
                const circleEl = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
                circleEl.setAttribute('cx', lx);
                circleEl.setAttribute('cy', ly);
                circleEl.setAttribute('r', '5');
                circleEl.setAttribute('fill', '#ef4444');
                annotationGroup.appendChild(circleEl);

                const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                text.setAttribute('x', lx + 8);
                text.setAttribute('y', ly + 4);
                text.setAttribute('fill', '#ffffff');
                text.setAttribute('font-size', '12px');
                text.setAttribute('font-family', 'sans-serif');
                text.setAttribute('font-weight', 'bold');
                text.setAttribute('filter', 'drop-shadow(0 1px 2px rgba(0,0,0,0.8))');
                text.textContent = annot.data?.label || 'Point';
                annotationGroup.appendChild(text);
            }
        }
    }

    function renderSegmentation() {
        segmentationGroup.innerHTML = '';
        if (!segmentationVisible || !segmentationData) return;

        const features = Array.isArray(segmentationData.features)
            ? segmentationData.features
            : (segmentationData.type === 'Feature' ? [segmentationData] : []);

        for (const feat of features) {
            const geom = feat.geometry;
            if (!geom) continue;

            const classification = feat.properties?.classification;
            let cname = 'Segmentation';
            let rgb = [220, 38, 38];

            if (classification && typeof classification === 'object') {
                if (classification.name) cname = classification.name;
                if (Array.isArray(classification.colorRGB)) {
                    rgb = classification.colorRGB;
                } else if (Array.isArray(classification.color)) {
                    rgb = classification.color;
                } else if (typeof classification.color === 'number') {
                    const c = classification.color;
                    rgb = [(c >> 16) & 255, (c >> 8) & 255, c & 255];
                }
            }

            const strokeColor = `rgb(${rgb[0]}, ${rgb[1]}, ${rgb[2]})`;
            const fillColor = `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${segmentationOpacity})`;

            const renderRings = (rings) => {
                if (!Array.isArray(rings) || !rings.length) return;
                const outer = rings[0];
                if (!Array.isArray(outer) || outer.length < 3) return;

                const pointsStr = outer.map((coord) => {
                    const [sx, sy] = slideToScreen(coord[0], coord[1]);
                    return `${sx.toFixed(1)},${sy.toFixed(1)}`;
                }).join(' ');

                const poly = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
                poly.setAttribute('points', pointsStr);
                poly.setAttribute('stroke', strokeColor);
                poly.setAttribute('stroke-width', '2');
                poly.setAttribute('stroke-linejoin', 'round');
                poly.setAttribute('fill', fillColor);
                poly.style.pointerEvents = 'auto';
                poly.style.cursor = 'pointer';

                const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
                title.textContent = cname;
                poly.appendChild(title);

                segmentationGroup.appendChild(poly);
            };

            if (geom.type === 'Polygon' && Array.isArray(geom.coordinates)) {
                renderRings(geom.coordinates);
            } else if (geom.type === 'MultiPolygon' && Array.isArray(geom.coordinates)) {
                for (const polyCoords of geom.coordinates) {
                    renderRings(polyCoords);
                }
            }
        }
    }

    async function loadSegmentation(fid) {
        if (!fid) return;
        try {
            const resp = await fetch(`/${namespace}/api/wsi/${fid}/segmentation/`, { credentials: 'same-origin' });
            if (resp.ok) {
                const data = await resp.json();
                if (data && data.hasSegmentation && data.geojson) {
                    segmentationData = data.geojson;
                    renderSegmentation();
                    if (onSegmentationLoaded) {
                        onSegmentationLoaded(data);
                    }
                }
            }
        } catch (err) {
            console.debug('Failed to load segmentation:', err);
        }
    }

    const resizeObserver = new ResizeObserver(() => resize());
    resizeObserver.observe(element);
    window.addEventListener('resize', resize);
    fitToScreen();
    loadSegmentation(fileId);

    const api = {
        fitToScreen,
        resize,
        render,
        setTool(tool) {
            currentTool = tool;
            activeDrawing = null;
            render();
        },
        setZoomLevel(mag) {
            // mag e.g. 1.25, 2.5, 5, 10, 20, 40
            zoom = mag / 40.0;
            render();
        },
        getAnnotations() {
            return [...annotations];
        },
        setAnnotations(newAnnots) {
            annotations = Array.isArray(newAnnots) ? [...newAnnots] : [];
            render();
        },
        clearAnnotations() {
            annotations = [];
            activeDrawing = null;
            onAnnotationsChanged(annotations);
            render();
        },
        setAnnotationsVisible(visible) {
            annotationsVisible = visible;
            render();
        },
        setSegmentation(geojson) {
            segmentationData = geojson;
            renderSegmentation();
        },
        getSegmentation() {
            return segmentationData;
        },
        setSegmentationVisible(visible) {
            segmentationVisible = visible;
            renderSegmentation();
        },
        isSegmentationVisible() {
            return segmentationVisible;
        },
        setSegmentationOpacity(op) {
            segmentationOpacity = Math.max(0, Math.min(1, op));
            renderSegmentation();
        },
        getSegmentationOpacity() {
            return segmentationOpacity;
        },
        loadSegmentation,
        destroy() {
            if (fetchDebounceTimer) {
                clearTimeout(fetchDebounceTimer);
                fetchDebounceTimer = null;
            }
            for (const controller of inFlightFetches.values()) {
                controller.abort();
            }
            inFlightFetches.clear();
            pendingFetches.clear();
            resizeObserver.disconnect();
            window.removeEventListener('resize', resize);
            delete element.__wsiViewport;
            element.innerHTML = '';
        },
    };

    element.__wsiViewport = api;
    return api;
}
