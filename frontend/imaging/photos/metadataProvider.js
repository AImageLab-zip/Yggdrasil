/**
 * Metadata for `yggweb:` images, and the omission the whole surface rests on.
 *
 * ## No `pixelSpacing` unless it is actually known
 *
 * A photograph has no intrinsic scale. The roadmap's rule for this phase is to say so
 * rather than invent 1 mm/px, and the mechanism is an *omission* -- verified end to end
 * in the shipped package rather than assumed:
 *
 * 1. `core/utilities/buildMetadata.js` `getImagePlaneModule` returns the provider's
 *    module untouched only if all six of `columnPixelSpacing`, `rowPixelSpacing`,
 *    `columnCosines`, `rowCosines`, `imagePositionPatient` and `imageOrientationPatient`
 *    are truthy. Otherwise it clones it, sets `usingDefaultValues: true` and fills the
 *    spacings in as 1.
 * 2. `StackViewport._getImagePlaneModule` then sets
 *    `hasPixelSpacing = !usingDefaultValues || calibration?.scale > 0 ||
 *    calibration?.rowPixelSpacing > 0`.
 * 3. `tools/utilities/getCalibratedUnits.js` reads
 *    `unit = hasPixelSpacing ? 'mm' : 'px'`, and `BaseTool.calculateLengthInIndex`
 *    divides by `scale = (calibration?.scale || 1) / (calibration?.columnPixelSpacing ||
 *    spacing[0])`, which is 1 when nothing is calibrated.
 *
 * So omitting the spacing produces a length in pixels labelled `px`, with nothing faked
 * anywhere. Note step 1's early return: a module that sets `usingDefaultValues` *itself*
 * short-circuits the whole check, so this module never sets it.
 *
 * ## And when it *is* known, two places have to agree
 *
 * `calibrateIfNecessary` computes `hasPixelSpacing` from `scale > 0 ||
 * (!usingDefaultValues && rowPixelSpacing > 0)`, and the `calibratedPixelSpacing`
 * payload carries no `scale`. So registering the calibration alone leaves the label
 * reading `px User` -- accurate about provenance and wrong about the unit. The spacing
 * goes into `imagePlaneModule` **as well**, which is what makes `usingDefaultValues`
 * false, and the calibration entry is what appends `User` to the unit
 * (`CalibrationTypes.USER === 'User'`, appended by `getCalibratedUnits`) so the number
 * carries its provenance where a clinician reads it.
 *
 * ## Three modules are destructured without a guard
 *
 * `buildMetadata` reads `imagePixelModule`, `generalSeriesModule` and `imagePlaneModule`
 * without checking any of them, so returning `undefined` for one is a `TypeError` rather
 * than a fallback to another provider. Everything else must return `undefined`, though,
 * or this provider answers for modules it knows nothing about and shadows the ones that
 * do.
 */

/**
 * Provider priority. Above the default providers, below nothing in particular -- there
 * is no other provider that answers for `yggweb:` ids.
 */
export const PHOTO_METADATA_PRIORITY = 100;

/** The modules this provider answers for. Anything else must fall through. */
export const HANDLED_MODULES = Object.freeze([
    'imagePlaneModule',
    'imagePixelModule',
    'generalSeriesModule',
    'calibratedPixelSpacing',
]);

/** `CalibrationTypes.USER` in `@cornerstonejs/metadata`. Pinned by a test. */
export const USER_CALIBRATION = 'User';

/**
 * The plane module: geometry, and the spacing only if it is real.
 *
 * The cosines and position are identity values rather than omissions. A photograph has
 * no patient orientation, but the six-way check above needs them present for a
 * *calibrated* image to keep its spacing -- and an identity orientation is the honest
 * statement "the image plane is the image plane", where an omission would be read as an
 * unknown.
 *
 * @param {object} record `{width, height, pixelSpacingMm}` -- `pixelSpacingMm` null when
 *   the image has never been calibrated.
 * @returns {object}
 */
