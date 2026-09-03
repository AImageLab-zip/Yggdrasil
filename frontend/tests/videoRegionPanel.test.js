/**
 * The region panel, the annotation list and the quadrant panel, as the page wires them.
 *
 * Three controls in this markup were authored and bound to nothing, which is the exact
 * failure `pageControls.js`'s own header refuses -- "a control that is present and inert
 * is worse than one that is absent":
 *
 *   - the region rows offered a name and no way to rename, hide or delete one, while the
 *     endpoints for all three had existed since Phase 10;
 *   - `#shapes-list-panel` was force-hidden and never populated;
 *   - `#quadrant-types-panel`, `#timeline-add-class-btn` and `#timeline-class-admin-list`
 *     appeared in **no** JavaScript file at all, so the panel kept `d-none` forever,
 *     `activeQuadrantId` could never leave null, and "Add Marker" could only refuse.
 *
 * The fake DOM here is deliberately small and shared: what is under test is which request
 * a click makes and what the list then says, not how a browser lays it out.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

/** An element good enough for the binder: classes, dataset, children, listeners. */
function element(tag = 'div') {
    const listeners = new Map();
    const node = {
        tag,
        style: { setProperty() {} },
        dataset: {},
        attributes: {},
        className: '',
        title: '',
        type: '',
        value: '',
        children: [],
        __classes: new Set(),
        get textContent() {
            return node.children.map((child) => child.textContent ?? child.text ?? '').join('');
        },
        set textContent(value) {
            node.children.length = 0;
            if (value) {
                node.children.push({ textContent: value });
            }
        },
        classList: {
            add: (name) => node.__classes.add(name),
            remove: (name) => node.__classes.delete(name),
            contains: (name) => node.__classes.has(name),
            // Matches the DOM: with no second argument it *flips*, and it returns
            // whether the class is now present. The binder reads that return value to
            // learn whether annotation mode just went on or off, so a fake that always
            // removed and returned `undefined` made every toggle look like "shown".
            toggle: (name, on) => {
                const next = on === undefined ? !node.__classes.has(name) : Boolean(on);
                if (next) {
                    node.__classes.add(name);
                } else {
                    node.__classes.delete(name);
                }
                return next;
            },
        },
        setAttribute(name, value) {
            node.attributes[name] = String(value);
        },
        getAttribute(name) {
            return node.attributes[name] ?? null;
        },
        appendChild(child) {
            node.children.push(child);
            child.parent = node;
            return child;
        },
        append(...children) {
            for (const child of children) {
                node.appendChild(child);
            }
        },
        replaceWith(next) {
            const at = node.parent?.children.indexOf(node) ?? -1;
            if (at >= 0) {
                node.parent.children[at] = next;
                next.parent = node.parent;
            }
        },
        remove() {},
        addEventListener: (type, handler) => listeners.set(type, handler),
        querySelectorAll: (selector) =>
            selector === '[data-tool]' ? node.children.filter((child) => child.dataset?.tool) : [],
        querySelector: (selector) =>
            selector === '[data-tool].active'
                ? node.children.find(
                      (child) => child.dataset?.tool && child.classList.contains('active')
                  ) ?? null
                : null,
        /** Walk up from this node for one carrying any of the named `data-` keys. */
        closest(selector) {
            const keys = selector
                .split(',')
                .map((part) => part.trim().replace(/^\[data-/, '').replace(/\]$/, ''))
                .map((name) => name.replace(/-([a-z])/g, (_m, c) => c.toUpperCase()));
            for (let current = node; current; current = current.parent) {
                if (keys.some((key) => current.dataset?.[key] !== undefined)) {
                    return current;
                }
            }
            return null;
        },
        /** Deliver an event as the browser would, from a descendant. */
        async fire(type, target) {
            await listeners.get(type)?.({ target: target ?? node });
        },
        has: (type) => listeners.has(type),
    };
    return node;
}

/**
 * The control inside one chip carrying a given `data-` key.
 *
 * By key rather than by child index: a region chip nests its actions inside a
 * `.ygg-type-chip__actions` group, so the eye is `children[1].children[0]` and an index
 * walk states the DOM's shape in every test that clicks anything.
 */
function control(chip, key) {
    const walk = (node) => {
        if (node.dataset?.[key] !== undefined) {
            return node;
        }
        for (const child of node.children ?? []) {
            const found = walk(child);
            if (found) {
                return found;
            }
        }
        return null;
    };
    return walk(chip);
}

