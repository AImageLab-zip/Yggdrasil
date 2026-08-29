/**
 * CBCTConvert - Front-end manager for in-browser CBCT conversion.
 * Orchestrates worker execution for MetaImage and NIfTI repair.
 *
 * **DICOM is not converted.** It is uploaded as-is and stored as DICOM by
 * `common/dicom/ingest.py` (Phase 8). Everything that used to turn a selected DICOM
 * folder into a .nii.gz -- and throw the series away -- is deleted; a `.dcm` or an
 * extensionless file now falls through to the server untouched, which is the point.
 */

(function (root, factory) {
    'use strict';
    var api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    root.CBCTConvert = api;
}(typeof window !== 'undefined' ? window : globalThis, function () {
    'use strict';

    function isMetaImageFile(file) {
        var name = (file.name || '').toLowerCase();
        return name.endsWith('.mha');
    }

    function isNiftiFile(file) {
        var name = (file.name || '').toLowerCase();
        return name.endsWith('.nii') || name.endsWith('.nii.gz');
    }

    function readFileAsArrayBuffer(file) {
        return new Promise(function (resolve, reject) {
            var reader = new FileReader();
            reader.onload = function () { resolve(reader.result); };
            reader.onerror = function () { reject(new Error('Failed to read file: ' + file.name)); };
            reader.readAsArrayBuffer(file);
        });
    }

    /**
     * Convert a MetaImage or a raw NIfTI into a compressed NIfTI (.nii.gz) File object.
     *
     * DICOM is not accepted: it is stored natively and never converted.
     *
     * @param {FileList|File[]} files
     * @param {Object} [options]
     * @param {Function} [options.onProgress]
     * @param {Function} [options.onNeedsOrientation] - Called when a file lacks orientation metadata
     * @returns {Promise<{file: File, orientation: string}>}
     */
    function convertFiles(files, options) {
        options = options || {};
        var onProgress = options.onProgress || function () {};
        var onNeedsOrientation = options.onNeedsOrientation || function () {
            return Promise.resolve('RAS'); // Default orientation choice
        };

        var fileList = Array.from(files || []);
        if (!fileList.length) {
            return Promise.reject(new Error('No files selected for conversion.'));
        }

        var metaImageFiles = fileList.filter(isMetaImageFile);
        var niftiFiles = fileList.filter(isNiftiFile);

        var worker = new Worker('/static/js/worker/cbct_convert_worker.js');

        return new Promise(function (resolve, reject) {
            function cleanup() {
                try { worker.terminate(); } catch (e) {}
            }

            function handleWorkerResponse(data, bufferGetter) {
                if (data.ok) {
                    onProgress(100, 'Conversion complete!');
                    var arrayBuffer = data.buffer;
                    var blob = new Blob([arrayBuffer], { type: 'application/octet-stream' });
                    var convertedFile = new File([blob], 'cbct_patient.nii.gz', { type: 'application/octet-stream' });
                    cleanup();
                    resolve({ file: convertedFile, orientation: data.orientation });
                } else if (data.error === 'NEEDS_ORIENTATION') {
                    onProgress(50, 'Orientation metadata required...');
                    onNeedsOrientation(data)
                        .then(function (orientation) {
                            onProgress(60, 'Applying chosen orientation (' + orientation + ')...');
                            bufferGetter().then(function (buffer) {
                                worker.postMessage({
                                    type: 'PROCESS_NIFTI',
                                    buffer: buffer,
                                    requestedOrientation: orientation
                                }, [buffer]);
                            }).catch(function (err) {
                                cleanup();
                                reject(err);
                            });
                        })
                        .catch(function (err) {
                            cleanup();
                            reject(err);
                        });
                } else {
                    cleanup();
                    reject(new Error(data.error || 'In-browser conversion failed.'));
                }
            }

            worker.onmessage = function (event) {
                handleWorkerResponse(event.data, function () {
                    return readFileAsArrayBuffer(niftiFiles[0]);
                });
            };

            worker.onerror = function (event) {
                cleanup();
                reject(new Error('Worker execution error: ' + (event.message || 'Unknown error')));
            };

            if (metaImageFiles.length > 0) {
                onProgress(10, 'Reading MetaImage file...');
                readFileAsArrayBuffer(metaImageFiles[0])
                    .then(function (buffer) {
                        onProgress(40, 'Converting MetaImage to NIfTI...');
                        worker.postMessage({ type: 'CONVERT_METAIMAGE', buffer: buffer }, [buffer]);
                    })
                    .catch(function (err) {
                        cleanup();
                        reject(err);
                    });
            } else if (niftiFiles.length > 0) {
                onProgress(10, 'Reading NIfTI file...');
                readFileAsArrayBuffer(niftiFiles[0])
                    .then(function (buffer) {
                        onProgress(30, 'Inspecting NIfTI orientation metadata...');
                        worker.postMessage({ type: 'PROCESS_NIFTI', buffer: buffer }, [buffer]);
                    })
                    .catch(function (err) {
                        cleanup();
                        reject(err);
                    });
            } else {
                cleanup();
                reject(new Error('Selected file(s) are not supported here. This converter handles NIfTI (.nii) and MetaImage (.mha); DICOM is uploaded as-is.'));
            }
        });
    }

    return {
        convertFiles: convertFiles,
        isMetaImageFile: isMetaImageFile,
        isNiftiFile: isNiftiFile
    };
}));
