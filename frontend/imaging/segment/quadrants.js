/**
 * The quadrant timeline: a labelled bar of coloured spans with the playhead on it, and a
 * *needle* at every marker. A needle is dragged (or nudged a frame at a time with the arrow
 * keys) to place a marker precisely, and right-clicking the bar or a needle opens a menu of
 * actions. The quadrants themselves are managed in the Labels card, with the same list the
 * segmentation labels use.
 *
 * A marker is the instant a quadrant *starts*; it runs until the next marker or the end of
 * the video. The spans are derived here, never stored, so editing a marker cannot require
 * rewriting its neighbour. No Cornerstone: this is the page's own DOM and the quadrant API.
 */

import { renderLabelPanel } from './labelPanel.js';

const FALLBACK_COLOR = '#6c757d';

/**
 * @param {Array<{timeMs: number, code: string}>} markers any order.
 * @param {number} durationMs
 * @param {Map<string, {name: string, color: string}>} labels by code.
 * @returns {Array<{startMs, endMs, code, name, color}>}
 */
export function markerSegments(markers, durationMs, labels) {
    if (!Number.isFinite(durationMs) || durationMs <= 0) {
        return [];
    }
    const sorted = [...(markers ?? [])]
        .filter((marker) => Number.isFinite(marker?.timeMs))
        .sort((a, b) => a.timeMs - b.timeMs);
    return sorted.map((marker, index) => {
        const label = labels.get(marker.code);
        return {
            startMs: Math.max(0, Math.min(marker.timeMs, durationMs)),
            endMs: index + 1 < sorted.length ? Math.min(sorted[index + 1].timeMs, durationMs) : durationMs,
            code: marker.code,
            name: label?.name ?? '—',
            color: label?.color || FALLBACK_COLOR,
        };
    });
}

/** The segment in force at an instant, or `null` before the first marker. */
export function quadrantAt(segments, timeMs) {
    let found = null;
    for (const segment of segments) {
        if (timeMs >= segment.startMs) {
            found = segment;
        }
    }
    return found;
}

/** `markers` with one at `timeMs` set to `code`, replacing any marker already on that instant. */
export function withMarker(markers, timeMs, code) {
    return [...markers.filter((marker) => marker.timeMs !== timeMs), { timeMs, code }].sort(
        (a, b) => a.timeMs - b.timeMs
    );
}

export function withoutMarker(markers, timeMs) {
    return markers.filter((marker) => marker.timeMs !== timeMs);
}

/** A marker is redundant when the quadrant already in force at that instant is the same. */
export function isRedundant(markers, timeMs, code) {
    return quadrantAt(
        markers.filter((m) => m.timeMs <= timeMs).map((m) => ({ startMs: m.timeMs, code: m.code })),
        timeMs
    )?.code === code;
}

/** Move the marker at `fromMs` to `toMs`. `null` when another marker already holds `toMs`. */
export function moveMarker(markers, fromMs, toMs) {
    if (fromMs === toMs) {
        return markers;
    }
    if (markers.some((marker) => marker.timeMs === toMs)) {
        return null;
    }
    return markers
        .map((marker) => (marker.timeMs === fromMs ? { ...marker, timeMs: toMs } : marker))
        .sort((a, b) => a.timeMs - b.timeMs);
}

/** Give the marker at `timeMs` another quadrant. */
export function retypeMarker(markers, timeMs, code) {
    return markers.map((marker) => (marker.timeMs === timeMs ? { ...marker, code } : marker));
}

/**
 * The instant under a pointer on the bar, snapped to the frame grid when `stepMs` is known.
 * @param {number} clientX
 * @param {{left: number, width: number}} rect the bar's box.
 */
export function msFromPointer(clientX, rect, durationMs, stepMs = 0) {
    if (!(rect.width > 0) || !(durationMs > 0)) {
        return 0;
    }
    const fraction = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
    let ms = fraction * durationMs;
    if (stepMs > 0) {
        ms = Math.round(ms / stepMs) * stepMs;
    }
    return Math.round(Math.min(durationMs, Math.max(0, ms)));
}

function element(doc, tag, props = {}) {
    const node = doc.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
        if (key === 'class') {
            node.className = value;
        } else if (key === 'text') {
            node.textContent = value;
        } else if (key.startsWith('on')) {
            node.addEventListener(key.slice(2), value);
        } else {
            node.setAttribute(key, value);
        }
    }
    return node;
}