/** Every `data-` key a chip's controls carry, outermost first. */
function controlKeys(chip) {
    const keys = [];
    const walk = (node) => {
        const own = Object.keys(node.dataset ?? {})[0];
        if (own !== undefined) {
            keys.push(own);
        }
        for (const child of node.children ?? []) {
            walk(child);
        }
    };
    walk(chip);
    return keys;
}

/** A document exposing exactly the ids the binder asks for. */
function panelDoc(ids, { prompts = [], confirm = true } = {}) {
    const nodes = new Map(ids.map((id) => [id, element()]));
    const asked = [];
    const doc = {
        getElementById: (id) => nodes.get(id) ?? null,
        createElement: (tag) => element(tag),
        createTextNode: (text) => ({ text, textContent: text }),
        addEventListener() {},
        defaultView: {
            addEventListener() {},
            removeEventListener() {},
            setTimeout() {},
            prompt: (question, value) => {
                asked.push(question);
                return prompts.length ? prompts.shift() : value;
            },
            confirm: () => confirm,
        },
    };
    return { doc, nodes, asked };
}

/** The ids every test here needs resolved, so the binder reaches the code under test. */
const PANEL_IDS = [
    'video-annotate-viewport',
    'annotation-toolbar',
    'annotation-toggle-btn',
    'region-types-panel',
    'region-list',
    'add-region-btn',
    'shapes-list-panel',
    'shapes-list',
    'shapes-filter-btn',
    'temporal-classification-bar',
    'timeline-add-pin-btn',
    'timeline-class-list',
    'quadrant-types-panel',
    'timeline-add-class-btn',
    'timeline-class-admin-list',
];

/** A mounted surface with two regions and one mask, and a recorder for its editor. */
function annotatingSurface({ regionTypes, annotations = [] } = {}) {
    const editorCalls = [];
    return {
        calls: editorCalls,
        surface: {
            editor: {
                region: regionTypes?.[0]?.name ?? null,
                selectRegion: (code) => editorCalls.push(['selectRegion', code]),
                setActiveTool: () => 'ok',
                setRegionVisible: (code, visible) =>
                    editorCalls.push(['setRegionVisible', code, visible]),
                setRegionsVisible: () => {},
                setRegionColor: (code, color) => editorCalls.push(['setRegionColor', code, color]),
                clearRegionAt: (code) => editorCalls.push(['clearRegionAt', code]) && true,
                moveRegionAt: (from, to) => {
                    editorCalls.push(['moveRegionAt', from, to]);
                    return true;
                },
                setBrushSize: () => true,
            },
            canAnnotate: true,
            reason: '',
            regionTypes: regionTypes ?? [],
            annotations: () => annotations,
            fps: 25,
            frameCount: 100,
            durationMs: 4000,
            timeMs: 0,
            dirty: false,
            patientId: 7,
            markDirty: () => editorCalls.push(['markDirty']),
            resize() {},
            goToInstant: async (timeMs) => {
                editorCalls.push(['goToInstant', timeMs]);
                return timeMs;
            },
            save: async () => ({ ok: true }),
            updateRegionType: (code, payload) => editorCalls.push(['updateRegionType', code, payload]),
            removeRegionType: (code) => editorCalls.push(['removeRegionType', code]),
            store: { annotatedTimes: () => [] },
        },
    };
}

/** A `fetch` that answers the endpoints this panel calls, recording every request. */
function recordingFetch(answers = {}) {
    const requests = [];
    const fetchImpl = async (url, options = {}) => {
        requests.push({ url, method: options.method ?? 'GET', body: options.body });
        const answer = Object.entries(answers).find(([pattern]) => url.includes(pattern));
        const payload = answer ? answer[1] : {};
        return {
            ok: payload.status ? payload.status < 400 : true,
            status: payload.status ?? 200,
            json: async () => payload.body ?? {},
        };
    };
    return { fetchImpl, requests };
}

const REGIONS = [
    { id: 1, name: 'Liver', color: '#3498db' },
    { id: 2, name: 'Fat', color: '#e74c3c' },
];

