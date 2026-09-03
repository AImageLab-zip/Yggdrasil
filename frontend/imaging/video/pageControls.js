/**
 * The laparoscopy page's own controls, wired to a mounted video surface.
 *
 * The markup is Yggdrasil's (decision #4) and stays that way; this is the binder that
 * connects it, the way `imaging/grid/controls.js` does for the volume grid. It lives here
 * rather than in a `<script type="module">` in the template for the reason that file's
 * header gives: **a template id joining two files is an untested interface**, and the
 * arithmetic behind a frame bar and a timeline is worth a unit test rather than a click.
 *
 * What it wires, and what it deliberately does not:
 *
 *   - The **frame bar**, the **keyboard**, the **region list**, the **save button** and
 *     the **quadrant timeline**. Every one of these was rendered by the template and
 *     connected to nothing: the surface's own `goToInstant`, `save` and
 *     `editor.selectRegion` had no callers at all, which meant the frame bar was
 *     decorative, no drawing tool could activate (they all need a selected region), and
 *     there was no way to save.
 *   - **Not** the Magic Tool, which the roadmap records as a separate release blocker,
 *     and **not** the shapes list, which describes per-stroke rows that decision #14
 *     removed when the labelmap became canonical. Both are hidden rather than bound: a
 *     control that is present and inert is worse than one that is absent.
 *
 * Everything that needs the DOM takes it as an argument, and everything that does not is
 * exported on its own.
 */

/** The ids this module resolves. Mirrored by `laparoscopy/tests_video_surface.py`. */
import { readCsrfToken, report } from './bootstrap.js';
import { isMeasurable, observeSize } from '../runtime/elementSize.js';

export const VIDEO_CONTROL_IDS = Object.freeze({
    toolbar: 'annotation-toolbar',
    toggle: 'annotation-toggle-btn',
    viewport: 'video-annotate-viewport',
    placeholder: 'video-placeholder',
    tour: 'laparoscopy-tour-btn',
    brushSize: 'brush-size-input',
    brushSizeLabel: 'brush-size-label',
    zoomIn: 'zoom-in-btn',
    zoomOut: 'zoom-out-btn',
    zoomReset: 'zoom-reset-btn',
    save: 'save-annotations-btn',
    savingIndicator: 'savingIndicator',

    frameBar: 'frame-nav-bar',
    frameFirst: 'frame-first',
    framePrev10: 'frame-prev10',
    framePrev: 'frame-prev',
    framePlay: 'frame-play',
    frameNext: 'frame-next',
    frameNext10: 'frame-next10',
    frameLast: 'frame-last',
    frameTimestamp: 'frame-timestamp',

    regionPanel: 'region-types-panel',
    regionList: 'region-list',
    regionAdd: 'add-region-btn',
    regionVisibility: 'toggle-regions-visibility-btn',
    shapesPanel: 'shapes-list-panel',
    shapesList: 'shapes-list',
    shapesFilter: 'shapes-filter-btn',
    magicPanel: 'magic-toolbox-panel',

    timelineBar: 'temporal-classification-bar',
    timelineTrackWrap: 'timeline-track-wrap',
    timelinePins: 'timeline-pins-layer',
    timelineSegments: 'timeline-segments-layer',
    timelinePlayhead: 'timeline-playhead',
    timelineCurrent: 'timeline-current-time',
    timelineDuration: 'timeline-duration',
    timelineAddPin: 'timeline-add-pin-btn',
    timelineClassList: 'timeline-class-list',
    // The quadrant administration panel. Authored in the template with its own chip CSS
    // in Phase 10 and resolved by nothing, so it kept `d-none` for its whole life: there
    // was no way to create a quadrant type in the page, `activeQuadrantId` could never
    // leave null, and "Add Marker" could only ever answer "Pick a quadrant before adding
    // a marker."
    quadrantPanel: 'quadrant-types-panel',
    quadrantAdd: 'timeline-add-class-btn',
    quadrantAdminList: 'timeline-class-admin-list',
    timelineClassLabel: 'timeline-class-active-label',
    timelineClassSwatch: 'timeline-class-active-swatch',
    timelineActiveClass: 'timeline-active-class',
});

/**
 * How a mask's recorded tool reads in the annotation list.
 *
 * Keyed by the toolbar key `editor.setActiveTool` was given, which is what `masks.js`
 * stores -- not Cornerstone's `toolName`, which is an implementation detail the record
 * should not inherit. A key with no entry here (or a mask stored before attribution
 * existed) renders as a row with no tool, which is the truthful rendering of "the record
 * does not say".
 */
export const TOOL_LABELS = Object.freeze({
    brush: { label: 'Brush', icon: 'fa-paint-brush' },
    eraser: { label: 'Eraser', icon: 'fa-eraser' },
    polygon: { label: 'Polygon', icon: 'fa-draw-polygon' },
    'rect-scissors': { label: 'Rectangle', icon: 'fa-vector-square' },
    'circle-scissors': { label: 'Circle', icon: 'fa-circle' },
});

/** Resolve every id once. Missing ones are null, and every binding tolerates that. */
export function videoControlPlan(doc) {
    const plan = {};
    for (const [name, id] of Object.entries(VIDEO_CONTROL_IDS)) {
        plan[name] = doc.getElementById(id);
    }
    return plan;
}

/**
 * `MM:SS.mmm`, which is what the template ships as its placeholder text.
 *
 * Milliseconds and not frames: two studies at different frame rates show the same
 * instant the same way, and a timestamp is what somebody writes in a report.
 *
 * @param {number} ms
 * @returns {string}
 */
export function formatTimestamp(ms) {
    const total = Number.isFinite(ms) && ms > 0 ? Math.round(ms) : 0;
    const minutes = Math.floor(total / 60000);
    const seconds = Math.floor((total % 60000) / 1000);
    const millis = total % 1000;
    return (
        `${String(minutes).padStart(2, '0')}:` +
        `${String(seconds).padStart(2, '0')}.` +
        `${String(millis).padStart(3, '0')}`
    );
}

/**
 * Multiply a viewport's magnification.
 *
 * **`VideoViewport` has no `setZoom` and no `getZoom`.** The three zoom buttons called
 * them through optional chaining, so `getZoom?.()` was `undefined`, `undefined * 1.2` was
 * `NaN`, and `setZoom?.(NaN)` was a no-op: every press did nothing at all, silently, which
 * is exactly how it was reported. What the class does implement is the camera pair
 * (`getCamera`/`setCamera`), and that is the seam the zoom has to go through.
 *
 * `parallelScale` is **half the world height the viewport shows**, so it moves the
 * opposite way to magnification: zooming in by a factor divides it. Getting that backwards
 * is a zoom-out button that zooms in, which is why the arithmetic is here with a name on
 * it rather than inline at the three call sites.
 *
 * Left general rather than special-cased to video: `parallelScale` means the same thing on
 * every Cornerstone viewport, so a viewport that *does* offer `setZoom` is still served
 * correctly by it.
 *
 * @param {object} viewport
 * @param {number} factor >1 magnifies.
 * @returns {boolean} whether a camera was actually changed.
 */
export function zoomBy(viewport, factor) {
    if (!Number.isFinite(factor) || factor <= 0) {
        return false;
    }
    const camera = viewport?.getCamera?.();
    const scale = camera?.parallelScale;
    if (!Number.isFinite(scale) || scale <= 0) {
        return false;
    }
    viewport.setCamera({ ...camera, parallelScale: scale / factor });
    return true;
}

