/**
 * Themed dialogs, because `prompt()` is not one.
 *
 * The browser's `prompt()` and `confirm()` are unstyled, appear outside the page, cannot
 * be laid out, and on some browsers offer to suppress themselves permanently -- which for
 * a calibration dialog means a user who once ticked "prevent this page from creating more
 * dialogs" can no longer calibrate an image, with no error to explain it.
 *
 * These build the same markup the rest of the app uses (`.modal` > `.modal-dialog` >
 * `.modal-content`, dismissed by `[data-ygg-dismiss="modal"]`) and drive it through
 * `window.yggUI.Modal` when it is present, so the backdrop, the escape handling and the
 * `ygg-modal-open` body class all behave the way every other dialog on the page does.
 * Falling back to a bare `display: block` keeps the surface usable if `ygg-ui.js` has not
 * loaded, rather than trading one broken dialog for another.
 *
 * Both return promises that resolve to `null` on cancel -- distinguishable from an empty
 * string, which is what a user who confirms a blank field meant.
 */

const DIALOG_CLASS = 'ygg-photo-dialog';

/**
 * Build the modal element and return it plus a `close` that tears it down.
 *
 * A new element per call rather than one reused hidden node: the surface can be mounted
 * more than once on a page (tab switches, a stack rebuilt after an image edit), and a
 * shared node would leave two callers fighting over one dialog's fields.
 */
function buildModal(doc, { title, icon, body, confirmLabel, cancelLabel }) {
    const host = doc.createElement('div');
    host.className = `modal ${DIALOG_CLASS}`;
    host.setAttribute('tabindex', '-1');
    host.setAttribute('role', 'dialog');
    host.setAttribute('aria-modal', 'true');
    host.innerHTML = `
        <div class="modal-dialog modal-dialog-centered">
            <div class="modal-content">
                <div class="modal-header">
                    <h5 class="modal-title">${icon ? `<i class="fas ${icon} me-2"></i>` : ''}${title}</h5>
                    <button type="button" class="btn-close" data-ygg-dismiss="modal" aria-label="Close"></button>
                </div>
                <div class="modal-body" data-dialog-body></div>
                <div class="modal-footer">
                    <button type="button" class="btn btn-outline-secondary" data-dialog-cancel>${cancelLabel}</button>
                    <button type="button" class="btn btn-primary" data-dialog-confirm>${confirmLabel}</button>
                </div>
            </div>
        </div>
    `;
    host.querySelector('[data-dialog-body]').append(body);
    doc.body.appendChild(host);

    // Drive it through the app's own Modal when available, so the backdrop click, the
    // dismiss buttons and the body scroll-lock behave like every other dialog here.
    const instance = globalThis.yggUI?.Modal?.getOrCreateInstance?.(host) ?? null;
    if (instance) {
        instance.show();
    } else {
        host.classList.add('show');
        host.style.display = 'block';
    }

    return {
        host,
        close() {
            if (instance) {
                instance.hide();
            }
            host.remove();
        },
    };
}

/**
 * Ask for a number, in a themed dialog.
 *
 * @param {object} options
 * @param {string} options.title
 * @param {string} options.message shown above the field.
 * @param {string} [options.unit] suffix rendered beside the input.
 * @param {number} [options.min] rejected below this, with a message rather than a silent
 *   refusal -- the caller's own validation is the authority, but a dialog that accepts a
 *   value and then reports failure through a toast is worse than one that says so here.
 * @param {Document} [options.doc]
 * @returns {Promise<number|null>} null on cancel.
 */
