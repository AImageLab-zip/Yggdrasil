/**
 * The segmentation tools: brush, eraser and polygon.
 *
 * Cornerstone ships `BrushTool`, `RectangleScissorsTool` and `CircleScissorsTool`. Each is
 * a *fill* tool whose behaviour is chosen by an `activeStrategy`, so an eraser is the same
 * class with the erase strategy -- registered under its own `toolName`, because a tool
 * Cornerstone is injected, so the pure parts -- the point-in-polygon test and the tool
 * table -- are testable without a GPU. The classes themselves are exercised in a browser
 * (the spike drove all eight through real mouse events).
 *
 * Tool names are what the tool group knows them by. Upstream's are singular
 * (`RectangleScissor`, `CircleScissor`) and `Brush`, not the class names -- using the
 * class name in `setToolActive` warns and does nothing.
 */

/**
 * Even-odd point-in-polygon.
 *
 * @param {number} x
 * @param {number} y
 * @param {Array<[number, number]>} polygon
 */
export function pointInPolygon(x, y, polygon) {
    let inside = false;
    for (let index = 0, previous = polygon.length - 1; index < polygon.length; previous = index++) {
        const [xi, yi] = polygon[index];
        const [xj, yj] = polygon[previous];
        if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) {
            inside = !inside;
        }
    }
    return inside;
}

/**
 * Pixel bounds of a polygon, clamped to the image, as the `[[x0,x1],[y0,y1],[z0,z1]]` a
 * Cornerstone strategy iterates. Empty (`null`) when the polygon misses the image.
 */
export function polygonBounds(polygon, [width, height]) {
    if (!polygon.length) {
        return null;
    }
    const xs = polygon.map((point) => point[0]);
    const ys = polygon.map((point) => point[1]);
    const x0 = Math.max(0, Math.floor(Math.min(...xs)));
    const x1 = Math.min(width - 1, Math.ceil(Math.max(...xs)));
    const y0 = Math.max(0, Math.floor(Math.min(...ys)));
    const y1 = Math.min(height - 1, Math.ceil(Math.max(...ys)));
    if (x0 > x1 || y0 > y1) {
        return null;
    }
    return [[x0, x1], [y0, y1], [0, 0]];
}

/**
 * Paint (or clear) every pixel within `radius` of the segment a-b, in image space. Cornerstone's
 * own circle fill converts every pixel to world coordinates and runs several closures and a
 * segmentation lookup on each one; with a big brush that is the freeze. This touches only
 * the typed array.
 *
 * @param {[number, number]} size image `[width, height]`
 * @param {[number, number]} a stroke start, image px
 * @param {[number, number]} b stroke end, image px
 * @param {number} radius image px
 * @param {(index: number) => void} visit called with the flat pixel index
 */
export function forEachInCapsule([width, height], a, b, radius, visit) {
    const x0 = Math.max(0, Math.floor(Math.min(a[0], b[0]) - radius));
    const x1 = Math.min(width - 1, Math.ceil(Math.max(a[0], b[0]) + radius));
    const y0 = Math.max(0, Math.floor(Math.min(a[1], b[1]) - radius));
    const y1 = Math.min(height - 1, Math.ceil(Math.max(a[1], b[1]) + radius));
    const dx = b[0] - a[0];
    const dy = b[1] - a[1];
    const lengthSquared = dx * dx + dy * dy;
    const r2 = radius * radius;
    for (let y = y0; y <= y1; y++) {
        for (let x = x0; x <= x1; x++) {
            let t = lengthSquared ? ((x - a[0]) * dx + (y - a[1]) * dy) / lengthSquared : 0;
            t = t < 0 ? 0 : t > 1 ? 1 : t;
            const ex = x - (a[0] + t * dx);
            const ey = y - (a[1] + t * dy);
            if (ex * ex + ey * ey <= r2) {
                visit(y * width + x);
            }
        }
    }
}

/**
 * What the toolbar offers. `erase` is the variant a shape tool switches to when the
 * toolbar's Erase switch is on; the brush has a button of its own for erasing, so it has none.
 */
