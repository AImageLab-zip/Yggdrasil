/**
 * The video surface's tool names, pinned against the classes they name.
 *
 * This file exists because of a shipped defect. `TOOL_PLAN` and `VIDEO_TOOL_NAMES` were
 * hand-written from the class names minus `Tool`, and three of Cornerstone's `toolName`
 * statics do not follow that pattern:
 *
 *     RectangleScissorsTool.toolName                 === 'RectangleScissor'
 *     CircleScissorsTool.toolName                    === 'CircleScissor'
 *     PlanarFreehandContourSegmentationTool.toolName === 'PlanarFreehandContourSegmentationTool'
 *
 * `ToolGroup.addTool` answers an unknown name with `console.warn` and a bare `return`
 * (`store/ToolGroupManager/ToolGroup.js:50`), so the scissors and the polygon tool were
 * never instantiated, their toolbar buttons did nothing, and the only evidence was three
 * warnings in a browser console. Nothing in the suite read the real statics, so the
 * build was green throughout.
 *
 * The pins are deliberately *positive*, in the manner of `videoLabelmapSupport.test.js`:
 * a rename in a Cornerstone bump fails here, and says which name moved.
 */

import { readFileSync } from 'node:fs';
import { strict as assert } from 'node:assert';
import test from 'node:test';

import { TOOL_PLAN, VIDEO_TOOL_NAMES } from '../imaging/video/editor.js';

const TOOLS = 'node_modules/@cornerstonejs/tools/dist/esm/tools';

/** The `toolName` static a Cornerstone tool source assigns, read from the file. */
function toolNameOf(sourcePath, className) {
    const source = readFileSync(`${TOOLS}/${sourcePath}`, 'utf8');
    const match = source.match(new RegExp(`\\n${className}\\.toolName = '([^']+)';`));
    assert.ok(match, `${sourcePath} no longer assigns ${className}.toolName.`);
    return match[1];
}

/** Every tool the toolbar can activate, and the class it must resolve to. */
const EXPECTED = Object.freeze({
    pan: ['PanTool.js', 'PanTool'],
    zoom: ['ZoomTool.js', 'ZoomTool'],
    brush: ['segmentation/BrushTool.js', 'BrushTool'],
    // Exported as `EraserTool`, defined as `AnnotationEraserTool`.
    eraser: ['AnnotationEraserTool.js', 'AnnotationEraserTool'],
    'rect-scissors': ['segmentation/RectangleScissorsTool.js', 'RectangleScissorsTool'],
    'circle-scissors': ['segmentation/CircleScissorsTool.js', 'CircleScissorsTool'],
    // The plain freehand ROI, not the contour *segmentation* one -- see
    // `imaging/video/polygonFill.js` for why, and note that the two `toolName` statics
    // differ in shape as well as in name.
    polygon: ['annotation/PlanarFreehandROITool.js', 'PlanarFreehandROITool'],
    measure: ['annotation/LengthTool.js', 'LengthTool'],
    label: ['annotation/ArrowAnnotateTool.js', 'ArrowAnnotateTool'],
});

test('every TOOL_PLAN entry names a tool the library is actually registered under', () => {
    assert.deepEqual(
        Object.keys(TOOL_PLAN).sort(),
        Object.keys(EXPECTED).sort(),
        'A toolbar key was added or removed; extend this pin with the class it maps to.'
    );
    for (const [key, [sourcePath, className]] of Object.entries(EXPECTED)) {
        assert.equal(
            TOOL_PLAN[key].tool,
            toolNameOf(sourcePath, className),
            `TOOL_PLAN['${key}'] does not match ${className}.toolName, so ` +
                "`toolGroup.addTool` would warn and leave that button inert."
        );
    }
});

test('the three names that do not follow the class-minus-Tool pattern still do not', () => {
    // Stated explicitly so the reason this file exists survives a future tidy-up: these
    // are the exact three that were guessed wrong.
    assert.equal(toolNameOf('segmentation/RectangleScissorsTool.js', 'RectangleScissorsTool'), 'RectangleScissor');
    assert.equal(toolNameOf('segmentation/CircleScissorsTool.js', 'CircleScissorsTool'), 'CircleScissor');
    assert.equal(
        toolNameOf(
            'annotation/PlanarFreehandContourSegmentationTool.js',
            'PlanarFreehandContourSegmentationTool'
        ),
        'PlanarFreehandContourSegmentationTool'
    );
    // And the one this surface actually uses does follow it, which is the trap in the
    // other direction: the two names are one word apart and only one of them can create
    // an annotation without a Contour segmentation.
    assert.equal(toolNameOf('annotation/PlanarFreehandROITool.js', 'PlanarFreehandROITool'), 'PlanarFreehandROI');
});

test('VIDEO_TOOL_NAMES is exactly the set TOOL_PLAN can activate', () => {
    assert.deepEqual(
        [...VIDEO_TOOL_NAMES].sort(),
        [...new Set(Object.values(TOOL_PLAN).map((plan) => plan.tool))].sort(),
        'A tool added to the group that no button can reach is dead weight; one a button ' +
            'names but the group never added is a control that does nothing.'
    );
});
