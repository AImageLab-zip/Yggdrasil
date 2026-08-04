(function(root, factory) {
    'use strict';

    const api = factory();
    if (typeof module === 'object' && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.NiiVueRenderModes = api;
    }
})(typeof window !== 'undefined' ? window : null, function() {
    'use strict';

    const VERSION = '0.69.0';
    const DECLARATION_ANCHOR = 'out vec4 fColor;';
    const HEAD_ANCHOR = 'bool overlayIsClipCutaway = isClipAllVolumes && isClipCutaway;';
    const TAIL_ANCHOR = 'if (firstHit.a < len) {';

    const WINDOW_GLSL = `
uniform vec2 yggWindow;
float yggIntensity(vec4 sampleValue) {
    return max(max(sampleValue.r, sampleValue.g), sampleValue.b);
}
vec4 yggVoxel(vec3 position) {
    vec4 sampleValue = texture(volume, position);
    float sourceIntensity = yggIntensity(sampleValue);
    float width = max(yggWindow.y - yggWindow.x, 0.001);
    float intensity = clamp((sourceIntensity - yggWindow.x) / width, 0.0, 1.0);
    float alphaScale = sourceIntensity > 0.0001
        ? clamp(sampleValue.a / sourceIntensity, 0.0, 1.0)
        : 0.0;
    sampleValue.rgb = clamp((sampleValue.rgb - vec3(yggWindow.x)) / width, 0.0, 1.0);
    sampleValue.a = intensity * alphaScale;
    return sampleValue;
}
`;

    const RENDER_LOOPS = {
        mip: `
    vec4 yggMaximum = vec4(0.0);
    float yggMaximumValue = 0.0;
    samplePos -= deltaDir * ran * (isClip ? 1.41 : 1.0);
    while (samplePos.a <= len) {
        if (skipSample(samplePos.a, sampleRange) ^^ isClipCutaway) {
            samplePos += deltaDirFast;
            continue;
        }
        vec4 colorSample = yggVoxel(samplePos.xyz);
        float sampleValue = yggIntensity(colorSample);
        if (sampleValue > yggMaximumValue) {
            yggMaximum = colorSample;
            yggMaximumValue = sampleValue;
            firstHit = samplePos;
        }
        samplePos += deltaDir;
    }
    if (yggMaximumValue > 0.0)
        colAcc = vec4(yggMaximum.rgb, earlyTermination);
    if (firstHit.a < len)
        backNearest = firstHit.a;
`,
        amip: `
    vec4 yggMaximum = vec4(0.0);
    float yggMaximumValue = 0.0;
    float yggTransmittance = 1.0;
    samplePos -= deltaDir * ran * (isClip ? 1.41 : 1.0);
    while (samplePos.a <= len) {
        if (skipSample(samplePos.a, sampleRange) ^^ isClipCutaway) {
            samplePos += deltaDirFast;
            continue;
        }
        vec4 colorSample = yggVoxel(samplePos.xyz);
        float density = yggIntensity(colorSample);
        float attenuatedValue = density * yggTransmittance;
        if (attenuatedValue > yggMaximumValue) {
            yggMaximum = vec4(colorSample.rgb * yggTransmittance, colorSample.a);
            yggMaximumValue = attenuatedValue;
            firstHit = samplePos;
        }
        // Heuristic Beer-Lambert extinction: enough front-to-back shadowing to
        // retain depth without hiding dense anatomy behind the first surface.
        yggTransmittance *= exp(-density * 0.018);
        if (yggTransmittance < 0.01)
            break;
        samplePos += deltaDir;
    }
    if (yggMaximumValue > 0.0)
        colAcc = vec4(yggMaximum.rgb, earlyTermination);
    if (firstHit.a < len)
        backNearest = firstHit.a;
`,
        shaded: `
    float yggClipCloseThreshold = 5.0 * deltaDir.a;
    float yggClipClose = isClipCutaway ? sampleRange.y : sampleRange.x;
    if (!isClip)
        yggClipClose = -1.0;
    while (samplePos.a <= len) {
        if (skipSample(samplePos.a, sampleRange) ^^ isClipCutaway) {
            samplePos += deltaDirFast;
            continue;
        }
        vec4 colorSample = yggVoxel(samplePos.xyz);
        if (colorSample.a >= 0.01) {
            vec4 gradientSample = texture(gradient, samplePos.xyz);
            gradientSample.rgb = gradientSample.rgb * 2.0 - 1.0;
            if (gradientSample.a > 0.0)
                gradientSample.rgb = normalize(gradientSample.rgb);
            vec3 normal = mat3(normMtx) * gradientSample.rgb;
            normal.y = -normal.y;
            vec3 materialColor = texture(matCap, normal.xy * 0.5 + 0.5).rgb * 2.0;
            if (abs(samplePos.a - yggClipClose) > yggClipCloseThreshold)
                colorSample.rgb *= mix(vec3(1.0), materialColor, gradientAmount);
            if (firstHit.a > len)
                firstHit = samplePos;
            backNearest = min(backNearest, samplePos.a);
            colorSample.a = 1.0 - pow(1.0 - colorSample.a, opacityCorrection);
            colorSample.a *= pow(max(gradientSample.a, 0.0), 4.8);
            colorSample.rgb *= colorSample.a;
            colAcc = (1.0 - colAcc.a) * colorSample + colAcc;
            if (colAcc.a > earlyTermination)
                break;
        }
        samplePos += deltaDir;
    }
`
    };

    function finiteNumber(value, fallback) {
        return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
    }

    function clamp(value, low, high) {
        return Math.max(low, Math.min(high, value));
    }

    function volumeRange(volume) {
        const fallbackMin = finiteNumber(volume && volume.cal_min, 0);
        const fallbackMax = finiteNumber(volume && volume.cal_max, fallbackMin + 1);
        let min = finiteNumber(volume && volume.global_min, fallbackMin);
        let max = finiteNumber(volume && volume.global_max, fallbackMax);
        if (!(max > min)) {
            min = fallbackMin;
            max = fallbackMax > fallbackMin ? fallbackMax : fallbackMin + 1;
        }

        let robustMin = finiteNumber(volume && volume.robust_min, min);
        let robustMax = finiteNumber(volume && volume.robust_max, max);
        robustMin = clamp(robustMin, min, max);
        robustMax = clamp(robustMax, min, max);
        if (!(robustMax > robustMin)) {
            robustMin = min;
            robustMax = max;
        }

        const span = max - min;
        const lowFraction = (robustMin - min) / span;
        const highFraction = (robustMax - min) / span;
        return {
            min,
            max,
            robustMin,
            robustMax,
            level: Math.round(clamp((lowFraction + highFraction) * 50, 0, 100)),
            window: Math.max(1, Math.round(clamp(highFraction - lowFraction, 0, 1) * 100))
        };
    }

    function windowCuts(level, width) {
        const center = clamp(finiteNumber(Number(level), 50), 0, 100) / 100;
        const normalizedWidth = clamp(finiteNumber(Number(width), 100), 1, 100) / 100;
        return [center - normalizedWidth / 2, center + normalizedWidth / 2];
    }

    function spliceRenderFragment(fragmentSource, loopSource) {
        if (typeof fragmentSource !== 'string' || typeof loopSource !== 'string') {
            return null;
        }
        const headEndIndex = fragmentSource.indexOf(HEAD_ANCHOR);
        if (headEndIndex < 0) {
            return null;
        }
        const bodyStart = headEndIndex + HEAD_ANCHOR.length;
        const tailIndex = fragmentSource.indexOf(TAIL_ANCHOR, bodyStart);
        const declarationIndex = fragmentSource.indexOf(DECLARATION_ANCHOR);
        if (tailIndex < 0 || declarationIndex < 0 || declarationIndex > bodyStart) {
            return null;
        }
        const declarationEnd = declarationIndex + DECLARATION_ANCHOR.length;
        const head = fragmentSource.slice(0, declarationEnd) + WINDOW_GLSL + fragmentSource.slice(declarationEnd, bodyStart);
        return head + loopSource + '\n    ' + fragmentSource.slice(tailIndex);
    }

    function spliceSliceFragment(fragmentSource) {
        if (typeof fragmentSource !== 'string') {
            return null;
        }
        const declaration = 'out vec4 color;';
        const sample = 'vec4 background = texture(volume, texPos);';
        if (!fragmentSource.includes(declaration) || !fragmentSource.includes(sample)) {
            return null;
        }
        return fragmentSource
            .replace(declaration, declaration + '\nuniform vec2 yggWindow;')
            .replace(sample, sample + `
    float yggWidth = max(yggWindow.y - yggWindow.x, 0.001);
    float yggSource = max(max(background.r, background.g), background.b);
    float yggValue = clamp((yggSource - yggWindow.x) / yggWidth, 0.0, 1.0);
    background.rgb = clamp((background.rgb - vec3(yggWindow.x)) / yggWidth, 0.0, 1.0);
    background.a = yggValue;
`);
    }

    function shaderSources(gl, shader) {
        const sources = { vertex: null, fragment: null };
        if (!gl || !shader || !shader.program || typeof gl.getAttachedShaders !== 'function') {
            return sources;
        }
        const attached = gl.getAttachedShaders(shader.program) || [];
        attached.forEach(function(attachedShader) {
            const source = gl.getShaderSource(attachedShader);
            if (gl.getShaderParameter(attachedShader, gl.SHADER_TYPE) === gl.VERTEX_SHADER) {
                sources.vertex = source;
            } else {
                sources.fragment = source;
            }
        });
        return sources;
    }

    function setUniform2f(gl, shader, name, x, y) {
        const location = shader && shader.uniforms ? shader.uniforms[name] : null;
        if (location === null || location === undefined || location === -1) {
            return false;
        }
        shader.use(gl);
        gl.uniform2f(location, x, y);
        return true;
    }

    function createController(nv) {
        if (!nv || !nv.gl || !nv.volumes || !nv.volumes[0]) {
            throw new Error('NiiVue render modes require an initialized volume');
        }

        const range = volumeRange(nv.volumes[0]);
        const state = {
            level: range.level,
            width: range.window,
            range,
            liveSlice: false,
            liveRender: false,
            renderActive: false,
            activeMode: null,
            shaders: {},
            modeAvailability: {},
            nativeShadedFailed: false,
            drawFrame: null
        };

        function pushWindow(shader) {
            const cuts = windowCuts(state.level, state.width);
            return setUniform2f(nv.gl, shader, 'yggWindow', cuts[0], cuts[1]);
        }

        function applyCalRange() {
            const volume = nv.volumes && nv.volumes[0];
            if (!volume) {
                return;
            }
            setCalRange();
            if (typeof nv.updateGLVolume === 'function') {
                nv.updateGLVolume();
            } else {
                nv.drawScene();
            }
        }

        function setCalRange() {
            const volume = nv.volumes && nv.volumes[0];
            if (!volume) {
                return;
            }
            const cuts = windowCuts(state.level, state.width);
            const span = state.range.max - state.range.min;
            volume.cal_min = state.range.min + cuts[0] * span;
            volume.cal_max = state.range.min + cuts[1] * span;
        }

        function bakeFullRange() {
            const volume = nv.volumes && nv.volumes[0];
            if (!volume) {
                return;
            }
            volume.cal_min = state.range.min;
            volume.cal_max = state.range.max;
        }

        function installSliceShader() {
            if (!nv.sliceMMShader || typeof nv.updateGLVolume !== 'function') {
                return false;
            }
            const sources = shaderSources(nv.gl, nv.sliceMMShader);
            const customSource = spliceSliceFragment(sources.fragment);
            if (!sources.vertex || !customSource) {
                return false;
            }
            try {
                // Upload against NiiVue's stock slice shader first. Installing the
                // custom shader before this bake would sample the old robust-range
                // texture with uninitialized yggWindow uniforms.
                bakeFullRange();
                nv.updateGLVolume();

                const shader = new nv.sliceMMShader.constructor(nv.gl, sources.vertex, customSource);
                shader.use(nv.gl);
                const textureUnits = { volume: 0, colormap: 1, overlay: 2, drawing: 7, paqd: 8 };
                Object.keys(textureUnits).forEach(function(name) {
                    const location = shader.uniforms && shader.uniforms[name];
                    if (location !== null && location !== undefined && location !== -1) {
                        nv.gl.uniform1i(location, textureUnits[name]);
                    }
                });
                const drawOpacityLocation = shader.uniforms && shader.uniforms.drawOpacity;
                if (drawOpacityLocation !== null && drawOpacityLocation !== undefined && drawOpacityLocation !== -1) {
                    nv.gl.uniform1f(drawOpacityLocation, nv.drawOpacity);
                }
                nv.customSliceShader = shader;
                state.liveSlice = pushWindow(shader);
                nv.drawScene();
                return state.liveSlice;
            } catch (error) {
                console.warn('CBCT live slice windowing is unavailable:', error);
                nv.customSliceShader = null;
                state.liveSlice = false;
                return false;
            }
        }

        function buildModeShader(mode) {
            if (Object.prototype.hasOwnProperty.call(state.shaders, mode)) {
                return state.shaders[mode];
            }
            const isShaded = mode === 'shaded';
            const base = isShaded ? nv.renderGradientShader : nv.renderVolumeShader;
            const sources = shaderSources(nv.gl, base);
            const fragment = spliceRenderFragment(sources.fragment, RENDER_LOOPS[mode]);
            if (!base || !sources.vertex || !fragment) {
                state.shaders[mode] = null;
                return null;
            }
            try {
                state.shaders[mode] = new base.constructor(nv.gl, sources.vertex, fragment);
            } catch (error) {
                console.warn('CBCT ' + mode + ' shader failed to compile:', error);
                state.shaders[mode] = null;
            }
            return state.shaders[mode];
        }

        function modeAvailability() {
            ['mip', 'amip', 'shaded'].forEach(function(mode) {
                const custom = !!buildModeShader(mode);
                const nativeFallback = mode === 'shaded' &&
                    !state.nativeShadedFailed &&
                    typeof nv.setVolumeRenderIllumination === 'function';
                state.modeAvailability[mode] = {
                    available: custom || nativeFallback,
                    custom,
                    fallback: !custom && nativeFallback
                };
            });
            return {
                mip: Object.assign({}, state.modeAvailability.mip),
                amip: Object.assign({}, state.modeAvailability.amip),
                shaded: Object.assign({}, state.modeAvailability.shaded)
            };
        }

        function nativeShadedFallback(message) {
            state.liveRender = false;
            setCalRange();
            const result = {
                available: true,
                custom: false,
                fallback: true,
                pending: true,
                message
            };
            try {
                const operation = nv.setVolumeRenderIllumination(0.5);
                result.ready = Promise.resolve(operation).then(function() {
                    result.pending = false;
                    return result;
                }, function(error) {
                    console.warn('NiiVue native shaded fallback failed:', error);
                    result.available = false;
                    result.fallback = false;
                    result.pending = false;
                    result.message = 'NiiVue native shaded rendering failed on this GPU.';
                    state.nativeShadedFailed = true;
                    state.modeAvailability.shaded = { available: false, custom: false, fallback: false };
                    return result;
                });
            } catch (error) {
                console.warn('NiiVue native shaded fallback failed:', error);
                result.available = false;
                result.fallback = false;
                result.pending = false;
                result.message = 'NiiVue native shaded rendering failed on this GPU.';
                state.nativeShadedFailed = true;
                state.modeAvailability.shaded = { available: false, custom: false, fallback: false };
                result.ready = Promise.resolve(result);
            }
            return result;
        }

        function setMode(mode) {
            if (!RENDER_LOOPS[mode]) {
                return { available: false, custom: false, message: 'Unknown render mode' };
            }
            const availability = modeAvailability()[mode];
            state.renderActive = true;
            state.activeMode = mode;

            if (!availability.custom) {
                state.liveRender = false;
                if (availability.fallback) {
                    return nativeShadedFallback('Custom shaded shader unavailable; using NiiVue native shading.');
                }
                return {
                    available: false,
                    custom: false,
                    message: mode === 'amip'
                        ? 'Attenuated MIP is unavailable on this GPU.'
                        : 'MIP is unavailable on this GPU.'
                };
            }

            const shader = state.shaders[mode];
            const illumination = mode === 'shaded' ? 0.5 : 0;
            try {
                nv.renderGradientValues = false;
                nv.opts.gradientAmount = illumination;
                nv.gradientTextureAmount = mode === 'shaded' ? Math.max(illumination, 0.01) : 0;
                nv.initRenderShader(shader, illumination);
                if (mode === 'shaded') {
                    shader.use(nv.gl);
                    nv.gl.uniform1i(shader.uniforms.matCap, 5);
                    nv.gl.uniform1i(shader.uniforms.gradient, 6);
                }
                nv.renderShader = shader;
                bakeFullRange();
                nv.updateGLVolume();
                state.liveRender = pushWindow(shader);
                nv.drawScene();
                return {
                    available: true,
                    custom: true,
                    fallback: false,
                    message: state.liveRender
                        ? ''
                        : 'Live windowing unavailable; using safe texture updates.'
                };
            } catch (error) {
                console.warn('Failed to install CBCT ' + mode + ' mode:', error);
                state.liveRender = false;
                state.shaders[mode] = null;
                if (mode === 'shaded' && typeof nv.setVolumeRenderIllumination === 'function') {
                    state.modeAvailability[mode] = { available: true, custom: false, fallback: true };
                    return nativeShadedFallback('Custom shaded shader failed; using NiiVue native shading.');
                }
                state.modeAvailability[mode] = { available: false, custom: false, fallback: false };
                return { available: false, custom: false, message: 'Render mode failed to initialize on this GPU.' };
            }
        }

        function drawWindow() {
            state.drawFrame = null;
            const shader = state.renderActive ? nv.renderShader : nv.customSliceShader;
            const live = state.renderActive ? state.liveRender : state.liveSlice;
            try {
                if (live && pushWindow(shader)) {
                    nv.drawScene();
                } else {
                    applyCalRange();
                }
            } catch (error) {
                console.warn('CBCT window update failed:', error);
            }
        }

        function setWindow(level, width) {
            state.level = clamp(finiteNumber(Number(level), state.level), 0, 100);
            state.width = clamp(finiteNumber(Number(width), state.width), 1, 100);
            if (state.drawFrame !== null) {
                return;
            }
            if (typeof requestAnimationFrame === 'function') {
                state.drawFrame = requestAnimationFrame(drawWindow);
            } else {
                drawWindow();
            }
        }

        function reapply() {
            const shader = state.renderActive ? nv.renderShader : nv.customSliceShader;
            const live = state.renderActive ? state.liveRender : state.liveSlice;
            if (live) {
                pushWindow(shader);
                nv.drawScene();
            }
        }

        installSliceShader();
        setWindow(state.level, state.width);

        return {
            version: VERSION,
            getInitialWindow: function() {
                return { level: range.level, window: range.window, range: Object.assign({}, range) };
            },
            getWindow: function() {
                return { level: state.level, window: state.width };
            },
            setWindow,
            setMode,
            getModeAvailability: modeAvailability,
            reapply,
            isLiveWindowing: function() {
                return state.renderActive ? state.liveRender : state.liveSlice;
            }
        };
    }

    return {
        VERSION,
        volumeRange,
        windowCuts,
        spliceRenderFragment,
        spliceSliceFragment,
        createController,
        _renderLoops: RENDER_LOOPS
    };
});