/**
 * Black or white, whichever can be read on a `#rrggbb` background.
 *
 * The region colours are chosen by whoever created the region type and run from pale
 * yellow to navy, so a fixed foreground is illegible against half of them. sRGB relative
 * luminance with the usual 0.55 split, which is the same rule the swatch borders assume.
 *
 * @param {string} color
 * @returns {string} a CSS colour.
 */
export function readableOn(color) {
    const hex = /^#([0-9a-f]{6})$/i.exec(String(color ?? '').trim());
    if (!hex) {
        return '#fff';
    }
    const value = Number.parseInt(hex[1], 16);
    const channel = (shift) => {
        const part = ((value >> shift) & 255) / 255;
        return part <= 0.03928 ? part / 12.92 : ((part + 0.055) / 1.055) ** 2.4;
    };
    const luminance = 0.2126 * channel(16) + 0.7152 * channel(8) + 0.0722 * channel(0);
    return luminance > 0.55 ? '#000' : '#fff';
}

/** What each frame-bar button means, in milliseconds from where the playhead is. */
export const FRAME_STEPS = Object.freeze({
    framePrev10: -10000,
    framePrev: -1000,
    frameNext: 1000,
    frameNext10: 10000,
});

/**
 * Where a frame-bar button or an arrow key should move the playhead to.
 *
 * Absolute, not a delta, because "first" and "last" are absolute and mixing the two at
 * the call site is how an off-by-one ends up in the only place a frame number matters.
 *
 * @param {string} action one of {@link FRAME_STEPS}' keys, `frameFirst` or `frameLast`.
 * @param {number} currentMs
 * @param {number|null} durationMs
 * @returns {number|null} the instant to move to, or null when the action means nothing.
 */
export function frameTarget(action, currentMs, durationMs) {
    if (action === 'frameFirst') {
        return 0;
    }
    if (action === 'frameLast') {
        // Nothing to go to when the probe did not state a frame count. Better to do
        // nothing than to jump to zero, which is the opposite end.
        return Number.isFinite(durationMs) && durationMs > 0 ? durationMs : null;
    }
    const step = FRAME_STEPS[action];
    return step === undefined ? null : (Number.isFinite(currentMs) ? currentMs : 0) + step;
}

/**
 * What a keystroke means, or null if it means nothing here.
 *
 * The bindings are the ones the template already advertises on its own buttons -- the
 * arrows step a second and Shift+arrow steps ten -- so the page and the keyboard say the
 * same thing. A keystroke inside a text field is never one of these.
 *
 * @param {object} event `{key, shiftKey, ctrlKey, metaKey, altKey, target}`.
 * @returns {{kind: 'tool', tool: string}|{kind: 'frame', action: string}|null}
 */
export function keyAction(event) {
    if (!event || event.ctrlKey || event.metaKey || event.altKey) {
        return null;
    }
    const tag = event.target?.tagName?.toUpperCase?.();
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || event.target?.isContentEditable) {
        return null;
    }
    if (event.key === 'ArrowLeft') {
        return { kind: 'frame', action: event.shiftKey ? 'framePrev10' : 'framePrev' };
    }
    if (event.key === 'ArrowRight') {
        return { kind: 'frame', action: event.shiftKey ? 'frameNext10' : 'frameNext' };
    }
    if (event.shiftKey) {
        return null;
    }
    const tool = { b: 'brush', e: 'eraser', p: 'polygon', h: 'pan' }[String(event.key).toLowerCase()];
    return tool ? { kind: 'tool', tool } : null;
}

/**
 * The coloured spans a set of quadrant markers describes.
 *
 * A marker is the instant a quadrant *starts*; it runs until the next one, or to the end
 * of the recording. Deriving that here rather than storing it is what keeps a marker
 * edit from having to rewrite its neighbour.
 *
 * @param {Array<{time_ms: number, quadrant_type_id: number}>} markers any order.
 * @param {number|null} durationMs
 * @param {Map<number, {name: string, color: string}>} types
 * @returns {Array<{startMs, endMs, name, color}>}
 */
export function markerSegments(markers, durationMs, types) {
    if (!Number.isFinite(durationMs) || durationMs <= 0) {
        return [];
    }
    const sorted = [...(markers ?? [])]
        .filter((marker) => Number.isFinite(marker?.time_ms))
        .sort((a, b) => a.time_ms - b.time_ms);
    return sorted.map((marker, index) => {
        const type = types.get(marker.quadrant_type_id);
        return {
            startMs: Math.max(0, Math.min(marker.time_ms, durationMs)),
            endMs: index + 1 < sorted.length ? Math.min(sorted[index + 1].time_ms, durationMs) : durationMs,
            name: type?.name ?? '—',
            color: type?.color ?? '#6c757d',
        };
    });
}

/** Which quadrant is in force at an instant, or null before the first marker. */
export function quadrantAt(segments, timeMs) {
    return segments.find((segment) => timeMs >= segment.startMs && timeMs < segment.endMs)
        ?? (segments.length && timeMs >= segments[segments.length - 1].endMs
            ? segments[segments.length - 1]
            : null);
}

/** Percentage across the track for an instant, clamped to it. */
export function trackPercent(timeMs, durationMs) {
    if (!Number.isFinite(durationMs) || durationMs <= 0) {
        return 0;
    }
    const clamped = Math.max(0, Math.min(Number.isFinite(timeMs) ? timeMs : 0, durationMs));
    return (clamped / durationMs) * 100;
}

const noticed = (doc, level, message) => {
    const notify = doc?.defaultView?.appNotify;
    if (typeof notify === 'function') {
        notify(level, message);
    } else {
        console.info(`[ygg-video] ${message}`);
    }
};

/**
 * Wire the page to a mounted surface.
 *
 * @param {object} options
 * @param {object} options.surface from `mountVideoAnnotator`.
 * @param {Document} [options.doc]
 * @param {Function} [options.fetchImpl]
 * @returns {{plan: object, bound: string[]}} what it found and connected.
 */
