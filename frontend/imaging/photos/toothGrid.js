/**
 * The 32-button FDI tooth grid.
 *
 * Ported from `static/js/intraoral_segmentation.js:803-885`, which Phase 5 deletes. The
 * grid is how a polygon gets its FDI code, and the code is what decides the segment a
 * polygon is exported under -- so this is not decoration. It is the only place in the
 * editor where a tooth is *named*.
 *
 * ## Split into a model and a renderer
 *
 * {@link toothButtons} is pure: state in, one descriptor per button out. The DOM half
 * takes those descriptors and does nothing else. That split is what makes the interesting
 * parts testable without a browser -- which quadrant a code belongs to, which icon it
 * borrows, whether it is mirrored, how many polygons it has, and whether the only-selected
 * filter hides it.
 *
 * ## Order comes from labelMapper, and only from there
 *
 * The codes are `MOUTH_ORDER` in `./labelMapper.js`, not a second array. Three orderings
 * of the same 32 teeth exist in this codebase and two of them are here: the buttons read
 * left-to-right as a clinician sees the arch, while the label schema is quadrant-major.
 * Keeping one copy of each, in the module that owns the mapping, is what stops a display
 * reorder from silently becoming a storage change.
 */

import { MOUTH_ORDER, isFdiCode } from './labelMapper.js';

/**
 * The gradient the buttons are tinted along, midline outward.
 *
 * Kept verbatim from the editor being replaced. A clinician reads the arch by colour as
 * much as by number, and re-picking these would make every screenshot and every printed
 * report from before the migration disagree with the app.
 *
 * Lower-cased, unlike the original's literals: {@link interpolateHex} emits lower case, and
 * two spellings of one colour make every equality comparison -- in a test, or in a "is this
 * tooth already tinted" check -- depend on which end produced the string.
 */
export const PALETTE = Object.freeze([
    '#1e5bff',
    '#00a9ff',
    '#00d4c7',
    '#38d66b',
    '#dceb00',
    '#fff066',
]);

/**
 * Blend two `#rrggbb` colours.
 *
 * @param {string} from
 * @param {string} to
 * @param {number} ratio 0..1
 * @returns {string} `#rrggbb`
 */
export function interpolateHex(from, to, ratio) {
    const parse = (hex) => {
        const body = hex.replace('#', '');
        return [
            parseInt(body.slice(0, 2), 16),
            parseInt(body.slice(2, 4), 16),
            parseInt(body.slice(4, 6), 16),
        ];
    };
    const start = parse(from);
    const end = parse(to);
    return `#${start
        .map((channel, index) => Math.round(channel + (end[index] - channel) * ratio))
        .map((channel) => channel.toString(16).padStart(2, '0'))
        .join('')}`;
}

/**
 * The colour at position `index` of `total` along {@link PALETTE}.
 *
 * @param {number} index
 * @param {number} total
 * @returns {string}
 */
export function gradientColor(index, total) {
    const ratio = total <= 1 ? 0 : index / (total - 1);
    const position = ratio * (PALETTE.length - 1);
    const left = Math.floor(position);
    const right = Math.min(left + 1, PALETTE.length - 1);
    return interpolateHex(PALETTE[left], PALETTE[right], position - left);
}

/**
 * A tooth's colour: sixteen steps across each arch, from the patient's right to their left.
 *
 * Quadrants 1 and 4 are the patient's right, so their teeth count *inward* to the midline
 * (18 first, 11 eighth) and quadrants 2 and 3 continue outward. That is what makes the
 * gradient continuous across the arch rather than mirrored at the midline.
 *
 * @param {string} code an FDI code.
 * @returns {string} `#rrggbb`
 */
export function toothColor(code) {
    if (!isFdiCode(code)) {
        return PALETTE[0];
    }
    const quadrant = Number(code[0]);
    const position = Number(code[1]);
    const rightSide = quadrant === 1 || quadrant === 4;
    return gradientColor(rightSide ? 8 - position : 8 + position - 1, 16);
}

/**
 * Which tooth SVG a code borrows, in `static/icons/teeth/`.
 *
 * The icon set only draws the patient's right side, plus two gaps. Quadrant 2 borrows
 * quadrant 1's icon and quadrant 4 borrows quadrant 3's, mirrored ({@link
 * toothIconMirrored}). `37` and `47` have no icon of their own and borrow `36`.
 *
 * Kept exactly as the old editor mapped it, substitutions included: a missing file would
 * render an empty button, and inventing a nearest-neighbour here would put a molar's
 * outline on a premolar.
 *
 * @param {string} code
 * @returns {string} the icon's basename.
 */