export function imagePlaneModuleFor(record) {
    const module = {
        rows: record.height,
        columns: record.width,
        rowCosines: [1, 0, 0],
        columnCosines: [0, 1, 0],
        imagePositionPatient: [0, 0, 0],
        imageOrientationPatient: [1, 0, 0, 0, 1, 0],
        // Deliberately absent: a photograph is not comparable with any other series, and
        // a Frame of Reference UID is precisely the claim that it is.
    };

    const spacing = record.pixelSpacingMm;
    if (!spacing) {
        // Absent, not null and not 1. `getImagePlaneModule` tests truthiness, so null
        // and 1 would take different branches and only one of them is right.
        return module;
    }
    return {
        ...module,
        rowPixelSpacing: spacing.y_mm,
        columnPixelSpacing: spacing.x_mm,
        pixelSpacing: [spacing.y_mm, spacing.x_mm],
    };
}

/**
 * The pixel module. 8-bit web images, so the values are fixed rather than read.
 *
 * @param {object} record
 * @returns {object}
 */
export function imagePixelModuleFor(record) {
    const color = record.numberOfComponents > 1;
    return {
        samplesPerPixel: color ? 3 : 1,
        photometricInterpretation: color ? 'RGB' : 'MONOCHROME2',
        rows: record.height,
        columns: record.width,
        bitsAllocated: 8,
        bitsStored: 8,
        highBit: 7,
        pixelRepresentation: 0,
        planarConfiguration: color ? 0 : undefined,
        windowCenter: 128,
        windowWidth: 256,
    };
}

/**
 * The series module. Its absence is a `TypeError`, which is the only reason it exists.
 *
 * `modality` is `'OT'` -- DICOM's "Other". Naming a real modality here would let a
 * preset table match on it, and the presets are CT-only for the reason decision #16
 * gives: a number that looks authoritative and is not.
 *
 * @param {object} record
 * @returns {object}
 */
export function generalSeriesModuleFor(record) {
    return {
        modality: 'OT',
        seriesInstanceUID: undefined,
        studyInstanceUID: undefined,
        seriesNumber: record.index ?? undefined,
        seriesDescription: record.description ?? '',
    };
}

/**
 * The calibration entry, or `undefined` for an image nobody has calibrated.
 *
 * Carries `type: 'User'` so `getCalibratedUnits` appends it and the on-screen unit reads
 * `mm User`. That the number came from somebody clicking two points on the picture is
 * exactly the caveat a millimetre reading on a photograph needs to carry.
 *
 * @param {object} record
 * @returns {object|undefined}
 */
export function calibratedPixelSpacingFor(record) {
    const spacing = record.pixelSpacingMm;
    if (!spacing) {
        return undefined;
    }
    return {
        rowPixelSpacing: spacing.y_mm,
        columnPixelSpacing: spacing.x_mm,
        type: USER_CALIBRATION,
    };
}

/**
 * A Cornerstone metadata provider over a registry of image records.
 *
 * @param {Map<string, object>|{get: Function}} registry imageId -> record.
 * @returns {Function} `(type, imageId) => object|undefined`
 */
export function createPhotoMetadataProvider(registry) {
    return function providePhotoMetadata(type, imageId) {
        // An imageId Cornerstone passes as an array is a multi-frame query; a photo
        // stack has no multi-frame images, so nothing here answers one.
        if (typeof imageId !== 'string') {
            return undefined;
        }
        const record = registry.get(imageId);
        if (!record) {
            return undefined;
        }
        switch (type) {
            case 'imagePlaneModule':
                return imagePlaneModuleFor(record);
            case 'imagePixelModule':
                return imagePixelModuleFor(record);
            case 'generalSeriesModule':
                return generalSeriesModuleFor(record);
            case 'calibratedPixelSpacing':
                return calibratedPixelSpacingFor(record);
            default:
                // Every other module falls through to whichever provider does know. An
                // empty object here would shadow them and report "known, and empty".
                return undefined;
        }
    };
}

/**
 * Would Cornerstone treat this plane module as carrying a real spacing?
 *
 * A local transcription of `getImagePlaneModule`'s six-way truthiness test, so a test
 * can assert the answer rather than the shape. It is duplicated logic on purpose: the
 * point is to fail when upstream's predicate and this module's output stop agreeing.
 *
 * @param {object} module
 * @returns {boolean}
 */
export function upstreamWouldKeepSpacing(module) {
    return Boolean(
        module.columnPixelSpacing &&
            module.rowPixelSpacing &&
            module.columnCosines &&
            module.rowCosines &&
            module.imagePositionPatient &&
            module.imageOrientationPatient
    );
}
