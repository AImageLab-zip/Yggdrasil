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
