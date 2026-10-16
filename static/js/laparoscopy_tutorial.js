/**
 * Laparoscopy patient page — the Tutorial button's guided tour (driver.js, loaded by the
 * template from jsDelivr).
 *
 * One tour: the quadrant timeline and labels, then it opens Annotation Mode itself (the
 * "Next" on that step clicks the page's own toggle) and walks the drawing tools. When the
 * tour ends it closes Annotation Mode again if it was the one that opened it. Opening it
 * is a real entry: on a project with no labels yet the page creates the first one.
 */
(function () {
    'use strict';

    var button = document.getElementById('tutorial-btn');
    var toggle = document.getElementById('annotation-toggle-btn');
    if (!button || !toggle) { return; }

    var openedByTour = false;

    // The page disables the toggle while Annotation Mode opens or closes.
    function whenIdle(callback) {
        (function check() {
            if (toggle.disabled) { window.setTimeout(check, 100); } else { callback(); }
        })();
    }

    function isOpen() {
        return toggle.dataset.active === 'true';
    }

    function openAnnotationMode(element, step, opts) {
        if (isOpen()) { opts.driver.moveNext(); return; }
        if (toggle.disabled) { return; }
        openedByTour = true;
        toggle.click();
        var next = document.querySelector('.ygg-tour .driver-popover-next-btn');
        if (next) { next.textContent = 'Opening…'; }
        whenIdle(function () {
            if (!opts.driver.isActive()) { return; }
            if (isOpen()) { opts.driver.moveNext(); } else { opts.driver.destroy(); }
        });
    }

    var STEPS = [
        {
            element: '#video-timeline',
            popover: {
                title: 'Playback',
                description: 'Play and pause, drag the slider to scrub, or jump &minus;10 / &minus;1 / +1 / +10 ' +
                    'seconds.'
            }
        },
        {
            element: '#quadrant-timeline',
            popover: {
                title: 'Quadrant timeline',
                description: 'Which abdominal quadrant the camera is in as the procedure goes on. A quadrant ' +
                    'starts at its marker and runs until the next one.'
            }
        },
        {
            element: '[data-quadrant-mark]',
            popover: {
                title: 'Mark a quadrant',
                description: 'Pick a quadrant in the list, move the playhead to where the camera enters it, ' +
                    'then press <b>Mark</b>.'
            }
        },
        {
            element: '#quadrant-bar',
            popover: {
                title: 'Fine-tune markers',
                description: 'Drag a needle (or focus it and use &larr; / &rarr;) to move a marker. ' +
                    'Right-click the bar or a needle to jump to, change or remove one.'
            }
        },
        {
            element: '#labels-card',
            popover: {
                title: 'Labels',
                description: '<b>Segmentation</b> holds the regions you paint, <b>Quadrants</b> the ones the ' +
                    'timeline uses. Click a swatch to pick your own colour (only you see it); project ' +
                    'admins add, rename and remove labels.'
            }
        },
        {
            element: '.side-panel',
            popover: {
                title: 'Files, Settings, Activity',
                description: '<b>Files</b>: the uploaded video, its processed versions and extra uploads. ' +
                    '<b>Settings</b>: this scan\'s tags. <b>Activity</b>: what happened to it and when.'
            }
        },
        {
            element: '#annotation-toggle-btn',
            popover: {
                title: 'Annotation Mode',
                description: 'Freezes the frame you are paused on so you can paint masks on it. ' +
                    '<b>Next</b> opens it for you.',
                onNextClick: openAnnotationMode
            }
        },
        {
            element: '#segment-viewport',
            popover: {
                title: 'Paint on the frame',
                description: 'Left mouse button paints. Right-drag or Ctrl + scroll zooms, middle-drag pans. ' +
                    'The buttons at the top right of the toolbar zoom, reset the view and undo / redo ' +
                    '(<b>Ctrl+Z</b> / <b>Ctrl+Y</b>).'
            }
        },
        {
            element: '#segment-toolbar [aria-label="Segmentation tools"]',
            popover: {
                title: 'Drawing tools',
                description: '<b>Brush</b> paints the selected label, <b>Eraser</b> removes it. ' +
                    '<b>Polygon</b>: click to place points, click the first one (or press Enter) to fill; ' +
                    'Backspace removes a point, Esc cancels.'
            }
        },
        {
            element: '#segment-size-row',
            popover: {
                title: 'Brush size',
                description: 'Sets the brush and eraser width. You paint with the label selected in the ' +
                    '<b>Labels</b> card.'
            }
        },
        {
            element: '#assist-card',
            popover: {
                title: 'Assist',
                description: 'Choose <b>Segment by clicking</b>, then click the object on the frame: the ' +
                    'model paints it into the selected label. <b>Add</b> (green) / <b>Remove</b> (red) ' +
                    'set what the next click does; each click refines the mask, and undo works as usual. ' +
                    'The result is also tracked onto the next <b>N</b> frames, wherever the label is still empty.'
            }
        },
        {
            element: '#video-timeline',
            popover: {
                title: 'Other frames',
                description: '&larr; / &rarr; step one frame; the slider and &minus;10 &hellip; +10 jump further.'
            }
        },
        {
            element: '#annotation-toggle-btn',
            popover: {
                title: 'Saving and leaving',
                description: 'Your work saves automatically (the status is under the frame). This button ' +
                    'takes you back to the video.'
            }
        }
    ];

    // In Annotation Mode the page steps frames on ←/→ keydown; driver.js navigates on keyup.
    // Swallow the keydown while the tour is open so one key press does not do both.
    function swallowArrows(event) {
        if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
            event.stopPropagation();
        }
    }

    button.addEventListener('click', function () {
        if (!window.driver || !window.driver.js) {
            if (typeof window.appNotify === 'function') {
                window.appNotify('warning', 'The tutorial could not load. Check your connection and reload the page.');
            }
            return;
        }
        openedByTour = false;
        window.addEventListener('keydown', swallowArrows, true);
        window.driver.js.driver({
            steps: STEPS,
            skipMissingElement: true,  // a read-only user has no Mark button
            showProgress: true,
            progressText: '{{current}} of {{total}}',
            nextBtnText: 'Next',
            prevBtnText: 'Back',
            doneBtnText: 'Done',
            popoverClass: 'ygg-tour',
            stagePadding: 6,
            stageRadius: 8,
            onDestroyed: function () {
                window.removeEventListener('keydown', swallowArrows, true);
                if (openedByTour) {
                    whenIdle(function () { if (isOpen()) { toggle.click(); } });
                }
            }
        }).drive();
    });
})();
