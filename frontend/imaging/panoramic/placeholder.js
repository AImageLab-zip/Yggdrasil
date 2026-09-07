/**
 * What the saved-panoramic pane says while there is no panoramic to show.
 *
 * A newly uploaded patient used to be met by `#cbctPanoramicError` -- a warning triangle
 * over "Panoramic image not available." -- from the moment the page rendered, because
 * `static/js/modality_viewers/panoramic.js` had one box for both "there isn't one yet"
 * and "the image failed to load". Those are different facts, and only one of them is an
 * error.
 *
 * The split, which is the same one `controls.js:setEditReady` draws for the Edit button:
 * **`panoramic.js` decides whether the placeholder is visible, this module decides what it
 * says.** Visibility belongs to the pane that knows whether an image is on screen;
 * wording belongs to the surface that knows whether the CBCT has arrived, whether a
 * segmentation exists to fit an arch against, and how the unattended pass ended.
 *
 * Both panes -- the inline card in the CBCT tab and the standalone Panoramic tab -- are
 * written by one call. They show the same patient, so a state that reached one and not
 * the other would be a bug with no cause worth having.
 */

/** The states the pane can be in while it is empty. */
export const PLACEHOLDER = Object.freeze({
    /** The CBCT is still downloading. Nothing can be decided yet. */
    WAITING: 'waiting',
    /** The volume is in hand and the unattended pass is running. */
    GENERATING: 'generating',
    /** No panoramic, and one can be made: the button is the way. */
    OFFER: 'offer',
    /** No panoramic, and this page cannot make one. Say so; offer nothing. */
    UNAVAILABLE: 'unavailable',
});

/** What each state reads as, unless the caller has something more specific to say. */
export const PLACEHOLDER_TEXT = Object.freeze({
    [PLACEHOLDER.WAITING]: 'Waiting for the CBCT…',
    [PLACEHOLDER.GENERATING]: 'Generating the panoramic…',
    [PLACEHOLDER.OFFER]: 'No panoramic yet.',
    [PLACEHOLDER.UNAVAILABLE]: 'No panoramic has been generated for this patient yet.',
});

/** The nodes the template gives this module, by data attribute rather than by id. */
export const PLACEHOLDER_SELECTOR = '[data-panoramic-placeholder]';
export const MESSAGE_SELECTOR = '[data-panoramic-placeholder-message]';
export const GENERATE_SELECTOR = '[data-panoramic-generate]';

/**
 * Put every placeholder on the page into one state.
 *
 * @param {Document} doc
 * @param {string} state one of {@link PLACEHOLDER}.
 * @param {object} [options]
 * @param {string} [options.message] wording for this state, where the default is not
 *   specific enough -- a patient whose CBCT has not been segmented yet, say.
 * @param {() => void} [options.onGenerate] bound to the button, once per button, and only
 *   ever reached from `OFFER`: it is the only state that shows it.
 * @returns {number} how many placeholders were written, for the tests.
 */
export function setPlaceholder(doc, state, { message, onGenerate } = {}) {
    const nodes = Array.from(doc?.querySelectorAll?.(PLACEHOLDER_SELECTOR) ?? []);
    const text = message || PLACEHOLDER_TEXT[state] || PLACEHOLDER_TEXT[PLACEHOLDER.UNAVAILABLE];
    for (const node of nodes) {
        const label = node.querySelector?.(MESSAGE_SELECTOR);
        if (label) {
            label.textContent = text;
        }
        const button = node.querySelector?.(GENERATE_SELECTOR);
        if (!button) {
            continue;
        }
        button.hidden = state !== PLACEHOLDER.OFFER;
        // Bound once, not once per state change: this function is called several times
        // over a page's life and a handler added on each would run the pass as many times
        // as the pane had changed its mind.
        if (onGenerate && !button.dataset.bound) {
            button.dataset.bound = 'true';
            button.addEventListener('click', () => onGenerate());
        }
    }
    return nodes.length;
}