test('each region row offers rename, hide and delete', async () => {
    const { bindVideoControls } = await import('../imaging/video/pageControls.js');
    const { doc, nodes } = panelDoc(PANEL_IDS);
    const { surface } = annotatingSurface({ regionTypes: REGIONS });

    bindVideoControls({ surface, doc, fetchImpl: recordingFetch().fetchImpl });

    const rows = nodes.get('region-list').children;
    assert.equal(rows.length, 2);
    assert.deepEqual(controlKeys(rows[0]), [
        'region',
        'regionVisibility',
        'regionEdit',
        'regionDelete',
    ]);
});

test('rename and delete are absent when the template did not render the Add button', async () => {
    // The template gates `#add-region-btn` on `user_profile.is_admin`, and the endpoints
    // enforce the same rule. Its presence is reused as the gate rather than passing the
    // flag down a second path that could disagree with it.
    const { bindVideoControls } = await import('../imaging/video/pageControls.js');
    const { doc, nodes } = panelDoc(PANEL_IDS.filter((id) => id !== 'add-region-btn'));
    const { surface } = annotatingSurface({ regionTypes: REGIONS });

    bindVideoControls({ surface, doc, fetchImpl: recordingFetch().fetchImpl });

    assert.deepEqual(controlKeys(nodes.get('region-list').children[0]), [
        'region',
        'regionVisibility',
    ]);
});

test('hiding one region does not touch the others, and is not persisted', async () => {
    const { bindVideoControls } = await import('../imaging/video/pageControls.js');
    const { doc, nodes } = panelDoc(PANEL_IDS);
    const { surface, calls } = annotatingSurface({ regionTypes: REGIONS });
    const { fetchImpl, requests } = recordingFetch();

    bindVideoControls({ surface, doc, fetchImpl });
    const list = nodes.get('region-list');
    await list.fire('click', control(list.children[0], 'regionVisibility'));

    assert.deepEqual(calls, [['setRegionVisible', 'Liver', false]]);
    // Whether a reader has a layer folded away while they work is not a fact about the
    // study, so nothing is written -- the only traffic is the timeline's own read.
    assert.deepEqual(
        requests.filter((request) => request.method !== 'GET'),
        []
    );
    // And the row now offers the opposite action.
    assert.equal(
        control(nodes.get('region-list').children[0], 'regionVisibility').dataset.regionVisibility,
        'Liver'
    );
});

test('renaming a region PATCHes only what changed', async () => {
    const { bindVideoControls } = await import('../imaging/video/pageControls.js');
    const { doc, nodes } = panelDoc(PANEL_IDS, { prompts: ['Fegato', '#3498db'] });
    const { surface } = annotatingSurface({ regionTypes: REGIONS });
    const { fetchImpl, requests } = recordingFetch({
        'region-types/1/': { body: { id: 1, name: 'Fegato', color: '#3498db' } },
    });

    bindVideoControls({ surface, doc, fetchImpl });
    const list = nodes.get('region-list');
    await list.fire('click', control(list.children[0], 'regionEdit'));

    const patch = requests.find((request) => request.method === 'PATCH');
    assert.ok(patch, 'the rename reached the endpoint');
    assert.equal(patch.url, '/laparoscopy/api/region-types/1/');
    // The colour was offered and left alone, so it is not sent -- a PATCH that restated
    // it would write a per-user override nobody asked for.
    assert.deepEqual(JSON.parse(patch.body), { name: 'Fegato' });
});

test('a recoloured region repaints its mask, not only its swatch', async () => {
    const { bindVideoControls } = await import('../imaging/video/pageControls.js');
    const { doc, nodes } = panelDoc(PANEL_IDS, { prompts: ['Liver', '#00ff00'] });
    const { surface, calls } = annotatingSurface({ regionTypes: REGIONS });
    const { fetchImpl, requests } = recordingFetch({
        'region-types/1/': { body: { id: 1, name: 'Liver', color: '#00ff00' } },
    });

    bindVideoControls({ surface, doc, fetchImpl });
    const list = nodes.get('region-list');
    await list.fire('click', control(list.children[0], 'regionEdit'));

    assert.deepEqual(JSON.parse(requests.at(-1).body), { color: '#00ff00' });
    // Re-registering the representation is what colours a *new* region and short-circuits
    // for one already on screen, so the swatch would move and the mask would not.
    assert.ok(calls.some(([name, code, color]) =>
        name === 'setRegionColor' && code === 'Liver' && color === '#00ff00'));
});

