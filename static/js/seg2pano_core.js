/*
 * Dependency-free numerical core for segmentation-to-panorex generation.
 * Ported from seg2pano reference commit 1e7dcfb.
 */
(function(root, factory) {
    'use strict';
    var api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    root.Seg2PanoCore = api;
})(typeof self !== 'undefined' ? self : globalThis, function() {
    'use strict';

    var MANDIBLE_LABELS = new Set([1, 31, 32, 33, 34, 35, 36, 37, 38, 41, 42, 43, 44, 45, 46, 47, 48]);
    var CANAL_LABELS = new Set([3, 4]);

    function clamp(value, low, high) {
        return Math.max(low, Math.min(high, value));
    }

    function roundHalfEven(value) {
        var floor = Math.floor(value);
        var fraction = value - floor;
        if (fraction < 0.5) return floor;
        if (fraction > 0.5) return floor + 1;
        return floor % 2 === 0 ? floor : floor + 1;
    }

    function canonicalZToNative(z, depth, flipZ) {
        return flipZ ? depth - 1 - z : z;
    }

    function rawDerivedFlipZ(affine) {
        return !!(affine && affine[2] && Number(affine[2][2]) > 0);
    }

    function autoSelectZ(seg, dimensions, flipZ) {
        var width = dimensions.width;
        var height = dimensions.height;
        var depth = dimensions.depth;
        var plane = width * height;
        var canalCounts = new Uint32Array(depth);
        var jawSlice = new Uint8Array(depth);
        var canalTotal = 0;
        var jawSliceTotal = 0;

        for (var nativeZ = 0; nativeZ < depth; nativeZ++) {
            var z = flipZ ? depth - 1 - nativeZ : nativeZ;
            var start = nativeZ * plane;
            var canalCount = 0;
            var hasJaw = false;
            for (var i = start, end = start + plane; i < end; i++) {
                var label = seg[i];
                if (CANAL_LABELS.has(label)) canalCount++;
                if (label === 1) hasJaw = true;
            }
            canalCounts[z] = canalCount;
            canalTotal += canalCount;
            if (hasJaw) {
                jawSlice[z] = 1;
                jawSliceTotal++;
            }
        }

        if (canalTotal) {
            var leftRank = Math.floor((canalTotal - 1) / 2);
            var rightRank = Math.floor(canalTotal / 2);
            var cumulative = 0;
            var leftZ = 0;
            var rightZ = 0;
            var foundLeft = false;
            for (var canalZ = 0; canalZ < depth; canalZ++) {
                cumulative += canalCounts[canalZ];
                if (cumulative > leftRank && !foundLeft) {
                    leftZ = canalZ;
                    foundLeft = true;
                }
                if (cumulative > rightRank) {
                    rightZ = canalZ;
                    break;
                }
            }
            return Math.trunc((leftZ + rightZ) / 2);
        }

        if (!jawSliceTotal) throw new Error('Segmentation has neither IAN canal nor lower-jaw voxels.');
        var jawSum = 0;
        for (var jawZ = 0; jawZ < depth; jawZ++) if (jawSlice[jawZ]) jawSum += jawZ;
        return roundHalfEven(jawSum / jawSliceTotal);
    }

    function mandibleMask(seg, dimensions, z, flipZ) {
        var width = dimensions.width;
        var height = dimensions.height;
        var depth = dimensions.depth;
        if (z < 0 || z >= depth) throw new Error('Z slice is outside the volume.');
        var nativeZ = canonicalZToNative(z, depth, flipZ);
        var start = nativeZ * width * height;
        var mask = new Uint8Array(width * height);
        var count = 0;
        for (var i = 0; i < mask.length; i++) {
            if (MANDIBLE_LABELS.has(seg[start + i])) {
                mask[i] = 1;
                count++;
            }
        }
        if (!count) throw new Error('No mandible voxels are present on Z ' + z + '.');
        return mask;
    }

    function ellipseOffsets(size) {
        var radius = Math.floor(size / 2);
        var offsets = [];
        for (var y = -radius; y <= radius; y++) {
            var normalized = radius ? y / radius : 0;
            var extent = roundHalfEven(radius * Math.sqrt(Math.max(0, 1 - normalized * normalized)));
            for (var x = -extent; x <= extent; x++) offsets.push([x, y]);
        }
        return offsets;
    }

    function dilate(mask, width, height, offsets) {
        var output = new Uint8Array(mask.length);
        for (var y = 0; y < height; y++) {
            for (var x = 0; x < width; x++) {
                var index = y * width + x;
                for (var k = 0; k < offsets.length; k++) {
                    var sx = x + offsets[k][0];
                    var sy = y + offsets[k][1];
                    if (sx >= 0 && sx < width && sy >= 0 && sy < height && mask[sy * width + sx]) {
                        output[index] = 1;
                        break;
                    }
                }
            }
        }
        return output;
    }

    function erode(mask, width, height, offsets) {
        var output = new Uint8Array(mask.length);
        for (var y = 0; y < height; y++) {
            for (var x = 0; x < width; x++) {
                var keep = true;
                for (var k = 0; k < offsets.length; k++) {
                    var sx = x + offsets[k][0];
                    var sy = y + offsets[k][1];
                    if (sx >= 0 && sx < width && sy >= 0 && sy < height && !mask[sy * width + sx]) {
                        keep = false;
                        break;
                    }
                }
                if (keep) output[y * width + x] = 1;
            }
        }
        return output;
    }

    function ellipticalClose(mask, width, height, size) {
        var offsets = ellipseOffsets(size || 15);
        return erode(dilate(mask, width, height, offsets), width, height, offsets);
    }

    function largestComponent(mask, width, height, foreground) {
        var visited = new Uint8Array(mask.length);
        var queue = new Int32Array(mask.length);
        var best = [];
        var neighbors = [[-1, -1], [0, -1], [1, -1], [-1, 0], [1, 0], [-1, 1], [0, 1], [1, 1]];
        var target = foreground === undefined ? 1 : foreground;

        for (var seed = 0; seed < mask.length; seed++) {
            if (visited[seed] || mask[seed] !== target) continue;
            var head = 0;
            var tail = 0;
            var component = [];
            queue[tail++] = seed;
            visited[seed] = 1;
            while (head < tail) {
                var index = queue[head++];
                component.push(index);
                var x = index % width;
                var y = Math.floor(index / width);
                for (var d = 0; d < neighbors.length; d++) {
                    var nx = x + neighbors[d][0];
                    var ny = y + neighbors[d][1];
                    if (nx < 0 || nx >= width || ny < 0 || ny >= height) continue;
                    var next = ny * width + nx;
                    if (!visited[next] && mask[next] === target) {
                        visited[next] = 1;
                        queue[tail++] = next;
                    }
                }
            }
            if (component.length > best.length) best = component;
        }

        var output = new Uint8Array(mask.length);
        for (var i = 0; i < best.length; i++) output[best[i]] = 1;
        return output;
    }

    function fillHoles(mask, width, height) {
        var inverse = new Uint8Array(mask.length);
        for (var i = 0; i < mask.length; i++) inverse[i] = mask[i] ? 0 : 1;
        var background = largestComponent(inverse, width, height, 1);
        var filled = new Uint8Array(mask.length);
        for (var j = 0; j < mask.length; j++) filled[j] = background[j] ? 0 : 1;
        return filled;
    }

    function crossSkeleton(mask, width, height) {
        var image = new Uint8Array(mask);
        var skeleton = new Uint8Array(mask.length);
        var cross = [[0, 0], [-1, 0], [1, 0], [0, -1], [0, 1]];
        while (true) {
            var eroded = erode(image, width, height, cross);
            var opened = dilate(eroded, width, height, cross);
            var remaining = 0;
            for (var i = 0; i < image.length; i++) {
                if (image[i] && !opened[i]) skeleton[i] = 1;
                if (eroded[i]) remaining++;
            }
            if (!remaining) return skeleton;
            image = eroded;
        }
    }

    function solveLeastSquares(columns, values) {
        var rows = values.length;
        var count = columns.length;
        var q = new Array(count);
        var r = new Array(count);
        var col;
        for (var j = 0; j < count; j++) {
            r[j] = new Float64Array(count);
            col = new Float64Array(columns[j]);
            for (var k = 0; k < j; k++) {
                var dot = 0;
                for (var i = 0; i < rows; i++) dot += q[k][i] * col[i];
                r[k][j] = dot;
                for (var subtract = 0; subtract < rows; subtract++) col[subtract] -= dot * q[k][subtract];
            }
            var norm = 0;
            for (var n = 0; n < rows; n++) norm += col[n] * col[n];
            norm = Math.sqrt(norm);
            if (norm < 1e-12) throw new Error('Polynomial fit is rank deficient.');
            r[j][j] = norm;
            for (var normalize = 0; normalize < rows; normalize++) col[normalize] /= norm;
            q[j] = col;
        }
        var qty = new Float64Array(count);
        for (var qi = 0; qi < count; qi++) {
            for (var row = 0; row < rows; row++) qty[qi] += q[qi][row] * values[row];
        }
        var coefficients = new Float64Array(count);
        for (var back = count - 1; back >= 0; back--) {
            var value = qty[back];
            for (var right = back + 1; right < count; right++) value -= r[back][right] * coefficients[right];
            coefficients[back] = value / r[back][back];
        }
        return coefficients;
    }

    function polyfit(points, degree) {
        if (!points || points.length < degree + 1) throw new Error('Too few points for degree-' + degree + ' fit.');
        var minX = Infinity;
        var maxX = -Infinity;
        for (var i = 0; i < points.length; i++) {
            minX = Math.min(minX, points[i][0]);
            maxX = Math.max(maxX, points[i][0]);
        }
        var center = (minX + maxX) / 2;
        var scale = (maxX - minX) / 2 || 1;
        var columns = new Array(degree + 1);
        for (var column = 0; column <= degree; column++) columns[column] = new Float64Array(points.length);
        var values = new Float64Array(points.length);
        for (var row = 0; row < points.length; row++) {
            var normalizedX = (points[row][0] - center) / scale;
            var power = 1;
            for (var powerIndex = 0; powerIndex <= degree; powerIndex++) {
                columns[powerIndex][row] = power;
                power *= normalizedX;
            }
            values[row] = points[row][1];
        }
        return { coefficients: Array.from(solveLeastSquares(columns, values)), center: center, scale: scale, degree: degree };
    }

    function evaluatePolynomial(poly, x) {
        var nx = (x - poly.center) / poly.scale;
        var result = 0;
        for (var i = poly.coefficients.length - 1; i >= 0; i--) result = result * nx + poly.coefficients[i];
        return result;
    }

    function fitArchPolynomial(mask, width, height) {
        var closed = ellipticalClose(mask, width, height, 15);
        var component = largestComponent(closed, width, height, 1);
        var filled = fillHoles(component, width, height);
        var skeleton = crossSkeleton(filled, width, height);
        var points = [];
        for (var i = 0; i < skeleton.length; i++) if (skeleton[i]) points.push([i % width, Math.floor(i / width)]);
        if (points.length < 3) throw new Error('Mandible skeleton is too small to fit an arch.');
        return { polynomial: polyfit(points, 2), start: 0, end: width, mask: component, skeleton: skeleton };
    }

    function archLines(poly, start, end, offset) {
        var distance = offset === undefined ? 50 : offset;
        var delta = 0.3;
        var coords = [];
        var x = start + 1;
        var iterations = 0;
        while (x < end) {
            var fx = evaluatePolynomial(poly, x);
            if (fx > 0) coords.push([x, fx]);
            var tangent = (evaluatePolynomial(poly, x + delta / 2) - evaluatePolynomial(poly, x - delta / 2)) / delta;
            x += Math.sqrt(1 / (tangent * tangent + 1));
            if (++iterations > 1000000) throw new Error('Arch sampling did not converge.');
        }

        var low = [];
        var high = [];
        var derivative = [];
        for (var i = 0; i < coords.length; i++) {
            var point = coords[i];
            var slope = (evaluatePolynomial(poly, point[0] + delta / 2) - evaluatePolynomial(poly, point[0] - delta / 2)) / delta;
            if (Math.abs(slope) <= 1e-8) {
                low.push([point[0], point[1] + distance]);
                high.push([point[0], point[1] - distance]);
                derivative.push(0);
                continue;
            }
            var normal = -1 / slope;
            var cosine = Math.sqrt(1 / (normal * normal + 1));
            var sine = Math.sqrt(normal * normal / (normal * normal + 1));
            if (normal > 0) {
                low.push([point[0] + distance * cosine, point[1] + distance * sine]);
                high.push([point[0] - distance * cosine, point[1] - distance * sine]);
            } else {
                low.push([point[0] - distance * cosine, point[1] + distance * sine]);
                high.push([point[0] + distance * cosine, point[1] - distance * sine]);
            }
            derivative.push(normal);
        }
        return { low: low, coords: coords, high: high, derivative: derivative };
    }

    function extractControlPoints(coords, desiredCount) {
        if (!coords || coords.length < 4) throw new Error('Arch line has too few points for a spline.');
        var count = desiredCount || 10;
        var offset = Math.floor(coords.length / count);
        if (offset < 1) offset = 1;
        var cp = [coords[0]];
        for (var i = 1; i < coords.length - 1; i += offset) cp.push(coords[i]);
        cp.push(coords[coords.length - 2]);
        cp.push(coords[coords.length - 1]);
        return cp.map(function(point) { return [Number(point[0]), Number(point[1])]; });
    }

    function linspace(start, end, count) {
        if (count <= 0) return [];
        if (count === 1) return [start];
        var values = new Array(count);
        for (var i = 0; i < count; i++) values[i] = start + (end - start) * i / (count - 1);
        return values;
    }

    function interpolatePoint(a, b, ta, tb, t) {
        var denominator = tb - ta;
        if (Math.abs(denominator) < 1e-12) return [a[0], a[1]];
        return [
            (tb - t) / denominator * a[0] + (t - ta) / denominator * b[0],
            (tb - t) / denominator * a[1] + (t - ta) / denominator * b[1]
        ];
    }

    function catmullRomSegment(p0, p1, p2, p3) {
        function nextT(t, a, b) {
            var dx = b[0] - a[0];
            var dy = b[1] - a[1];
            return t + Math.pow(dx * dx + dy * dy, 0.25);
        }
        var t0 = 0;
        var t1 = nextT(t0, p0, p1);
        var t2 = nextT(t1, p1, p2);
        var t3 = nextT(t2, p2, p3);
        var pointCount = Math.trunc(Math.hypot(p2[0] - p1[0], p2[1] - p1[1]));
        if (!pointCount || t1 === t0 || t2 === t1 || t3 === t2) return [];
        return linspace(t1, t2, pointCount).map(function(t) {
            var a1 = interpolatePoint(p0, p1, t0, t1, t);
            var a2 = interpolatePoint(p1, p2, t1, t2, t);
            var a3 = interpolatePoint(p2, p3, t2, t3, t);
            var b1 = interpolatePoint(a1, a2, t0, t2, t);
            var b2 = interpolatePoint(a2, a3, t1, t3, t);
            return interpolatePoint(b1, b2, t1, t2, t);
        });
    }

    function catmullRomChain(controlPoints) {
        var curves = [];
        for (var i = 0; i < controlPoints.length - 3; i++) {
            var segment = catmullRomSegment(controlPoints[i], controlPoints[i + 1], controlPoints[i + 2], controlPoints[i + 3]);
            for (var j = 0; j < segment.length; j++) if (Number.isFinite(segment[j][0])) curves.push(segment[j]);
        }
        return curves;
    }

    function polynomialFromControlPoints(controlPoints) {
        var spline = catmullRomChain(controlPoints);
        if (spline.length < 13) throw new Error('Edited spline has too few samples for degree-12 fitting.');
        var minX = Infinity;
        var maxX = -Infinity;
        for (var i = 0; i < spline.length; i++) {
            minX = Math.min(minX, spline[i][0]);
            maxX = Math.max(maxX, spline[i][0]);
        }
        return { polynomial: polyfit(spline, 12), start: minX, end: maxX, spline: spline };
    }

    function slabCoordinates(lines, intervals) {
        var count = intervals === undefined ? 40 : intervals;
        var side = new Array(lines.coords.length);
        for (var column = 0; column < lines.coords.length; column++) {
            var high = lines.high[column];
            var low = lines.low[column];
            var sign = lines.derivative[column] > 0 ? 1 : -1;
            var xStep = Math.abs(high[0] - low[0]) / count;
            var yStep = Math.abs(high[1] - low[1]) / count;
            var points = new Array(count + 1);
            for (var i = 0; i <= count; i++) points[i] = [high[0] + sign * i * xStep, high[1] + i * yStep];
            side[column] = points;
        }
        return side;
    }

    function buildAutoGeometry(mask, width, height) {
        var fitted = fitArchPolynomial(mask, width, height);
        var baseLines = archLines(fitted.polynomial, fitted.start, fitted.end, 50);
        var cp = extractControlPoints(baseLines.coords, 10);
        var spline = catmullRomChain(cp);
        var projectionLines = archLines(fitted.polynomial, fitted.start, fitted.end, 20);
        return {
            source: 'auto', polynomial: fitted.polynomial, start: fitted.start, end: fitted.end,
            controlPoints: cp, spline: spline, centerline: projectionLines.coords,
            slab: slabCoordinates(projectionLines, 40), cleanedMask: fitted.mask, skeleton: fitted.skeleton
        };
    }

    function buildEditedGeometry(controlPoints) {
        var fitted = polynomialFromControlPoints(controlPoints);
        var projectionLines = archLines(fitted.polynomial, fitted.start, fitted.end, 20);
        return {
            source: 'custom_cp', polynomial: fitted.polynomial, start: fitted.start, end: fitted.end,
            controlPoints: controlPoints.map(function(point) { return [Number(point[0]), Number(point[1])]; }),
            spline: fitted.spline, centerline: projectionLines.coords, slab: slabCoordinates(projectionLines, 40)
        };
    }

    function bilinearAt(data, dimensions, x, y, z, flipZ, slope, intercept) {
        var width = dimensions.width;
        var height = dimensions.height;
        var x1 = Math.floor(x);
        var x2 = x1 + 1;
        var y1 = Math.floor(y);
        var y2 = y1 + 1;
        // NumPy permits negative indices and raises on positive overflow; the
        // reference catches that overflow and leaves the sample at zero.
        if (x1 < -width || y1 < -height || x2 >= width || y2 >= height) return 0;
        var nativeZ = canonicalZToNative(z, dimensions.depth, flipZ);
        var sampleX1 = x1 < 0 ? width + x1 : x1;
        var sampleX2 = x2 < 0 ? width + x2 : x2;
        var sampleY1 = y1 < 0 ? height + y1 : y1;
        var sampleY2 = y2 < 0 ? height + y2 : y2;
        var dx = x - x1;
        var dy = y - y1;
        var start = nativeZ * width * height;
        var p1 = data[start + sampleY1 * width + sampleX1];
        var p2 = data[start + sampleY2 * width + sampleX1];
        var p3 = data[start + sampleY1 * width + sampleX2];
        var p4 = data[start + sampleY2 * width + sampleX2];
        var value = p1 * (1 - dx) * (1 - dy) + p2 * (1 - dx) * dy + p3 * dx * (1 - dy) + p4 * dx * dy;
        return value * (slope || 1) + (intercept || 0);
    }

    function projectColumnPair(data, dimensions, slab, column, outputMip, outputRaysum, flipZ, slope, intercept) {
        var points = slab[column];
        var outputWidth = slab.length;
        for (var z = 0; z < dimensions.depth; z++) {
            var maximum = -Infinity;
            var clippedSum = 0;
            for (var sample = 0; sample < points.length; sample++) {
                var value = bilinearAt(data, dimensions, points[sample][0], points[sample][1], z, flipZ, slope, intercept);
                if (value > maximum) maximum = value;
                clippedSum += Math.max(0, value);
            }
            var index = z * outputWidth + column;
            outputMip[index] = maximum;
            outputRaysum[index] = clippedSum;
        }
    }

    function normalizeOpenCV(values) {
        var min = Infinity;
        var max = -Infinity;
        for (var i = 0; i < values.length; i++) {
            if (values[i] < min) min = values[i];
            if (values[i] > max) max = values[i];
        }
        var output = new Uint8Array(values.length);
        if (!Number.isFinite(min) || max <= min) return output;
        var factor = 255 / (max - min);
        // The reference converts cv2.normalize's float output with astype(uint8),
        // which truncates rather than applying Canvas/Uint8Clamped rounding.
        for (var j = 0; j < values.length; j++) output[j] = clamp(Math.trunc((values[j] - min) * factor), 0, 255);
        return output;
    }

    return {
        MANDIBLE_LABELS: MANDIBLE_LABELS,
        CANAL_LABELS: CANAL_LABELS,
        clamp: clamp,
        roundHalfEven: roundHalfEven,
        rawDerivedFlipZ: rawDerivedFlipZ,
        canonicalZToNative: canonicalZToNative,
        autoSelectZ: autoSelectZ,
        mandibleMask: mandibleMask,
        ellipticalClose: ellipticalClose,
        largestComponent: largestComponent,
        fillHoles: fillHoles,
        crossSkeleton: crossSkeleton,
        polyfit: polyfit,
        evaluatePolynomial: evaluatePolynomial,
        fitArchPolynomial: fitArchPolynomial,
        archLines: archLines,
        extractControlPoints: extractControlPoints,
        catmullRomSegment: catmullRomSegment,
        catmullRomChain: catmullRomChain,
        polynomialFromControlPoints: polynomialFromControlPoints,
        slabCoordinates: slabCoordinates,
        buildAutoGeometry: buildAutoGeometry,
        buildEditedGeometry: buildEditedGeometry,
        bilinearAt: bilinearAt,
        projectColumnPair: projectColumnPair,
        normalizeOpenCV: normalizeOpenCV
    };
});
