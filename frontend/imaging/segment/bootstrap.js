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
import { renderLabelPanel, describeSave, labelAfterRemoval, paintableLabels } from './labelPanel.js';
import { createSaver, SaveStatus } from './saver.js';
import { SEGMENT_TOOLS } from './tools.js';

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
    const timelineMarks = $(doc, 'video-timeline-marks');
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

    /**
     * Tick the timeline wherever a frame already has a mask, so "where have I worked" is
     * visible without stepping through 187 frames to find out.
     */
    function renderMarks(sources) {
        if (!timelineMarks) {
            return;
        }
        const frames = sources?.[String(data.fileId)]?.frames ?? {};
        const duration = data.frameCount / data.fps;
        timelineMarks.replaceChildren();
        let count = 0;
        for (const timeMs of Object.keys(frames)) {
            const seconds = Number(timeMs) / 1000;
            if (!Number.isFinite(seconds) || seconds < 0 || seconds > duration) {
                continue;
            }
            const tick = doc.createElement('span');
            tick.className = 'video-timeline__mark';
            tick.style.left = `${(seconds / duration) * 100}%`;
            timelineMarks.appendChild(tick);
            count += 1;
        }
        timelineMarks.title = count === 1 ? '1 annotated frame' : `${count} annotated frames`;
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

    function refreshPicker() {
        const picker = $(doc, 'segment-label-select');
        if (!picker || !state) {
            return;
        }
        picker.replaceChildren(
            ...paintableLabels(state.labels).map((label) => {
                const option = doc.createElement('option');
                option.value = label.code;
                option.textContent = label.name;
                option.selected = label.code === selected;
                return option;
            })
        );
        picker.disabled = !picker.options.length;
    }

    function refreshPanel() {
        if (!labelContainer || !state) {
            return;
        }
        refreshPicker();
        renderLabelPanel({
            container: labelContainer,
            labels: state.labels,
            view: {
                selected,
                hidden,
                opacity: (code) => session?.opacityOf(code) ?? 0.5,
                canEdit: Boolean(data.canModify),
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
        // Only the open frame can be dirty (one session = one frame), so the batch is it.
        if (!session || !keys.includes(String(session.frame.timeMs))) {
            return { revision: expectedRevision };
        }
        const frame = await session.collect();
        const result = await api.save(
            buildSaveBody({
                fileId: data.fileId,
                revision: expectedRevision,
                fps: data.fps,
                frames: [frame],
            })
        );
        // A frame may be revisited before Annotation Mode closes, so keep the manifest used
        // by openSession current rather than treating this freshly saved frame as empty.
        state.sources = result.sources ?? state.sources;
        renderMarks(state.sources);
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
            renderMarks(state.sources);
            // Painting needs a label, and "no labels yet" under the image was easy to miss,
            // so a project's first annotation session starts with one.
            if (data.canModify && !state.labels.some((label) => label.active)) {
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
    // The marks are useful before Annotation Mode is opened too; a failed read just leaves
    // the track plain.
    // The labels are managed from the Labels card at any time, so the vocabulary is read on
    // load rather than when Annotation Mode opens.
    void api.state().then(
        (current) => {
            state = current;
            renderMarks(current.sources);
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
    $(doc, 'segment-label-select')?.addEventListener('change', (event) => {
        selected = event.target.value || null;
        session?.selectLabel(selected);
        refreshPanel();
    });
    for (const tool of SEGMENT_TOOLS) {
        const button = doc.querySelector(`[data-segment-tool="${tool.id}"]`);
        button?.addEventListener('click', () => {
            toolId = tool.id;
            session?.setTool(tool.id);
            for (const other of doc.querySelectorAll('[data-segment-tool]')) {
                other.classList.toggle('active', other === button);
            }
            const sizeRow = $(doc, 'segment-size-row');
            sizeRow?.classList.toggle('d-none', !tool.hasSize);
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

    // Leaving the page with unsaved work: the browser's own prompt, and a best-effort save.
    doc.defaultView?.addEventListener('beforeunload', (event) => {
        if (saver && saver.pending > 0) {
            event.preventDefault();
            event.returnValue = '';
        }
    });

    return { enter, leave, get session() { return session; } };
}
