"""The controlled vocabularies the annotation model is built out of.

Every choice list lives here rather than on the model that uses it, because
these values are written into the database and read back by adapters,
management commands and (eventually) an export format. A value in this file is
a *contract*: renaming one silently reinterprets stored rows.

Nothing here imports Django models, so validators and adapters -- which must
stay pure -- can use it freely.
"""


class ResourceKind:
    """What a :class:`~annotations.models.SourceResource` points at.

    ``FILE`` is a ``common.FileRegistry`` row (optionally one keyed member of a
    multi-file bundle). ``DICOM_SERIES``/``DICOM_INSTANCE`` are placeholders for
    the Phase 8 catalog and carry their UIDs in ``identity_key`` today.
    ``DERIVED_RESOURCE`` is something produced from another resource that is not
    itself a stored file (a rendered panoramic strip, a resampled grid).
    ``LOGICAL_VOLUME`` is the one that needs explaining: a CBCT "volume" a user
    annotates may be a *bundle member* inside a processed artifact, and the
    thing annotations are anchored to is the volume, not the bundle.
    """

    FILE = "file"
    DICOM_SERIES = "dicom_series"
    DICOM_INSTANCE = "dicom_instance"
    DERIVED_RESOURCE = "derived_resource"
    LOGICAL_VOLUME = "logical_volume"

    CHOICES = [
        (FILE, "File"),
        (DICOM_SERIES, "DICOM series"),
        (DICOM_INSTANCE, "DICOM instance"),
        (DERIVED_RESOURCE, "Derived resource"),
        (LOGICAL_VOLUME, "Logical volume"),
    ]
    ALL = frozenset(value for value, _ in CHOICES)


class CoordinateSystem:
    """Every stored coordinate declares which frame it is expressed in.

    The repository currently has five frames in use and no conversion layer
    between them, which is exactly how a landmark ends up plotted in the wrong
    place. Naming the frame on the row is the smallest change that makes a
    wrong conversion a *detectable* error rather than a silent one.

    Two distinctions are load-bearing and easy to get wrong:

    * **LPS vs RAS.** DICOM and Cornerstone use LPS millimetres; NIfTI's world
      frame is RAS+. They differ by two sign flips, so a value stored in one and
      read as the other lands mirrored across the sagittal and coronal planes --
      the same class of silent mirroring ``static/js/volume_metadata.js`` guards
      against at load time. They are separate values here so no code can treat
      them as interchangeable by omission.
    * **Pixels vs normalized.** A [0, 1] fraction survives a resolution change;
      a pixel index does not. The laparoscopy surface stores both (stroke
      geometry in frame pixels, SAM2 prompt points as fractions), so the
      distinction is already real data, not a hypothetical.

    ``RESOURCE_LOCAL`` is the mesh case: IOS landmarks come out of
    ``worldToLocal`` against a specific mesh and have no patient frame at all.
    Recording them as ``PATIENT_LPS_MM`` would be a false statement about what
    the numbers mean, so they get their own frame, scoped to the resource that
    defines it.
    """

    #: DICOM / Cornerstone patient coordinate system, millimetres (x=L, y=P, z=S).
    PATIENT_LPS_MM = "patient_lps_mm"
    #: NIfTI world frame, millimetres (x=R, y=A, z=S). Not LPS. See above.
    PATIENT_RAS_MM = "patient_ras_mm"
    #: Fractional IJK index into a named volume resource.
    VOLUME_VOXEL = "volume_voxel"
    #: Column/row pixel index in a 2D image resource, origin at the top-left.
    IMAGE_PIXEL = "image_pixel"
    #: [0, 1] fraction of a 2D image's extent.
    IMAGE_NORMALIZED = "image_normalized"
    #: Pixel/index coordinates inside one named slice of a volume. The slice
    #: axis and index live on the selector; without them the pair is meaningless.
    SLICE_PIXEL = "slice_pixel"
    #: Pixel index in a video frame at *source* resolution, not display size.
    VIDEO_PIXEL = "video_pixel"
    #: [0, 1] fraction of the video frame.
    VIDEO_NORMALIZED = "video_normalized"
    #: The resource's own local frame (a mesh's object space).
    RESOURCE_LOCAL = "resource_local"
    #: The annotation has no geometry: a classification, a caption, a whole-file
    #: event. Selectors still have to declare something, and saying "none" is
    #: honest where naming a frame would not be.
    NONE = "none"

    CHOICES = [
        (PATIENT_LPS_MM, "Patient LPS (mm)"),
        (PATIENT_RAS_MM, "Patient RAS (mm)"),
        (VOLUME_VOXEL, "Volume voxel (IJK)"),
        (IMAGE_PIXEL, "Image pixel"),
        (IMAGE_NORMALIZED, "Image normalized"),
        (SLICE_PIXEL, "Slice pixel"),
        (VIDEO_PIXEL, "Video pixel"),
        (VIDEO_NORMALIZED, "Video normalized"),
        (RESOURCE_LOCAL, "Resource local"),
        (NONE, "None"),
    ]
    ALL = frozenset(value for value, _ in CHOICES)

    #: Frames whose coordinates are three-dimensional.
    THREE_D = frozenset({PATIENT_LPS_MM, PATIENT_RAS_MM, VOLUME_VOXEL, RESOURCE_LOCAL})
    #: Frames whose coordinates are two-dimensional.
    TWO_D = frozenset(
        {IMAGE_PIXEL, IMAGE_NORMALIZED, SLICE_PIXEL, VIDEO_PIXEL, VIDEO_NORMALIZED}
    )
    #: Frames whose values are fractions of an extent, so every ordinate is in
    #: [0, 1] and a resolution change does not invalidate them.
    NORMALIZED = frozenset({IMAGE_NORMALIZED, VIDEO_NORMALIZED})
    #: Frames measured in real-world millimetres. Only these may carry a
    #: millimetre measurement without a calibration factor.
    MILLIMETRE = frozenset({PATIENT_LPS_MM, PATIENT_RAS_MM})