export function bindVideoControls({ surface, doc = globalThis.document, fetchImpl } = {}) {
    const plan = videoControlPlan(doc);
    const bound = [];
    const doFetch = fetchImpl ?? ((...args) => doc.defaultView.fetch(...args));
    const show = (element) => element?.classList?.remove('d-none');
    const hide = (element) => element?.classList?.add('d-none');

    // The recording is on screen from here on, whatever the annotation half turned out
    // to be. The placeholder is removed rather than hidden: it says "No video uploaded",
    // and leaving that sentence in the document for a screen reader to find would be the
    // same false claim in a quieter voice.
    plan.placeholder?.remove();
    show(plan.viewport);
    show(plan.frameBar);
    show(plan.tour);
    bound.push('viewport');

    // **The reveal above is what makes the canvas measurable, and Cornerstone has to be
    // told.** The viewport was built while the element carried `d-none`, so its canvas is
    // 0x0 and every rendered frame goes nowhere -- a black box over a recording that
    // loaded correctly, reported by nothing. Same signal the volume grid and the photo
    // stack use, and it also covers the container changing later: the patient page puts
    // this surface inside a column that reflows.
    if (plan.viewport) {
        observeSize(plan.viewport, (measurable) => {
            if (measurable) {
                surface.resize?.();
            }
        });
        // Once now, because a ResizeObserver's first callback is not guaranteed to
        // follow a class change that happened in this same task.
        if (isMeasurable(plan.viewport)) {
            surface.resize?.();
        }
    }

    // Never wired, and not being wired now -- see the module header.
    hide(plan.magicPanel);

    // The timeline's state, declared here because `showTime` reads it on the first call
    // and a degraded mount reaches that call without ever running the timeline's setup.
    /**
     * Whether the annotation list has been set up.
     *
     * Declared up here rather than beside the list because `goTo` redraws the list and
     * `goTo` is bound before the degraded mount returns -- a `let` declared later would be
     * in its temporal dead zone and a frame button on a playback-only mount would throw.
     */
    let annotationsReady = false;
    /**
     * Whether the reader is in annotation mode.
     *
     * Declared here for the same reason as `annotationsReady`: the play binding below is
     * bound before the toggle that owns this, and playback asks which mode it is in.
     */
    let annotating = false;
    /** @type {Map<number, {name: string, color: string}>} */
    const quadrantTypes = new Map();
    let markers = [];
    let segments = [];
    let activeQuadrantId = null;

    // --- frame navigation ---------------------------------------------------------

    /**
     * Paint the clock and the playhead.
     *
     * `timeMs` defaults to the surface's own instant -- the frame a mask would be filed
     * against. During playback it is the *video element's* instant instead: the mask
     * position deliberately does not follow a playing video (see the play binding), and
     * the readouts froze along with it, so the timestamp and the playhead sat still for
     * the whole of a recording while it played. Displaying where the video is costs
     * nothing and changes no annotation state.
     */
    const showTime = (timeMs = surface.timeMs) => {
        const text = formatTimestamp(timeMs);
        if (plan.frameTimestamp) {
            plan.frameTimestamp.textContent = text;
        }
        if (plan.timelineCurrent) {
            plan.timelineCurrent.textContent = text;
        }
        if (plan.timelinePlayhead) {
            plan.timelinePlayhead.style.left = `${trackPercent(timeMs, surface.durationMs)}%`;
        }
        showQuadrantAtPlayhead(timeMs);
    };

    const goTo = async (timeMs) => {
        if (timeMs === null) {
            return;
        }
        // Navigating is not watching. Stopped here rather than at each of the seven call
        // sites -- the frame bar, the keyboard, the timeline, the annotation rows -- so
        // none of them can be the one that forgets and leaves the native overlay covering
        // the frame it just navigated to. `stopPlayback` clears `playing` before it lands,
        // so its own `goTo` does not re-enter this.
        if (playing) {
            await stopPlayback();
        }
        await surface.goToInstant(timeMs);
        showTime();
        // A frame change flushes the frame being left into the store, so it can add a row
        // (the first stroke on that frame) or remove one (the last stroke erased).
        drawAnnotations();
    };

    /**
     * Keep the clock and the playhead on a playing video.
     *
     * `timeupdate` rather than a `requestAnimationFrame` loop: the browser fires it a few
     * times a second, which is what a millisecond readout and a 1px playhead need, and it
     * stops on its own when the video does. Bound once per element -- `setVideo` builds
     * one `<video>` for the surface's lifetime, and a listener added on every press of
     * play would accumulate one per press.
     *
     * **A set, not one element.** There are two the page may play -- Cornerstone's, and the
     * native overlay on the compressed film -- and a reader who leaves annotation mode and
     * comes back alternates between them, so a single "the one we follow" slot re-bound
     * both listeners on every switch back.
     */
    const followed = new WeakSet();
    function followPlayback(videoElement) {
        if (!videoElement || followed.has(videoElement)) {
            return;
        }
        followed.add(videoElement);
        videoElement.addEventListener('timeupdate', () => {
            showTime(Math.round((videoElement.currentTime || 0) * 1000));
        });
        // **A film that runs out has stopped playing, and the button has to say so.**
        // Cornerstone's loop pauses itself at the end of its frame range and the native
        // overlay simply ends; either way nothing told the page, so the control was left
        // reading "pause" over a stopped recording and the canvas never landed on the
        // frame the reader had reached.
        videoElement.addEventListener('ended', () => {
            stopPlayback();
        });
    }

    for (const action of ['frameFirst', 'framePrev10', 'framePrev', 'frameNext', 'frameNext10', 'frameLast']) {
        const button = plan[action];
        if (!button) {
            continue;
        }
        button.addEventListener('click', () => {
            goTo(frameTarget(action, surface.timeMs, surface.durationMs));
        });
        bound.push(action);
    }

    // Play is the viewport's, not the store's: `showFrame` is how an *annotated* frame is
    // reached, and running it sixty times a second would rebuild a labelmap per frame.
    // Playing is for looking; the playhead catches up when it stops.
    let playing = false;

    /**
     * Which of the two playback paths a press of play should take.
     *
     * **Outside annotation mode the browser plays the video itself** -- see
     * `editor.setNativePlayback`. In annotation mode the canvas keeps it, because that is
     * the path that composites the masks, and a reader who is drawing is entitled to see
     * the layers they are drawing while it moves.
     */
    const playsNatively = () => !annotating;

    /**
     * The element the current run is playing, so the clock follows it and the stop reads
     * its instant.
     *
     * **Not always Cornerstone's.** Outside annotation mode the overlay may be playing the
     * *compressed* film while the canvas holds a frame of the subsampled one -- see
     * `editor.setNativePlayback`. The two share a clock, so `instantFor` is right either
     * way, but it has to be asked of the element that actually moved.
     */
    let played = null;

    /** Stop, hand the picture back to the canvas, and land on a real frame. */
    async function stopPlayback() {
        if (!playing) {
            return;
        }
        playing = false;
        if (plan.framePlay) {
            plan.framePlay.innerHTML = '<i class="fas fa-play"></i>';
        }
        const viewport = surface.editor?.viewport;
        surface.editor?.setNativePlayback?.(false);
        viewport?.pause?.();
        // Land on a real frame rather than wherever the compositor stopped: every mask is
        // filed against a frame number, so "roughly here" is not a position this surface
        // can hold.
        const stoppedOn = played ?? viewport?.videoElement;
        played = null;
        await goTo(surface.instantFor(stoppedOn, null));
    }

    if (plan.framePlay) {
        plan.framePlay.addEventListener('click', async () => {
            const viewport = surface.editor?.viewport;
            if (!viewport) {
                return;
            }
            if (playing) {
                await stopPlayback();
                return;
            }
            playing = true;
            plan.framePlay.innerHTML = '<i class="fas fa-pause"></i>';
            const overlay = playsNatively()
                ? surface.editor?.setNativePlayback?.(true, surface.timeMs)
                : null;
            played = overlay ?? viewport.videoElement;
            followPlayback(played);
            if (!overlay) {
                viewport.play?.();
            }
        });
        bound.push('framePlay');
    }

    doc.addEventListener('keydown', (event) => {
        const action = keyAction(event);
        if (!action) {
            return;
        }
        event.preventDefault();
        if (action.kind === 'frame') {
            goTo(frameTarget(action.action, surface.timeMs, surface.durationMs));
            return;
        }
        selectTool(action.tool);
    });
    bound.push('keyboard');

    // --- the annotation half, which a degraded mount does not have ------------------

    if (!surface.canAnnotate) {
        hide(plan.toolbar);
        hide(plan.toggle);
        hide(plan.regionPanel);
        hide(plan.timelineBar);
        // Said on the page, not only in the console: the reader is looking at a video
        // whose annotation controls are missing, and "why" is the whole question.
        noticed(doc, 'warning', `Annotations are unavailable for this video: ${surface.reason}`);
        showTime();
        return { plan, bound };
    }

    show(plan.toggle);
    show(plan.timelineBar);

    // --- tools ----------------------------------------------------------------------

    /**
     * Paint the toolbar to match what is armed, or to match nothing being armed.
     *
     * @param {string|null} key
     */
    function markTool(key) {
        for (const other of plan.toolbar?.querySelectorAll('[data-tool]') ?? []) {
            other.classList.toggle('active', other.dataset.tool === key);
        }
    }

    function selectTool(key) {
        const outcome = surface.editor.setActiveTool(key);
        if (outcome === 'needs-region') {
            // Refused rather than silently ignored: a button that looks pressed and does
            // nothing is worse than one that says so.
            noticed(doc, 'warning', 'Pick a region before drawing on one.');
            return false;
        }
        if (outcome !== 'ok') {
            // A toolbar button naming a tool that does not exist. Reported as what it is
            // -- it used to fall into the branch above and tell the reader to pick a
            // region, which was a true sentence with no action behind it.
            report(`no tool is mapped to '${key}'`);
            noticed(doc, 'warning', 'That tool is not available on this surface.');
            return false;
        }
        markTool(key);
        return true;
    }

    plan.toolbar?.addEventListener('click', (event) => {
        const button = event.target.closest('[data-tool]');
        if (button) {
            selectTool(button.dataset.tool);
        }
    });

    /**
     * **Nothing is armed until the reader arms it.**
     *
     * The template gives the brush `.active` at render time, and the binder used to take
     * that as an instruction and arm it. So the page opened with a drawing tool live on a
     * viewport whose annotation toolbar was still hidden: a click on the video painted
     * into whichever region happened to be selected, before the reader had entered
     * annotation mode or chosen anything. The markup's `.active` is a default that was
     * never a decision, and this clears it rather than honouring it.
     *
     * Entering annotation mode does not arm one either -- see the toggle below.
     */
    markTool(null);
    surface.editor.setActiveTool(null);
    bound.push('tools');

    if (plan.toggle) {
        /**
         * Annotation mode: the toolbar, the region panel, and whether a tool is live.
         *
         * The visibility half was already here. What is new is that **leaving annotation
         * mode disarms**: hiding the toolbar used to leave the last tool bound to the
         * primary button, so a drag meant to pan the video went on painting into a region
         * from behind a panel the reader had just closed.
         */
        plan.toggle.addEventListener('click', async () => {
            // **Stopped before the mode changes**, because the two modes play the
            // recording by different means: switching under a running playback would
            // leave the browser compositing an overlay that annotation mode expects the
            // canvas to own, with the masks painting underneath it.
            await stopPlayback();
            const hidden = plan.toolbar.classList.toggle('d-none');
            annotating = !hidden;
            plan.toggle.classList.toggle('active', !hidden);
            plan.regionPanel?.classList.toggle('d-none', hidden);
            if (hidden) {
                surface.editor.setActiveTool(null);
                markTool(null);
            }
        });
        bound.push('annotationToggle');
    }

    if (plan.brushSize) {
        plan.brushSize.addEventListener('input', () => {
            const size = Number(plan.brushSize.value);
            if (plan.brushSizeLabel) {
                plan.brushSizeLabel.textContent = String(size);
            }
            surface.editor.setBrushSize?.(size);
        });
        bound.push('brushSize');
    }

    for (const [name, factor] of [['zoomIn', 1.2], ['zoomOut', 1 / 1.2], ['zoomReset', null]]) {
        const button = plan[name];
        if (!button) {
            continue;
        }
        button.addEventListener('click', () => {
            const viewport = surface.editor?.viewport;
            if (!viewport) {
                return;
            }
            if (factor === null) {
                viewport.resetCamera?.();
            } else {
                zoomBy(viewport, factor);
            }
            viewport.render?.();
            // The native overlay is a projection of the camera, so it has to be
            // re-projected when the camera moves -- otherwise a zoom during playback
            // moves the canvas underneath and leaves the picture on screen where it was.
            if (playing && playsNatively()) {
                surface.editor?.setNativePlayback?.(true);
            }
        });
        bound.push(name);
    }

    // --- regions --------------------------------------------------------------------

    let activeRegion = surface.editor.region;
    /**
     * Regions the reader has folded away.
     *
     * Client-side only, and deliberately: a stored flag would follow them to another
     * workstation and read there as a missing annotation.
     *
     * @type {Set<string>}
     */
    const hiddenRegions = new Set();

    /**
     * One icon button, for the chips and the annotation rows.
     *
     * @param {object} options
     * @param {string} options.icon a Font Awesome glyph.
     * @param {string} options.title the tooltip, and the accessible name.
     * @param {string} [options.variant] extra classes. Defaults to the chip button, which
     *   is what both type lists want; the annotation rows pass Bootstrap's own.
     * @param {[string, string]} options.data the `data-` key and value the delegated
     *   click handler dispatches on.
     */
    function iconButton({ icon, title, variant = '', data: [key, value] }) {
        const button = doc.createElement('button');
        button.type = 'button';
        // Bootstrap variants still need Bootstrap's own box; the chip buttons carry their
        // whole box in `.ygg-type-chip__btn` (see `patient_detail.css`).
        button.className = variant.startsWith('btn-')
            ? `btn btn-sm ${variant} py-0 px-1`
            : `ygg-type-chip__btn ${variant}`.trim();
        button.title = title;
        button.setAttribute('aria-label', title);
        button.dataset[key] = value;
        const glyph = doc.createElement('i');
        glyph.className = `fas ${icon}`;
        glyph.setAttribute('aria-hidden', 'true');
        button.appendChild(glyph);
        return button;
    }

    /**
     * One chip for a named, coloured type -- a region type or a quadrant type.
     *
     * Built once for both, because they are the same object to a reader: a name, a
     * colour, whether it is selected, and the actions on it. The region list used to be
     * a Bootstrap outline button with its visibility and edit buttons as separate pills
     * *beside* it, so two regions read as four unrelated controls in a row with nothing
     * saying which eye belonged to which name. The actions now live inside the pill they
     * act on. See `.ygg-type-chip` in `static/css/patient_detail.css`.
     *
     * @param {object} options
     * @param {string} options.name
     * @param {string} options.color the type's `#rrggbb`.
     * @param {boolean} [options.active] the one being drawn in / marked with.
     * @param {boolean} [options.muted] folded away by the reader.
     * @param {[string, string]} options.select the `data-` key and value that selects it.
     * @param {Array<object>} [options.actions] `iconButton` descriptors.
     * @returns {HTMLElement} the chip.
     */
    function typeChip({ name, color, active = false, muted = false, select, actions = [] }) {
        const chip = doc.createElement('li');
        chip.className = 'ygg-type-chip';
        chip.classList.toggle('is-active', active);
        chip.classList.toggle('is-muted', muted);
        // The two colours the stylesheet cannot know: the type's own, and a foreground
        // that is legible on it once the chip is filled with it.
        chip.style.setProperty('--chip-color', color ?? 'transparent');
        chip.style.setProperty('--chip-ink', readableOn(color));

        const button = doc.createElement('button');
        button.type = 'button';
        button.className = 'ygg-type-chip__select';
        button.dataset[select[0]] = select[1];
        button.title = name;
        // Says the same thing to a screen reader that the fill says to everyone else.
        button.setAttribute('aria-pressed', String(active));

        const dot = doc.createElement('span');
        dot.className = 'ygg-type-chip__dot';
        dot.setAttribute('aria-hidden', 'true');
        const label = doc.createElement('span');
        label.className = 'ygg-type-chip__name';
        label.textContent = name;
        const check = doc.createElement('i');
        check.className = 'fas fa-check ygg-type-chip__check';
        check.setAttribute('aria-hidden', 'true');
        button.append(dot, label, check);
        chip.appendChild(button);

        if (actions.length) {
            const group = doc.createElement('span');
            group.className = 'ygg-type-chip__actions';
            for (const action of actions) {
                group.appendChild(iconButton(action));
            }
            chip.appendChild(group);
        }
        return chip;
    }

    /**
     * Ask the server to change a region type, and take the answer on.
     *
     * `PATCH` carries whichever of name and colour changed. The colour is stored per user
     * (`RegionTypeUserColor`), the name is project-wide and administrator-only -- the
     * endpoint decides both, and a refusal is shown rather than guessed at.
     */
    async function patchRegionType(type, body) {
        try {
            const response = await doFetch(`/laparoscopy/api/region-types/${type.id}/`, {
                method: 'PATCH',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': readCsrfToken(doc) },
                body: JSON.stringify(body),
            });
            const payload = await response.json().catch(() => null);
            if (!response.ok) {
                noticed(doc, 'error', payload?.error ?? `The region type could not be changed (HTTP ${response.status}).`);
                return false;
            }
            const renamed = payload?.name && payload.name !== type.name;
            if (renamed && activeRegion === type.name) {
                activeRegion = payload.name;
            }
            if (renamed && hiddenRegions.delete(type.name)) {
                hiddenRegions.add(payload.name);
            }
            const renamedTo = renamed ? payload.name : type.name;
            surface.updateRegionType?.(type.name, payload ?? {});
            if (activeRegion) {
                surface.editor.selectRegion(activeRegion);
            }
            if (payload?.color) {
                // Straight at the colour LUT entry: re-registering the representation is
                // what colours a *new* region, and it short-circuits for one already on
                // screen -- so the swatch would move and the mask would not.
                surface.editor.setRegionColor?.(renamedTo, payload.color);
            }
            drawRegions();
            drawAnnotations();
            return true;
        } catch (error) {
            noticed(doc, 'error', `The region type could not be changed: ${error.message}`);
            return false;
        }
    }

    /** Delete a region type, and everything this session holds under its code. */
    async function deleteRegionType(type) {
        const confirmed = doc.defaultView?.confirm?.(
            `Delete the region type "${type.name}"? Every mask drawn in it is removed from `
                + 'this session and will be gone from the record on the next save.'
        );
        if (!confirmed) {
            return false;
        }
        try {
            const response = await doFetch(`/laparoscopy/api/region-types/${type.id}/`, {
                method: 'DELETE',
                credentials: 'same-origin',
                headers: { 'X-CSRFToken': readCsrfToken(doc) },
            });
            if (!response.ok && response.status !== 204) {
                const payload = await response.json().catch(() => null);
                noticed(doc, 'error', payload?.error ?? `The region type could not be deleted (HTTP ${response.status}).`);
                return false;
            }
            hiddenRegions.delete(type.name);
            surface.removeRegionType?.(type.name);
            activeRegion = surface.editor.region;
            surface.markDirty();
            drawRegions();
            drawAnnotations();
            return true;
        } catch (error) {
            noticed(doc, 'error', `The region type could not be deleted: ${error.message}`);
            return false;
        }
    }
    function drawRegions() {
        if (!plan.regionList) {
            return;
        }
        plan.regionList.textContent = '';
        // **An empty list is a statement, not a blank.** Every drawing tool needs a
        // selected region, so a project with no region types leaves the whole toolbar
        // answering "Pick a region before drawing on one" over a panel offering nothing
        // to pick -- a true sentence with no action behind it, which is how it was
        // reported. The panel says what is actually wrong, and the Add button beside it
        // is the action; naming what a project draws is annotator work rather than
        // administration (`_handle_type_detail` requires `profile.is_annotator()`, and
        // the template gates the button on `user_profile.is_annotator`), so a reader
        // without it is told who can, and the sentence differs.
        if (surface.regionTypes.length === 0) {
            const note = doc.createElement('li');
            note.className = 'small text-muted';
            note.dataset.regionsEmpty = 'true';
            note.textContent = plan.regionAdd
                ? 'This project defines no region types yet. Add one to start drawing.'
                : 'This project defines no region types yet, so nothing can be drawn. '
                  + 'An annotator can add one.';
            plan.regionList.appendChild(note);
            return;
        }
        for (const type of surface.regionTypes) {
            const hidden = hiddenRegions.has(type.name);
            // Hide/show is offered to every reader: whether a layer is folded away while
            // they work on the one under it is not a fact about the study. Rename,
            // recolour and delete edit the project's vocabulary, and the endpoints
            // enforce that again (`_handle_type_detail` requires `profile.is_annotator()`)
            // -- the Add button's presence is the template's own gate, so it is reused
            // here rather than passing the same flag down a second path.
            const actions = [
                {
                    icon: hidden ? 'fa-eye-slash' : 'fa-eye',
                    title: hidden ? `Show ${type.name}` : `Hide ${type.name}`,
                    data: ['regionVisibility', type.name],
                },
            ];
            if (plan.regionAdd) {
                actions.push({
                    icon: 'fa-pen',
                    title: `Rename or recolour ${type.name}`,
                    data: ['regionEdit', type.name],
                });
                actions.push({
                    icon: 'fa-times',
                    title: `Delete ${type.name}`,
                    variant: 'ygg-type-chip__btn--danger',
                    data: ['regionDelete', type.name],
                });
            }
            plan.regionList.appendChild(
                typeChip({
                    name: type.name,
                    color: type.color,
                    active: type.name === activeRegion,
                    muted: hidden,
                    select: ['region', type.name],
                    actions,
                })
            );
        }
    }

    plan.regionList?.addEventListener('click', async (event) => {
        const control = event.target.closest(
            '[data-region],[data-region-visibility],[data-region-edit],[data-region-delete]'
        );
        if (!control) {
            return;
        }
        const { region, regionVisibility, regionEdit, regionDelete } = control.dataset;

        if (regionVisibility) {
            const nextVisible = hiddenRegions.delete(regionVisibility)
                ? true
                : (hiddenRegions.add(regionVisibility), false);
            surface.editor.setRegionVisible?.(regionVisibility, nextVisible);
            drawRegions();
            return;
        }

        const type = surface.regionTypes.find(
            (known) => known.name === (regionEdit ?? regionDelete)
        );
        if (regionEdit && type) {
            const name = doc.defaultView?.prompt?.('Name for this region type:', type.name)?.trim();
            if (name === undefined || name === null) {
                return;
            }
            const color = doc.defaultView
                ?.prompt?.('Colour for this region type, as #rrggbb:', type.color)
                ?.trim();
            const body = {};
            if (name && name !== type.name) {
                body.name = name;
            }
            if (color && color !== type.color) {
                body.color = color;
            }
            if (Object.keys(body).length) {
                await patchRegionType(type, body);
            }
            return;
        }
        if (regionDelete && type) {
            await deleteRegionType(type);
            return;
        }

        if (region) {
            activeRegion = region;
            surface.editor.selectRegion(activeRegion);
            drawRegions();
            drawAnnotations();
        }
    });
    drawRegions();
    bound.push('regions');

    // --- the annotation list --------------------------------------------------------

    show(plan.shapesPanel);
    /** Scope the list to the selected region. Off by default: the whole study first. */
    let filterToActiveRegion = false;
    annotationsReady = true;

    /**
     * One row per mask this session holds.
     *
     * **The addressable unit is a (region, frame) mask, and that is what a row is.** There
     * is no stroke history to list: decision #14 made the labelmap canonical, so a frame's
     * mask for a region is one thing however many times the brush crossed it. The tool is
     * the last one to have written that mask (`masks.js`'s `toolAt`) and is absent for
     * every mask stored before attribution existed -- the tool was never recorded, so the
     * row says the region and the instant and claims nothing else.
     */
    function drawAnnotations() {
        // `goTo` calls this, and `goTo` is also reached by a mount that has no annotation
        // half at all -- a list rendered into a panel that stays hidden would be work done
        // to be invisible.
        if (!plan.shapesList || !annotationsReady) {
            return;
        }
        plan.shapesList.textContent = '';
        const rows = (surface.annotations?.() ?? []).filter(
            (row) => !filterToActiveRegion || row.regionCode === activeRegion
        );
        plan.shapesFilter?.classList.toggle('active', filterToActiveRegion);

        if (!rows.length) {
            const note = doc.createElement('li');
            note.className = 'list-group-item small text-muted py-1';
            note.dataset.annotationsEmpty = 'true';
            note.textContent = filterToActiveRegion
                ? 'Nothing is drawn in the selected region yet.'
                : 'Nothing is drawn on this video yet.';
            plan.shapesList.appendChild(note);
            return;
        }

        for (const row of rows) {
            const item = doc.createElement('li');
            item.className = 'list-group-item d-flex align-items-center gap-2 py-1';
            item.dataset.annotationTime = String(row.timeMs);
            item.dataset.annotationRegion = row.regionCode;

            const swatch = doc.createElement('span');
            swatch.style.cssText =
                'display:inline-block;width:10px;height:10px;border-radius:50%;'
                + 'border:1px solid rgba(0,0,0,.35);flex:0 0 auto;';
            swatch.style.background = row.color ?? 'transparent';

            const label = doc.createElement('button');
            label.type = 'button';
            label.className = 'btn btn-sm btn-link p-0 small text-start flex-grow-1';
            label.dataset.annotationSeek = String(row.timeMs);
            label.title = 'Go to this frame';
            const tool = TOOL_LABELS[row.tool];
            if (tool) {
                const glyph = doc.createElement('i');
                glyph.className = `fas ${tool.icon} me-1`;
                glyph.setAttribute('aria-hidden', 'true');
                label.appendChild(glyph);
            }
            label.appendChild(
                doc.createTextNode(
                    `${tool ? `${tool.label} • ` : ''}${row.regionCode} @${formatTimestamp(row.timeMs)}`
                )
            );

            item.append(swatch, label);
            item.appendChild(
                iconButton({
                    icon: 'fa-exchange-alt',
                    title: 'Move this mask to another region type',
                    variant: 'btn-outline-secondary',
                    data: ['annotationMove', `${row.timeMs}|${row.regionCode}`],
                })
            );
            item.appendChild(
                iconButton({
                    icon: 'fa-trash',
                    title: 'Clear this mask',
                    variant: 'btn-outline-danger',
                    data: ['annotationDelete', `${row.timeMs}|${row.regionCode}`],
                })
            );
            plan.shapesList.appendChild(item);
        }
    }

    /** The region picker the move button swaps itself for. */
    function regionPicker(timeMs, fromCode) {
        const select = doc.createElement('select');
        select.className = 'form-select form-select-sm py-0';
        select.dataset.annotationMoveTo = `${timeMs}|${fromCode}`;
        const blank = doc.createElement('option');
        blank.value = '';
        blank.textContent = 'Move to...';
        select.appendChild(blank);
        for (const type of surface.regionTypes) {
            if (type.name === fromCode) {
                continue;
            }
            const option = doc.createElement('option');
            option.value = type.name;
            option.textContent = type.name;
            select.appendChild(option);
        }
        return select;
    }

    plan.shapesList?.addEventListener('click', async (event) => {
        const control = event.target.closest(
            '[data-annotation-seek],[data-annotation-move],[data-annotation-delete]'
        );
        if (!control) {
            return;
        }
        const { annotationSeek, annotationMove, annotationDelete } = control.dataset;
        if (annotationSeek) {
            await goTo(Number(annotationSeek));
            return;
        }

        const [timeMs, regionCode] = (annotationMove ?? annotationDelete).split('|');

        if (annotationDelete) {
            // **Seek first.** The editor edits the labelmap of the frame on screen, which
            // is what makes the change visible and what lets the next flush carry it into
            // the store like any stroke. Acting on an off-screen frame would need a second
            // write path into the store and would change the record with nothing to show.
            await goTo(Number(timeMs));
            surface.editor.clearRegionAt?.(regionCode);
            surface.markDirty();
            drawAnnotations();
            return;
        }
        if (surface.regionTypes.length < 2) {
            noticed(doc, 'warning', 'There is no other region type to move this mask to.');
            return;
        }
        // **Not seeking yet, deliberately.** `goTo` redraws this list, which would detach
        // the very button being replaced -- the picker would be swapped into a row that is
        // no longer in the document and never appear. The move seeks when the choice is
        // made instead, which is also the moment the reader wants to see the frame.
        control.replaceWith(regionPicker(Number(timeMs), regionCode));
    });

    plan.shapesList?.addEventListener('change', async (event) => {
        const select = event.target.closest('[data-annotation-move-to]');
        if (!select?.value) {
            return;
        }
        const [timeMs, fromCode] = select.dataset.annotationMoveTo.split('|');
        // The tool the row was showing, carried across: it is the same mask under a new
        // heading, and the tool the reader happens to have armed while re-filing it says
        // nothing about who drew it.
        const tool = (surface.annotations?.() ?? []).find(
            (row) => row.timeMs === Number(timeMs) && row.regionCode === fromCode
        )?.tool;
        await goTo(Number(timeMs));
        if (surface.editor.moveRegionAt?.(fromCode, select.value, tool ?? null)) {
            surface.markDirty();
        }
        drawAnnotations();
    });

    plan.shapesFilter?.addEventListener('click', () => {
        filterToActiveRegion = !filterToActiveRegion;
        drawAnnotations();
    });
    drawAnnotations();
    bound.push('annotations');

    // The Add button has been in the template since Phase 10 and was bound to nothing --
    // the exact control-that-does-nothing this module's header refuses. It is rendered
    // for annotators and administrators (`user_profile.is_annotator` in the template),
    // and the endpoint enforces that again.
    if (plan.regionAdd) {
        plan.regionAdd.addEventListener('click', async () => {
            const name = doc.defaultView?.prompt?.('Name for the new region type:')?.trim();
            if (!name) {
                return;
            }
            try {
                const response = await doFetch('/laparoscopy/api/region-types/', {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRFToken': readCsrfToken(doc),
                    },
                    body: JSON.stringify({ name }),
                });
                const payload = await response.json().catch(() => null);
                if (!response.ok) {
                    noticed(doc, 'error', payload?.error ?? `The region type could not be added (HTTP ${response.status}).`);
                    return;
                }
                // The surface builds a labelmap per region, so it has to be told before
                // the new type can be drawn on -- and the current frame re-shown so that
                // labelmap exists for it.
                if (surface.addRegionType?.(payload)) {
                    await surface.goToInstant(surface.timeMs);
                }
                activeRegion = activeRegion ?? payload?.name ?? null;
                if (activeRegion) {
                    surface.editor.selectRegion(activeRegion);
                }
                drawRegions();
            } catch (error) {
                noticed(doc, 'error', `The region type could not be added: ${error.message}`);
            }
        });
        bound.push('regionAdd');
    }

    if (plan.regionVisibility) {
        let visible = true;
        plan.regionVisibility.addEventListener('click', () => {
            visible = !visible;
            surface.editor.setRegionsVisible?.(visible);
            plan.regionVisibility.innerHTML = visible
                ? '<i class="fas fa-eye-slash me-1"></i>Hide all'
                : '<i class="fas fa-eye me-1"></i>Show all';
        });
        bound.push('regionVisibility');
    }

    // --- saving ---------------------------------------------------------------------

    if (plan.save) {
        plan.save.addEventListener('click', async () => {
            plan.save.disabled = true;
            try {
                const result = await surface.save();
                if (result.ok) {
                    // The save flushed the current frame, which is the last chance for a
                    // row to appear or go.
                    drawAnnotations();
                    flashSaved();
                } else {
                    noticed(doc, 'error', result.message ?? 'The save did not complete.');
                }
            } catch (error) {
                noticed(doc, 'error', `The save failed: ${error.message}`);
            } finally {
                plan.save.disabled = false;
            }
        });
        bound.push('save');
    }

    function flashSaved() {
        const indicator = plan.savingIndicator;
        if (!indicator) {
            return;
        }
        indicator.style.display = 'block';
        doc.defaultView?.setTimeout?.(() => {
            indicator.style.display = 'none';
        }, 1500);
    }

    doc.defaultView?.addEventListener?.('beforeunload', (event) => {
        if (surface.store?.annotatedTimes().length && surface.dirty) {
            event.preventDefault();
            event.returnValue = '';
        }
    });
    bound.push('unloadGuard');

    // --- the quadrant timeline --------------------------------------------------------

    function showQuadrantAtPlayhead(timeMs = surface.timeMs) {
        if (!plan.timelineActiveClass) {
            return;
        }
        const current = quadrantAt(segments, timeMs);
        plan.timelineActiveClass.textContent = `Current: ${current?.name ?? '-'}`;
    }

    function drawTimeline() {
        segments = markerSegments(markers, surface.durationMs, quadrantTypes);
        if (plan.timelineSegments) {
            plan.timelineSegments.textContent = '';
            for (const segment of segments) {
                const bar = doc.createElement('div');
                bar.className = 'timeline-segment';
                bar.style.left = `${trackPercent(segment.startMs, surface.durationMs)}%`;
                bar.style.width = `${
                    trackPercent(segment.endMs, surface.durationMs)
                    - trackPercent(segment.startMs, surface.durationMs)
                }%`;
                bar.style.setProperty('--segment-color', segment.color);
                bar.title = `${segment.name} — ${formatTimestamp(segment.startMs)}`;
                plan.timelineSegments.appendChild(bar);
            }
        }
        if (plan.timelinePins) {
            plan.timelinePins.textContent = '';
            for (const marker of markers) {
                const type = quadrantTypes.get(marker.quadrant_type_id);
                const pin = doc.createElement('button');
                pin.type = 'button';
                pin.className = 'timeline-pin';
                pin.dataset.markerId = String(marker.id ?? '');
                pin.dataset.timeMs = String(marker.time_ms);
                pin.style.left = `${trackPercent(marker.time_ms, surface.durationMs)}%`;
                pin.style.setProperty('--pin-color', type?.color ?? '#0d6efd');
                pin.title = `${type?.name ?? '—'} at ${formatTimestamp(marker.time_ms)} — click to remove`;
                plan.timelinePins.appendChild(pin);
            }
        }
        showQuadrantAtPlayhead();
    }

    async function saveMarkers(next) {
        const response = await doFetch(
            `/laparoscopy/api/patient/${surface.patientId}/quadrant-markers/`,
            {
                // **`PUT`, not `POST`.** `patient_quadrant_markers` is
                // `@require_http_methods(["GET", "PUT"])`, so every marker add and every
                // marker removal answered 405 -- "The markers could not be saved (HTTP
                // 405)" -- and the whole timeline was unwritable. The verb is right for
                // what this does anyway: the client sends the entire set and the server
                // replaces it, exactly as the region save does.
                method: 'PUT',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': readCsrfToken(doc) },
                body: JSON.stringify({
                    markers: next.map((marker) => ({
                        time_ms: marker.time_ms,
                        quadrant_type_id: marker.quadrant_type_id,
                    })),
                }),
            }
        );
        if (!response.ok) {
            const payload = await response.json().catch(() => null);
            noticed(doc, 'error', payload?.error ?? `The markers could not be saved (HTTP ${response.status}).`);
            return false;
        }
        markers = (await response.json()).markers ?? next;
        drawTimeline();
        return true;
    }

    plan.timelineAddPin?.addEventListener('click', async () => {
        if (activeQuadrantId === null) {
            noticed(doc, 'warning', 'Pick a quadrant before adding a marker.');
            return;
        }
        // Rounded on both sides of the comparison: the new marker is stored rounded, so
        // filtering against the unrounded instant left the old marker in place for the
        // server to dedupe -- silently, and only sometimes.
        const timeMs = Math.round(surface.timeMs);
        await saveMarkers([
            ...markers.filter((marker) => marker.time_ms !== timeMs),
            { time_ms: timeMs, quadrant_type_id: activeQuadrantId },
        ]);
    });

    plan.timelinePins?.addEventListener('click', async (event) => {
        const pin = event.target.closest('[data-time-ms]');
        if (!pin) {
            return;
        }
        const timeMs = Number(pin.dataset.timeMs);
        if (!doc.defaultView?.confirm?.(`Remove the marker at ${formatTimestamp(timeMs)}?`)) {
            return;
        }
        await saveMarkers(markers.filter((marker) => marker.time_ms !== timeMs));
    });

    plan.timelineTrackWrap?.addEventListener('click', (event) => {
        if (event.target.closest('.timeline-pin')) {
            return;
        }
        const box = plan.timelineTrackWrap.getBoundingClientRect();
        if (!box.width) {
            return;
        }
        const fraction = Math.max(0, Math.min((event.clientX - box.left) / box.width, 1));
        goTo(fraction * (surface.durationMs ?? 0));
    });

    /**
     * Make one quadrant the one the next marker will carry.
     *
     * Reached from the dropdown *and* from a chip in the administration panel: the chip
     * renders `is-active` for the current one, and a chip that showed a selection it
     * could not change would be a control that only ever reports.
     *
     * @param {number} id
     */
    function selectQuadrant(id) {
        activeQuadrantId = id;
        const type = quadrantTypes.get(activeQuadrantId);
        if (plan.timelineClassLabel) {
            plan.timelineClassLabel.textContent = type?.name ?? '—';
        }
        if (plan.timelineClassSwatch) {
            plan.timelineClassSwatch.style.background = type?.color ?? '#ccc';
        }
        drawQuadrantTypes();
    }

    plan.timelineClassList?.addEventListener('click', (event) => {
        const option = event.target.closest('[data-quadrant-id]');
        if (option) {
            selectQuadrant(Number(option.dataset.quadrantId));
        }
    });

    if (plan.timelineDuration) {
        plan.timelineDuration.textContent = formatTimestamp(surface.durationMs ?? 0);
    }

    // --- quadrant types ---------------------------------------------------------------

    /**
     * The quadrant selector, and the administrative panel beside it.
     *
     * **The panel was authored and bound to nothing.** `#quadrant-types-panel`,
     * `#timeline-add-class-btn` and `#timeline-class-admin-list` have been in the template
     * since Phase 10 with seventy lines of `.quadrant-chip` CSS written for them, and
     * appeared in no JavaScript file at all -- so the panel kept its `d-none` forever,
     * there was no way to create a quadrant type in the page, `activeQuadrantId` could
     * never leave `null`, and "Add Marker" could only answer "Pick a quadrant before
     * adding a marker." The endpoints
     * (`/laparoscopy/api/quadrant-types/`, GET/POST and PATCH/DELETE) have existed the
     * whole time; this is the binder they were waiting for.
     */
    function drawQuadrantTypes() {
        if (plan.timelineClassList) {
            plan.timelineClassList.textContent = '';
            for (const type of quadrantTypes.values()) {
                const item = doc.createElement('li');
                const option = doc.createElement('button');
                option.type = 'button';
                option.className = 'ygg-dropdown-item d-flex align-items-center gap-2';
                option.dataset.quadrantId = String(type.id);
                const swatch = doc.createElement('span');
                swatch.style.cssText = 'display:inline-block;width:10px;height:10px;border-radius:50%;';
                swatch.style.background = type.color;
                option.append(swatch, doc.createTextNode(type.name));
                item.appendChild(option);
                plan.timelineClassList.appendChild(item);
            }
        }
        if (!plan.quadrantAdminList) {
            return;
        }
        // The panel is inside the template's `{% if user_profile.is_annotator %}`, so its
        // presence is the gate -- the same rule the region rows use for their own
        // administrative controls.
        show(plan.quadrantPanel);
        plan.quadrantAdminList.textContent = '';
        if (!quadrantTypes.size) {
            const note = doc.createElement('span');
            note.className = 'small text-muted';
            note.dataset.quadrantsEmpty = 'true';
            note.textContent = 'No quadrants yet. Add one to start marking sections.';
            plan.quadrantAdminList.appendChild(note);
            return;
        }
        for (const type of quadrantTypes.values()) {
            // The same chip the region list is built from -- see `typeChip`. These two
            // lists were built twice with the quadrant half carrying seventy lines of
            // inline stylesheet in the template, inside the admin block, so a non-admin's
            // page did not even load the CSS describing them.
            plan.quadrantAdminList.appendChild(
                typeChip({
                    name: type.name,
                    color: type.color,
                    active: type.id === activeQuadrantId,
                    select: ['quadrantSelect', String(type.id)],
                    actions: [
                        {
                            icon: 'fa-pen',
                            title: `Rename or recolour ${type.name}`,
                            data: ['quadrantEdit', String(type.id)],
                        },
                        {
                            icon: 'fa-times',
                            title: `Delete ${type.name}`,
                            variant: 'ygg-type-chip__btn--danger',
                            data: ['quadrantDelete', String(type.id)],
                        },
                    ],
                })
            );
        }
    }

    /** Re-read the project's quadrant types after any change to them. */
    async function reloadQuadrantTypes() {
        const response = await doFetch('/laparoscopy/api/quadrant-types/', {
            credentials: 'same-origin',
        });
        const payload = response.ok ? await response.json() : { types: [] };
        quadrantTypes.clear();
        for (const type of payload.types ?? []) {
            quadrantTypes.set(type.id, type);
        }
        if (activeQuadrantId !== null && !quadrantTypes.has(activeQuadrantId)) {
            activeQuadrantId = null;
            if (plan.timelineClassLabel) {
                plan.timelineClassLabel.textContent = '—';
            }
        }
        drawQuadrantTypes();
        drawTimeline();
    }

    plan.quadrantAdd?.addEventListener('click', async () => {
        const name = doc.defaultView?.prompt?.('Name for the new quadrant:')?.trim();
        if (!name) {
            return;
        }
        try {
            const response = await doFetch('/laparoscopy/api/quadrant-types/', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': readCsrfToken(doc) },
                body: JSON.stringify({ name }),
            });
            const payload = await response.json().catch(() => null);
            if (!response.ok) {
                noticed(doc, 'error', payload?.error ?? `The quadrant could not be added (HTTP ${response.status}).`);
                return;
            }
            await reloadQuadrantTypes();
        } catch (error) {
            noticed(doc, 'error', `The quadrant could not be added: ${error.message}`);
        }
    });

    plan.quadrantAdminList?.addEventListener('click', async (event) => {
        const control = event.target.closest(
            '[data-quadrant-select],[data-quadrant-edit],[data-quadrant-delete]'
        );
        if (!control) {
            return;
        }
        const { quadrantSelect, quadrantEdit, quadrantDelete } = control.dataset;
        if (quadrantSelect) {
            selectQuadrant(Number(quadrantSelect));
            return;
        }
        const type = quadrantTypes.get(Number(quadrantEdit ?? quadrantDelete));
        if (!type) {
            return;
        }
        try {
            if (quadrantEdit) {
                const name = doc.defaultView?.prompt?.('Name for this quadrant:', type.name)?.trim();
                if (name === undefined || name === null) {
                    return;
                }
                const color = doc.defaultView
                    ?.prompt?.('Colour for this quadrant, as #rrggbb:', type.color)
                    ?.trim();
                const body = {};
                if (name && name !== type.name) {
                    body.name = name;
                }
                if (color && color !== type.color) {
                    body.color = color;
                }
                if (!Object.keys(body).length) {
                    return;
                }
                const response = await doFetch(`/laparoscopy/api/quadrant-types/${type.id}/`, {
                    method: 'PATCH',
                    credentials: 'same-origin',
                    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': readCsrfToken(doc) },
                    body: JSON.stringify(body),
                });
                if (!response.ok) {
                    const payload = await response.json().catch(() => null);
                    noticed(doc, 'error', payload?.error ?? `The quadrant could not be changed (HTTP ${response.status}).`);
                    return;
                }
                await reloadQuadrantTypes();
                return;
            }

            const inUse = markers.some((marker) => marker.quadrant_type_id === type.id);
            if (!doc.defaultView?.confirm?.(`Delete the quadrant "${type.name}"?`)) {
                return;
            }
            // The endpoint refuses to orphan markers and asks which type they should
            // become instead (`_quadrant_type_delete_hook`). Asking here rather than
            // relaying its error means the reader is asked the question once.
            let replacementId;
            if (inUse) {
                const others = [...quadrantTypes.values()].filter((known) => known.id !== type.id);
                if (!others.length) {
                    noticed(
                        doc,
                        'error',
                        `"${type.name}" is used by markers on this video and is the only quadrant, `
                            + 'so there is nothing to reassign them to. Remove the markers first.'
                    );
                    return;
                }
                const answer = doc.defaultView?.prompt?.(
                    `Markers use "${type.name}". Which quadrant should they become?\n`
                        + others.map((known) => known.name).join(', '),
                    others[0].name
                );
                const replacement = others.find((known) => known.name === answer?.trim());
                if (!replacement) {
                    return;
                }
                replacementId = replacement.id;
            }
            const response = await doFetch(`/laparoscopy/api/quadrant-types/${type.id}/`, {
                method: 'DELETE',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': readCsrfToken(doc) },
                ...(replacementId ? { body: JSON.stringify({ replacement_id: replacementId }) } : {}),
            });
            if (!response.ok && response.status !== 204) {
                const payload = await response.json().catch(() => null);
                noticed(doc, 'error', payload?.error ?? `The quadrant could not be deleted (HTTP ${response.status}).`);
                return;
            }
            const reload = await doFetch(
                `/laparoscopy/api/patient/${surface.patientId}/quadrant-markers/`,
                { credentials: 'same-origin' }
            );
            markers = reload.ok ? (await reload.json()).markers ?? markers : markers;
            await reloadQuadrantTypes();
        } catch (error) {
            noticed(doc, 'error', `The quadrant could not be changed: ${error.message}`);
        }
    });

    // Fetched rather than rendered into the page: the quadrant types are project-level
    // and a patient page that inlined them would go stale the moment an administrator
    // added one. A failure here costs the timeline and nothing else.
    (async () => {
        try {
            const [types, stored] = await Promise.all([
                doFetch('/laparoscopy/api/quadrant-types/', { credentials: 'same-origin' })
                    .then((response) => (response.ok ? response.json() : { types: [] })),
                doFetch(`/laparoscopy/api/patient/${surface.patientId}/quadrant-markers/`, {
                    credentials: 'same-origin',
                }).then((response) => (response.ok ? response.json() : { markers: [] })),
            ]);
            for (const type of types.types ?? []) {
                quadrantTypes.set(type.id, type);
            }
            drawQuadrantTypes();
            markers = stored.markers ?? [];
            drawTimeline();
        } catch (error) {
            console.info(`[ygg-video] the quadrant timeline could not be loaded: ${error.message}`);
        }
    })();
    bound.push('timeline');

    showTime();
    return { plan, bound };
}
