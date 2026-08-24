/**
 * CBCTConvert - Front-end manager for in-browser CBCT conversion.
 * Orchestrates worker execution for DICOM series, MetaImage, and NIfTI repair.
 */

(function (root, factory) {
    'use strict';
    var api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    root.CBCTConvert = api;
}(typeof window !== 'undefined' ? window : globalThis, function () {
    'use strict';

    function isDicomFile(file) {
        var name = (file.name || '').toLowerCase();
        return name.endsWith('.dcm') || name.endsWith('.dicom') || !name.includes('.');
    }

    /**
     * Whether a buffer carries the DICOM 'DICM' marker at byte 128.
     *
     * `isDicomFile` has to classify by name (files are not read yet), and it
     * treats every extensionless file as DICOM so that DICOM directories work.
     * Once the bytes are in hand this confirms it, so a folder of unrelated
     * extensionless files reports "not DICOM" instead of the misleading
     * "No valid DICOM slices could be parsed".
     */
    function isDicomBuffer(arrayBuffer) {
        if (!arrayBuffer || arrayBuffer.byteLength < 132) return false;
        var marker = new Uint8Array(arrayBuffer, 128, 4);
        return marker[0] === 0x44 && marker[1] === 0x49 && marker[2] === 0x43 && marker[3] === 0x4d;
    }

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
     * Convert selected files into a compressed NIfTI (.nii.gz) File object.
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

        var dicomFiles = fileList.filter(isDicomFile);
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

            if (dicomFiles.length > 0) {
                onProgress(10, 'Reading DICOM files (' + dicomFiles.length + ')...');
                Promise.all(dicomFiles.map(readFileAsArrayBuffer))
                    .then(function (buffers) {
                        var dicomBuffers = buffers.filter(isDicomBuffer);
                        if (!dicomBuffers.length) {
                            throw new Error(
                                'None of the ' + buffers.length + ' selected file(s) are DICOM ' +
                                '(no DICM marker). Select a DICOM folder, or a .nii.gz / .nii / .mha volume.'
                            );
                        }
                        onProgress(40, 'Converting DICOM series to NIfTI...');
                        worker.postMessage(
                            { type: 'CONVERT_DICOM_SERIES', buffers: dicomBuffers },
                            dicomBuffers.slice()
                        );
                    })
                    .catch(function (err) {
                        cleanup();
                        reject(err);
                    });
            } else if (metaImageFiles.length > 0) {
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
                reject(new Error('Selected file(s) are not supported. Please select DICOM, NIfTI, or MetaImage (.mha).'));
            }
        });
    }

    return {
        convertFiles: convertFiles,
        isDicomFile: isDicomFile,
        isDicomBuffer: isDicomBuffer,
        isMetaImageFile: isMetaImageFile,
        isNiftiFile: isNiftiFile
    };
}));
