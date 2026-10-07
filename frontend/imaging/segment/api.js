/**
 * The image-segmentation HTTP client.
 *
 * Every call takes `fetch` as an argument so the suite can run it against a fake. Errors
 * carry what the caller must branch on -- `status`, and `conflict` for a 409 -- and the
 * server's own message, because "Request failed" tells an annotator nothing.
 *
 * Writes carry the CSRF token in `X-CSRFToken`. It comes from the hidden input
 * (`csrfToken()` in `photos/bootstrap.js`): `CSRF_USE_SESSIONS` means there is no cookie.
 */

export class ApiError extends Error {
    constructor(message, { status = 0, conflict = false } = {}) {
        super(message);
        this.name = 'ApiError';
        this.status = status;
        this.conflict = conflict;
    }
}

/** `/{namespace}/api/patients/{id}/image-segmentation/` */
export function endpointFor({ projectNamespace, patientId }) {
    return `/${projectNamespace}/api/patients/${patientId}/image-segmentation/`;
}

/** `/{namespace}/api/patients/{id}/quadrants/` */
export function quadrantsEndpointFor({ projectNamespace, patientId }) {
    return `/${projectNamespace}/api/patients/${patientId}/quadrants/`;
}

async function request(fetchImpl, url, { method = 'GET', body, csrf } = {}) {
    const headers = { Accept: 'application/json' };
    if (body !== undefined) {
        headers['Content-Type'] = 'application/json';
    }
    if (method !== 'GET') {
        headers['X-CSRFToken'] = csrf ?? '';
    }
    let response;
    try {
        response = await fetchImpl(url, {
            method,
            headers,
            credentials: 'same-origin',
            body: body === undefined ? undefined : JSON.stringify(body),
        });
    } catch (cause) {
        throw new ApiError(`Could not reach the server: ${cause.message}`);
    }
    let payload = null;
    try {
        payload = await response.json();
    } catch {
        // Not JSON: Django's HTML error page, or a proxy's. The status still says enough.
    }
    if (!response.ok) {
        throw new ApiError(payload?.error ?? `The server answered ${response.status}.`, {
            status: response.status,
            conflict: response.status === 409,
        });
    }
    return payload;
}

/**
 * @param {object} options
 * @param {string} options.endpoint see {@link endpointFor}.
 * @param {Function} options.fetchImpl
 * @param {string} options.csrf
 */
export function createApi({ endpoint, fetchImpl = (...args) => globalThis.fetch(...args), csrf }) {
    const labels = `${endpoint}labels/`;
    return {
        state: () => request(fetchImpl, `${endpoint}state/`),

        /** `null` when the frame has no stored mask. */
        async frame(fileId, timeMs) {
            try {
                return await request(
                    fetchImpl,
                    `${endpoint}frame/?fileId=${encodeURIComponent(fileId)}&timeMs=${encodeURIComponent(timeMs)}`
                );
            } catch (error) {
                if (error.status === 404) {
                    return null;
                }
                throw error;
            }
        },

        save: (body) => request(fetchImpl, endpoint, { method: 'POST', body, csrf }),

        createLabel: (name, color) =>
            request(fetchImpl, labels, { method: 'POST', body: { name, color }, csrf }),
        updateLabel: (code, changes) =>
            request(fetchImpl, `${labels}${encodeURIComponent(code)}/`, {
                method: 'PATCH',
                body: changes,
                csrf,
            }),
        retireLabel: (code) =>
            request(fetchImpl, `${labels}${encodeURIComponent(code)}/`, {
                method: 'DELETE',
                csrf,
            }),
    };
}

/** The quadrant timeline's client: the whole marker list is read and replaced at once. */
export function createQuadrantsApi({ endpoint, fetchImpl = (...args) => globalThis.fetch(...args), csrf }) {
    const labels = `${endpoint}labels/`;
    return {
        state: () => request(fetchImpl, endpoint),
        save: (body) => request(fetchImpl, endpoint, { method: 'PUT', body, csrf }),
        createLabel: (name, color) =>
            request(fetchImpl, labels, { method: 'POST', body: { name, color }, csrf }),
        updateLabel: (code, changes) =>
            request(fetchImpl, `${labels}${encodeURIComponent(code)}/`, {
                method: 'PATCH',
                body: changes,
                csrf,
            }),
        retireLabel: (code) =>
            request(fetchImpl, `${labels}${encodeURIComponent(code)}/`, { method: 'DELETE', csrf }),
    };
}
