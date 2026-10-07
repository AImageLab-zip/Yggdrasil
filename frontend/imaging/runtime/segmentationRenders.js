/**
 * Make Cornerstone's segmentation render scheduler coalesce its requests instead of queueing them.
 *
 * Segmentations are drawn by one shared scheduler in `@cornerstonejs/tools`. In 5.8.2 (and still
 * in 5.11.5 and upstream `main`), a request made while a frame is already scheduled is not merged
 * into that frame: it is pushed onto `_pendingRenderQueue`, and the queue is drained one entry per
 * frame. The drain **stops** at the first entry that names no viewport, and `addSegmentations`
 * produces one every time (it asks to render a segmentation no viewport shows yet). So any burst of
 * segmentation calls strands every request queued behind it, until something unrelated asks for a
 * render while the scheduler happens to be idle.
 *
 * Labelmap colours are applied by that render. An actor created by a data write instead (restoring
 * a saved mask, the first brush stroke) has no colour transfer function until it runs, so a value-1
 * mask draws near-black. On the laparoscopy annotator that was "the mask goes dark after changing
 * frame; clicking a label brings the colour back" -- the click being the unrelated request.
 *
 * The fix is what Cornerstone's annotation scheduler already does: every request joins the next
 * frame, and the flags are cleared *before* rendering, so a request made by a render (mounting an
 * actor makes one) gets a frame of its own instead of being dropped.
 *
 * ponytail: overrides two private members of a pinned dependency. Re-check
 * `stateManagement/segmentation/SegmentationRenderingEngine.js` on every Cornerstone bump; delete
 * this once upstream coalesces. The test drives the real engine, so a renamed member fails it.
 */
export function coalesceSegmentationRenders(engine) {
    engine._setViewportsToBeRenderedNextFrame = (viewportIds) => {
        viewportIds.forEach((viewportId) => engine._needsRender.add(viewportId));
        engine._render();
    };
    engine._renderFlaggedSegmentations = () => {
        const viewportIds = [...engine._needsRender];
        engine._needsRender.clear();
        engine._animationFrameSet = false;
        engine._animationFrameHandle = null;
        viewportIds.forEach((viewportId) => engine._triggerRender(viewportId));
    };
}