export function askForNumber({
    title,
    message,
    unit = '',
    min = null,
    placeholder = '',
    doc = globalThis.document,
}) {
    return new Promise((resolve) => {
        const body = doc.createElement('div');
        const label = doc.createElement('p');
        label.className = 'mb-2';
        label.textContent = message;

        const group = doc.createElement('div');
        group.className = 'd-flex align-items-center gap-2';
        const input = doc.createElement('input');
        input.type = 'number';
        input.className = 'form-control';
        input.step = 'any';
        input.placeholder = placeholder;
        if (min !== null) {
            input.min = String(min);
        }
        group.appendChild(input);
        if (unit) {
            const suffix = doc.createElement('span');
            suffix.className = 'text-muted';
            suffix.textContent = unit;
            group.appendChild(suffix);
        }

        const error = doc.createElement('p');
        error.className = 'text-danger small mb-0 mt-2';
        error.hidden = true;

        body.append(label, group, error);

        const modal = buildModal(doc, {
            title,
            icon: 'fa-ruler',
            body,
            confirmLabel: 'Set',
            cancelLabel: 'Cancel',
        });

        const finish = (value) => {
            modal.close();
            resolve(value);
        };
        const submit = () => {
            const value = Number(input.value);
            if (!input.value.trim() || !Number.isFinite(value)) {
                error.textContent = 'Enter a number.';
                error.hidden = false;
                return;
            }
            if (min !== null && value <= min) {
                error.textContent = `Enter a number greater than ${min}.`;
                error.hidden = false;
                return;
            }
            finish(value);
        };

        modal.host.querySelector('[data-dialog-confirm]').addEventListener('click', submit);
        modal.host.querySelector('[data-dialog-cancel]').addEventListener('click', () => finish(null));
        modal.host
            .querySelector('[data-ygg-dismiss="modal"]')
            .addEventListener('click', () => finish(null));
        input.addEventListener('keydown', (event) => {
            if (event.key === 'Enter') {
                event.preventDefault();
                submit();
            }
            if (event.key === 'Escape') {
                finish(null);
            }
        });
        input.focus();
    });
}

/**
 * Ask for a short piece of text, in a themed dialog.
 *
 * Used for naming a point. An empty confirm resolves to `null` rather than `''`: an
 * unnamed point marker is indistinguishable from a stray click, and Cornerstone's
 * `LabelTool` draws nothing at all for an empty label, so the annotation would exist and
 * be invisible.
 *
 * @returns {Promise<string|null>}
 */
export function askForText({
    title,
    message,
    placeholder = '',
    initial = '',
    maxLength = 60,
    doc = globalThis.document,
}) {
    return new Promise((resolve) => {
        const body = doc.createElement('div');
        const label = doc.createElement('p');
        label.className = 'mb-2';
        label.textContent = message;

        const input = doc.createElement('input');
        input.type = 'text';
        input.className = 'form-control';
        input.placeholder = placeholder;
        input.value = initial;
        input.maxLength = maxLength;

        body.append(label, input);

        const modal = buildModal(doc, {
            title,
            icon: 'fa-tag',
            body,
            confirmLabel: 'Name it',
            cancelLabel: 'Cancel',
        });

        const finish = (value) => {
            modal.close();
            resolve(value);
        };
        const submit = () => {
            const value = input.value.trim();
            finish(value || null);
        };

        modal.host.querySelector('[data-dialog-confirm]').addEventListener('click', submit);
        modal.host.querySelector('[data-dialog-cancel]').addEventListener('click', () => finish(null));
        modal.host
            .querySelector('[data-ygg-dismiss="modal"]')
            .addEventListener('click', () => finish(null));
        input.addEventListener('keydown', (event) => {
            if (event.key === 'Enter') {
                event.preventDefault();
                submit();
            }
            if (event.key === 'Escape') {
                finish(null);
            }
        });
        input.focus();
        input.select();
    });
}

/**
 * A full-width dialog that hands its body element to a caller.
 *
 * This is what hosts the RGB image editor: that editor attaches to an `<img>` and builds
 * its own toolbar into the image's container, so it needs a container to own rather than a
 * question to ask. `onClose` runs on every exit path, so a caller can reload a stack
 * whether the user saved or dismissed.
 *
 * @returns {{body: HTMLElement, close: Function}}
 */
export function openPanel({ title, icon = '', onClose, doc = globalThis.document }) {
    const body = doc.createElement('div');
    const modal = buildModal(doc, {
        title,
        icon,
        body,
        confirmLabel: 'Done',
        cancelLabel: 'Close',
    });
    // One dialog, one exit: the footer's two buttons and the header cross all mean
    // "finished", because the editor has its own save control inside the body and a
    // second, differently-labelled save in the chrome would be ambiguous about which one
    // commits.
    modal.host.querySelector('[data-dialog-cancel]').textContent = 'Close';
    modal.host.querySelector('[data-dialog-confirm]').remove();

    const close = () => {
        modal.close();
        onClose?.();
    };
    modal.host.querySelector('[data-dialog-cancel]').addEventListener('click', close);
    modal.host.querySelector('[data-ygg-dismiss="modal"]').addEventListener('click', close);

    return { body, close, host: modal.host };
}
