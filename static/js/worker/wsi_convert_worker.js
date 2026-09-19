/**
 * Web Worker for in-browser gigapixel JPEG to Pyramidal BigTIFF conversion using WebAssembly libvips.
 */

/* global importScripts, Vips */

var vipsInstance = null;

async function getVips() {
    if (!vipsInstance) {
        importScripts('/static/vendor/vips/vips.js');
        var vipsScriptUrl = new URL('/static/vendor/vips/vips.js', self.location.origin).href;

        var initPromise = self.Vips({
            mainScriptUrlOrBlob: vipsScriptUrl,
            dynamicLibraries: [],
            locateFile: function (fileName) {
                return '/static/vendor/vips/' + fileName;
            },
            print: function (msg) {
                console.log('[wasm-vips]', msg);
            },
            printErr: function (msg) {
                console.error('[wasm-vips error]', msg);
            }
        });

        var timeoutPromise = new Promise(function (_, reject) {
            setTimeout(function () {
                var isCrossIsolated = (typeof crossOriginIsolated !== 'undefined') ? crossOriginIsolated : false;
                var hasSAB = (typeof SharedArrayBuffer !== 'undefined');
                reject(new Error(
                    'WASM initialization timed out after 20s (crossOriginIsolated=' +
                    isCrossIsolated + ', SharedArrayBuffer=' + hasSAB + ')'
                ));
            }, 20000);
        });

        vipsInstance = await Promise.race([initPromise, timeoutPromise]);
    }
    return vipsInstance;
}

self.onmessage = async function (event) {
    var data = event.data;
    if (!data || data.type !== 'CONVERT_JPG_TO_TIFF') return;

    try {
        self.postMessage({ type: 'PROGRESS', percent: 10, message: 'Initializing WebAssembly engine...' });
        var vips = await getVips();

        self.postMessage({ type: 'PROGRESS', percent: 25, message: 'Reading gigapixel scan...' });
        var timestamp = Date.now();
        var inputName = 'input_' + timestamp + '.jpg';
        var outputName = 'output_' + timestamp + '.tif';

        vips.FS.writeFile(inputName, new Uint8Array(data.buffer));

        self.postMessage({ type: 'PROGRESS', percent: 45, message: 'Building pyramidal BigTIFF levels...' });
        var img = vips.Image.newFromFile(inputName);

        img.tiffsave(outputName, {
            tile: true,
            tile_width: 256,
            tile_height: 256,
            pyramid: true,
            bigtiff: true,
            compression: 'jpeg',
            Q: 90
        });

        self.postMessage({ type: 'PROGRESS', percent: 90, message: 'Finalizing pyramidal slide...' });
        var tiffBuffer = vips.FS.readFile(outputName);

        try { img.delete(); } catch (e) {}
        try { vips.FS.unlink(inputName); } catch (e) {}
        try { vips.FS.unlink(outputName); } catch (e) {}

        self.postMessage({
            ok: true,
            buffer: tiffBuffer.buffer,
            originalName: data.filename
        }, [tiffBuffer.buffer]);
    } catch (err) {
        self.postMessage({
            ok: false,
            error: err.message || String(err)
        });
    }
};
