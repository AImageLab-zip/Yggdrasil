'use strict';

/*
 * The pure half of static/js/caption_structuring.js.
 *
 * Everything tested here is a plain function of plain values, which is why the module
 * separates them from the controller: the SSE framing, the enable/disable truth table and
 * the escaping are the parts that are wrong in ways nobody notices by clicking around.
 *
 * The escaping tests are the security-relevant ones: renderStructuredBlock interpolates
 * text that came out of a language model straight into markup.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadModule() {
    const listeners = {};
    const window = {
        addEventListener() {},
    };
    const document = {
        addEventListener(name, handler) { listeners[name] = handler; },
        getElementById() { return null; },
        querySelector() { return null; },
        querySelectorAll() { return []; },
    };
    const context = { window, document, console, setTimeout, clearTimeout };
    vm.createContext(context);
    vm.runInNewContext(
        fs.readFileSync(path.join(__dirname, '../caption_structuring.js'), 'utf8'),
        context
    );
    return window.CaptionStructuring;
}

const CS = loadModule();

function loadWithDocument(document) {
    const window = { addEventListener() {} };
    const context = { window, document, console, setTimeout, clearTimeout };
    vm.createContext(context);
    vm.runInNewContext(
        fs.readFileSync(path.join(__dirname, '../caption_structuring.js'), 'utf8'),
        context
    );
    return window.CaptionStructuring;
}


// --------------------------------------------------------------- SSE framing

test('parseSseChunk splits complete events and keeps the remainder', () => {
    const { events, rest } = CS.parseSseChunk(
        'data: {"type":"start"}\n\ndata: {"type":"delta","text":"a"}\n\ndata: {"ty'
    );
    assert.equal(events.length, 2);
    assert.equal(events[0].type, 'start');
    assert.equal(events[1].text, 'a');
    assert.equal(rest, 'data: {"ty');
});

test('parseSseChunk reassembles an event split across two reads', () => {
    // The reader hands over arbitrary byte boundaries, so this is the normal case,
    // not an edge case.
    const first = CS.parseSseChunk('data: {"type":"del');
    assert.equal(first.events.length, 0);

    const second = CS.parseSseChunk(first.rest + 'ta","text":"hello"}\n\n');
    assert.equal(second.events.length, 1);
    assert.equal(second.events[0].text, 'hello');
});

test('parseSseChunk ignores keep-alive comments', () => {
    const { events } = CS.parseSseChunk(': OPENROUTER PROCESSING\n\ndata: {"type":"delta"}\n\n');
    assert.equal(events.length, 1);
});

test('parseSseChunk skips a malformed frame without losing the rest', () => {
    const { events } = CS.parseSseChunk(
        'data: {"type":"delta","text":"a"}\n\ndata: {not json\n\ndata: {"type":"delta","text":"b"}\n\n'
    );
    // Compared by value, not deepEqual: arrays built inside the vm context carry that
    // context's Array prototype, which strict deep equality counts as a difference.
    assert.equal(events.length, 2);
    assert.equal(events[0].text, 'a');
    assert.equal(events[1].text, 'b');
});

test('parseSseChunk tolerates an empty buffer', () => {
    assert.equal(CS.parseSseChunk('').events.length, 0);
    assert.equal(CS.parseSseChunk(undefined).events.length, 0);
});

// ------------------------------------------------------------ button state

function state(overrides) {
    return Object.assign({
        isRecording: false,
        inFlight: false,
        textLength: 50,
        minLength: 10,
        hasCaption: true,
        templateForModality: true,
    }, overrides);
}

test('the button is enabled for a caption long enough to structure', () => {
    assert.equal(CS.shouldEnableStructureButton(state()), true);
});

test('the button is disabled while recording', () => {
    assert.equal(CS.shouldEnableStructureButton(state({ isRecording: true })), false);
});

test('the button is disabled while a run is in flight', () => {
    assert.equal(CS.shouldEnableStructureButton(state({ inFlight: true })), false);
});

test('the button is disabled when no template covers the modality', () => {
    assert.equal(CS.shouldEnableStructureButton(state({ templateForModality: false })), false);
});

test('the 10-character boundary matches the server', () => {
    // common/caption_structuring.py refuses shorter with 409 too_short.
    assert.equal(CS.shouldEnableStructureButton(state({ textLength: 9 })), false);
    assert.equal(CS.shouldEnableStructureButton(state({ textLength: 10 })), true);
});

test('an empty box is fine when there is already a caption to act on', () => {
    assert.equal(CS.shouldEnableStructureButton(state({ textLength: 0 })), true);
    assert.equal(
        CS.shouldEnableStructureButton(state({ textLength: 0, hasCaption: false })),
        false
    );
});

// ------------------------------------------------------ template availability

test('a wildcard template covers every modality', () => {
    assert.equal(CS.templateAvailableFor('cbct', ['*']), true);
    assert.equal(CS.templateAvailableFor('', ['*']), true);
});

test('an exact modality match is found', () => {
    assert.equal(CS.templateAvailableFor('mri', ['mri', 'wsi']), true);
    assert.equal(CS.templateAvailableFor('confocal', ['mri', 'wsi']), false);
});

test('a domain-prefixed slug matches the bare template it is filed under', () => {
    // Urology's modalities are "urology-mri"; its templates are filed under "mri", and
    // common/report_templates.py normalizes the same way server-side.
    assert.equal(CS.templateAvailableFor('urology-mri', ['mri']), true);
});

test('no templates at all means nothing is available', () => {
    assert.equal(CS.templateAvailableFor('mri', []), false);
    assert.equal(CS.templateAvailableFor('mri', undefined), false);
});

// ------------------------------------------------------------------ escaping

test('escapeHtml neutralises every markup character', () => {
    assert.equal(
        CS.escapeHtml('<script>alert("x") & \'y\'</script>'),
        '&lt;script&gt;alert(&quot;x&quot;) &amp; &#39;y&#39;&lt;/script&gt;'
    );
});

test('escapeHtml handles null and undefined', () => {
    assert.equal(CS.escapeHtml(null), '');
    assert.equal(CS.escapeHtml(undefined), '');
});

test('renderStructuredBlock escapes model output', () => {
    // This is the security-relevant one: the text came from a language model.
    const html = CS.renderStructuredBlock({
        id: 1,
        status: 'completed',
        attempt: 1,
        warnings: [],
        completed_at: '2026-01-01T10:00:00+00:00',
        structured_text: '## side\n<script>alert(1)</script></textarea>',
    });
    assert.ok(!html.includes('<script>'));
    assert.ok(!html.includes('</textarea>'));
    assert.ok(html.includes('&lt;script&gt;'));
});

test('renderStructuredBlock renders nothing for an unfinished report', () => {
    assert.equal(CS.renderStructuredBlock({ status: 'processing' }), '');
    assert.equal(CS.renderStructuredBlock(null), '');
});

test('renderStructuredBlock shows the attempt number only on a rerun', () => {
    const first = CS.renderStructuredBlock({
        id: 1, status: 'completed', attempt: 1, warnings: [], structured_text: 'x',
    });
    const second = CS.renderStructuredBlock({
        id: 2, status: 'completed', attempt: 2, warnings: [], structured_text: 'x',
    });
    assert.ok(!first.includes('attempt'));
    assert.ok(second.includes('attempt 2'));
});

// ------------------------------------------------------------ error messages

test('every server error code has clinician-facing text', () => {
    // Kept in step with common/domain_views/caption_reports.py::_error_code and the
    // StructuringRefused codes in common/caption_structuring.py.
    const codes = [
        'disabled', 'permission_denied', 'not_configured', 'no_template',
        'too_short', 'too_long', 'in_flight', 'upstream_timeout', 'rate_limited',
        'upstream_error', 'malformed_response', 'stalled',
    ];
    const seen = new Set();
    codes.forEach((code) => {
        const message = CS.structuringErrorMessage(code);
        assert.ok(message && message.length > 10, `no message for ${code}`);
        assert.ok(!seen.has(message), `duplicate message for ${code}`);
        seen.add(message);
    });
});

test('an unknown code still produces a message', () => {
    assert.ok(CS.structuringErrorMessage('something_new').length > 0);
    assert.ok(CS.structuringErrorMessage(undefined).length > 0);
});

// ------------------------------------------------- per-row buttons and templates

test('a caption row whose modality has no template is not offered structuring', () => {
    // The row button used to be gated on nothing but inFlight, so on a modality with no
    // template it could only ever be clicked into a 409 no_template.
    const rows = [
        { dataset: { modality: 'urology-mri' }, disabled: false, title: '' },
        { dataset: { modality: 'urology-wsi' }, disabled: false, title: '' },
    ];
    const button = { disabled: false, title: '' };
    const document = {
        addEventListener() {},
        getElementById(id) { return id === 'structureCaption' ? button : null; },
        querySelector() { return null; },
        querySelectorAll(selector) {
            return selector === '.btn-structure-caption' ? rows : [];
        },
    };
    const controller = new (loadWithDocument(document).Controller)();
    controller.availableModalities = ['mri'];

    controller.syncButtonState();

    assert.equal(rows[0].disabled, false);
    assert.equal(rows[1].disabled, true);
    assert.match(rows[1].title, /No report template/);
});

test('runStructuring gives up quietly when the panel has no textarea', () => {
    // Nothing to write into means the markup changed; throwing here would leave the
    // controller marked in-flight and the button dead for the rest of the page's life.
    const document = {
        addEventListener() {},
        getElementById() { return null; },
        querySelector() { return null; },
        querySelectorAll() { return []; },
    };
    const controller = new (loadWithDocument(document).Controller)();
    controller.panel = { querySelector() { return null; }, dataset: {} };

    assert.doesNotThrow(() => controller.runStructuring('7'));
    assert.equal(controller.inFlight, false);
});
// ------------------------------------------------------- headings, not keys

test('a section heading is shown as the template own wording', () => {
    const out = CS.prettifySections('## pi_rads_score\nPI-RADS 4.', { pi_rads_score: 'Punteggio PI-RADS' });
    assert.equal(out, 'Punteggio PI-RADS\nPI-RADS 4.');
});

test('a key with no label is read as words rather than shown raw', () => {
    const out = CS.prettifySections('### subject_specific_findings\nSomething.', {});
    assert.equal(out, 'Subject specific findings\nSomething.');
});

test('every heading in a multi-section answer loses its hashes', () => {
    const out = CS.prettifySections('## a\none\n\n## b\ntwo', { a: 'First', b: 'Second' });
    assert.equal(out, 'First\none\n\nSecond\ntwo');
    assert.ok(!out.includes('#'));
});

test('a hash inside the body is left alone', () => {
    // Only a line that is itself a heading is rewritten; prose is what was dictated.
    const out = CS.prettifySections('## notes\ntooth #14 is missing', { notes: 'Notes' });
    assert.equal(out, 'Notes\ntooth #14 is missing');
});

test('a half-arrived heading is not mangled once the rest turns up', () => {
    // Deltas split anywhere, so the whole answer is re-rendered on every one.
    const labels = { pi_rads_score: 'Punteggio PI-RADS' };
    assert.equal(CS.prettifySections('## pi_', labels), 'Pi');
    assert.equal(CS.prettifySections('## pi_rads_score', labels), 'Punteggio PI-RADS');
});

test('an omitted section is left out of the streaming view too', () => {
    // The finished report drops it, so showing it mid-stream would be a heading that
    // appears and then vanishes when the "done" frame lands.
    const raw = '## side\nA sinistra.\n\n## uncategorised\nciao ciao prova';
    const out = CS.prettifySections(raw, { side: 'Sede' }, ['uncategorised']);
    assert.equal(out, 'Sede\nA sinistra.');
});

test('omitting nothing keeps every section', () => {
    const raw = '## side\nA sinistra.\n\n## uncategorised\nstray';
    const out = CS.prettifySections(raw, { side: 'Sede' }, []);
    assert.ok(out.includes('stray'));
});


// --------------------------------------------- rows inserted without a page reload

test('a caption saved in this session gets the structure button its row lacks', () => {
    // vocal_caption.js builds the row in JS, and that markup has only edit and delete;
    // the server-rendered row has the structure button. Without this the button only
    // turned up after a reload.
    const actions = {
        children: [],
        querySelector(selector) {
            if (selector === '.btn-structure-caption') {
                return this.children.find((c) => c.className.includes('btn-structure-caption')) || null;
            }
            if (selector === '.btn-edit-caption') return this.edit;
            return null;
        },
        insertBefore(node) { this.children.push(node); },
        appendChild(node) { this.children.push(node); },
    };
    actions.edit = { className: 'btn-edit-caption' };
    const row = { querySelector: () => actions };
    const document = {
        addEventListener() {},
        createElement: () => ({ dataset: {}, className: '', innerHTML: '', type: '' }),
        getElementById() { return null; },
        querySelector(selector) {
            return selector.indexOf('caption-item-compact') !== -1 ? row : null;
        },
        querySelectorAll() { return []; },
    };
    const controller = new (loadWithDocument(document).Controller)();
    controller.panel = {};

    controller.decorateRow({ id: 12, modality: 'urology-mri', text_caption: 'typed' });
    assert.equal(actions.children.length, 1);
    assert.equal(actions.children[0].dataset.captionId, 12);
    assert.equal(actions.children[0].dataset.modality, 'urology-mri');

    // Twice must not mean two buttons.
    actions.children[0].className = 'btn btn-outline-primary btn-sm btn-structure-caption';
    controller.decorateRow({ id: 12, modality: 'urology-mri', text_caption: 'typed' });
    assert.equal(actions.children.length, 1);
});

test('a row gets no structure button where structuring is unavailable', () => {
    const document = {
        addEventListener() {},
        createElement: () => ({ dataset: {}, className: '', innerHTML: '' }),
        getElementById() { return null; },
        querySelector() { throw new Error('must not look for a row'); },
        querySelectorAll() { return []; },
    };
    const controller = new (loadWithDocument(document).Controller)();
    controller.panel = null;   // the panel is absent when structuring_available is false

    assert.doesNotThrow(() => controller.decorateRow({ id: 3, text_caption: 'typed' }));
});


// ------------------------------------------------------ how tall the report box is

test('a long report is given room rather than a scrolling keyhole', () => {
    // 60% of an 900px viewport = 540.
    assert.equal(CS.reportHeight(2000, 900), 540);
});

test('a short report still gets a usable minimum', () => {
    assert.equal(CS.reportHeight(40, 900), 220);
});

test('a report between the two is shown at its own height', () => {
    assert.equal(CS.reportHeight(360, 900), 360);
});

test('a tiny viewport never produces a box smaller than the minimum', () => {
    assert.equal(CS.reportHeight(2000, 200), 220);
});


// ----------------------------------------------------------- an unfilled report

test('a report with no filled section is recognised as empty', () => {
    assert.equal(CS.reportIsEmpty({ structured_text: '' }), true);
    assert.equal(CS.reportIsEmpty({ structured_text: '   \n ' }), true);
    assert.equal(CS.reportIsEmpty(null), true);
    assert.equal(CS.reportIsEmpty({ structured_text: 'Sede\nA sinistra.' }), false);
});

test('the row badge says nothing was filed instead of claiming a report', () => {
    const empty = CS.renderStructuredBlock({
        id: 3, status: 'completed', structured_text: '', warnings: [{ code: 'nothing_filed' }],
    });
    assert.ok(empty.includes('Nothing filed'));
    assert.ok(!empty.includes('>Structured'));

    const filled = CS.renderStructuredBlock({
        id: 4, status: 'completed', structured_text: 'Sede\nA sinistra.', warnings: [],
    });
    assert.ok(filled.includes('Structured'));
});

// ------------------------------------------------ rerun from the patient list

function loadWithStreams() {
    // TextDecoder is a Node global, not a vm-context one; the browser has it natively.
    const window = { addEventListener() {} };
    const document = {
        addEventListener() {},
        getElementById() { return null; },
        querySelector() { return null; },
        querySelectorAll() { return []; },
    };
    const context = { window, document, console, setTimeout, clearTimeout, TextDecoder };
    vm.createContext(context);
    vm.runInNewContext(
        fs.readFileSync(path.join(__dirname, '../caption_structuring.js'), 'utf8'),
        context
    );
    return window.CaptionStructuring;
}

const RS = loadWithStreams();

function sseResponse(chunks) {
    const encoder = new TextEncoder();
    let index = 0;
    return {
        ok: true,
        status: 200,
        body: {
            getReader() {
                return {
                    read() {
                        if (index < chunks.length) {
                            return Promise.resolve({ done: false, value: encoder.encode(chunks[index++]) });
                        }
                        return Promise.resolve({ done: true });
                    },
                };
            },
        },
    };
}

function jsonResponse(status, body) {
    return { ok: status < 400, status, json: () => Promise.resolve(body) };
}

const DONE = 'data: {"type":"done","report":{"id":1}}\n\n';

test('a done frame split across reads is a success', async () => {
    const outcome = await RS.drainStructuringResponse(sseResponse([
        'data: {"type":"start"}\n\n: keepalive\n\ndata: {"type":"do',
        'ne","report":{"id":9}}\n\n',
    ]));
    assert.equal(outcome.ok, true);
    assert.equal(outcome.report.id, 9);
});

test('an error frame is a failure with its code', async () => {
    const outcome = await RS.drainStructuringResponse(sseResponse([
        'data: {"type":"start"}\n\ndata: {"type":"error","code":"rate_limited","detail":"429"}\n\n',
    ]));
    assert.equal(outcome.ok, false);
    assert.equal(outcome.code, 'rate_limited');
});

test('a stream that ends with neither frame is not a success', async () => {
    const outcome = await RS.drainStructuringResponse(sseResponse(['data: {"type":"delta","text":"x"}\n\n']));
    assert.equal(outcome.ok, false);
    assert.equal(outcome.code, 'stalled');
});

test('an HTTP refusal before the stream opens carries its code', async () => {
    const outcome = await RS.drainStructuringResponse(jsonResponse(409, { code: 'not_complete' }));
    assert.equal(outcome.ok, false);
    assert.equal(outcome.code, 'not_complete');
});

function fakeServer(listResponse, outcomes) {
    const calls = [];
    let inFlight = 0;
    let maxInFlight = 0;
    const fetch = (url, init) => {
        calls.push({ url, method: (init && init.method) || 'GET' });
        if (url.endsWith('/voice-captions/structurable/')) return Promise.resolve(listResponse);
        inFlight += 1;
        maxInFlight = Math.max(maxInFlight, inFlight);
        const response = outcomes.shift();
        return new Promise((resolve) => setTimeout(() => { inFlight -= 1; resolve(response); }, 5));
    };
    return { fetch, calls, maxInFlight: () => maxInFlight };
}

test('every eligible caption is structured, one at a time', async () => {
    const server = fakeServer(
        jsonResponse(200, { captions: [{ id: 3 }, { id: 4 }] }),
        [sseResponse([DONE]), sseResponse([DONE])]
    );
    const progress = [];
    const summary = await RS.rerunPatientCaptions({
        base: '/maxillo/patient/7', token: 't', fetch: server.fetch,
        onProgress: (index, total) => progress.push(`${index}/${total}`),
    });
    assert.equal(summary.total, 2);
    assert.equal(summary.done, 2);
    assert.deepEqual(progress, ['1/2', '2/2']);
    assert.deepEqual(server.calls.map((c) => c.url), [
        '/maxillo/patient/7/voice-captions/structurable/',
        '/maxillo/patient/7/voice-caption/3/structure/',
        '/maxillo/patient/7/voice-caption/4/structure/',
    ]);
    // A burst would fail most of a rate-limited batch.
    assert.equal(server.maxInFlight(), 1);
});

test('a rate limit stops the rerun instead of failing every caption in turn', async () => {
    const server = fakeServer(
        jsonResponse(200, { captions: [{ id: 3 }, { id: 4 }, { id: 5 }] }),
        [sseResponse([DONE]), sseResponse(['data: {"type":"error","code":"rate_limited"}\n\n'])]
    );
    const summary = await RS.rerunPatientCaptions({ base: '/b', token: 't', fetch: server.fetch });
    assert.equal(summary.done, 1);
    assert.equal(summary.stoppedBy, 'rate_limited');
    assert.equal(server.calls.filter((c) => c.method === 'POST').length, 2);
});

test('a caption that fails on its own does not stop the others', async () => {
    const server = fakeServer(
        jsonResponse(200, { captions: [{ id: 3 }, { id: 4 }] }),
        [jsonResponse(409, { code: 'in_flight' }), sseResponse([DONE])]
    );
    const summary = await RS.rerunPatientCaptions({ base: '/b', token: 't', fetch: server.fetch });
    assert.equal(summary.done, 1);
    assert.equal(summary.stoppedBy, null);
    assert.deepEqual(JSON.parse(JSON.stringify(summary.failed)), [{ id: 3, code: 'in_flight' }]);
});

test('a refused list structures nothing and says why', async () => {
    const server = fakeServer(jsonResponse(403, { code: 'disabled' }), []);
    const summary = await RS.rerunPatientCaptions({ base: '/b', token: 't', fetch: server.fetch });
    assert.equal(summary.total, 0);
    assert.equal(summary.stoppedBy, 'disabled');
    const message = RS.rerunSummaryMessage(summary);
    assert.equal(message.type, 'error');
    assert.match(message.text, /not enabled/);
});

test('the summary toast matches the outcome', () => {
    assert.deepEqual(
        JSON.parse(JSON.stringify(RS.rerunSummaryMessage({ total: 2, done: 2, failed: [], stoppedBy: null }))),
        { type: 'success', text: 'Structured 2 of 2 captions.' }
    );
    assert.equal(
        RS.rerunSummaryMessage({ total: 2, done: 1, failed: [{ id: 1, code: 'in_flight' }], stoppedBy: null }).type,
        'warning'
    );
    assert.equal(RS.rerunSummaryMessage({ total: 0, done: 0, failed: [], stoppedBy: null }).type, 'info');
    assert.match(
        RS.rerunSummaryMessage({ total: 1, done: 1, failed: [], stoppedBy: null }).text,
        /1 of 1 caption\./
    );
});

test('only failures every caption would share stop a rerun', () => {
    ['rate_limited', 'not_configured', 'disabled', 'budget_exhausted'].forEach((code) => {
        assert.equal(RS.stopsRerun(code), true, code);
    });
    ['in_flight', 'too_short', 'not_complete', 'malformed_response'].forEach((code) => {
        assert.equal(RS.stopsRerun(code), false, code);
    });
});

test('an unfinished caption has its own message', () => {
    assert.match(CS.structuringErrorMessage('not_complete'), /still being processed/);
});