test('deleting a region is confirmed, and takes its masks with it', async () => {
    const { bindVideoControls } = await import('../imaging/video/pageControls.js');
    const { doc, nodes } = panelDoc(PANEL_IDS);
    const { surface, calls } = annotatingSurface({ regionTypes: REGIONS });
    const { fetchImpl, requests } = recordingFetch({ 'region-types/1/': { status: 204 } });

    bindVideoControls({ surface, doc, fetchImpl });
    const list = nodes.get('region-list');
    await list.fire('click', control(list.children[0], 'regionDelete'));

    assert.equal(requests.at(-1).method, 'DELETE');
    assert.ok(calls.some(([name, code]) => name === 'removeRegionType' && code === 'Liver'));
});

test('a refused delete is reported and changes nothing locally', async () => {
    const { bindVideoControls } = await import('../imaging/video/pageControls.js');
    const { doc, nodes } = panelDoc(PANEL_IDS);
    const { surface, calls } = annotatingSurface({ regionTypes: REGIONS });
    const { fetchImpl } = recordingFetch({
        'region-types/1/': { status: 403, body: { error: 'Annotator access required' } },
    });

    bindVideoControls({ surface, doc, fetchImpl });
    const list = nodes.get('region-list');
    await list.fire('click', control(list.children[0], 'regionDelete'));

    assert.equal(calls.some(([name]) => name === 'removeRegionType'), false);
});

const ANNOTATIONS = [
    { timeMs: 0, regionCode: 'Liver', color: '#3498db', tool: 'brush' },
    { timeMs: 1240, regionCode: 'Fat', color: '#e74c3c', tool: null },
];

test('the annotation list names the tool, the region and the instant', async () => {
    const { bindVideoControls } = await import('../imaging/video/pageControls.js');
    const { doc, nodes } = panelDoc(PANEL_IDS);
    const { surface } = annotatingSurface({ regionTypes: REGIONS, annotations: ANNOTATIONS });

    bindVideoControls({ surface, doc, fetchImpl: recordingFetch().fetchImpl });

    const rows = nodes.get('shapes-list').children;
    assert.equal(rows.length, 2);
    assert.match(rows[0].children[1].textContent, /Brush • Liver @00:00\.000/);
    // No tool recorded: the row says the region and the instant and claims nothing else.
    // Every mask stored before attribution existed reads like this.
    assert.match(rows[1].children[1].textContent, /^Fat @00:01\.240$/);
});

test('the panel is shown rather than force-hidden', async () => {
    const { bindVideoControls } = await import('../imaging/video/pageControls.js');
    const { doc, nodes } = panelDoc(PANEL_IDS);
    const { surface } = annotatingSurface({ regionTypes: REGIONS, annotations: ANNOTATIONS });

    bindVideoControls({ surface, doc, fetchImpl: recordingFetch().fetchImpl });

    assert.equal(nodes.get('shapes-list-panel').classList.contains('d-none'), false);
});

test('an empty list says so instead of showing a blank card', async () => {
    const { bindVideoControls } = await import('../imaging/video/pageControls.js');
    const { doc, nodes } = panelDoc(PANEL_IDS);
    const { surface } = annotatingSurface({ regionTypes: REGIONS });

    bindVideoControls({ surface, doc, fetchImpl: recordingFetch().fetchImpl });

    const rows = nodes.get('shapes-list').children;
    assert.equal(rows.length, 1);
    assert.equal(rows[0].dataset.annotationsEmpty, 'true');
});

test('clearing a mask seeks to its frame first', async () => {
    // The editor edits the labelmap of the frame on screen, which is what makes the change
    // visible and what lets the next flush carry it into the store like any stroke.
    const { bindVideoControls } = await import('../imaging/video/pageControls.js');
    const { doc, nodes } = panelDoc(PANEL_IDS);
    const { surface, calls } = annotatingSurface({
        regionTypes: REGIONS,
        annotations: ANNOTATIONS,
    });

    bindVideoControls({ surface, doc, fetchImpl: recordingFetch().fetchImpl });
    const list = nodes.get('shapes-list');
    await list.fire('click', control(list.children[1], 'annotationDelete'));

    assert.deepEqual(
        calls.filter(([name]) => name !== 'selectRegion'),
        [['goToInstant', 1240], ['clearRegionAt', 'Fat'], ['markDirty']]
    );
});

