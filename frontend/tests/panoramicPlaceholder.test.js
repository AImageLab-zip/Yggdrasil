import test from 'node:test';
import assert from 'node:assert/strict';

import {
    GENERATE_SELECTOR,
    MESSAGE_SELECTOR,
    PLACEHOLDER,
    PLACEHOLDER_SELECTOR,
    PLACEHOLDER_TEXT,
    setPlaceholder,
} from '../imaging/panoramic/placeholder.js';

/**
 * What the saved-panoramic pane says while there is nothing to show.
 *
 * The behaviour under test is small but the defect it replaces was not: a newly uploaded
 * patient was met by a warning triangle over "Panoramic image not available." from the
 * moment the page rendered, because "there isn't one yet" and "the image failed to load"
 * shared one box.
 */

function buildDocument({ panes = 2 } = {}) {
    const placeholders = [];
    for (let index = 0; index < panes; index += 1) {
        const message = { textContent: '' };
        const button = {
            hidden: true,
            dataset: {},
            clicks: 0,
            handlers: [],
            addEventListener(name, handler) { button.handlers.push({ name, handler }); },
            click() { for (const entry of button.handlers) entry.handler(); },
        };
        placeholders.push({
            message,
            button,
            querySelector(selector) {
                if (selector === MESSAGE_SELECTOR) return message;
                if (selector === GENERATE_SELECTOR) return button;
                return null;
            },
        });
    }
    return {
        placeholders,
        querySelectorAll: (selector) => (selector === PLACEHOLDER_SELECTOR ? placeholders : []),
    };
}

test('both panes are written by one call', async () => {
    // The inline card in the CBCT tab and the standalone Panoramic tab show the same
    // patient. A state that reached one and not the other is a bug with no cause worth
    // having.
    const doc = buildDocument();

    assert.equal(setPlaceholder(doc, PLACEHOLDER.WAITING), 2);

    for (const placeholder of doc.placeholders) {
        assert.equal(placeholder.message.textContent, PLACEHOLDER_TEXT[PLACEHOLDER.WAITING]);
    }
});

test('the button is offered by exactly one state', async () => {
    const doc = buildDocument({ panes: 1 });
    const [placeholder] = doc.placeholders;

    for (const state of [PLACEHOLDER.WAITING, PLACEHOLDER.GENERATING, PLACEHOLDER.UNAVAILABLE]) {
        setPlaceholder(doc, state, { onGenerate: () => {} });
        assert.equal(placeholder.button.hidden, true, `${state} offers no button`);
    }

    setPlaceholder(doc, PLACEHOLDER.OFFER, { onGenerate: () => {} });
    assert.equal(placeholder.button.hidden, false);
});

test('a caller with something more specific to say is not overruled', async () => {
    // A patient whose CBCT has not been segmented yet cannot have a panoramic generated
    // at all, and "No panoramic has been generated for this patient yet." does not say
    // why or when that changes.
    const doc = buildDocument({ panes: 1 });

    setPlaceholder(doc, PLACEHOLDER.UNAVAILABLE, { message: 'Once CBCT processing has finished.' });

    assert.equal(doc.placeholders[0].message.textContent, 'Once CBCT processing has finished.');
});

test('the generate handler is bound once, however often the state changes', async () => {
    // This function is called several times over a page's life -- waiting, generating,
    // and then offering. A handler added on each would run the pass as many times as the
    // pane had changed its mind.
    const doc = buildDocument({ panes: 1 });
    let runs = 0;
    const onGenerate = () => { runs += 1; };

    setPlaceholder(doc, PLACEHOLDER.WAITING, { onGenerate });
    setPlaceholder(doc, PLACEHOLDER.GENERATING, { onGenerate });
    setPlaceholder(doc, PLACEHOLDER.OFFER, { onGenerate });
    doc.placeholders[0].button.click();

    assert.equal(runs, 1);
});

test('a page without the partial is not an error', async () => {
    // Every setter on this surface tolerates a missing element: it renders inside a
    // patient page that must survive one section being absent.
    assert.equal(setPlaceholder({}, PLACEHOLDER.WAITING), 0);
    assert.equal(setPlaceholder(buildDocument({ panes: 0 }), PLACEHOLDER.OFFER), 0);
});