class SelectorKind:
    """Which part of a target resource an annotation applies to."""

    #: The whole resource; no sub-addressing.
    WHOLE_RESOURCE = "whole_resource"
    #: One frame of a video or multi-frame image, by index.
    FRAME = "frame"
    #: One slice of a volume, by axis and index.
    SLICE = "slice"
    #: A half-open time span, in integer milliseconds.
    TEMPORAL_INTERVAL = "temporal_interval"
    #: An axis-aligned sub-region, in the selector's coordinate system.
    SPATIAL_BOUNDS = "spatial_bounds"
    #: One segment value inside a labelmap resource.
    SEGMENT = "segment"

    CHOICES = [
        (WHOLE_RESOURCE, "Whole resource"),
        (FRAME, "Frame"),
        (SLICE, "Slice"),
        (TEMPORAL_INTERVAL, "Temporal interval"),
        (SPATIAL_BOUNDS, "Spatial bounds"),
        (SEGMENT, "Segment"),
    ]
    ALL = frozenset(value for value, _ in CHOICES)


class SliceAxis:
    """Which volume axis a ``SLICE`` selector indexes along.

    Named anatomically rather than as I/J/K because the stored index is only
    interpretable together with the volume's orientation, and "axial slice 128"
    is what the panoramic editor actually means.
    """

    AXIAL = "axial"
    CORONAL = "coronal"
    SAGITTAL = "sagittal"
    #: An arbitrary reformatted plane; its geometry lives in the selector bounds.
    OBLIQUE = "oblique"

    CHOICES = [
        (AXIAL, "Axial"),
        (CORONAL, "Coronal"),
        (SAGITTAL, "Sagittal"),
        (OBLIQUE, "Oblique"),
    ]
    ALL = frozenset(value for value, _ in CHOICES)