test('the move button offers the other region types and applies the choice', async () => {
    const { bindVideoControls } = await import('../imaging/video/pageControls.js');
    const { doc, nodes } = panelDoc(PANEL_IDS);
    const { surface, calls } = annotatingSurface({
        regionTypes: REGIONS,
        annotations: ANNOTATIONS,
    });

    bindVideoControls({ surface, doc, fetchImpl: recordingFetch().fetchImpl });
    const list = nodes.get('shapes-list');
    await list.fire('click', control(list.children[0], 'annotationMove'));

    // Opening the picker must not seek: `goTo` redraws this list, which would detach the
    // button being replaced and the picker would never appear.
    assert.equal(calls.some(([name]) => name === 'goToInstant'), false);
    const picker = list.children[0].children[2];
    assert.equal(picker.tag, 'select');
    // The region it is already in is not offered.
    assert.deepEqual(
        picker.children.map((option) => option.value),
        ['', 'Fat']
    );

    picker.value = 'Fat';
    await list.fire('change', picker);
    // The choice is what seeks, which is also the moment the reader wants the frame.
    assert.ok(calls.some(([name, timeMs]) => name === 'goToInstant' && timeMs === 0));
    assert.ok(calls.some(([name, from, to]) => name === 'moveRegionAt' && from === 'Liver' && to === 'Fat'));
});

test('the quadrant panel is revealed and lists what the project defines', async () => {
    const { bindVideoControls } = await import('../imaging/video/pageControls.js');
    const { doc, nodes } = panelDoc(PANEL_IDS);
    const { surface } = annotatingSurface({ regionTypes: REGIONS });
    const { fetchImpl } = recordingFetch({
        'quadrant-types/': { body: { types: [{ id: 3, name: 'RUQ', color: '#e74c3c' }] } },
        'quadrant-markers/': { body: { markers: [] } },
    });

    bindVideoControls({ surface, doc, fetchImpl });
    await new Promise((resolve) => setTimeout(resolve, 0));

    assert.equal(nodes.get('quadrant-types-panel').classList.contains('d-none'), false);
    const chips = nodes.get('timeline-class-admin-list').children;
    assert.equal(chips.length, 1);
    // The shared chip, the same one the region list is built from -- the quadrant half
    // used to carry its own stylesheet inlined in the template's admin block.
    assert.equal(chips[0].className, 'ygg-type-chip');
    assert.deepEqual(
        chips[0].children.map((child) => child.className),
        ['ygg-type-chip__select', 'ygg-type-chip__actions']
    );
    // Selectable from the panel as well as from the dropdown: the chip renders
    // `is-active` for the current quadrant, and one that showed a selection it could not
    // change would be a control that only ever reports.
    assert.deepEqual(controlKeys(chips[0]), ['quadrantSelect', 'quadrantEdit', 'quadrantDelete']);
    // And the selector the marker button reads, which is what could never be populated.
    assert.equal(nodes.get('timeline-class-list').children.length, 1);
});

test('a project with no quadrants says how to make one', async () => {
    const { bindVideoControls } = await import('../imaging/video/pageControls.js');
    const { doc, nodes } = panelDoc(PANEL_IDS);
    const { surface } = annotatingSurface({ regionTypes: REGIONS });
    const { fetchImpl } = recordingFetch({
        'quadrant-types/': { body: { types: [] } },
        'quadrant-markers/': { body: { markers: [] } },
    });

    bindVideoControls({ surface, doc, fetchImpl });
    await new Promise((resolve) => setTimeout(resolve, 0));

    const admin = nodes.get('timeline-class-admin-list');
    assert.equal(admin.children[0].dataset.quadrantsEmpty, 'true');
});

test('adding a quadrant POSTs it and re-reads the list', async () => {
    const { bindVideoControls } = await import('../imaging/video/pageControls.js');
    const { doc, nodes } = panelDoc(PANEL_IDS, { prompts: ['LUQ'] });
    const { surface } = annotatingSurface({ regionTypes: REGIONS });
    const { fetchImpl, requests } = recordingFetch({
        'quadrant-types/': { body: { types: [{ id: 4, name: 'LUQ', color: '#3498db' }] } },
        'quadrant-markers/': { body: { markers: [] } },
    });

    bindVideoControls({ surface, doc, fetchImpl });
    await new Promise((resolve) => setTimeout(resolve, 0));
    await nodes.get('timeline-add-class-btn').fire('click');

    const post = requests.find((request) => request.method === 'POST');
    assert.ok(post, 'the add button reached the endpoint it had never been bound to');
    assert.equal(post.url, '/laparoscopy/api/quadrant-types/');
    assert.deepEqual(JSON.parse(post.body), { name: 'LUQ' });
});