export const SEGMENT_TOOLS = Object.freeze([
    { id: 'brush', label: 'Brush', fill: 'Brush', erase: null, hasSize: true },
    { id: 'eraser', label: 'Eraser', fill: 'BrushErase', erase: null, hasSize: true },
    { id: 'polygon', label: 'Polygon', fill: 'PolygonFill', erase: null },
]);

/** Every registered name, for the tool group to add and to switch off. */
export const ALL_TOOL_NAMES = Object.freeze(
    SEGMENT_TOOLS.flatMap((tool) => [tool.fill, tool.erase]).filter(Boolean)
);

/** The tool name for a toolbar selection. */
export function toolNameFor(toolId, { erase = false } = {}) {
    const tool = SEGMENT_TOOLS.find((candidate) => candidate.id === toolId);
    if (!tool) {
        throw new Error(`Unknown segmentation tool: ${toolId}`);
    }
    return erase && tool.erase ? tool.erase : tool.fill;
}

/**
 * Build the tool classes against an injected Cornerstone.
 *
 * @param {object} cs
 * @param {Function} cs.BrushTool
 * @param {Function} cs.RectangleScissorsTool
 * @param {Function} cs.BrushStrategy
 * @param {object} cs.compositions `strategies/compositions/index`
 * @param {object} cs.StrategyCallbacks
 * @param {Function} cs.transformWorldToIndex `csUtils.transformWorldToIndex`
 * @param {Function} cs.getEnabledElement core's `getEnabledElement`
 * @param {object} cs.drawing the tools package's SVG drawing helpers (`drawPolyline`, `drawCircle`)
 * @param {Function} cs.resetElementCursor `cursors.elementCursor.resetElementCursor`
 * @param {Function} cs.triggerAnnotationRender `utilities.triggerAnnotationRenderForViewportIds`
 * @returns {{classes: Function[], configuration: Map<string, object>}}
 */