class PayloadFormat:
    """How one revision's annotation content is encoded.

    A revision may carry several payloads at once: exactly one canonical
    representation plus any number of derived or interchange ones. The canonical
    one is what Yggdrasil reads back; the rest are conveniences that may be
    regenerated at any time.

    ``CORNERSTONE_STATE`` is deliberately *not* canonical anywhere. It is an
    editable scratch copy of a viewer's serialized tool state, kept so a user
    can resume editing exactly where they left off, and it is free to become
    stale relative to the canonical items.
    """

    #: Yggdrasil's own JSON document -- the default canonical form.
    YGGDRASIL_JSON = "yggdrasil_json"
    #: A viewer's serialized tool state. Never canonical. Never authoritative.
    CORNERSTONE_STATE = "cornerstone_state"
    #: Dense segmentation as a NIfTI labelmap in object storage.
    NIFTI_LABELMAP = "nifti_labelmap"
    #: Dense segmentation as DICOM SEG, for interchange.
    DICOM_SEG = "dicom_seg"
    #: Contours as DICOM RTSTRUCT, for interchange.
    DICOM_RTSTRUCT = "dicom_rtstruct"
    #: Measurements as a DICOM Structured Report, for interchange.
    DICOM_SR = "dicom_sr"
    #: A baked PNG preview -- the panoramic MIP/ray-sum strips, which are
    #: derived artifacts that decision #8 requires to stay exportable.
    PNG_RENDER = "png_render"
    #: The laparoscopy mask archive, whose bytes must stay compatible.
    NPZ_MASK = "npz_mask"

    CHOICES = [
        (YGGDRASIL_JSON, "Yggdrasil JSON"),
        (CORNERSTONE_STATE, "Cornerstone state"),
        (NIFTI_LABELMAP, "NIfTI labelmap"),
        (DICOM_SEG, "DICOM SEG"),
        (DICOM_RTSTRUCT, "DICOM RTSTRUCT"),
        (DICOM_SR, "DICOM SR"),
        (PNG_RENDER, "PNG render"),
        (NPZ_MASK, "NPZ mask"),
    ]
    ALL = frozenset(value for value, _ in CHOICES)

    #: Formats whose bytes live in object storage and are addressed by a
    #: ``FileRegistry`` row rather than stored inline as JSON.
    ARTIFACT = frozenset(
        {NIFTI_LABELMAP, DICOM_SEG, DICOM_RTSTRUCT, DICOM_SR, PNG_RENDER, NPZ_MASK}
    )
    #: Formats stored inline in the payload's JSON column.
    INLINE = frozenset({YGGDRASIL_JSON, CORNERSTONE_STATE})


class Geometry2DType:
    """Shapes a :class:`Geometry2DItem` can hold.

    The point-count rules that go with each type are in
    ``annotations.validators.geometry``; keeping them out of here leaves this
    module free of logic.
    """

    POINT = "point"
    POLYLINE = "polyline"
    POLYGON = "polygon"
    RECTANGLE = "rectangle"
    ELLIPSE = "ellipse"
    CIRCLE = "circle"
    FREEHAND = "freehand"

    CHOICES = [
        (POINT, "Point"),
        (POLYLINE, "Polyline"),
        (POLYGON, "Polygon"),
        (RECTANGLE, "Rectangle"),
        (ELLIPSE, "Ellipse"),
        (CIRCLE, "Circle"),
        (FREEHAND, "Freehand"),
    ]
    ALL = frozenset(value for value, _ in CHOICES)


class Geometry3DType:
    """Shapes a :class:`SpatialAnnotation3DItem` can hold.

    Separate from the 2D list rather than shared, because the two item models
    have disjoint invariants and a shared vocabulary would invite writing a
    ``rectangle`` into a 3D row.
    """

    POINT = "point"
    POLYLINE = "polyline"
    #: An ordered set of coplanar points defining a plane (origin + two axes).
    PLANE = "plane"
    #: An axis-aligned box, two opposite corners.
    BOX = "box"
    #: Centre point plus a radius carried in ``attributes``.
    SPHERE = "sphere"

    CHOICES = [
        (POINT, "Point"),
        (POLYLINE, "Polyline"),
        (PLANE, "Plane"),
        (BOX, "Box"),
        (SPHERE, "Sphere"),
    ]
    ALL = frozenset(value for value, _ in CHOICES)


