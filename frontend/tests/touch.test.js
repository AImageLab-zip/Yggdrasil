/**
 * Touch input for the viewers (`imaging/runtime/touch.js`).
 *
 * Phones reach the viewers through the public demo. Touch is spelled as extra tool
 * bindings, never as a rendering flag -- `isMobile` and the crosshair's `mobile` profile
 * stay off, and `renderingBudget.test.js` / `grid.test.js` still pin that.
 *
 * The surfaces are read as text for the same reason `renderingBudget.test.js` does it:
 * they import Cornerstone, which does not load under `node --test`.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import {
    PHONE_QUERY,
    PINCH_PANS,
    VIEW_SELECTOR,
    VIEW_TOUCH_POINTS,
    enableTwoFingerNavigation,
    installPhoneTouchPolicy,
    isPhoneViewport,
    pinchCentre,
    pinchScale,
    touchZoomBindings,
    trackPinch,
} from '../imaging/runtime/touch.js';
import { RENDERING_CONFIG } from '../imaging/runtime/config.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const IMAGING = join(HERE, '..', 'imaging');
const REPO = join(HERE, '..', '..');

const read = (...parts) => readFileSync(join(IMAGING, ...parts), 'utf8');

test('two fingers are the view: pinch zooms and pans in one gesture', () => {
    assert.equal(VIEW_TOUCH_POINTS, 2);
    assert.deepEqual(touchZoomBindings(), [{ numTouchPoints: 2 }]);
    // Touch bindings carry no mouse button, so they never replace a mouse binding.
    assert.equal(touchZoomBindings()[0].mouseButton, undefined);
    assert.deepEqual({ ...PINCH_PANS }, { pan: true });

    const calls = [];
    const group = {
        setToolActive: (name, options) => calls.push(['active', name, options]),
        setToolConfiguration: (name, config) => calls.push(['config', name, config]),
    };
    enableTwoFingerNavigation(group, 'Zoom');
    assert.deepEqual(calls, [
        ['active', 'Zoom', { bindings: [{ numTouchPoints: 2 }] }],
        ['config', 'Zoom', PINCH_PANS],
    ]);
});

function touchEvent(target, fingers) {
    return {
        target,
        touches: { length: fingers },
        cancelable: true,
        stopped: false,
        prevented: false,
        stopPropagation() { this.stopped = true; },
        preventDefault() { this.prevented = true; },
    };
}

function policyRig({ phone = true } = {}) {
    const listeners = {};
    const doc = { addEventListener: (type, fn, opts) => { listeners[type] = { fn, opts }; } };
    installPhoneTouchPolicy(doc, (query) => ({ matches: phone && query === PHONE_QUERY }));
    const inView = { closest: (sel) => (sel === VIEW_SELECTOR ? {} : null) };
    const outside = { closest: () => null };
    return { doc, listeners, inView, outside };
}

test('on a phone, one finger on a viewer is kept from the viewer so the page scrolls', () => {
    const { listeners, inView } = policyRig();
    for (const type of ['touchstart', 'touchmove']) {
        const e = touchEvent(inView, 1);
        listeners[type].fn(e);
        assert.ok(e.stopped, `${type}: the viewer never sees it`);
        assert.ok(!e.prevented, `${type}: the browser is free to scroll`);
    }
    // Capture, so it runs before Cornerstone's own touchstart; not passive, so the
    // two-finger branch can cancel.
    assert.deepEqual(listeners.touchstart.opts, { capture: true, passive: false });
});

test('on a phone, two fingers on a viewer are the viewer\'s and the page stays still', () => {
    const { listeners, inView } = policyRig();
    const e = touchEvent(inView, 2);
    listeners.touchstart.fn(e);
    assert.ok(e.prevented);
    assert.ok(!e.stopped, 'the viewer still receives it');
});

test('the policy ignores touches off the viewers, and everything above phone width', () => {
    const { listeners, outside } = policyRig();
    const off = touchEvent(outside, 1);
    listeners.touchstart.fn(off);
    assert.ok(!off.stopped && !off.prevented);

    const desktop = policyRig({ phone: false });
    const e = touchEvent(desktop.inView, 1);
    desktop.listeners.touchstart.fn(e);
    assert.ok(!e.stopped && !e.prevented, 'a touchscreen workstation keeps its input');
});

test('the policy installs once per document', () => {
    const { doc, listeners } = policyRig();
    const first = listeners.touchstart.fn;
    installPhoneTouchPolicy(doc, () => ({ matches: true }));
    assert.equal(listeners.touchstart.fn, first);
    assert.equal(isPhoneViewport(() => ({ matches: true })), true);
    assert.equal(isPhoneViewport(undefined), false);
});

test('the phone CSS lets the browser pan from a viewer despite Cornerstone\'s inline none', () => {
    const css = readFileSync(join(REPO, 'static', 'css', 'theme.css'), 'utf8');
    assert.match(css, /\[data-viewport-uid\],\s*\[data-touch-view\] \{ touch-action: pan-x pan-y !important; \}/);
    // And nothing reinstates `none` on a whole viewer window on phones.
    const grid = readFileSync(join(REPO, 'static', 'css', 'viewer_grid.css'), 'utf8');
    assert.doesNotMatch(grid, /\.viewer-window\.loaded \{\s*touch-action: none/);
});

test('touch never turns on the rendering-side mobile flag', () => {
    assert.equal(RENDERING_CONFIG.isMobile, false);
    assert.doesNotMatch(read('runtime', 'touch.js'), /isMobile\s*:/);
});

test('every Cornerstone surface gets two-finger navigation, and init installs the policy', () => {
    const surfaces = {
        grid: read('grid', 'viewportManager.js'),
        photos: read('photos', 'stackViewport.js'),
        panoramic: read('panoramic', 'archViewport.js'),
        mesh: read('mesh', 'meshViewport.js'),
    };
    for (const [name, source] of Object.entries(surfaces)) {
        assert.match(source, /from '\.\.\/runtime\/touch\.js'/, `${name} imports touch.js`);
        assert.match(source, /enableTwoFingerNavigation\(/, `${name} pinches and pans`);
    }
    // The grid has two groups, slices and the volume render, and both navigate.
    assert.equal((surfaces.grid.match(/enableTwoFingerNavigation\(/g) ?? []).length, 2);
    assert.match(read('runtime', 'init.js'), /installPhoneTouchPolicy\(\)/);
});

test('Cornerstone still resolves touch tools by numTouchPoints, one finger by Primary', () => {
    // The whole scheme rests on this dispatcher: a binding matches when its
    // `numTouchPoints` equals the fingers down, or -- for one finger -- when it is the
    // default Primary mouse binding. A version bump that changes either leaves every
    // test above green and the phone viewers dead.
    const dispatcher = readFileSync(
        join(REPO, 'node_modules', '@cornerstonejs', 'tools', 'dist', 'esm',
            'eventDispatchers', 'shared', 'getActiveToolForTouchEvent.js'),
        'utf8'
    );
    assert.match(dispatcher, /binding\.numTouchPoints === numTouchPoints/);
    assert.match(dispatcher, /numTouchPoints === 1 &&\s*binding\.mouseButton === defaultMousePrimary/);

    const zoom = readFileSync(
        join(REPO, 'node_modules', '@cornerstonejs', 'tools', 'dist', 'esm', 'tools', 'ZoomTool.js'),
        'utf8'
    );
    assert.match(zoom, /pinchToZoom: true/);
});

test('pinchScale is the ratio of finger spreads, and 1 on degenerate input', () => {
    const a = [{ x: 0, y: 0 }, { x: 10, y: 0 }];
    const b = [{ x: 0, y: 0 }, { x: 20, y: 0 }];
    assert.equal(pinchScale(a, b), 2);
    assert.equal(pinchScale(b, a), 0.5);
    const same = [{ x: 5, y: 5 }, { x: 5, y: 5 }];
    assert.equal(pinchScale(same, b), 1);
    assert.equal(pinchScale(a, same), 1);
    assert.equal(pinchScale([], b), 1);
    assert.equal(pinchScale(undefined, b), 1);
});

test('pinchCentre is the midpoint', () => {
    assert.deepEqual(pinchCentre([{ x: 0, y: 10 }, { x: 20, y: 30 }]), { x: 10, y: 20 });
});

function pointer(type, pointerId, clientX, clientY) {
    const e = new Event(type);
    Object.assign(e, { pointerId, clientX, clientY });
    return e;
}

test('trackPinch reports scale and centre only while exactly two fingers are down', () => {
    const element = new EventTarget();
    const calls = [];
    const pinch = trackPinch(element, (scale, centre, previousCentre) => calls.push({ scale, centre, previousCentre }));

    element.dispatchEvent(pointer('pointerdown', 1, 0, 0));
    assert.equal(pinch.isPinching(), false);
    element.dispatchEvent(pointer('pointermove', 1, 5, 0));
    assert.equal(calls.length, 0, 'one finger is not a pinch');

    element.dispatchEvent(pointer('pointerdown', 2, 15, 0));
    assert.equal(pinch.isPinching(), true);
    element.dispatchEvent(pointer('pointermove', 2, 25, 0));
    assert.equal(calls.length, 1);
    assert.equal(calls[0].scale, 2);
    assert.deepEqual(calls[0].centre, { x: 15, y: 0 });
    assert.deepEqual(calls[0].previousCentre, { x: 10, y: 0 });

    element.dispatchEvent(pointer('pointerup', 2, 25, 0));
    assert.equal(pinch.isPinching(), false);
    element.dispatchEvent(pointer('pointermove', 1, 0, 0));
    assert.equal(calls.length, 1, 'lifting a finger ends the pinch');

    // A cancelled pointer (the OS took the gesture) is forgotten like a lifted one.
    element.dispatchEvent(pointer('pointerdown', 3, 50, 0));
    assert.equal(pinch.isPinching(), true);
    element.dispatchEvent(pointer('pointercancel', 3, 50, 0));
    assert.equal(pinch.isPinching(), false);

    pinch.destroy();
    element.dispatchEvent(pointer('pointerdown', 4, 0, 0));
    element.dispatchEvent(pointer('pointerdown', 5, 10, 0));
    assert.equal(pinch.isPinching(), false, 'destroy removes the listeners');
});

test('two fingers moving together, without spreading, still pan', () => {
    const element = new EventTarget();
    const calls = [];
    trackPinch(element, (scale, centre, previousCentre) => calls.push({ scale, centre, previousCentre }));
    element.dispatchEvent(pointer('pointerdown', 1, 0, 0));
    element.dispatchEvent(pointer('pointerdown', 2, 10, 0));
    element.dispatchEvent(pointer('pointermove', 1, 0, 20));
    element.dispatchEvent(pointer('pointermove', 2, 10, 20));
    assert.equal(calls.length, 2);
    assert.deepEqual(calls[1].centre, { x: 5, y: 20 });
    // Fingers report one at a time, so single steps spread and close; the gesture as
    // a whole kept its spread, so the zoom nets out and only the pan remains.
    const net = calls.reduce((product, call) => product * call.scale, 1);
    assert.ok(Math.abs(net - 1) < 1e-9);
});

test('the WSI viewer opts into the policy and leaves one phone finger to the page', () => {
    const source = read('wsi', 'wsiViewport.js');
    assert.match(source, /element\.style\.touchAction = 'none'/);
    assert.match(source, /element\.dataset\.touchView = ''/);
    assert.match(source, /installPhoneTouchPolicy\(\)/);
    assert.match(source, /e\.pointerType === 'touch' && isPhoneViewport\(\)\) return;/);
    assert.match(source, /addEventListener\('pointercancel'/);
    assert.match(source, /const pinch = trackPinch\(element/);
    // Registered before the one-finger pointerdown, so the second finger already
    // counts by the time that handler sees it.
    const pinchAt = source.indexOf('const pinch = trackPinch(element');
    const dragAt = source.indexOf("element.addEventListener('pointerdown'");
    assert.ok(pinchAt > 0 && pinchAt < dragAt);
    assert.match(source, /pinch\.destroy\(\)/);
});