export function createSegmentTools(cs) {
    const { BrushTool, RectangleScissorsTool, BrushStrategy, compositions, StrategyCallbacks } = cs;

    class BrushErase extends BrushTool {}
    BrushErase.toolName = 'BrushErase';

    // --- fast circle brush ------------------------------------------------------------
    // Replaces Cornerstone's FILL_INSIDE_CIRCLE / ERASE_INSIDE_CIRCLE (see forEachInCapsule).
    const toIndex = (segmentationImageData, world) =>
        cs.transformWorldToIndex(segmentationImageData, world);
    const initializeFastCircle = {
        [StrategyCallbacks.Initialize]: (operationData) => {
            const { points, segmentationImageData } = operationData;
            if (!points?.length) {
                return;
            }
            const [bottom, top] = points.map((world) => toIndex(segmentationImageData, world));
            const center = [(bottom[0] + top[0]) / 2, (bottom[1] + top[1]) / 2];
            const strokeWorld = operationData.strokePointsWorld?.length
                ? operationData.strokePointsWorld
                : null;
            const stroke = (strokeWorld ?? [null, null]).map((world) =>
                world ? toIndex(segmentationImageData, world) : center
            );
            operationData.fastCircle = {
                a: stroke[0],
                b: stroke[stroke.length - 1],
                radius: Math.hypot(top[0] - bottom[0], top[1] - bottom[1]) / 2,
            };
            operationData.centerIJK = [Math.round(center[0]), Math.round(center[1]), 0];
        },
    };
    const fastFill = (erase) => ({
        [StrategyCallbacks.Fill]: (operationData) => {
            const { fastCircle, segmentationImageData, segmentationVoxelManager, memo } = operationData;
            if (!fastCircle) {
                return;
            }
            const value = erase ? 0 : operationData.labelValue ?? operationData.segmentIndex;
            forEachInCapsule(
                segmentationImageData.getDimensions(),
                fastCircle.a,
                fastCircle.b,
                fastCircle.radius,
                (index) => {
                    if (segmentationVoxelManager.getAtIndex(index) !== value) {
                        memo.voxelManager.setAtIndex(index, value);
                    }
                }
            );
            segmentationVoxelManager.addPoint(operationData.centerIJK);
        },
    });
    const fastBrush = (name, erase) =>
        new BrushStrategy(
            name,
            fastFill(erase),
            initializeFastCircle,
            compositions.determineSegmentIndex,
            compositions.preview,
            compositions.labelmapStatistics
        ).strategyFunction;
    const FAST_STRATEGIES = {
        FAST_FILL: fastBrush('FastCircle', false),
        FAST_ERASE: fastBrush('FastCircleErase', true),
    };

    // --- shared polygon fill strategy -------------------------------------------------
    const initializePolygon = {
        [StrategyCallbacks.Initialize]: (operationData) => {
            const { points, segmentationImageData } = operationData;
            if (!points?.length) {
                return;
            }
            const ring = points.map((world) => {
                const index = cs.transformWorldToIndex(segmentationImageData, world);
                return [index[0], index[1]];
            });
            const bounds = polygonBounds(ring, segmentationImageData.getDimensions());
            // A polygon off the image fills nothing: an empty box, not an error.
            const [[x0, x1], [y0, y1]] = bounds ?? [[0, -1], [0, -1]];
            operationData.centerIJK = [Math.round((x0 + x1) / 2), Math.round((y0 + y1) / 2), 0];
            operationData.isInObjectBoundsIJK = bounds ?? [[0, -1], [0, -1], [0, 0]];
            operationData.isInObject = (worldPoint) => {
                const index = cs.transformWorldToIndex(segmentationImageData, worldPoint);
                return pointInPolygon(index[0], index[1], ring);
            };
        },
    };
    const POLYGON = new BrushStrategy(
        'Polygon',
        compositions.regionFill,
        compositions.setValue,
        initializePolygon,
        compositions.determineSegmentIndex,
        compositions.preview,
        compositions.labelmapStatistics
    );
    const fillInsidePolygon = POLYGON.strategyFunction;

    // --- the polygon: click to place vertices, close to fill ---------------------------
    /** Canvas pixels within which a click on the first vertex closes the polygon. */
    const CLOSE_RADIUS = 10;

    class PolygonFill extends RectangleScissorsTool {
        constructor(props = {}) {
            super(props, {
                supportedInteractionTypes: ['Mouse', 'Touch'],
                configuration: {
                    strategies: { FILL_INSIDE: fillInsidePolygon },
                    defaultStrategy: 'FILL_INSIDE',
                    activeStrategy: 'FILL_INSIDE',
                },
            });
            // Clicks, not drags, drive this tool, so the rectangle's mouse-up/drag
            // listeners must never attach: they would end the shape on the first release.
            this._activateDraw = () => {};
            this._deactivateDraw = () => {};
            const begin = this.preMouseDownCallback;
            this.vertices = [];
            this.hover = null;
            this.element = null;

            // Document-level, because a viewport <div> is not focusable and would never
            // receive the key presses that close, undo and cancel a polygon.
            this._onKey = (event) => {
                if (!this.editData) {
                    return;
                }
                if (event.key === 'Enter') {
                    event.preventDefault();
                    this._close();
                } else if (event.key === 'Escape') {
                    event.preventDefault();
                    this._cancel();
                } else if (event.key === 'Backspace') {
                    event.preventDefault();
                    this.vertices.pop();
                    if (!this.vertices.length) {
                        this._cancel();
                    } else {
                        this._redraw();
                    }
                }
            };
            this._onMove = (event) => {
                this.hover = [...event.detail.currentPoints.world];
                this._redraw();
            };
            this._onDoubleClick = () => {
                // A double click is two mouse-downs, so the second vertex duplicates the
                // first; drop it before closing.
                this.vertices.pop();
                this._close();
            };

            this.preMouseDownCallback = (event) => {
                const { element, currentPoints } = event.detail;
                if (!this.editData) {
                    if (!begin(event)) {
                        return false;
                    }
                    // `begin` hides the cursor for a drag; a polygon is aimed, so keep it.
                    cs.resetElementCursor(element);
                    this.element = element;
                    this.vertices = [];
                    element.addEventListener('CORNERSTONE_TOOLS_MOUSE_MOVE', this._onMove);
                    element.addEventListener('dblclick', this._onDoubleClick);
                    globalThis.document?.addEventListener('keydown', this._onKey);
                }
                const { viewport } = cs.getEnabledElement(element);
                const first = this.vertices[0];
                if (first && this.vertices.length >= 3) {
                    const [fx, fy] = viewport.worldToCanvas(first);
                    const [cx, cy] = currentPoints.canvas;
                    if (Math.hypot(fx - cx, fy - cy) <= CLOSE_RADIUS) {
                        this._close();
                        event.preventDefault();
                        return true;
                    }
                }
                this.vertices.push([...currentPoints.world]);
                this._redraw();
                event.preventDefault();
                return true;
            };

            this.renderAnnotation = (enabledElement, svgDrawingHelper) => {
                if (!this.editData || !this.vertices.length) {
                    return false;
                }
                const { viewport } = enabledElement;
                const uid = this.editData.annotation.annotationUID;
                const color = `rgb(${this.editData.annotation.metadata.segmentColor.slice(0, 3)})`;
                const path = [...this.vertices, ...(this.hover ? [this.hover] : [])].map((world) =>
                    viewport.worldToCanvas(world)
                );
                cs.drawing.drawPolyline(svgDrawingHelper, uid, 'polygon', path, {
                    color,
                    lineWidth: 2,
                });
                // The first vertex is the one to click to close; make it a visible target.
                cs.drawing.drawCircle(
                    svgDrawingHelper,
                    uid,
                    'first',
                    viewport.worldToCanvas(this.vertices[0]),
                    CLOSE_RADIUS / 2,
                    { color, fill: color }
                );
                return true;
            };
        }

        _redraw() {
            cs.triggerAnnotationRender(this.editData?.viewportIdsToRender ?? []);
        }

        _teardown() {
            const element = this.element;
            element?.removeEventListener('CORNERSTONE_TOOLS_MOUSE_MOVE', this._onMove);
            element?.removeEventListener('dblclick', this._onDoubleClick);
            globalThis.document?.removeEventListener('keydown', this._onKey);
            const viewportIds = this.editData?.viewportIdsToRender ?? [];
            this.editData = null;
            this.isDrawing = false;
            this.hover = null;
            this.vertices = [];
            this.element = null;
            cs.triggerAnnotationRender(viewportIds);
            return element;
        }

        _cancel() {
            this._teardown();
        }

        _close() {
            if (!this.editData) {
                return;
            }
            const element = this.element;
            if (this.vertices.length < 3) {
                this._cancel();
                return;
            }
            const operationData = {
                ...this.editData,
                points: this.vertices.map((point) => [...point]),
                createMemo: this.createMemo.bind(this),
            };
            this._teardown();
            this.applyActiveStrategy(cs.getEnabledElement(element), operationData);
            this.doneEditMemo();
        }

        // A tool switch mid-polygon abandons it, rather than leaving key listeners behind.
        onSetToolPassive() {
            this._cancel();
        }

        onSetToolDisabled() {
            this._cancel();
        }
    }
    PolygonFill.toolName = 'PolygonFill';

    const classes = [
        BrushTool,
        BrushErase,
        PolygonFill,
    ];

    // Per-tool configuration goes to `toolGroup.addTool(name, config)`: it is what builds
    // the instance the viewport uses, so a setting made anywhere else is read by nothing.
    const configuration = new Map([
        [BrushTool.toolName, { strategies: FAST_STRATEGIES, defaultStrategy: 'FAST_FILL', activeStrategy: 'FAST_FILL' }],
        [BrushErase.toolName, { strategies: FAST_STRATEGIES, defaultStrategy: 'FAST_ERASE', activeStrategy: 'FAST_ERASE' }],
        [PolygonFill.toolName, { activeStrategy: 'FILL_INSIDE' }],
    ]);

    return { classes, configuration };
}