class MeasurementKind:
    """What a stored number measures."""

    LENGTH = "length"
    PERIMETER = "perimeter"
    DIAMETER = "diameter"
    ANGLE = "angle"
    AREA = "area"
    VOLUME = "volume"
    MEAN = "mean"
    STDDEV = "stddev"
    MIN = "min"
    MAX = "max"
    COUNT = "count"

    CHOICES = [
        (LENGTH, "Length"),
        (PERIMETER, "Perimeter"),
        (DIAMETER, "Diameter"),
        (ANGLE, "Angle"),
        (AREA, "Area"),
        (VOLUME, "Volume"),
        (MEAN, "Mean"),
        (STDDEV, "Standard deviation"),
        (MIN, "Minimum"),
        (MAX, "Maximum"),
        (COUNT, "Count"),
    ]
    ALL = frozenset(value for value, _ in CHOICES)


class MeasurementUnit:
    """Units a measurement may be reported in.

    ``PX``/``PX2`` exist so an uncalibrated length is *reported* as pixels
    instead of being dressed up as millimetres. That is not a display nicety:
    a clinician reading "12.4 mm" off an uncalibrated photograph gets a wrong
    number with no way to tell. The database enforces the pairing -- see
    ``MeasurementItem``'s check constraint.
    """

    MM = "mm"
    MM2 = "mm2"
    MM3 = "mm3"
    PX = "px"
    PX2 = "px2"
    PX3 = "px3"
    DEG = "deg"
    HU = "hu"
    #: A bare count or ratio.
    NONE = "none"

    CHOICES = [
        (MM, "mm"),
        (MM2, "mm²"),
        (MM3, "mm³"),
        (PX, "px"),
        (PX2, "px²"),
        (PX3, "px³"),
        (DEG, "degrees"),
        (HU, "HU"),
        (NONE, "(none)"),
    ]
    ALL = frozenset(value for value, _ in CHOICES)

    #: Units that make a claim about real-world size and therefore require a
    #: calibrated measurement.
    REQUIRES_CALIBRATION = frozenset({MM, MM2, MM3})


class AnnotationStatus:
    """Where a set is in its lifecycle.

    Note what is *not* here: "deleted". Decision #18 makes the lock monotonic,
    so removing annotation work does not restore a patient's raw data to an
    editable state, and a status that pretended otherwise would be misleading.
    """

    DRAFT = "draft"
    CONFIRMED = "confirmed"
    SUPERSEDED = "superseded"

    CHOICES = [
        (DRAFT, "Draft"),
        (CONFIRMED, "Confirmed"),
        (SUPERSEDED, "Superseded"),
    ]
    ALL = frozenset(value for value, _ in CHOICES)


class AnnotationOrigin:
    """Who or what produced a revision.

    ``PREDICTION`` matters to the lock: machine output is not annotation work,
    which is why ``ios_landmarks_prediction`` and an ``auto`` panoramic geometry
    have never locked a patient's raw data. Keeping the distinction on the row
    means the lock can ask the question directly instead of inferring it from a
    file-type suffix.
    """

    MANUAL = "manual"
    PREDICTION = "prediction"
    #: A manual edit that started from a prediction.
    CORRECTED_PREDICTION = "corrected_prediction"
    #: Produced by the Phase 2 conversion of a legacy table.
    MIGRATION = "migration"
    #: Imported from an interchange format (SEG, RTSTRUCT, SR).
    IMPORT = "import"

    CHOICES = [
        (MANUAL, "Manual"),
        (PREDICTION, "Prediction"),
        (CORRECTED_PREDICTION, "Corrected prediction"),
        (MIGRATION, "Migration"),
        (IMPORT, "Import"),
    ]
    ALL = frozenset(value for value, _ in CHOICES)

    #: Origins that count as human annotation work for the purposes of the raw
    #: data lock. A migrated row counts: it represents work a person did, just
    #: recorded by a different mechanism.
    HUMAN = frozenset({MANUAL, CORRECTED_PREDICTION, MIGRATION, IMPORT})
