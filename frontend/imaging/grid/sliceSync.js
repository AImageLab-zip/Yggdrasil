/**
 * Slice synchronisation: windows on the same plane scroll together.
 *
 * The 2.x viewer did this and the brain help still promises it ("windows with the same
 * orientation scroll together"); the 3.0 grid kept the policy in `windowState.js`
 * (`setFreeScroll`, `orientationGroup`) but nothing ever moved a viewport with it, and
 * the link button that opted a window out went with NiiVue.
 *
 * **Depth, not slice index.** Four brain series are co-registered but need not share a
 * slice spacing, so "slice 40 of 155" in one is not slice 40 in another. What they share
 * is patient space: a target is moved along the view-plane normal until its focal point
 * sits at the source's depth, and Cornerstone resamples whatever plane is there. Pan and
 * zoom are left alone -- only the component along the normal moves.
 */

/** Below this (mm) two windows already show the same plane; moving would only jitter. */
export const SLICE_SYNC_EPSILON_MM = 1e-3;

const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];

/**
 * The camera that puts `target` at `source`'s depth, or null when nothing should move.
 *
 * Null when either camera is incomplete, when the two do not look along the same axis
 * (a sagittal window has no depth in common with an axial one), or when they already
 * agree -- which is also what stops a target's own camera event from echoing back.
 *
 * @param {object} source camera with `focalPoint`, `viewPlaneNormal`.
 * @param {object} target camera with `focalPoint`, `position`, `viewPlaneNormal`.
 * @returns {{focalPoint: number[], position: number[]}|null}
 */
export function cameraAtDepthOf(source, target) {
    const normal = source?.viewPlaneNormal;
    if (!normal || !source.focalPoint || !target?.focalPoint || !target.position || !target.viewPlaneNormal) {
        return null;
    }
    // Same axis, either direction: |cos| close to 1.
    if (Math.abs(dot(normal, target.viewPlaneNormal)) < 0.999) {
        return null;
    }
    const delta = dot(
        [
            source.focalPoint[0] - target.focalPoint[0],
            source.focalPoint[1] - target.focalPoint[1],
            source.focalPoint[2] - target.focalPoint[2],
        ],
        normal
    );
    if (Math.abs(delta) < SLICE_SYNC_EPSILON_MM) {
        return null;
    }
    const shift = (point) => point.map((value, axis) => value + delta * normal[axis]);
    return { focalPoint: shift(target.focalPoint), position: shift(target.position) };
}

/**
 * Keep the grid's windows on the same depth as they scroll.
 *
 * Listens for camera changes on every window and, through `targetsFor`, moves the
 * windows the policy says should follow. Re-entrancy is guarded: moving a target fires
 * that target's own camera event, which must not broadcast back.
 *
 * @param {object} options
 * @param {Map<number, HTMLElement>|Array<[number, HTMLElement]>} options.windows index -> element.
 * @param {(windowIndex: number) => object|null} options.viewportFor
 * @param {(windowIndex: number) => number[]} options.targetsFor the sync policy.
 * @param {string} [options.eventName]
 * @returns {{syncFrom: (windowIndex: number) => number[]}}
 */
export function bindSliceSync({
    windows,
    viewportFor,
    targetsFor,
    eventName = 'CORNERSTONE_CAMERA_MODIFIED',
}) {
    let syncing = false;

    /** Move every follower of `sourceIndex` to its depth; returns the windows moved. */
    const syncFrom = (sourceIndex) => {
        if (syncing) {
            return [];
        }
        const source = viewportFor(sourceIndex)?.getCamera?.();
        if (!source) {
            return [];
        }
        const moved = [];
        syncing = true;
        try {
            for (const targetIndex of targetsFor(sourceIndex)) {
                const viewport = viewportFor(targetIndex);
                const camera = cameraAtDepthOf(source, viewport?.getCamera?.());
                if (!camera) {
                    continue;
                }
                viewport.setCamera(camera);
                viewport.render?.();
                moved.push(targetIndex);
            }
        } finally {
            syncing = false;
        }
        return moved;
    };

    for (const [windowIndex, element] of windows) {
        element?.addEventListener?.(eventName, () => syncFrom(windowIndex));
    }
    return { syncFrom };
}
