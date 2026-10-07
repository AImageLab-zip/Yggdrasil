/**
 * The label list: select, show/hide, opacity, colour, rename, retire, add.
 *
 * Rendering is a pure function of `(labels, view)` into a container, and every control calls
 * a handler rather than touching state, so the list can be rebuilt at any time -- after a
 * rename, after the server answers -- without a control being left holding a stale label.
 * Names go in with `textContent`, never `innerHTML`: a label name is user input.
 */

/** Percent shown on the opacity slider for an alpha in 0..1. */
export function opacityToPercent(alpha) {
    return Math.round(Math.min(1, Math.max(0, alpha)) * 100);
}

export function percentToOpacity(percent) {
    return Math.min(1, Math.max(0, Number(percent) / 100));
}

/** The labels an annotator can paint with: the active ones, in the server's order. */
export function paintableLabels(labels) {
    return labels.filter((label) => label.active);
}

/** Choose a label to select after `code` goes away: the first remaining paintable one. */
export function labelAfterRemoval(labels, code) {
    return paintableLabels(labels).find((label) => label.code !== code)?.code ?? null;
}

/** One line for the save indicator. */
export function describeSave(status, { revision, pending } = {}) {
    switch (status) {
        case 'saving':
            return 'Saving…';
        case 'dirty':
            return pending > 1 ? `Unsaved changes (${pending} frames)` : 'Unsaved changes';
        case 'conflict':
            return 'Someone else saved first. Reload the page to continue.';
        case 'error':
            return 'Could not save. Your work is kept here; it will retry on your next edit.';
        default:
            return revision ? `Saved (revision ${revision})` : 'Nothing to save';
    }
}

function element(doc, tag, props = {}, children = []) {
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
    for (const child of children) {
        node.appendChild(child);
    }
    return node;
}

/**
 * @param {object} options
 * @param {HTMLElement} options.container emptied and refilled.
 * @param {Array<{code, name, color, active}>} options.labels
 * @param {{selected: string|null, hidden: Set<string>, opacity: (code: string) => number,
 *   canEdit: boolean}} options.view
 * @param {object} options.handlers `select(code)`, `toggleVisible(code)`, `opacity(code, alpha)`,
 *   `color(code, hex)`, `rename(code)`, `retire(code)`, `add()`.
 */
export function renderLabelPanel({ container, labels, view, handlers }) {
    const doc = container.ownerDocument;
    container.replaceChildren();

    const paintable = paintableLabels(labels);
    const noun = view.noun ?? 'label';
    const showDisplay = view.showDisplay !== false;
    if (!paintable.length) {
        container.appendChild(
            element(doc, 'p', {
                class: 'segment-labels__empty',
                text: view.canEdit ? `No ${noun}s yet. Add one to get started.` : `No ${noun}s.`,
            })
        );
    }

    for (const label of paintable) {
        const selected = view.selected === label.code;
        const hidden = view.hidden.has(label.code);
        const row = element(
            doc,
            'div',
            {
                class: `segment-label${selected ? ' is-selected' : ''}${hidden ? ' is-hidden' : ''}`,
                'data-label-code': label.code,
            },
            [
                element(doc, 'input', {
                    type: 'color',
                    class: 'segment-label__swatch',
                    value: label.color || '#3498db',
                    title: view.canEdit ? 'Change colour' : 'Colour',
                    'aria-label': `Colour of ${label.name}`,
                    ...(view.canEdit ? {} : { disabled: 'disabled' }),
                    oninput: (event) => handlers.color(label.code, event.target.value),
                }),
                element(doc, 'button', {
                    type: 'button',
                    class: 'segment-label__name',
                    text: label.name,
                    title: view.selectTitle ?? 'Paint with this label',
                    'aria-pressed': selected ? 'true' : 'false',
                    onclick: () => handlers.select(label.code),
                }),
                ...(showDisplay
                    ? [
                element(
                    doc,
                    'button',
                    {
                        type: 'button',
                        class: 'segment-label__icon segment-label__visibility',
                        title: hidden ? 'Show on the image' : 'Hide from the image',
                        'aria-label': hidden ? `Show ${label.name}` : `Hide ${label.name}`,
                        'aria-pressed': hidden ? 'true' : 'false',
                        onclick: () => handlers.toggleVisible(label.code),
                    },
                    [
                        element(doc, 'i', {
                            class: hidden ? 'fas fa-eye-slash' : 'fas fa-eye',
                            'aria-hidden': 'true',
                        }),
                    ]
                )
                      ]
                    : []),
                // The rest only matters for the label being worked on, so it appears for
                // the selected row (or one holding keyboard focus) instead of repeating on
                // every row.
                element(doc, 'div', { class: 'segment-label__more' }, [
                    ...(showDisplay
                        ? [
                    element(doc, 'label', { class: 'segment-label__opacity-field' }, [
                        element(doc, 'span', { text: 'Opacity' }),
                        element(doc, 'input', {
                            type: 'range',
                            min: '5',
                            max: '100',
                            class: 'segment-label__opacity',
                            'aria-label': `Opacity of ${label.name}`,
                            value: String(opacityToPercent(view.opacity(label.code))),
                            oninput: (event) =>
                                handlers.opacity(label.code, percentToOpacity(event.target.value)),
                        }),
                    ])
                          ]
                        : []),
                    ...(view.canEdit
                        ? [
                              element(
                                  doc,
                                  'button',
                                  {
                                      type: 'button',
                                      class: 'segment-label__icon',
                                      title: 'Rename',
                                      'aria-label': `Rename ${label.name}`,
                                      onclick: () => handlers.rename(label.code),
                                  },
                                  [element(doc, 'i', { class: 'fas fa-pen', 'aria-hidden': 'true' })]
                              ),
                              element(
                                  doc,
                                  'button',
                                  {
                                      type: 'button',
                                      class: 'segment-label__icon segment-label__icon--danger',
                                      title: 'Retire (the pixels already drawn are kept)',
                                      'aria-label': `Retire ${label.name}`,
                                      onclick: () => handlers.retire(label.code),
                                  },
                                  [element(doc, 'i', { class: 'fas fa-trash', 'aria-hidden': 'true' })]
                              ),
                          ]
                        : []),
                ]),
            ]
        );
        container.appendChild(row);
    }

    if (view.canEdit) {
        container.appendChild(
            element(
                doc,
                'button',
                { type: 'button', class: 'segment-labels__add', onclick: () => handlers.add() },
                [
                    element(doc, 'i', { class: 'fas fa-plus', 'aria-hidden': 'true' }),
                    element(doc, 'span', { text: `New ${noun}` }),
                ]
            )
        );
    }
}