test('markers are written with PUT, which is the verb the view accepts', async () => {
    // `patient_quadrant_markers` is `@require_http_methods(["GET", "PUT"])`, so the POST
    // this used to send answered 405 on every marker add and every marker removal.
    const { bindVideoControls } = await import('../imaging/video/pageControls.js');
    const { doc, nodes } = panelDoc(PANEL_IDS);
    const { surface } = annotatingSurface({ regionTypes: REGIONS });
    const { fetchImpl, requests } = recordingFetch({
        'quadrant-types/': { body: { types: [{ id: 3, name: 'RUQ', color: '#e74c3c' }] } },
        'quadrant-markers/': { body: { markers: [] } },
    });

    bindVideoControls({ surface, doc, fetchImpl });
    await new Promise((resolve) => setTimeout(resolve, 0));

    const selector = nodes.get('timeline-class-list');
    await selector.fire('click', selector.children[0].children[0]);
    // The panel says which quadrant the next marker will carry, not only the dropdown.
    assert.equal(
        nodes.get('timeline-class-admin-list').children[0].classList.contains('is-active'),
        true
    );
    await nodes.get('timeline-add-pin-btn').fire('click');

    const write = requests.find((request) => request.url.includes('quadrant-markers') && request.method !== 'GET');
    assert.ok(write, 'the marker reached the endpoint');
    assert.equal(write.method, 'PUT');
    assert.deepEqual(JSON.parse(write.body), {
        markers: [{ time_ms: 0, quadrant_type_id: 3 }],
    });
});


test('no tool is armed until the reader arms one', async () => {
    // The template ships the brush `.active`, and the binder used to take that as an
    // instruction and arm it. The page then opened with a drawing tool live on a viewport
    // whose annotation toolbar was still hidden, so a click on the video painted into
    // whichever region happened to be selected before the reader had entered annotation
    // mode or chosen anything. The markup's mark is a default nobody decided.
    const { bindVideoControls } = await import('../imaging/video/pageControls.js');
    const { doc, nodes } = panelDoc(PANEL_IDS);
    const toolbar = nodes.get('annotation-toolbar');
    const brush = element('button');
    brush.dataset.tool = 'brush';
    brush.classList.add('active');
    toolbar.appendChild(brush);

    const { surface } = annotatingSurface({ regionTypes: REGIONS });
    const activated = [];
    surface.editor.setActiveTool = (key) => {
        activated.push(key);
        return 'ok';
    };

    bindVideoControls({ surface, doc, fetchImpl: recordingFetch().fetchImpl });

    // Disarmed, not left alone: `null` is what tells Cornerstone to unbind the primary
    // button, and clearing the class alone would leave the tool live and merely unmarked.
    assert.deepEqual(activated, [null]);
    assert.equal(brush.classList.contains('active'), false);
});

test('leaving annotation mode puts the tools down', async () => {
    // Hiding the toolbar used to leave the last tool bound to the primary button, so a
    // drag meant to pan the video went on painting into a region from behind a panel the
    // reader had just closed.
    const { bindVideoControls } = await import('../imaging/video/pageControls.js');
    const { doc, nodes } = panelDoc(PANEL_IDS);
    const toolbar = nodes.get('annotation-toolbar');
    // As the template ships it: hidden, so the first toggle turns annotation mode *on*.
    toolbar.classList.add('d-none');
    const brush = element('button');
    brush.dataset.tool = 'brush';
    toolbar.appendChild(brush);

    const { surface } = annotatingSurface({ regionTypes: REGIONS });
    const activated = [];
    surface.editor.setActiveTool = (key) => {
        activated.push(key);
        return 'ok';
    };

    bindVideoControls({ surface, doc, fetchImpl: recordingFetch().fetchImpl });
    const toggle = nodes.get('annotation-toggle-btn');

    await toggle.fire('click');   // annotation mode on
    // Through the toolbar, because that is where the delegated listener is.
    await toolbar.fire('click', brush);
    assert.equal(activated.at(-1), 'brush');
    assert.equal(brush.classList.contains('active'), true);

    await toggle.fire('click');   // and off again
    assert.equal(activated.at(-1), null);
    assert.equal(brush.classList.contains('active'), false);
});