/**
 * @param {object} options
 * @param {Document} options.doc
 * @param {object} options.api `createQuadrantsApi(...)`.
 * @param {number} options.fileId the video the markers are filed against.
 * @param {boolean} options.canModify
 * @param {() => number} options.getDurationMs
 * @param {() => number} options.getTimeMs the playhead, in ms.
 * @param {Function} [options.ask] themed text prompt.
 * @param {(text: string, isError?: boolean) => void} [options.say]
 * @param {(ms: number, info: {final: boolean}) => void} [options.seek] move the video; called
 *   while a needle is dragged (`final: false`) and once more when it settles (`final: true`).
 * @param {number} [options.stepMs] one frame, for snapping and for the arrow keys.
 */
export async function mountQuadrants({
    doc,
    api,
    fileId,
    canModify,
    getDurationMs,
    getTimeMs,
    ask,
    say = () => {},
    seek = () => {},
    stepMs = 0,
}) {
    const lane = doc.getElementById('video-quadrants-lane');
    const bar = doc.getElementById('quadrant-bar');
    const panel = doc.getElementById('quadrant-timeline');
    const labelContainer = doc.getElementById('quadrant-labels');
    if (!lane || !panel || !bar) {
        return null;
    }
    let state = { revision: 0, labels: [], markers: [] };
    let active = null;
    /** The needle in focus (a marker's `timeMs`), and an in-flight drag of one. */
    let selectedMs = null;
    let drag = null;

    const labelMap = () => new Map(state.labels.map((label) => [label.code, label]));
    const find = (code) => state.labels.find((label) => label.code === code);

    /**
     * Show `markers` at once and save them in the background. The bar must not wait for the
     * round trip (a needle that snaps back and then forward again is what made dragging feel
     * broken); if the server refuses, the previous markers come back with the reason.
     */
    async function persist(markers) {
        const previous = state.markers;
        state = { ...state, markers };
        render();
        try {
            state = { ...state, ...(await api.save({ fileId, expectedRevision: state.revision, markers })) };
            say('');
        } catch (error) {
            state = { ...state, markers: previous };
            say(error.conflict ? 'Someone else changed the quadrants. Reload the page.' : error.message, true);
        }
        render();
    }

    function replaceLabel(label) {
        const index = state.labels.findIndex((candidate) => candidate.code === label.code);
        state.labels[index >= 0 ? index : state.labels.length] = label;
        if (!label.active && active === label.code) {
            active = state.labels.find((candidate) => candidate.active)?.code ?? null;
        }
        render();
    }

    const attempt = async (work) => {
        try {
            await work();
        } catch (error) {
            say(error.message, true);
        }
    };

    function renderLabels() {
        if (!labelContainer) {
            return;
        }
        renderLabelPanel({
            container: labelContainer,
            labels: state.labels,
            view: {
                selected: active,
                hidden: new Set(),
                opacity: () => 1,
                canEdit: canModify,
                noun: 'quadrant',
                showDisplay: false,
                selectTitle: 'Use this quadrant for "Mark here"',
            },
            handlers: {
                select(code) {
                    active = code;
                    render();
                },
                color: (code, hex) =>
                    attempt(async () => replaceLabel((await api.updateLabel(code, { color: hex })).label)),
                rename: (code) =>
                    attempt(async () => {
                        const name = await ask?.({
                            title: 'Rename quadrant',
                            message: 'What should this quadrant be called?',
                            initial: find(code)?.name ?? '',
                        });
                        if (name && name !== find(code)?.name) {
                            replaceLabel((await api.updateLabel(code, { name })).label);
                        }
                    }),
                retire: (code) => attempt(async () => replaceLabel((await api.retireLabel(code)).label)),
                add: () =>
                    attempt(async () => {
                        const name = await ask?.({
                            title: 'New quadrant',
                            message: 'What should this quadrant be called?',
                            placeholder: 'e.g. Upper left',
                        });
                        if (name) {
                            const { label } = await api.createLabel(name);
                            active = label.code;
                            replaceLabel(label);
                        }
                    }),
            },
        });
    }

    /** The cheap part, safe to call on every `timeupdate`. */
    function showNow() {
        const duration = getDurationMs();
        const segments = markerSegments(state.markers, duration, labelMap());
        const timeMs = getTimeMs();
        const current = quadrantAt(segments, timeMs);
        const now = panel.querySelector('[data-quadrant-now]');
        if (now) {
            now.textContent = current ? current.name : 'None marked at this point';
            now.style.borderLeftColor = current?.color ?? 'transparent';
        }
        const playhead = panel.querySelector('[data-quadrant-playhead]');
        if (playhead) {
            playhead.style.left = `${duration > 0 ? Math.min(100, Math.max(0, (timeMs / duration) * 100)) : 0}%`;
        }
    }

    /** Markers as the bar shows them: the dragged one is already where the pointer is. */
    function shownMarkers() {
        return drag && drag.moved ? moveMarker(state.markers, drag.fromMs, drag.toMs) ?? state.markers : state.markers;
    }

    function renderSpans(markers) {
        const duration = getDurationMs();
        lane.replaceChildren();
        for (const segment of markerSegments(markers, duration, labelMap())) {
            const span = element(doc, 'span', { class: 'quadrant-timeline__span', title: segment.name });
            span.style.left = `${(segment.startMs / duration) * 100}%`;
            span.style.width = `${((segment.endMs - segment.startMs) / duration) * 100}%`;
            span.style.background = segment.color;
            lane.appendChild(span);
        }
    }

    function renderBar() {
        const duration = getDurationMs();
        const labels = labelMap();
        const markers = shownMarkers();

        renderSpans(markers);
        for (const old of bar.querySelectorAll('.quadrant-needle')) {
            old.remove();
        }
        for (const marker of markers) {
            const label = labels.get(marker.code);
            const seconds = (marker.timeMs / 1000).toFixed(2);
            const dragged = drag?.moved && marker.timeMs === drag.toMs;
            const needle = element(doc, 'button', {
                type: 'button',
                class: `quadrant-needle${marker.timeMs === selectedMs || dragged ? ' is-selected' : ''}`,
                'data-needle-ms': String(marker.timeMs),
                title: `${label?.name ?? '—'} from ${seconds} s${
                    canModify ? ' — drag, use ←/→, or right-click' : ''
                }`,
                'aria-label': `${label?.name ?? '—'} starts at ${seconds} seconds`,
            });
            needle.style.left = `${(marker.timeMs / duration) * 100}%`;
            needle.style.setProperty('--needle-color', label?.color || FALLBACK_COLOR);
            bar.appendChild(needle);
        }
        showNow();

        const pins = panel.querySelector('[data-quadrant-pins]');
        pins.replaceChildren();
        for (const marker of state.markers) {
            const label = labels.get(marker.code);
            const pin = element(doc, 'button', {
                type: 'button',
                class: 'quadrant-pin',
                title: canModify ? 'Remove this marker' : '',
                text: `${(marker.timeMs / 1000).toFixed(1)} s · ${label?.name ?? '—'}`,
                ...(canModify ? {} : { disabled: 'disabled' }),
                onclick: () => persist(withoutMarker(state.markers, marker.timeMs)),
            });
            pin.style.borderColor = label?.color || FALLBACK_COLOR;
            pins.appendChild(pin);
        }
    }

    function render() {
        const picker = panel.querySelector('[data-quadrant-select]');
        if (picker) {
            picker.replaceChildren(
                ...state.labels
                    .filter((label) => label.active)
                    .map((label) => {
                        const option = element(doc, 'option', { value: label.code, text: label.name });
                        option.selected = label.code === active;
                        return option;
                    })
            );
            picker.disabled = !picker.options.length;
        }
        const mark = panel.querySelector('[data-quadrant-mark]');
        if (mark) {
            mark.disabled = !active;
            mark.querySelector('span').textContent = active
                ? `Mark “${find(active)?.name}” from here`
                : 'Add a quadrant in Labels';
        }
        renderBar();
        renderLabels();
    }

    // Needles -------------------------------------------------------------------------

    const clampMs = (ms) => Math.max(0, Math.min(Math.round(ms), Math.floor(getDurationMs())));
    const pointerMs = (event) =>
        msFromPointer(event.clientX, bar.getBoundingClientRect(), getDurationMs(), stepMs);

    async function relocate(fromMs, toMs) {
        const target = clampMs(toMs);
        const next = moveMarker(state.markers, fromMs, target);
        if (!next) {
            say('Another marker is already at that instant.', true);
            renderBar();
            return false;
        }
        selectedMs = target;
        seek(target, { final: true });
        if (next !== state.markers) {
            await persist(next);
        }
        return true;
    }

    /**
     * While a needle is dragged, the *same* element is moved and only the coloured spans are
     * redrawn, once per animation frame -- rebuilding every needle on each pointer event (and
     * seeking the video on each one) is what made the drag stutter.
     */
    let dragFrame = null;
    function scheduleDragFrame() {
        if (dragFrame !== null) {
            return;
        }
        const run = () => {
            dragFrame = null;
            if (!drag?.moved) {
                return;
            }
            const needle = bar.querySelector(`[data-needle-ms="${drag.fromMs}"]`);
            if (needle) {
                needle.style.left = `${(drag.toMs / getDurationMs()) * 100}%`;
                needle.classList.add('is-selected', 'is-dragging');
            }
            renderSpans(shownMarkers());
            seek(drag.toMs, { final: false });
        };
        const view = doc.defaultView;
        dragFrame = view?.requestAnimationFrame ? view.requestAnimationFrame(run) : (run(), null);
    }

    bar.addEventListener('pointerdown', (event) => {
        const needle = event.target.closest?.('[data-needle-ms]');
        if (!needle || event.button !== 0) {
            return;
        }
        const timeMs = Number(needle.dataset.needleMs);
        selectedMs = timeMs;
        if (!canModify) {
            seek(timeMs, { final: true });
            renderBar();
            return;
        }
        drag = { fromMs: timeMs, toMs: timeMs, moved: false };
        bar.setPointerCapture?.(event.pointerId);
        event.preventDefault();
        needle.focus();
    });
    bar.addEventListener('pointermove', (event) => {
        if (!drag) {
            return;
        }
        const ms = pointerMs(event);
        if (ms !== drag.toMs) {
            drag.toMs = ms;
            drag.moved = true;
            scheduleDragFrame();
        }
    });
    bar.addEventListener('pointerup', async (event) => {
        if (!drag) {
            return;
        }
        const finished = drag;
        drag = null;
        if (dragFrame !== null) {
            doc.defaultView?.cancelAnimationFrame?.(dragFrame);
            dragFrame = null;
        }
        bar.releasePointerCapture?.(event.pointerId);
        if (!finished.moved) {
            seek(finished.fromMs, { final: true });
            renderBar();
            return;
        }
        await relocate(finished.fromMs, finished.toMs);
        bar.querySelector(`[data-needle-ms="${selectedMs}"]`)?.focus();
    });
    bar.addEventListener('pointercancel', () => {
        drag = null;
        if (dragFrame !== null) {
            doc.defaultView?.cancelAnimationFrame?.(dragFrame);
            dragFrame = null;
        }
        renderBar();
    });
    bar.addEventListener('keydown', async (event) => {
        const needle = event.target.closest?.('[data-needle-ms]');
        if (!needle) {
            return;
        }
        const timeMs = Number(needle.dataset.needleMs);
        const step = event.shiftKey ? 1000 : stepMs || 40;
        if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            selectedMs = timeMs;
            seek(timeMs, { final: true });
            renderBar();
        } else if (canModify && (event.key === 'ArrowLeft' || event.key === 'ArrowRight')) {
            event.preventDefault();
            event.stopPropagation(); // the page's own arrow-key frame stepping must not also fire
            if (await relocate(timeMs, timeMs + (event.key === 'ArrowLeft' ? -step : step))) {
                bar.querySelector(`[data-needle-ms="${selectedMs}"]`)?.focus();
            }
        } else if (canModify && (event.key === 'Delete' || event.key === 'Backspace')) {
            event.preventDefault();
            await persist(withoutMarker(state.markers, timeMs));
        }
    });

    // Right-click menu ----------------------------------------------------------------

    let menu = null;
    function closeMenu() {
        menu?.remove();
        menu = null;
    }

    function openMenu(x, y, items) {
        closeMenu();
        menu = element(doc, 'div', { class: 'quadrant-menu', role: 'menu' });
        for (const item of items) {
            if (item.heading) {
                menu.appendChild(element(doc, 'div', { class: 'quadrant-menu__heading', text: item.heading }));
                continue;
            }
            const row = element(doc, 'button', {
                type: 'button',
                role: 'menuitem',
                class: `quadrant-menu__item${item.danger ? ' is-danger' : ''}`,
                ...(item.disabled ? { disabled: 'disabled' } : {}),
                onclick: () => {
                    closeMenu();
                    void item.run();
                },
            });
            if (item.color) {
                const dot = element(doc, 'span', { class: 'quadrant-menu__dot' });
                dot.style.background = item.color;
                row.appendChild(dot);
            }
            row.appendChild(element(doc, 'span', { text: item.text }));
            menu.appendChild(row);
        }
        panel.appendChild(menu);
        // Keep it on screen: flip to the other side of the pointer when it would overflow.
        const view = doc.defaultView;
        const box = menu.getBoundingClientRect();
        menu.style.left = `${Math.max(0, Math.min(x, (view?.innerWidth ?? x + box.width) - box.width - 4))}px`;
        menu.style.top = `${Math.max(0, Math.min(y, (view?.innerHeight ?? y + box.height) - box.height - 4))}px`;
        menu.querySelector('button:not(:disabled)')?.focus();
    }

    const activeLabels = () => state.labels.filter((label) => label.active);

    bar.addEventListener('contextmenu', (event) => {
        event.preventDefault();
        const needle = event.target.closest?.('[data-needle-ms]');
        const items = [];
        if (needle) {
            const timeMs = Number(needle.dataset.needleMs);
            const marker = state.markers.find((candidate) => candidate.timeMs === timeMs);
            selectedMs = timeMs;
            renderBar();
            items.push({ text: 'Go to this marker', run: () => seek(timeMs, { final: true }) });
            if (canModify && marker) {
                items.push({
                    text: 'Move to the playhead',
                    run: () => relocate(timeMs, pointerFree(getTimeMs())),
                });
                items.push({ heading: 'Change quadrant' });
                for (const label of activeLabels()) {
                    items.push({
                        text: label.name,
                        color: label.color,
                        disabled: label.code === marker.code,
                        run: () => persist(retypeMarker(state.markers, timeMs, label.code)),
                    });
                }
                items.push({
                    text: 'Remove marker',
                    danger: true,
                    run: () => persist(withoutMarker(state.markers, timeMs)),
                });
            }
        } else {
            const timeMs = pointerMs(event);
            items.push({ text: `Go to ${(timeMs / 1000).toFixed(2)} s`, run: () => seek(timeMs, { final: true }) });
            if (canModify) {
                items.push({ heading: 'Start a quadrant here' });
                if (!activeLabels().length) {
                    items.push({ text: 'No quadrants yet — add one in Labels', disabled: true, run: () => {} });
                }
                for (const label of activeLabels()) {
                    items.push({
                        text: label.name,
                        color: label.color,
                        disabled: isRedundant(state.markers, timeMs, label.code),
                        run: async () => {
                            selectedMs = timeMs;
                            await persist(withMarker(state.markers, timeMs, label.code));
                        },
                    });
                }
            }
        }
        openMenu(event.clientX, event.clientY, items);
    });
    const pointerFree = (ms) => clampMs(stepMs > 0 ? Math.round(ms / stepMs) * stepMs : ms);
    doc.addEventListener('pointerdown', (event) => {
        if (menu && !menu.contains(event.target)) {
            closeMenu();
        }
    });
    doc.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') {
            closeMenu();
        }
    });
    doc.defaultView?.addEventListener('scroll', closeMenu, { passive: true });

    panel.querySelector('[data-quadrant-select]')?.addEventListener('change', (event) => {
        active = event.target.value || null;
        render();
    });
    panel.querySelector('[data-quadrant-mark]')?.addEventListener('click', () => {
        if (!active) {
            return;
        }
        const timeMs = Math.round(getTimeMs());
        if (isRedundant(state.markers, timeMs, active)) {
            say('That quadrant is already in force here.', true);
            return;
        }
        selectedMs = timeMs;
        void persist(withMarker(state.markers, timeMs, active)).then(() =>
            bar.querySelector(`[data-needle-ms="${timeMs}"]`)?.focus()
        );
    });

    try {
        state = await api.state();
        active = state.labels.find((label) => label.active)?.code ?? null;
    } catch (error) {
        say(error.message, true);
    }
    render();
    return { render, showNow };
}
