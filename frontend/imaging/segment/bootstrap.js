/**
 * Page glue for the segmentation surface: the Annotation Mode button, the toolbar, the
 * label panel and the save indicator, wired to a session.
 *
 * Everything that can fail does so *visibly*: a status line says what happened, and nothing
 * here throws into the page. A broken annotator must not take the patient record down with
 * it (the same rule `photos/bootstrap.js` follows).
 */

import { createApi, createQuadrantsApi, endpointFor, quadrantsEndpointFor } from './api.js';
import { mountQuadrants } from './quadrants.js';
import { createFrameSource } from './frameSource.js';
import { renderLabelPanel, describeSave, labelAfterRemoval } from './labelPanel.js';
import { createSaver, SaveStatus } from './saver.js';
import { ASSIST_TOOL, SEGMENT_TOOLS } from './tools.js';
import {
    assistSocketUrl,
    createAssistChannel,
    createPromptPoints,
    frameTimeMs,
    hasPixels,
    labelIsEmptyOn,
    maskTarget,
    mergeTrackedPlane,
    packMask,
    unpackMask,
} from './assist.js';
import { decodePlane, encodePlane } from './planes.js';

const DATA_ELEMENT_ID = 'imageSegmentData';

/** What a project's first label is called; renaming is one click. */
export const FIRST_LABEL_NAME = 'Label 1';

function report(message) {
    console.error(`[ygg-segment] ${message}`);
}

/**
 * The payload the page embeds, or `null` if it is absent or unusable.
 *
 * `fps`, `width` and `height` are the server's `ffprobe` result for the file being
 * annotated. Without them the surface does not mount: a guessed frame rate puts every mask
 * on the wrong frame while looking entirely right.
 */
export function readSegmentData(doc, elementId = DATA_ELEMENT_ID) {
    const node = doc?.getElementById?.(elementId);
    if (!node) {
        return null;
    }
    try {
        const data = JSON.parse(node.textContent ?? '{}');
        const ok =
            data?.patientId &&
            data?.projectNamespace &&
            Number.isInteger(data?.fileId) &&
            typeof data?.videoUrl === 'string' &&
            Number.isInteger(data?.frameCount) &&
            data.frameCount > 0 &&
            data?.fps > 0 &&
            data?.width > 0 &&
            data?.height > 0;
        if (!ok) {
            report(`#${elementId} lacks the video probe; not mounting.`);
            return null;
        }
        return data;
    } catch (error) {
        report(`#${elementId} is not valid JSON: ${error.message}`);
        return null;
    }
}

export function csrfToken(doc) {
    return doc?.querySelector?.('input[name="csrfmiddlewaretoken"]')?.value ?? '';
}

/** The save endpoint's body. Every frame named here replaces that frame wholesale. */
export function buildSaveBody({ fileId, revision, fps, frames }) {
    return {
        fileId,
        expectedRevision: revision,
        coordinateSystem: 'video_pixel',
        fps,
        frames,
    };
}

const $ = (doc, id) => doc.getElementById(id);

/**
 * @param {object} options
 * @param {Document} options.doc
 * @param {Function} options.openSession `openSession` bound to the injected Cornerstone.
 * @param {Function} [options.fetchImpl]
 */
