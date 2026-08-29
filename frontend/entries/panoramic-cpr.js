/**
 * Entry point: the live panoramic CPR (roadmap Phase 7, decision #8).
 *
 * Scope boundary that matters, and is the reason this file is a wiring harness rather than
 * an implementation: only the **interactive** layer is Cornerstone + vtk.js
 * `ImageCPRMapper`. The **baking** layer is untouched -- `static/js/seg2pano_core.js` and
 * `static/js/worker/seg2pano_worker.js` are the same bytes they were -- so the PNGs
 * collected by `common/export_catalog.py:232-241` keep theirs and stay exportable.
 *
 * The core arrives as a **global**, not as an import. It is a classic script loaded by
 * `templates/common/patient_detail.html`, so it has executed by the time this deferred
 * module runs; bundling a copy would be a second implementation of the arch mathematics,
 * which is the one thing this phase must not create.
 */

import {
    Enums as coreEnums,
    RenderingEngine,
    cache,
    getRenderingEngine,
    setVolumesForViewports,
} from '@cornerstonejs/core';

import {
    Enums as toolsEnums,
    PanTool,
    SplineROITool,
    ToolGroupManager,
    ZoomTool,
    addTool,
    annotation,
} from '@cornerstonejs/tools';

import vtkDataArray from '@kitware/vtk.js/Common/Core/DataArray.js';
import vtkImageData from '@kitware/vtk.js/Common/DataModel/ImageData.js';
import vtkPolyData from '@kitware/vtk.js/Common/DataModel/PolyData.js';
import vtkImageCPRMapper from '@kitware/vtk.js/Rendering/Core/ImageCPRMapper.js';
import vtkImageMapper from '@kitware/vtk.js/Rendering/Core/ImageMapper.js';
import vtkImageSlice from '@kitware/vtk.js/Rendering/Core/ImageSlice.js';

import { initImaging } from '../imaging/runtime/init.js';
import { volumeIdFor } from '../imaging/grid/layout.js';
import { primaryVolumeFrom } from '../imaging/grid/bootstrap.js';
import { volumeUrl } from '../imaging/ids/imageIds.js';
import { awaitVolumeLoad, fetchHeader, readScalarData } from '../imaging/grid/volumeLoading.js';
import { indexToWorldLps } from '../imaging/geometry/orientation.js';
import { bootstrapPanoramic } from '../imaging/panoramic/bootstrap.js';
import { canvasBlob, projectStrips, stripCanvas } from '../imaging/panoramic/bake.js';
import { openArchWorker } from '../imaging/panoramic/archWorker.js';
import { createArchViewport } from '../imaging/panoramic/archViewport.js';
import { createCprViewport } from '../imaging/panoramic/cprViewport.js';
import { createVolumeSupply, fetchSegmentation } from '../imaging/panoramic/volumeSupply.js';
import { CONTROL_IDS, controlPlan, setSlice } from '../imaging/panoramic/controls.js';

export const SURFACE = 'panoramic-cpr';

/**
 * The engine the volume grid already owns.
 *
 * F6: the context pool holds seven, and the grid uses four. Attaching the two panoramic
 * viewports to the same engine keeps the page inside that budget; a second engine would
 * open a second pool and a maxillo page would be asking for eleven contexts.
 */
export const GRID_ENGINE_ID = 'ygg-volume-grid';

function engine() {
    return getRenderingEngine(GRID_ENGINE_ID) ?? new RenderingEngine(GRID_ENGINE_ID);
}

/**
 * Invert the RAS affine, so a world point can be read back as the slice pixels the baker
 * speaks.
 *
 * Only the in-plane part is needed -- the slice index is known -- so this is a 2x2 solve
 * rather than a general inverse, and it fails loudly on a degenerate affine rather than
 * returning coordinates that look like arch.
 */
export function planeInverse(affine, sliceIndex) {
    const origin = indexToWorldLps(affine, [0, 0, sliceIndex]);
    const ex = indexToWorldLps(affine, [1, 0, sliceIndex]).map((value, axis) => value - origin[axis]);
    const ey = indexToWorldLps(affine, [0, 1, sliceIndex]).map((value, axis) => value - origin[axis]);
    // The two in-plane axes of an axial slice, projected onto the two world axes that vary
    // across it. A CBCT's axial plane is x/y in both frames, so this is well conditioned.
    const determinant = ex[0] * ey[1] - ex[1] * ey[0];
    if (!determinant) {
        throw new Error('The axial plane is degenerate; the arch cannot be read back.');
    }
    return (world) => {
        const dx = world[0] - origin[0];
        const dy = world[1] - origin[1];
        return [
            (dx * ey[1] - dy * ey[0]) / determinant,
            (dy * ex[0] - dx * ex[1]) / determinant,
        ];
    };
}

/**
 * Build the surface: the volume, the worker, the two viewports and the bake.
 *
 * Everything Cornerstone- or vtk-shaped is resolved here and handed to
 * `bootstrapPanoramic` as plain functions, which is what lets the whole state machine be
 * driven in `node --test` against fakes.
 */
