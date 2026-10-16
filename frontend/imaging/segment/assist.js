/**
 * Assisted segmentation, the parts that are not Cornerstone: the worker's mask format, the
 * prompt points, and the relay socket (`common.consumers.AssistSessionConsumer`).
 *
 * The socket opens with the page so the worker precomputes while the annotator is still
 * watching the video; closing the page closes the worker session.
 */

/** A 0/1 plane as the worker's `bitpack_u1_v1`: row-major, LSB-first in each byte, base64. */
export function packMask(plane) {
    const bytes = new Uint8Array(Math.ceil(plane.length / 8));
    for (let index = 0; index < plane.length; index += 1) {
        if (plane[index]) {
            bytes[index >> 3] |= 1 << (index & 7);
        }
    }
    let binary = '';
    for (const byte of bytes) {
        binary += String.fromCharCode(byte);
    }
    return btoa(binary);
}

/**
 * The inverse, for a mask the worker says is `shape` = [height, width]. Refuses anything that
 * is not exactly this frame: a transposed mask has the same byte count and would paint
 * plausible nonsense.
 */
export function unpackMask(b64, shape, width, height) {
    const binary = atob(b64);
    if (shape?.[0] !== height || shape?.[1] !== width || binary.length !== Math.ceil((width * height) / 8)) {
        throw new Error('The assistant answered with a mask for another frame size.');
    }
    const plane = new Uint8Array(width * height);
    for (let index = 0; index < plane.length; index += 1) {
        plane[index] = (binary.charCodeAt(index >> 3) >> (index & 7)) & 1;
    }
    return plane;
}

export const hasPixels = (plane) => plane.some((value) => value !== 0);

/**
 * The points of the (frame, label) being worked on that the worker has not answered yet.
 * Changing frame or label starts over; an answered point is forgotten, so it never steers a
 * later prompt (the mask it produced does, as that prompt's starting shape).
 */
export function createPromptPoints() {
    let key = null;
    let points = [];
    function pointsFor(frameIndex, code) {
        const next = `${frameIndex}:${code}`;
        if (next !== key) {
            key = next;
            points = [];
        }
        return points;
    }
    return {
        for: pointsFor,
        add(frameIndex, code, x, y, label) {
            pointsFor(frameIndex, code).push({ x, y, label });
        },
        forget(frameIndex, code, answered) {
            const current = pointsFor(frameIndex, code);
            const keep = current.filter((point) => !answered.includes(point));
            current.splice(0, current.length, ...keep);
        },
    };
}

/** The label a returned mask belongs on, or null when the annotator has moved on. */
export function maskTarget(sent, message, current) {
    if (!sent || message.frameIndex !== sent.frameIndex) {
        return null;
    }
    if (current.frameIndex !== sent.frameIndex || current.code !== sent.code) {
        return null;
    }
    return sent.code;
}

export function assistSocketUrl({ protocol, host }, { projectNamespace, patientId, fileId }) {
    const scheme = protocol === 'https:' ? 'wss' : 'ws';
    return `${scheme}://${host}/ws/assist/${projectNamespace}/${patientId}/?fileId=${fileId}`;
}

/**
 * The relay socket. One prompt in flight at a time. A prompt is *built* only when the channel
 * is free -- `request(build)` keeps the newest builder, and `build()` returns
 * `{payload, context}` or null -- so it carries the points still unanswered and the mask as it
 * is after the previous answer, not a snapshot from the moment of the click.
 */
export function createAssistChannel({
    url,
    WebSocketImpl = globalThis.WebSocket,
    onStatus = () => {},
    onMask = () => {},
    onError = () => {},
}) {
    const socket = new WebSocketImpl(url);
    let status = 'connecting';
    let inFlight = null;
    let waiting = null;

    function setStatus(next) {
        status = next;
        onStatus(next);
    }

    function flush() {
        if (inFlight || !waiting || status !== 'ready') {
            return;
        }
        const next = waiting();
        waiting = null;
        if (!next) {
            return;
        }
        inFlight = next;
        socket.send(JSON.stringify({ type: 'prompt', ...inFlight.payload }));
        onStatus('busy');
    }

    socket.addEventListener('message', (event) => {
        let message;
        try {
            message = JSON.parse(event.data);
        } catch {
            return;
        }
        if (message.type === 'preparing') {
            setStatus('preparing');
        } else if (message.type === 'ready') {
            setStatus('ready');
            flush();
        } else if (message.type === 'mask') {
            onMask(message, inFlight?.context ?? null);
        } else if (message.type === 'done' || message.type === 'error') {
            if (message.type === 'error') {
                onError(message.message);
            }
            inFlight = null;
            if (status === 'ready') {
                onStatus('ready');
            }
            flush();
        }
    });
    socket.addEventListener('close', () => {
        inFlight = null;
        waiting = null;
        setStatus('closed');
    });

    return {
        get status() {
            return status;
        },
        request(build) {
            waiting = build;
            flush();
        },
        close() {
            socket.close();
        },
    };
}

/** The key a frame's mask is stored under, as `frameSource` computes it. */
export function frameTimeMs(frameIndex, fps) {
    return Math.round((frameIndex * 1000) / fps);
}

/**
 * Whether tracking may fill this frame for `code`: only when the stored manifest has no mask
 * of that label there. Frames already annotated are kept as they are.
 */
export function labelIsEmptyOn(manifestFrames, timeMs, code) {
    return !manifestFrames?.[String(timeMs)]?.labels?.[code];
}

/**
 * A tracked frame to save: the planes stored for it plus the tracked label's. A save replaces
 * a frame wholesale, so the other labels must ride along; a painted plane is never replaced.
 */
export function mergeTrackedPlane(storedPlanes, code, plane) {
    if (storedPlanes[code] && hasPixels(storedPlanes[code])) {
        return { ...storedPlanes };
    }
    return { ...storedPlanes, [code]: plane };
}
