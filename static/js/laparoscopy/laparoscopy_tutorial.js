(function () {
    'use strict';

    var STYLE_ID = 'laparoscopy-tour-style';
    var activeTour = null;

    function injectStyles() {
        if (document.getElementById(STYLE_ID)) return;

        var style = document.createElement('style');
        style.id = STYLE_ID;
        style.textContent = [
            '.lap-tour-backdrop{position:fixed;inset:0;background:transparent;z-index:2030;}',
            '.lap-tour-highlight{position:fixed;border:3px solid #0dcaf0;border-radius:12px;box-shadow:0 0 0 9999px rgba(15,23,42,.58),0 0 0 6px rgba(13,202,240,.22);z-index:2040;pointer-events:none;transition:all .16s ease;}',
            '.lap-tour-card{position:fixed;z-index:2050;width:min(360px,calc(100vw - 28px));background:#fff;border:1px solid rgba(15,23,42,.12);border-radius:14px;box-shadow:0 18px 45px rgba(15,23,42,.24);padding:1rem;}',
            '.lap-tour-card h5{font-size:1rem;margin:0 0 .35rem;font-weight:700;color:#172033;}',
            '.lap-tour-card p{font-size:.9rem;margin:0;color:#475467;line-height:1.45;}',
            '.lap-tour-progress{font-size:.76rem;color:#667085;font-weight:600;letter-spacing:.03em;text-transform:uppercase;}',
            '.lap-tour-actions{display:flex;justify-content:space-between;gap:.5rem;margin-top:.95rem;}',
            '.lap-tour-actions .btn{min-width:4.75rem;}',
            '@media (max-width:575.98px){.lap-tour-card{left:14px!important;right:14px!important;top:auto!important;bottom:14px!important;width:auto;}}'
        ].join('');
        document.head.appendChild(style);
    }

    function visible(el) {
        return !!(el && el.getClientRects && el.getClientRects().length);
    }

    function annotator() {
        return window.__laparoscopyAnnotator || null;
    }

    function ensureAnnotationMode(tour) {
        var instance = annotator();
        if (!instance || instance.annotationMode) return;
        tour.openedAnnotationMode = true;
        if (typeof instance._enterAnnotationMode === 'function') instance._enterAnnotationMode();
    }

    function restoreAnnotationMode(tour) {
        var instance = annotator();
        if (!tour.openedAnnotationMode || !instance || !instance.annotationMode) return;
        if (typeof instance._exitAnnotationMode === 'function') instance._exitAnnotationMode();
    }

    function stepsForTour(tour) {
        return [
            {
                selector: '#video-player-wrap',
                title: 'Watch the video',
                body: 'This is the surgery video. First pause on the frame you want to describe, then use the tools to mark what you see.'
            },
            {
                selector: '#frame-nav-bar',
                title: 'Move frame by frame',
                body: 'Use these buttons to move by 1 or 10 seconds. Example: pause the video, press +1s until the organ is clearly visible, then start drawing.'
            },
            {
                selector: '#annotation-toggle-btn',
                title: 'Annotation mode',
                body: 'Click this to start marking the video. The tutorial will turn it on now so you can see the tools.'
            },
            {
                selector: '#region-types-panel',
                title: 'Choose what you are marking',
                before: function () { ensureAnnotationMode(tour); },
                body: 'Pick the class before drawing. Example: choose Fegato before marking the liver, or colecisti before marking the gallbladder.'
            },
            {
                selector: '#annotation-toolbar [data-tool="brush"]',
                title: 'Brush tool',
                before: function () { ensureAnnotationMode(tour); },
                body: 'Use the brush like a marker pen. Example: select Fegato, choose the brush, then paint over the liver area on the current frame.'
            },
            {
                selector: '#brush-size-input',
                title: 'Brush size',
                before: function () { ensureAnnotationMode(tour); },
                body: 'Make the brush smaller for borders and bigger for large areas. Example: use a large brush to fill an organ, then a small brush near edges.'
            },
            {
                selector: '#annotation-toolbar [data-tool="polygon"]',
                title: 'Polygon tool',
                before: function () { ensureAnnotationMode(tour); },
                body: 'Use polygon when an area has clear borders. Example: click around the gallbladder edge, then press Enter to close the shape.'
            },
            {
                selector: '#annotation-toolbar [data-tool="eraser"]',
                title: 'Eraser tool',
                before: function () { ensureAnnotationMode(tour); },
                body: 'Use the eraser to fix mistakes. Example: if the brush goes outside the organ, select eraser and remove only the wrong part.'
            },
            {
                selector: '#annotation-toolbar [data-tool="pan"]',
                title: 'Pan and zoom',
                before: function () { ensureAnnotationMode(tour); },
                body: 'Zoom in when details are small, then use pan to move around. Example: zoom on a vessel, pan to center it, then draw more precisely.'
            },
            {
                selector: '#temporal-classification-bar',
                title: 'Mark video sections',
                before: function () { ensureAnnotationMode(tour); },
                body: 'Use this timeline when the video changes section. Example: move to the moment a new quadrant starts, choose the quadrant, and click Add Marker.'
            },
            {
                selector: '#magic-toolbox-panel',
                title: 'Magic Toolbox overview',
                before: function () { ensureAnnotationMode(tour); },
                body: 'Magic helps create a mask from a few clicks. It is useful when an organ has a complex shape and drawing it by hand would take too long.'
            },
            {
                selector: '#magic-tool-point-btn',
                title: 'Magic Point Tool',
                before: function () { ensureAnnotationMode(tour); },
                body: 'Click Point Tool, then click on the video. Example: choose Fegato, click Point Tool, then place points on the liver.'
            },
            {
                selector: '#magic-point-positive-btn',
                title: 'Positive points',
                before: function () { ensureAnnotationMode(tour); },
                body: 'Use + for pixels that are inside the object you want. Example: place two or three + points well inside the liver, not on the border.'
            },
            {
                selector: '#magic-point-negative-btn',
                title: 'Negative points',
                before: function () { ensureAnnotationMode(tour); },
                body: 'Use - for pixels that must stay outside the object. Example: if the AI includes stomach by mistake, place - points on the stomach.'
            },
            {
                selector: '#magic-window-seconds-input',
                title: 'Propagation window',
                before: function () { ensureAnnotationMode(tour); },
                body: 'This number controls how many seconds after the current frame Magic should try to follow the object. Start small, like 3 to 5 seconds.'
            },
            {
                selector: '#magic-send-prompts-btn',
                title: 'Send to Magic',
                before: function () { ensureAnnotationMode(tour); },
                body: 'Press Send after adding + and optional - points. Wait for the result, then correct it with more points, brush, or eraser if needed.'
            },
            {
                selector: '#shapes-list-panel',
                title: 'Review your marks',
                before: function () { ensureAnnotationMode(tour); },
                body: 'Every mark you create appears here. Use this list to check what you already drew on the current frame.'
            }
        ];
    }

    function buildTour() {
        injectStyles();

        var tour = {
            index: 0,
            openedAnnotationMode: false,
            steps: null,
            backdrop: document.createElement('div'),
            highlight: document.createElement('div'),
            card: document.createElement('div')
        };
        tour.steps = stepsForTour(tour);
        tour.backdrop.className = 'lap-tour-backdrop';
        tour.highlight.className = 'lap-tour-highlight';
        tour.card.className = 'lap-tour-card';
        return tour;
    }

    function targetFor(step) {
        return document.querySelector(step.selector);
    }

    function positionTour(tour, target) {
        var margin = 12;
        var rect = target.getBoundingClientRect();
        var card = tour.card;
        var highlight = tour.highlight;

        highlight.style.left = Math.max(8, rect.left - 6) + 'px';
        highlight.style.top = Math.max(8, rect.top - 6) + 'px';
        highlight.style.width = Math.max(24, rect.width + 12) + 'px';
        highlight.style.height = Math.max(24, rect.height + 12) + 'px';

        var cardRect = card.getBoundingClientRect();
        var below = rect.bottom + margin;
        var above = rect.top - cardRect.height - margin;
        var top = below + cardRect.height < window.innerHeight ? below : Math.max(12, above);
        var left = Math.min(
            Math.max(12, rect.left),
            Math.max(12, window.innerWidth - cardRect.width - 12)
        );

        card.style.left = left + 'px';
        card.style.top = top + 'px';
    }

    function render(tour) {
        var step = tour.steps[tour.index];
        if (!step) return finish(tour);
        if (typeof step.before === 'function') step.before();

        var target = targetFor(step);
        if (!visible(target)) {
            if (tour.index < tour.steps.length - 1) {
                tour.index += 1;
                render(tour);
            } else {
                finish(tour);
            }
            return;
        }

        target.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'nearest' });

        tour.card.innerHTML = [
            '<div class="d-flex justify-content-between align-items-center mb-2">',
            '<span class="lap-tour-progress">Step ', String(tour.index + 1), ' of ', String(tour.steps.length), '</span>',
            '<button type="button" class="btn-close" aria-label="Close tutorial" data-tour-close></button>',
            '</div>',
            '<h5>', step.title, '</h5>',
            '<p>', step.body, '</p>',
            '<div class="lap-tour-actions">',
            '<button type="button" class="btn btn-sm btn-outline-secondary" data-tour-prev ', tour.index === 0 ? 'disabled' : '', '>Back</button>',
            '<div class="d-flex gap-2">',
            '<button type="button" class="btn btn-sm btn-outline-secondary" data-tour-skip>Skip</button>',
            '<button type="button" class="btn btn-sm btn-info text-white" data-tour-next>', tour.index === tour.steps.length - 1 ? 'Done' : 'Next', '</button>',
            '</div>',
            '</div>'
        ].join('');

        window.setTimeout(function () {
            positionTour(tour, target);
        }, 180);
    }

    function finish(tour) {
        if (!tour) return;
        restoreAnnotationMode(tour);
        [tour.backdrop, tour.highlight, tour.card].forEach(function (el) { el.remove(); });
        window.removeEventListener('resize', tour.reposition);
        window.removeEventListener('scroll', tour.reposition, true);
        document.removeEventListener('keydown', tour.onKeydown);
        activeTour = null;
    }

    function startTour() {
        if (activeTour) finish(activeTour);

        var btn = document.getElementById('laparoscopy-tour-btn');
        if (!btn || btn.classList.contains('d-none')) return;

        var tour = buildTour();
        activeTour = tour;
        document.body.appendChild(tour.backdrop);
        document.body.appendChild(tour.highlight);
        document.body.appendChild(tour.card);

        tour.reposition = function () {
            var step = tour.steps[tour.index];
            var target = step ? targetFor(step) : null;
            if (visible(target)) positionTour(tour, target);
        };
        tour.onKeydown = function (event) {
            if (event.key === 'Escape') finish(tour);
            if (event.key === 'ArrowRight') {
                event.preventDefault();
                tour.index += 1;
                render(tour);
            }
            if (event.key === 'ArrowLeft' && tour.index > 0) {
                event.preventDefault();
                tour.index -= 1;
                render(tour);
            }
        };

        window.addEventListener('resize', tour.reposition);
        window.addEventListener('scroll', tour.reposition, true);
        document.addEventListener('keydown', tour.onKeydown);
        render(tour);
    }

    document.addEventListener('click', function (event) {
        var start = event.target.closest('#laparoscopy-tour-btn');
        if (start) {
            startTour();
            return;
        }

        if (!activeTour) return;
        if (event.target.closest('[data-tour-close]') || event.target.closest('[data-tour-skip]')) {
            finish(activeTour);
            return;
        }
        if (event.target.closest('[data-tour-prev]')) {
            activeTour.index = Math.max(0, activeTour.index - 1);
            render(activeTour);
            return;
        }
        if (event.target.closest('[data-tour-next]')) {
            activeTour.index += 1;
            render(activeTour);
        }
    });
})();