async function mount({ plan, data, source, onReady, onGeometry, onError }) {
    await initImaging();
    const core = globalThis.Seg2PanoCore;
    if (!core) {
        throw new Error('The panoramic reconstruction core is not loaded.');
    }

    const namespace = data.projectNamespace || 'api';
    const origin = globalThis.location?.origin;
    const primary = primaryVolumeFrom(data);
    if (!primary) {
        throw new Error('This patient has no CBCT to reconstruct from.');
    }
    const url = volumeUrl({ ...primary, bundleKey: primary.bundleKey, namespace, origin });
    const volumeId = volumeIdFor(url);

    const supply = createVolumeSupply({
        cornerstone: { cache, awaitVolumeLoad, readScalarData, fetchHeader },
        volumeId,
        url,
    });
    const descriptor = await supply.descriptor();
    if (!descriptor) {
        throw new Error('The CBCT is still loading.');
    }

    const segmentation = await fetchSegmentation({
        source, volumeUrl, cache: {}, namespace, origin,
    });

    const renderingEngine = engine();
    const cpr = createCprViewport({
        element: plan.cprStage,
        cornerstone: { renderingEngine, coreEnums },
        vtk: { vtkImageCPRMapper, vtkImageSlice, vtkPolyData, vtkDataArray },
    });
    cpr.setVolume(cache.getVolume(volumeId));

    const arch = createArchViewport({
        element: plan.axialStage,
        cornerstone: {
            renderingEngine, coreEnums, toolsEnums, addTool, ToolGroupManager, annotation,
            setVolumesForViewports,
            tools: { PanTool, ZoomTool, SplineROITool },
        },
        vtk: { vtkImageData, vtkDataArray, vtkImageMapper, vtkImageSlice },
        onArchEdited: (points) => surface?.editArch(points),
        onArchDragged: (points) => surface?.dragArch(points),
    });
    await arch.setVolume(volumeId);

    const toWorld = (point) => indexToWorldLps(descriptor.affine, [point[0], point[1], currentSlice]);
    let currentSlice = 0;
    let toIndex = planeInverse(descriptor.affine, 0);
    arch.bindEditing((world) => toIndex(world));

    const worker = openArchWorker({
        segmentation,
        raw: {
            dimensions: descriptor.dimensions,
            affine: descriptor.affine,
            flipZ: descriptor.flipZ,
        },
        onReady,
        onError,
        onGeometry: (geometry) => {
            currentSlice = geometry.z;
            toIndex = planeInverse(descriptor.affine, geometry.z);
            onGeometry(geometry);
        },
    });

    let surface = null;
    const mounted = {
        descriptor,
        worker,
        arch,
        cpr,
        core,
        worldFor: toWorld,
        projectStrips: (options) => projectStrips({ ...options, core }),
        encode: (strips) => ({
            mip: stripCanvas(strips.mip, strips.width, strips.height, core),
            raysum: stripCanvas(strips.raysum, strips.width, strips.height, core),
        }),
        encodeBlobs: async (canvases) => ({
            mip: await canvasBlob(canvases.mip),
            raysum: await canvasBlob(canvases.raysum),
        }),
        paint(canvas, slice) {
            const target = plan.resultCanvas;
            if (!target) {
                return;
            }
            target.width = canvas.width;
            target.height = canvas.height;
            const context = target.getContext('2d');
            context.drawImage(canvas, 0, 0);
            // The Z locator: which slice the arch is on, drawn over the strip it produced.
            const y = Math.max(0.5, Math.min(target.height - 0.5, slice + 0.5));
            context.save();
            context.strokeStyle = '#f6b84a';
            context.lineWidth = 1;
            context.beginPath();
            context.moveTo(0, y);
            context.lineTo(target.width, y);
            context.stroke();
            context.restore();
        },
        destroy() {
            worker.terminate();
            arch.destroy();
            cpr.destroy();
            supply.release();
        },
    };
    mounted.adopt = (created) => { surface = created; };
    return mounted;
}

/** Wire the toolbar to the surface. Ids only from `controls.js`, never spelled here. */
function bindControls(doc, surface) {
    const plan = controlPlan(doc);
    const on = (element, event, handler) => element?.addEventListener?.(event, handler);
    on(plan.zSlider, 'input', (event) => setSlice(plan, Number(event.target.value)));
    on(plan.zSlider, 'change', (event) => surface.setSlice(Number(event.target.value)));
    on(plan.prevZ, 'click', () => surface.setSlice(surface.state().slice - 1));
    on(plan.nextZ, 'click', () => surface.setSlice(surface.state().slice + 1));
    on(plan.resetAuto, 'click', () => surface.resetAuto());
    on(plan.save, 'click', () => surface.save());
    on(plan.retry, 'click', () => surface.activate());
    for (const button of plan.modes) {
        on(button, 'click', () => surface.setMode(button.dataset.panorexMode));
    }
}

async function start() {
    const surface = await bootstrapPanoramic({ mount });
    if (surface) {
        bindControls(document, surface);
    }
}

if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }
}

export { CONTROL_IDS, bootstrapPanoramic, mount };
