/**
 * WSIConvert - Front-end coordinator for in-browser gigapixel JPEG to pyramidal BigTIFF conversion.
 * Leverages WebAssembly libvips (wasm-vips) in a background Web Worker.
 */

(function (root, factory) {
    'use strict';
    var api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    root.WSIConvert = api;
}(typeof window !== 'undefined' ? window : globalThis, function () {
    'use strict';

    function isConvertibleJpeg(file) {
        if (!file || !file.name) return false;
        var name = file.name.toLowerCase();
        return name.endsWith('.jpg') || name.endsWith('.jpeg');
    }

    /**
     * Convert a flat gigapixel JPEG into a multi-resolution pyramidal BigTIFF (.tiff) File object.
     *
     * @param {File} file
     * @param {Object} [options]
     * @param {Function} [options.onProgress] - Callback (percent, message)
     * @returns {Promise<{file: File}>}
     */
    function convertJpegToTiff(file, options) {
        options = options || {};
        var onProgress = options.onProgress || function () {};

        if (!isConvertibleJpeg(file)) {
            return Promise.resolve({ file: file });
        }

        return new Promise(function (resolve, reject) {
            var reader = new FileReader();

            reader.onload = function () {
                var arrayBuffer = reader.result;
                var worker;
                try {
                    worker = new Worker('/static/js/worker/wsi_convert_worker.js');
                } catch (workerErr) {
                    reject(new Error('Failed to start WSI conversion worker: ' + workerErr.message));
                    return;
                }

                function cleanup() {
                    try { worker.terminate(); } catch (e) {}
                }

                worker.onmessage = function (event) {
                    var data = event.data;
                    if (!data) return;

                    if (data.type === 'PROGRESS') {
                        onProgress(data.percent, data.message);
                    } else if (data.ok) {
                        cleanup();
                        onProgress(100, 'Pyramidal conversion complete!');
                        var tiffBlob = new Blob([data.buffer], { type: 'image/tiff' });
                        var baseName = file.name.replace(/\.[^/.]+$/, '');
                        var convertedFile = new File([tiffBlob], baseName + '.tiff', { type: 'image/tiff' });
                        resolve({ file: convertedFile });
                    } else {
                        cleanup();
                        reject(new Error(data.error || 'Conversion failed'));
                    }
                };

                worker.onerror = function (err) {
                    cleanup();
                    reject(new Error('WSI Conversion Worker error: ' + (err.message || 'unknown error')));
                };

                onProgress(5, 'Preparing gigapixel scan for conversion...');
                worker.postMessage({
                    type: 'CONVERT_JPG_TO_TIFF',
                    buffer: arrayBuffer,
                    filename: file.name
                }, [arrayBuffer]);
            };

            reader.onerror = function () {
                reject(new Error('Failed to read JPEG file: ' + file.name));
            };

            reader.readAsArrayBuffer(file);
        });
    }

    return {
        isConvertibleJpeg: isConvertibleJpeg,
        convertJpegToTiff: convertJpegToTiff
    };
}));