export function toothIconSource(code) {
    const quadrant = code[0];
    const position = code[1];
    if (quadrant === '2') {
        return `1${position}`;
    }
    if (quadrant === '4') {
        return position === '7' ? '36' : `3${position}`;
    }
    if (code === '37') {
        return '36';
    }
    return code;
}

/**
 * Is this code's icon the mirror of the one it borrows?
 *
 * @param {string} code
 * @returns {boolean}
 */
export function toothIconMirrored(code) {
    return code[0] === '2' || code[0] === '4';
}

/**
 * Strip an inline SVG down to something safe and themeable to inject.
 *
 * `fill="#b2f2bb"` is the icon set's own hardcoded green; rewriting it to `currentColor`
 * is what lets the button tint itself from {@link toothColor}. The XML prolog and doctype
 * go because they are illegal inside an existing document.
 *
 * @param {string} svgText
 * @returns {string}
 */
export function normalizeToothSvg(svgText) {
    return svgText
        .replace(/<\?xml[\s\S]*?\?>/g, '')
        .replace(/<!DOCTYPE[\s\S]*?>/gi, '')
        .replace(/fill="#b2f2bb"/gi, 'fill="currentColor"')
        .replace(/<svg\b/i, '<svg aria-hidden="true" focusable="false"');
}

/**
 * How many polygons this tooth has on the image being edited.
 *
 * @param {object} teeth the `{FDI: [[[x, y], ...], ...]}` map for one image.
 * @param {string} code
 * @returns {number}
 */
export function polygonCount(teeth, code) {
    const polygons = teeth?.[code];
    return Array.isArray(polygons) ? polygons.length : 0;
}

/**
 * One descriptor per button, in mouth order.
 *
 * @param {object} options
 * @param {object} options.teeth the current image's teeth map, or `{}`.
 * @param {string|null} [options.selected] the selected FDI code.
 * @param {boolean} [options.onlySelected] hide every tooth but the selected one.
 * @param {boolean} [options.editable] false for a confirmed image or a read-only user.
 * @param {(teeth: object, code: string) => number} [options.countFor] how many things this
 *   tooth carries. Defaults to counting polygons; the IOS landmark workbench reuses this
 *   grid and counts landmarks instead. The seam is here rather than in a second grid,
 *   because "which tooth am I working on" is one question and the answer should look the
 *   same wherever it is asked.
 * @returns {Array<object>} `{code, color, count, iconSource, mirrored, selected, hidden,
 *   disabled}`
 */
export function toothButtons({
    teeth = {},
    selected = null,
    onlySelected = false,
    editable = true,
    countFor = polygonCount,
} = {}) {
    return MOUTH_ORDER.map((code) => ({
        code,
        color: toothColor(code),
        count: countFor(teeth, code),
        iconSource: toothIconSource(code),
        mirrored: toothIconMirrored(code),
        selected: code === selected,
        // Only ever hides *other* teeth, and only while one is selected -- a filter that
        // could hide everything would look like a viewer that had lost the grid.
        hidden: Boolean(onlySelected && selected) && code !== selected,
        // A confirmed image is still selectable, so its polygons can be looked at; it is
        // the drawing that is refused, by the server as well as here.
        disabled: !editable,
    }));
}

/**
 * The only-selected control's own state.
 *
 * @param {object} options
 * @param {boolean} options.onlySelected
 * @param {boolean} options.hasImage
 * @param {string|null} options.selected
 * @returns {{label: string, active: boolean, disabled: boolean}}
 */
export function onlySelectedControl({ onlySelected, hasImage, selected }) {
    return {
        label: onlySelected ? 'Show all' : 'Only selected',
        active: Boolean(onlySelected),
        disabled: !hasImage || !selected,
    };
}

/**
 * The confirm control's own state.
 *
 * @param {object} options
 * @param {boolean} options.confirmed
 * @param {boolean} options.hasImage
 * @param {boolean} options.canModify
 * @returns {{label: string, confirmed: boolean, disabled: boolean}}
 */
