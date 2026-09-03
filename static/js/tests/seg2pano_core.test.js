'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const core = require('../seg2pano_core.js');

test('auto Z uses canal voxel median and raw-derived canonical flip', () => {
    const dimensions = { width: 2, height: 2, depth: 5 };
    const seg = new Uint8Array(20);
    seg[0] = 3;
    seg[1] = 3;
    seg[16] = 4;
    assert.equal(core.autoSelectZ(seg, dimensions, false), 0);
    assert.equal(core.autoSelectZ(seg, dimensions, true), 4);
    assert.equal(core.rawDerivedFlipZ([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]), true);
});

test('jaw fallback averages occupied jaw slices and mask includes lower labels only', () => {
    const dimensions = { width: 3, height: 2, depth: 4 };
    const seg = new Uint8Array(24);
    seg[0] = 1;
    seg[12] = 31;
    seg[18] = 1;
    seg[19] = 48;
    seg[20] = 11;
    assert.equal(core.autoSelectZ(seg, dimensions, false), 2);
    assert.deepEqual(Array.from(core.mandibleMask(seg, dimensions, 3, false)), [1, 1, 0, 0, 0, 0]);
});

test('8-connected component and hole filling preserve diagonal connectivity', () => {
    const mask = new Uint8Array([
        1, 0, 0, 0, 0,
        0, 1, 1, 1, 0,
        0, 1, 0, 1, 0,
        0, 1, 1, 1, 0,
        0, 0, 0, 0, 1
    ]);
    const largest = core.largestComponent(mask, 5, 5, 1);
    assert.equal(largest.reduce((sum, value) => sum + value, 0), 10);
    const filled = core.fillHoles(largest, 5, 5);
    assert.equal(filled[12], 1);
});

test('stable quadratic and degree-12 fitting evaluate their source curves', () => {
    const quadratic = [];
    for (let x = -20; x <= 20; x++) quadratic.push([x, 2 + 3 * x + 0.5 * x * x]);
    const fit2 = core.polyfit(quadratic, 2);
    assert.ok(Math.abs(core.evaluatePolynomial(fit2, 7.5) - 52.625) < 1e-8);

    const degree12 = [];
    for (let i = 0; i < 80; i++) {
        const x = i / 79 * 2 - 1;
        degree12.push([x, Math.sin(x) + 0.02 * Math.pow(x, 12)]);
    }
    const fit12 = core.polyfit(degree12, 12);
    assert.ok(Math.abs(core.evaluatePolynomial(fit12, 0.37) - (Math.sin(0.37) + 0.02 * Math.pow(0.37, 12))) < 1e-9);
});

test('arch, centripetal Catmull-Rom and slab semantics match the reference shape', () => {
    const points = [];
    for (let x = 0; x < 100; x++) points.push([x, 40 + 0.01 * (x - 50) * (x - 50)]);
    const poly = core.polyfit(points, 2);
    const lines = core.archLines(poly, 0, 100, 20);
    const cp = core.extractControlPoints(lines.coords, 10);
    const spline = core.catmullRomChain(cp);
    const slab = core.slabCoordinates(lines, 40);
    assert.ok(lines.coords.length > 90);
    assert.ok(spline.length > 50);
    assert.equal(slab.length, lines.coords.length);
    assert.equal(slab[0].length, 41);
});

test('bilinear projection computes true maximum, clipped raysum and independent normalization', () => {
    const dimensions = { width: 2, height: 2, depth: 2 };
    const data = new Float32Array([
        -4, 0,
        4, 8,
        0, 4,
        8, 12
    ]);
    assert.equal(core.bilinearAt(data, dimensions, 0.5, 0.5, 0, false, 1, 0), 2);
    assert.equal(core.bilinearAt(data, dimensions, -0.5, 0, 0, false, 1, 0), -2);
    assert.equal(core.bilinearAt(data, dimensions, 1.5, 0, 0, false, 1, 0), 0);
    const slab = [[[0, 0], [0.5, 0.5]], [[0.5, 0], [0.5, 0.5]]];
    const mip = new Float32Array(4);
    const raysum = new Float32Array(4);
    core.projectColumnPair(data, dimensions, slab, 0, mip, raysum, false, 1, 0);
    assert.equal(mip[0], 2);
    assert.equal(raysum[0], 2);
    assert.deepEqual(Array.from(core.normalizeOpenCV(new Float32Array([10, 20, 30]))), [0, 127, 255]);
    assert.deepEqual(Array.from(core.normalizeOpenCV(new Float32Array([4, 4]))), [0, 0]);
});

test('MIP and raysum project across every one of the 41 slab samples', () => {
    const dimensions = { width: 43, height: 2, depth: 1 };
    const data = new Float32Array(dimensions.width * dimensions.height);
    const points = [];
    for (let x = 0; x < 41; x++) {
        points.push([x, 0]);
        data[x] = x - 20;
    }
    const mip = new Float32Array(1);
    const raysum = new Float32Array(1);
    core.projectColumnPair(data, dimensions, [points], 0, mip, raysum, false, 1, 0);
    assert.equal(mip[0], 20);
    assert.equal(raysum[0], 210);
});
