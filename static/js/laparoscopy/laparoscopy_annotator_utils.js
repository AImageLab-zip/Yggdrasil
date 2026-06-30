(function () {
    'use strict';

    function _pointToSegDist(px, py, ax, ay, bx, by) {
        var dx = bx - ax, dy = by - ay;
        if (dx === 0 && dy === 0) {
            dx = px - ax; dy = py - ay;
            return Math.sqrt(dx*dx + dy*dy);
        }
        var t  = Math.max(0, Math.min(1, ((px-ax)*dx + (py-ay)*dy) / (dx*dx + dy*dy)));
        var cx = ax + t*dx, cy = ay + t*dy;
        dx = px - cx; dy = py - cy;
        return Math.sqrt(dx*dx + dy*dy);
    }

    window.LaparoscopyAnnotatorUtils = {
        FRAME_TOLERANCE: 0.020,
        FRAME_STEP_S:    0.033,
        PALETTE: [
            '#e74c3c', '#3498db', '#2ecc71', '#f39c12',
            '#9b59b6', '#1abc9c', '#e67e22', '#34495e',
            '#e91e63', '#00bcd4', '#8bc34a', '#ff5722',
        ],

        el: function (id) {
            return document.getElementById(id);
        },

        on: function (id, event, fn) {
            var el = document.getElementById(id);
            if (el) el.addEventListener(event, fn);
        },

        fmtTime: function (t) {
            var mm = Math.floor(t / 60);
            var ss = Math.floor(t % 60);
            var ms = Math.floor((t % 1) * 1000);
            return String(mm).padStart(2, '0') + ':' +
                   String(ss).padStart(2, '0') + '.' +
                   String(ms).padStart(3, '0');
        },

        openColorPicker: function (initialColor, onChange) {
            var colorInput = document.createElement('input');
            colorInput.type = 'color';
            colorInput.value = initialColor;
            colorInput.style.position = 'absolute';
            colorInput.style.left = '-9999px';
            colorInput.style.top = '-9999px';
            document.body.appendChild(colorInput);
            colorInput.addEventListener('change', function () {
                onChange(colorInput.value);
                if (colorInput.parentNode) colorInput.parentNode.removeChild(colorInput);
            });
            colorInput.addEventListener('cancel', function () {
                if (colorInput.parentNode) colorInput.parentNode.removeChild(colorInput);
            });
            colorInput.click();
        },

        rdpSimplify: function (points, epsilon) {
            if (points.length <= 4) return points;
            var U = window.LaparoscopyAnnotatorUtils;
            var n  = points.length / 2;
            var ax = points[0],       ay = points[1];
            var bx = points[(n-1)*2], by = points[(n-1)*2+1];
            var maxDist = 0, maxIdx = 0;
            for (var i = 1; i < n - 1; i++) {
                var d = _pointToSegDist(points[i*2], points[i*2+1], ax, ay, bx, by);
                if (d > maxDist) { maxDist = d; maxIdx = i; }
            }
            if (maxDist <= epsilon) return [ax, ay, bx, by];
            var left  = U.rdpSimplify(points.slice(0, (maxIdx+1)*2), epsilon);
            var right = U.rdpSimplify(points.slice(maxIdx*2), epsilon);
            return left.slice(0, -2).concat(right);
        },

        polygonArea: function (flatPoints) {
            var n = flatPoints.length / 2;
            if (n < 3) return 0;
            var area = 0;
            for (var i = 0; i < n; i++) {
                var j  = (i + 1) % n;
                area  += flatPoints[i*2] * flatPoints[j*2+1];
                area  -= flatPoints[j*2] * flatPoints[i*2+1];
            }
            return area * 0.5;
        },
    };
})();