export function confirmControl({ confirmed, hasImage, canModify }) {
    return {
        label: confirmed ? 'Reopen' : 'Mark done',
        confirmed: Boolean(confirmed),
        disabled: !hasImage || !canModify,
    };
}

// ---------------------------------------------------------------------------
// The DOM half
// ---------------------------------------------------------------------------

/**
 * Fetched tooth SVGs, by basename.
 *
 * Module-level and permanent: 32 buttons are rebuilt on every state change, and without a
 * cache each rebuild would refetch up to 32 files. The promise is cached rather than the
 * text so concurrent rebuilds share one request.
 */
const svgCache = new Map();

/**
 * Fetch one tooth SVG, normalised.
 *
 * A failed fetch resolves to an empty string rather than rejecting: the button still has
 * its number and its colour, so a missing icon is a cosmetic loss and must not take the
 * grid down with it.
 *
 * @param {string} source basename, from {@link toothIconSource}.
 * @param {object} [deps]
 * @param {typeof fetch} [deps.fetchImpl]
 * @returns {Promise<string>}
 */
export function loadToothSvg(source, { fetchImpl = globalThis.fetch } = {}) {
    if (!svgCache.has(source)) {
        svgCache.set(
            source,
            fetchImpl(`/static/icons/teeth/${source}.svg`)
                .then((response) => (response.ok ? response.text() : ''))
                .then(normalizeToothSvg)
                .catch(() => '')
        );
    }
    return svgCache.get(source);
}

/**
 * Build the grid into `container`.
 *
 * Rebuilt wholesale rather than diffed. 32 buttons is small, and the alternative -- keeping
 * per-button DOM in step with counts, selection, the filter and the disabled state -- is
 * four ways for the grid to disagree with the document.
 *
 * @param {HTMLElement} container
 * @param {Array<object>} buttons from {@link toothButtons}.
 * @param {object} handlers
 * @param {(code: string) => void} handlers.onSelect
 * @param {(code: string) => void} [handlers.onZoom] double-click, to frame a tooth.
 * @param {Document} [handlers.documentRef] injected so a second surface -- and a test --
 *   can build the grid without reaching for the global.
 */
export function renderToothGrid(
    container,
    buttons,
    { onSelect, onZoom, documentRef = globalThis.document } = {},
) {
    if (!container || !documentRef) {
        return;
    }
    container.replaceChildren();
    for (const button of buttons) {
        const element = documentRef.createElement('button');
        element.type = 'button';
        element.className = 'seg-tooth-btn';
        element.classList.toggle('selected', button.selected);
        element.classList.toggle('no-mask', button.count === 0);
        element.hidden = button.hidden;
        element.disabled = button.disabled && !button.selected;
        element.dataset.tooth = button.code;
        element.style.setProperty('--tooth-color', button.color);
        element.setAttribute('aria-pressed', String(button.selected));
        element.setAttribute(
            'aria-label',
            button.count
                ? `Tooth ${button.code}, ${button.count} outline${button.count === 1 ? '' : 's'}`
                : `Tooth ${button.code}, no outline`
        );

        const icon = documentRef.createElement('span');
        icon.className = 'seg-tooth-icon';
        icon.classList.toggle('mirrored', button.mirrored);
        icon.setAttribute('aria-hidden', 'true');
        element.appendChild(icon);
        loadToothSvg(button.iconSource).then((svg) => {
            // `isConnected` because a rebuild may have replaced this node while the fetch
            // was in flight, and writing into a detached node is a silent leak.
            if (icon.isConnected) {
                icon.innerHTML = svg;
            }
        });

        const label = documentRef.createElement('span');
        label.className = 'seg-tooth-code';
        label.textContent = button.code;
        element.appendChild(label);

        if (button.selected) {
            const badge = documentRef.createElement('span');
            badge.className = 'seg-selected-badge';
            badge.setAttribute('aria-hidden', 'true');
            badge.innerHTML = '<i class="fas fa-paint-brush"></i>';
            element.appendChild(badge);
        }
        if (button.count > 0) {
            const badge = documentRef.createElement('span');
            badge.className = 'seg-count';
            badge.textContent = String(button.count);
            element.appendChild(badge);
        }

        element.addEventListener('click', () => onSelect?.(button.code));
        element.addEventListener('dblclick', (event) => {
            event.preventDefault();
            if (button.count) {
                onZoom?.(button.code);
            }
        });
        container.appendChild(element);
    }
}