export async function bootstrapSegmentation({ doc, openSession, fetchImpl, ask }) {
    const data = readSegmentData(doc);
    const toggle = $(doc, 'annotation-toggle-btn');
    if (!data || !toggle) {
        return null;
    }

    const video = doc.querySelector('#patient-video');
    const annotationVideo = doc.querySelector('#annotation-video');
    const workspace = $(doc, 'segment-workspace');
    const viewportElement = $(doc, 'segment-viewport');
    const statusLine = $(doc, 'segment-save-status');
    const frameInfo = $(doc, 'segment-frame-info');
    const labelContainer = $(doc, 'segment-labels');
    const messages = $(doc, 'segment-message');
    const timeline = $(doc, 'video-timeline-range');
    const timelineTime = $(doc, 'video-timeline-time');
    const timelineControls = [...doc.querySelectorAll('[data-video-seek]')];
    const startButton = $(doc, 'video-start');
    const stopButton = $(doc, 'video-stop');
    if (!video || !annotationVideo || !workspace || !viewportElement) {
        report('the annotation markup is missing; not mounting.');
        return null;
    }

    const api = createApi({
        endpoint: endpointFor(data),
        fetchImpl,
        csrf: csrfToken(doc),
    });
    const frameSource = createFrameSource({
        video: annotationVideo,
        probe: { fps: data.fps, width: data.width, height: data.height, frameCount: data.frameCount },
        createCanvas: (width, height) => {
            const canvas = doc.createElement('canvas');
            canvas.width = width;
            canvas.height = height;
            return canvas;
        },
    });

    function say(text, isError = false) {
        if (messages) {
            messages.textContent = text;
            messages.classList.toggle('text-danger', isError);
            messages.classList.toggle('d-none', !text);
        }
    }

    let quadrants = null;
    let session = null;
    let state = null;
    let saver = null;
    let toolId = 'brush';
    let sizeTouched = false;
    let editQueued = false;
    let selected = null;
    let navigating = false;
    const hidden = new Set();

    // Assisted segmentation ---------------------------------------------------------
    // The relay opens with the page, so the worker precomputes while the annotator is
    // still watching the video; leaving the page closes it (and the worker session).
    const assistButton = doc.querySelector(`[data-segment-tool="${ASSIST_TOOL}"]`);
    const assistStatus = $(doc, 'assist-status');
    const assistModes = [...doc.querySelectorAll('[data-assist-mode]')];
    const assistWindow = $(doc, 'assist-window');
    // Masks the worker tracked onto later frames, waiting for the autosaver: timeMs -> {code, plane}.
    const tracked = new Map();
    const assistOverlay = $(doc, 'segment-assist-points');
    const promptPoints = createPromptPoints();
    const ASSIST_STATUS = {
        connecting: 'Connecting…',
        preparing: 'Preparing video…',
        ready: 'Ready',
        busy: 'Working…',
        closed: 'Unavailable',
    };
    const SVG_NS = 'http://www.w3.org/2000/svg';
    let includeNext = true;
    const assist =
        data.canAssist && assistButton
            ? createAssistChannel({
                  url: assistSocketUrl(doc.defaultView.location, data),
                  onStatus: describeAssist,
                  onMask: applyAssistMask,
                  onError: (text) => say(text, true),
              })
            : null;

    function describeAssist(status) {
        assistButton.disabled = status !== 'ready' && status !== 'busy';
        if (assistStatus) {
            assistStatus.textContent = ASSIST_STATUS[status] ?? '';
            assistStatus.dataset.state = status;
        }
        if (status === 'closed' && toolId === ASSIST_TOOL) {
            say('The assistant is unavailable', true);
            doc.querySelector('[data-segment-tool="brush"]')?.click();
        }
    }

    function applyAssistMask(message, sent) {
        // A prompt's answers count only while the annotator is still where they clicked.
        if (!session || !sent || session.frame.frameIndex !== sent.frameIndex || selected !== sent.code) {
            return;
        }
        if (message.frameIndex > sent.frameIndex) {
            trackAssistMask(message, sent);
            return;
        }
        const code = maskTarget(sent, message, { frameIndex: session.frame.frameIndex, code: selected });
        if (!code) {
            return;
        }
        try {
            const { width, height } = session.frame;
            session.applyMask(code, unpackMask(message.mask, message.shape, width, height));
            // The answered points are done: the mask they made steers the next prompt.
            promptPoints.forget(sent.frameIndex, sent.code, sent.points);
            renderAssistPoints();
            refreshUndoRedoSoon();
        } catch (error) {
            say(error.message, true);
        }
    }

    function renderAssistPoints() {
        if (!assistOverlay) {
            return;
        }
        assistOverlay.replaceChildren();
        if (toolId !== ASSIST_TOOL || !session || !selected) {
            return;
        }
        for (const point of promptPoints.for(session.frame.frameIndex, selected)) {
            const [cx, cy] = session.canvasPointOf([point.x, point.y]);
            const dot = doc.createElementNS(SVG_NS, 'circle');
            dot.setAttribute('cx', String(cx));
            dot.setAttribute('cy', String(cy));
            dot.setAttribute('r', '5');
            dot.setAttribute('class', point.label ? 'is-add' : 'is-remove');
            assistOverlay.appendChild(dot);
        }
    }

    /**
     * A mask tracked onto a later frame: queued on the autosaver (the one writer, so no
     * revision conflicts with brush strokes), and only where the label is still empty there.
     */
    function trackAssistMask(message, sent) {
        if (!saver) {
            return;
        }
        const timeMs = frameTimeMs(message.frameIndex, data.fps);
        if (!labelIsEmptyOn(state?.sources?.[String(data.fileId)]?.frames, timeMs, sent.code)) {
            return;
        }
        let plane;
        try {
            plane = unpackMask(message.mask, message.shape, data.width, data.height);
        } catch (error) {
            say(error.message, true);
            return;
        }
        if (!hasPixels(plane)) {
            return; // tracking lost the object on this frame
        }
        tracked.set(String(timeMs), { code: sent.code, plane });
        saver.markDirty(timeMs);
    }

    /** A tracked frame as the save endpoint wants it: what is stored there, plus the tracked plane. */
    async function trackedFrame(timeMs, { code, plane }) {
        const stored = {};
        if (String(timeMs) in (state.sources?.[String(data.fileId)]?.frames ?? {})) {
            const saved = await api.frame(data.fileId, timeMs);
            for (const [savedCode, encoded] of Object.entries(saved.planes)) {
                stored[savedCode] = await decodePlane(encoded, saved.width, saved.height);
            }
        }
        const planes = {};
        for (const [planeCode, merged] of Object.entries(mergeTrackedPlane(stored, code, plane))) {
            planes[planeCode] = await encodePlane(merged);
        }
        return { timeMs, width: data.width, height: data.height, planes };
    }

    function windowFrames() {
        const value = Number(assistWindow?.value ?? 0);
        return Number.isInteger(value) ? Math.max(0, Math.min(50, value)) : 0;
    }

    /** Built when the channel is free: the points still unanswered, the mask as it is now. */
    function buildAssistPrompt() {
        if (!session || !selected) {
            return null;
        }
        const { frameIndex } = session.frame;
        const points = [...promptPoints.for(frameIndex, selected)];
        if (!points.length) {
            return null;
        }
        const plane = session.labels.readPlane(selected);
        return {
            payload: {
                frameIndex,
                window: windowFrames(),
                points: points.map((point) => [point.x, point.y]),
                labels: points.map((point) => point.label),
                ...(hasPixels(plane) ? { mask: packMask(plane) } : {}),
            },
            context: { frameIndex, code: selected, points },
        };
    }

    function formatTime(seconds) {
        const wholeSeconds = Math.max(0, Math.floor(seconds || 0));
        return `${Math.floor(wholeSeconds / 60)}:${String(wholeSeconds % 60).padStart(2, '0')}`;
    }

    function timelineDuration() {
        if (session) {
            return data.frameCount / data.fps;
        }
        return Number.isFinite(video.duration) ? video.duration : 0;
    }

    function updateTimeline(time = session ? session.frame.timeMs / 1000 : video.currentTime) {
        const duration = timelineDuration();
        const position = Math.max(0, Math.min(Number.isFinite(time) ? time : 0, duration));
        if (timeline) {
            timeline.max = String(duration);
            timeline.value = String(position);
        }
        if (timelineTime) {
            timelineTime.textContent = `${formatTime(position)} / ${formatTime(duration)}`;
        }
        quadrants?.showNow();
    }

    function setTimelineBusy(isBusy) {
        if (timeline) {
            timeline.disabled = isBusy;
        }
        for (const control of timelineControls) {
            control.disabled = isBusy;
        }
    }

    function setPlaybackControlsDisabled(disabled) {
        if (startButton) {
            startButton.disabled = disabled;
        }
        if (stopButton) {
            stopButton.disabled = disabled;
        }
    }

    function updateFrameInfo(frame) {
        if (frameInfo) {
            frameInfo.textContent = `Frame ${frame.frameIndex} · ${(frame.timeMs / 1000).toFixed(3)} s`;
        }
        updateTimeline(frame.timeMs / 1000);
    }

    function describeStatus() {
        if (statusLine && saver) {
            statusLine.textContent = describeSave(saver.status, {
                revision: saver.revision,
                pending: saver.pending,
            });
        }
    }


    function refreshPanel() {
        if (!labelContainer || !state) {
            return;
        }
        renderAssistPoints();
        renderLabelPanel({
            container: labelContainer,
            labels: state.labels,
            view: {
                selected,
                hidden,
                opacity: (code) => session?.opacityOf(code) ?? 0.5,
                canManage: Boolean(data.canManageLabels),
                // Visibility and opacity act on the open frame; with no session there is none.
                showDisplay: Boolean(session),
            },
            handlers: {
                select(code) {
                    selected = code;
                    session?.selectLabel(code);
                    refreshPanel();
                },
                toggleVisible(code) {
                    if (hidden.has(code)) {
                        hidden.delete(code);
                    } else {
                        hidden.add(code);
                    }
                    session?.setVisible(code, !hidden.has(code));
                    refreshPanel();
                },
                opacity: (code, alpha) => session?.setOpacity(code, alpha),
                async color(code, hex) {
                    session?.setColor(code, hex);
                    try {
                        const { label } = await api.updateLabel(code, { color: hex });
                        replaceLabel(label);
                    } catch (error) {
                        say(error.message, true);
                    }
                },
                async rename(code) {
                    const current = state.labels.find((label) => label.code === code);
                    const name = await ask?.({
                        title: 'Rename label',
                        message: 'What should this label be called?',
                        initial: current?.name ?? '',
                    });
                    if (!name || name === current?.name) {
                        return;
                    }
                    try {
                        const { label } = await api.updateLabel(code, { name });
                        replaceLabel(label);
                        refreshPanel();
                    } catch (error) {
                        say(error.message, true);
                    }
                },
                async retire(code) {
                    try {
                        const { label } = await api.retireLabel(code);
                        replaceLabel(label);
                        // Hidden, not removed: its pixels stay in the frame and in every
                        // revision that has them. See `session.js`.
                        session?.setVisible(code, false);
                        if (selected === code) {
                            selected = labelAfterRemoval(state.labels, code);
                            if (selected) {
                                session?.selectLabel(selected);
                            }
                        }
                        refreshPanel();
                    } catch (error) {
                        say(error.message, true);
                    }
                },
                async add() {
                    const name = await ask?.({
                        title: 'New label',
                        message: 'What should this label be called?',
                        placeholder: 'e.g. Gallbladder',
                    });
                    if (!name) {
                        return;
                    }
                    try {
                        const { label } = await api.createLabel(name);
                        state.labels.push(label);
                        await session?.addLabel(label);
                        selected = label.code;
                        session?.selectLabel(label.code);
                        refreshPanel();
                    } catch (error) {
                        say(error.message, true);
                    }
                },
            },
        });
    }

    function replaceLabel(label) {
        const index = state.labels.findIndex((candidate) => candidate.code === label.code);
        if (index >= 0) {
            state.labels[index] = label;
        }
    }

    async function send(keys, expectedRevision) {
        // The open frame (one session = one frame) and any frames Assist tracked onto.
        const frames = [];
        const open = session ? String(session.frame.timeMs) : null;
        if (open && keys.includes(open)) {
            frames.push(await session.collect());
        }
        const sentTracked = [];
        for (const key of keys) {
            const entry = tracked.get(key);
            if (entry && key !== open) {
                frames.push(await trackedFrame(Number(key), entry));
                sentTracked.push([key, entry]);
            }
        }
        if (!frames.length) {
            return { revision: expectedRevision };
        }
        const result = await api.save(
            buildSaveBody({
                fileId: data.fileId,
                revision: expectedRevision,
                fps: data.fps,
                frames,
            })
        );
        // A frame may be revisited before Annotation Mode closes, so keep the manifest used
        // by openSession current rather than treating this freshly saved frame as empty.
        state.sources = result.sources ?? state.sources;
        for (const [key, entry] of sentTracked) {
            if (tracked.get(key) === entry) {
                tracked.delete(key);
            }
        }
        return { revision: result.revision };
    }

    async function openFrame(frame) {
        session = await openSession({
            element: viewportElement,
            frame,
            labels: state.labels,
            api,
            fileId: data.fileId,
            storedFrames: state.sources?.[String(data.fileId)]?.frames ?? {},
            onEdit: (timeMs) => {
                if (data.canModify && !editQueued) {
                    // A drag fires this per mouse move; once per frame is enough.
                    editQueued = true;
                    (doc.defaultView || globalThis).requestAnimationFrame(() => {
                        editQueued = false;
                        saver?.markDirty(timeMs);
                        refreshUndoRedo();
                        refreshUndoRedoSoon();
                    });
                }
            },
        });
        const sizeInput = $(doc, 'segment-brush-size');
        if (sizeInput) {
            // Each frame is a new session; carry the annotator's tool and size over to it.
            sizeInput.max = String(session.brushSize.max);
            if (!sizeTouched) {
                sizeInput.value = String(session.brushSize.initial);
            }
            session.setBrushSize(Number(sizeInput.value));
        }
        session.setTool(toolId);
        updateFrameInfo(frame);
        session.resize();
        if (selected) {
            session.selectLabel(selected);
        }
        refreshPanel();
        refreshUndoRedo();
        // One second either side covers the usual navigation buttons. This stays bounded in
        // frameSource, and failures are non-fatal because the frame on screen is ready.
        void frameSource.prefetch([frame.frameIndex - 1, frame.frameIndex + 1]).catch(() => {});
    }

    async function selectAnnotationFrame(frame) {
        if (navigating || !session || frame.frameIndex === session.frame.frameIndex) {
            return;
        }
        navigating = true;
        setTimelineBusy(true);
        say('Loading frame...');
        try {
            const status = await saver.flush();
            if (status === SaveStatus.CONFLICT || status === SaveStatus.ERROR) {
                say(describeSave(status), true);
                return;
            }
            session.destroy();
            session = null;
            const nextFrame = await frameSource.grab(frame.frameIndex);
            await openFrame(nextFrame);
            say('');
        } catch (error) {
            report(error.stack ?? error.message);
            say(error.message, true);
        } finally {
            navigating = false;
            setTimelineBusy(false);
        }
    }

    function seekPlayback(time) {
        const duration = timelineDuration();
        if (!duration) {
            return;
        }
        video.currentTime = Math.max(0, Math.min(time, duration));
        updateTimeline(video.currentTime);
    }

    async function enter() {
        toggle.disabled = true;
        setTimelineBusy(true);
        setPlaybackControlsDisabled(true);
        say('');
        try {
            video.pause?.();
            // Read only after pausing: while playback is advancing, a time from before pause
            // can map to a different sampled frame than the picture the user just saw.
            const playbackTime = video.currentTime;
            const current = frameSource.nearestFrame(playbackTime);
            const frame = await frameSource.grab(current.frameIndex);
            state = await api.state();
            // Painting needs a label, and "no labels yet" under the image was easy to miss,
            // so a project's first annotation session starts with one -- when the user is
            // allowed to add labels at all (project admins).
            if (data.canManageLabels && !state.labels.some((label) => label.active)) {
                const { label } = await api.createLabel(FIRST_LABEL_NAME);
                state.labels.push(label);
            }
            selected = state.labels.find((label) => label.active)?.code ?? null;

            saver = createSaver({
                send,
                revision: state.revision,
                onStatus: describeStatus,
            });
            // Cornerstone sizes the viewport during openSession. It must be visible first:
            // creating it under `display: none` gives it a zero-sized backing canvas and a
            // visibly pixelated first render when the workspace is revealed afterwards.
            video.classList.add('d-none');
            workspace.classList.remove('d-none');
            await openFrame(frame);
            toggle.textContent = 'Exit Annotation Mode';
            toggle.dataset.active = 'true';
            describeStatus();
        } catch (error) {
            report(error.stack ?? error.message);
            say(error.message, true);
            await leave({ skipSave: true });
        } finally {
            toggle.disabled = false;
            setTimelineBusy(false);
        }
    }

    async function leave({ skipSave = false } = {}) {
        if (saver && !skipSave && data.canModify) {
            const status = await saver.flush();
            if (status === SaveStatus.CONFLICT || status === SaveStatus.ERROR) {
                say(describeSave(status), true);
                // Stay: leaving now would discard work the server never received.
                return false;
            }
        }
        saver?.destroy();
        saver = null;
        const playbackTime = session?.frame.timeMs / 1000;
        session?.destroy();
        session = null;
        workspace.classList.add('d-none');
        video.classList.remove('d-none');
        refreshPanel();
        setPlaybackControlsDisabled(false);
        if (Number.isFinite(playbackTime)) {
            seekPlayback(playbackTime);
        } else {
            updateTimeline();
        }
        toggle.textContent = 'Annotation Mode';
        delete toggle.dataset.active;
        return true;
    }

    function refreshUndoRedo() {
        const undo = $(doc, 'segment-undo');
        const redo = $(doc, 'segment-redo');
        if (undo) {
            undo.disabled = !session?.canUndo;
        }
        if (redo) {
            redo.disabled = !session?.canRedo;
        }
    }

    /**
     * Cornerstone announces a modified segmentation *before* it records the stroke in the
     * undo history, so on the very first stroke `canUndo` is still false when `onEdit` runs
     * (later strokes only looked right because the history was already non-empty). Look
     * again once the stroke has been committed.
     */
    function refreshUndoRedoSoon() {
        const view = doc.defaultView || globalThis;
        view.requestAnimationFrame(() => {
            refreshUndoRedo();
            view.setTimeout(refreshUndoRedo, 100);
        });
    }
    viewportElement.addEventListener('pointerup', refreshUndoRedoSoon);
    viewportElement.addEventListener('mouseup', refreshUndoRedoSoon);

    toggle.addEventListener('click', () => {
        void (session ? leave() : enter());
    });

    timelineControls.forEach((control) => {
        control.addEventListener('click', () => {
            const seconds = Number(control.dataset.videoSeek);
            const time = (session ? session.frame.timeMs / 1000 : video.currentTime) + seconds;
            if (session) {
                void selectAnnotationFrame(frameSource.nearestFrame(time));
            } else {
                seekPlayback(time);
            }
        });
    });
    startButton?.addEventListener('click', () => {
        void video.play().catch((error) => say(error.message, true));
    });
    stopButton?.addEventListener('click', () => video.pause());
    doc.addEventListener('keydown', (event) => {
        if (!session || navigating || (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight')) {
            return;
        }
        // Keep native keyboard behaviour for focused inputs, including the timeline slider.
        if (event.target?.closest?.('input, textarea, select, [contenteditable="true"]')) {
            return;
        }
        event.preventDefault();
        const offset = event.key === 'ArrowLeft' ? -1 : 1;
        void selectAnnotationFrame({
            frameIndex: Math.max(0, Math.min(data.frameCount - 1, session.frame.frameIndex + offset)),
        });
    });
    timeline?.addEventListener('input', () => {
        // Scrubbing previews the timestamp immediately, but does not start an expensive frame
        // load until the pointer is released (`change` below).
        updateTimeline(Number(timeline.value));
    });
    timeline?.addEventListener('change', () => {
        const time = Number(timeline.value);
        if (session) {
            void selectAnnotationFrame(frameSource.nearestFrame(time));
        } else {
            seekPlayback(time);
        }
    });
    video.addEventListener('loadedmetadata', () => updateTimeline());
    video.addEventListener('timeupdate', () => {
        if (!session) {
            updateTimeline();
        }
    });
    updateTimeline();
    // The labels are managed from the Labels card at any time, so the vocabulary is read on
    // load rather than when Annotation Mode opens.
    void api.state().then(
        (current) => {
            state = current;
            refreshPanel();
        },
        () => {}
    );
    for (const tab of doc.querySelectorAll('[data-labels-tab]')) {
        tab.addEventListener('click', () => {
            for (const other of doc.querySelectorAll('[data-labels-tab]')) {
                const on = other === tab;
                other.classList.toggle('is-active', on);
                other.setAttribute('aria-selected', String(on));
                $(doc, `labels-pane-${other.dataset.labelsTab}`).hidden = !on;
            }
        });
    }
    void mountQuadrants({
        doc,
        api: createQuadrantsApi({
            endpoint: quadrantsEndpointFor(data),
            fetchImpl,
            csrf: csrfToken(doc),
        }),
        fileId: data.fileId,
        canModify: Boolean(data.canModify),
        canManage: Boolean(data.canManageLabels),
        getDurationMs: () => timelineDuration() * 1000,
        getTimeMs: () => (session ? session.frame.timeMs : video.currentTime * 1000),
        ask,
        say,
        stepMs: 1000 / data.fps,
        // While a needle is dragged the video follows it; in Annotation Mode a frame load is
        // too heavy for that, so the frame changes once, when the needle settles.
        seek: (ms, { final }) => {
            if (!session) {
                // Let a seek in flight finish rather than restarting it on every move; the
                // final one always goes through.
                if (final || !video.seeking) {
                    seekPlayback(ms / 1000);
                }
            } else if (final) {
                void selectAnnotationFrame(frameSource.nearestFrame(ms / 1000));
            }
        },
    }).then((mounted) => {
        quadrants = mounted;
    });

    // Toolbar --------------------------------------------------------------------
    for (const tool of [...SEGMENT_TOOLS, { id: ASSIST_TOOL }]) {
        const button = doc.querySelector(`[data-segment-tool="${tool.id}"]`);
        button?.addEventListener('click', () => {
            toolId = tool.id;
            session?.setTool(tool.id);
            for (const other of doc.querySelectorAll('[data-segment-tool]')) {
                other.classList.toggle('active', other === button);
            }
            const sizeRow = $(doc, 'segment-size-row');
            sizeRow?.classList.toggle('d-none', !tool.hasSize);
            renderAssistPoints();
        });
    }
    $(doc, 'segment-brush-size')?.addEventListener('input', (event) => {
        sizeTouched = true;
        session?.setBrushSize(Number(event.target.value));
    });
    $(doc, 'segment-zoom-out')?.addEventListener('click', () => session?.zoomOut());
    $(doc, 'segment-zoom-in')?.addEventListener('click', () => session?.zoomIn());
    $(doc, 'segment-reset-view')?.addEventListener('click', () => session?.resetView());
    $(doc, 'segment-undo')?.addEventListener('click', () => {
        session?.undo();
        refreshUndoRedo();
    });
    $(doc, 'segment-redo')?.addEventListener('click', () => {
        session?.redo();
        refreshUndoRedo();
    });
    // Ctrl+Z undoes, Ctrl+Y or Ctrl+Shift+Z redoes (Cmd on macOS). Through the buttons, so a
    // disabled one does nothing; a text field keeps its own undo.
    doc.addEventListener('keydown', (event) => {
        if (!session || !(event.ctrlKey || event.metaKey) || event.altKey) {
            return;
        }
        if (event.target?.closest?.('textarea, select, [contenteditable="true"], input:not([type="range"])')) {
            return;
        }
        const key = event.key.toLowerCase();
        const redo = key === 'y' || (key === 'z' && event.shiftKey);
        if (key !== 'z' && !redo) {
            return;
        }
        event.preventDefault();
        $(doc, redo ? 'segment-redo' : 'segment-undo')?.click();
    });

    // Leaving the page with unsaved work: the browser's own prompt, and a best-effort save.
    doc.defaultView?.addEventListener('beforeunload', (event) => {
        if (saver && saver.pending > 0) {
            event.preventDefault();
            event.returnValue = '';
        }
    });

    viewportElement.addEventListener('click', (event) => {
        if (!assist || toolId !== ASSIST_TOOL || !session || !selected || event.button !== 0) {
            return;
        }
        const { frameIndex, width, height } = session.frame;
        const [x, y] = session.imagePointAt(event.clientX, event.clientY);
        if (!(x >= 0 && y >= 0 && x < width && y < height)) {
            return;
        }
        promptPoints.add(frameIndex, selected, x, y, includeNext ? 1 : 0);
        renderAssistPoints();
        assist.request(buildAssistPrompt);
    });
    // coreEnums.Events.CAMERA_MODIFIED: zoom and pan move the points with the frame.
    viewportElement.addEventListener('CORNERSTONE_CAMERA_MODIFIED', () => renderAssistPoints());
    for (const button of assistModes) {
        button.addEventListener('click', () => {
            includeNext = button.dataset.assistMode === 'add';
            for (const other of assistModes) {
                other.setAttribute('aria-checked', String(other === button));
            }
        });
    }

    return { enter, leave, get session() { return session; } };
}
