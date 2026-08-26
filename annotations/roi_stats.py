"""Intensity statistics over a stored ROI, computed from the voxels.

The half of decision #11 that could not be taken from the client.
``annotations.adapters.cornerstone`` deliberately refuses a probe's Hounsfield reading
or an ROI's mean, because neither is derivable from the geometry -- they need the data,
which an adapter does not have. This is where they come from instead.

Pure, and pure in the strong sense: it takes a NumPy array, an affine and a stored
geometry, and returns numbers. It does not query, does not download, and does not know
what a ``MeasurementItem`` is. The command next to it does the I/O. That split is what
makes the coordinate arithmetic below testable with a synthetic 8x8x8 volume whose
answer can be worked out by hand, rather than only against a real CBCT nobody can put
in a test.

**The frames, again.** Stored coordinates are ``patient_lps_mm``; a NIfTI affine maps
voxel indices to ``patient_ras_mm``. The two differ by negating x and y, and the
conversion happens in exactly one place here (:func:`lps_to_ras`) for the same reason
it does in the validation harness: a mirroring bug that agrees with itself reports
success. Every ROI is resolved to voxels through that one function.

**Values are modality values.** The array handed in is raw stored data; the caller
supplies the header's slope and intercept and every statistic comes back in Hounsfield
(or whatever the modality's unit is), never in stored units. A mean of 1400 is not a
number anyone can act on if the volume happened to be uint16-plus-intercept.
"""

import math

import numpy as np

from annotations.constants import Geometry3DType

#: Statistics this module produces, in the order they are reported.
STATISTIC_KINDS = ("mean", "stddev", "min", "max", "count")

#: An ROI resolving to fewer voxels than this is reported as empty rather than as a
#: statistic. One voxel is a legitimate probe; zero is a shape that missed the volume,
#: and a mean over nothing is NaN dressed up as a measurement.
MINIMUM_VOXELS = 1


def lps_to_ras(point):
    """The only sign flip in this module. See the header note."""
    return np.array([-point[0], -point[1], point[2]], dtype=float)


def world_to_voxel(affine, point_lps):
    """Map a patient-space point to continuous voxel indices.

    ``affine`` is the NIfTI 4x4 mapping voxel indices to **RAS** millimetres, so the
    point is converted out of LPS first and the affine is inverted, not transposed --
    an inverse and a transpose agree only for a rotation with unit spacing, which is
    exactly the case a test would use and never the case a CBCT is.
    """
    matrix = np.asarray(affine, dtype=float)
    ras = lps_to_ras(point_lps)
    inverse = np.linalg.inv(matrix[:3, :3])
    return inverse @ (ras - matrix[:3, 3])


def voxel_to_world(affine, index):
    """Map continuous voxel indices to patient LPS millimetres."""
    matrix = np.asarray(affine, dtype=float)
    ras = matrix[:3, :3] @ np.asarray(index, dtype=float) + matrix[:3, 3]
    return lps_to_ras(ras)


def voxel_spacing(affine):
    """Per-axis voxel size in millimetres, from the affine's column norms."""
    matrix = np.asarray(affine, dtype=float)
    return np.linalg.norm(matrix[:3, :3], axis=0)


# ---------------------------------------------------------------------------
# Masks
# ---------------------------------------------------------------------------


def point_indices(shape, affine, point_lps):
    """The single voxel containing a point, or an empty result if it is outside.

    Rounds rather than truncates: a probe dropped at a voxel's centre must resolve to
    *that* voxel, and truncation biases every reading half a voxel towards the origin.
    """
    index = np.rint(world_to_voxel(affine, point_lps)).astype(int)
    if np.any(index < 0) or np.any(index >= np.asarray(shape)):
        return np.empty((0, 3), dtype=int)
    return index.reshape(1, 3)


def sphere_indices(shape, affine, centre_lps, radius_mm):
    """Every voxel whose centre lies within ``radius_mm`` of ``centre_lps``.

    The test is done in **millimetres**, not in voxels. A CBCT with 0.3 x 0.3 x 0.4 mm
    spacing is anisotropic, and a radius applied in index space there would select an
    ellipsoid while reporting a sphere -- with the error growing exactly where the
    spacing is least uniform.
    """
    if radius_mm <= 0:
        return np.empty((0, 3), dtype=int)

    shape = np.asarray(shape, dtype=int)
    centre_voxel = world_to_voxel(affine, centre_lps)
    spacing = voxel_spacing(affine)

    # A generous bounding box in index space, then an exact test in world space. The
    # box only has to be a superset, so the per-axis radius uses the smallest spacing.
    extent = np.ceil(radius_mm / np.maximum(spacing, 1e-9)).astype(int) + 1
    low = np.maximum(np.floor(centre_voxel).astype(int) - extent, 0)
    high = np.minimum(np.ceil(centre_voxel).astype(int) + extent + 1, shape)
    if np.any(low >= high):
        return np.empty((0, 3), dtype=int)

    grid = np.stack(
        np.meshgrid(
            np.arange(low[0], high[0]),
            np.arange(low[1], high[1]),
            np.arange(low[2], high[2]),
            indexing="ij",
        ),
        axis=-1,
    ).reshape(-1, 3)

    matrix = np.asarray(affine, dtype=float)
    ras = grid @ matrix[:3, :3].T + matrix[:3, 3]
    centre_ras = lps_to_ras(centre_lps)
    within = np.linalg.norm(ras - centre_ras, axis=1) <= radius_mm
    return grid[within]


def box_indices(shape, affine, corners_lps):
    """Every voxel inside the axis-aligned patient-space box spanned by two corners."""
    corners = np.asarray(corners_lps, dtype=float)
    low_world = corners.min(axis=0)
    high_world = corners.max(axis=0)

    shape = np.asarray(shape, dtype=int)
    # The box is axis-aligned in *patient* space; under an oblique affine its voxel
    # footprint is not, so every corner of the world box is mapped and the index
    # bounding box taken from all eight. Mapping only two corners would clip the ROI
    # on any study that is not axis-aligned -- which is most of them after a head
    # tilt correction.
    world_corners = np.array(
        [
            [low_world[0] if bit & 1 else high_world[0],
             low_world[1] if bit & 2 else high_world[1],
             low_world[2] if bit & 4 else high_world[2]]
            for bit in range(8)
        ]
    )
    voxel_corners = np.array([world_to_voxel(affine, corner) for corner in world_corners])
    low = np.maximum(np.floor(voxel_corners.min(axis=0)).astype(int), 0)
    high = np.minimum(np.ceil(voxel_corners.max(axis=0)).astype(int) + 1, shape)
    if np.any(low >= high):
        return np.empty((0, 3), dtype=int)

    grid = np.stack(
        np.meshgrid(
            np.arange(low[0], high[0]),
            np.arange(low[1], high[1]),
            np.arange(low[2], high[2]),
            indexing="ij",
        ),
        axis=-1,
    ).reshape(-1, 3)

    matrix = np.asarray(affine, dtype=float)
    ras = grid @ matrix[:3, :3].T + matrix[:3, 3]
    lps = np.stack([-ras[:, 0], -ras[:, 1], ras[:, 2]], axis=1)
    inside = np.all((lps >= low_world - 1e-9) & (lps <= high_world + 1e-9), axis=1)
    return grid[inside]


def polygon_indices(shape, affine, points_lps):
    """Voxels under a planar polygon, sampled on the polygon's own plane.

    A planar ROI has no thickness, so there is no set of voxels it strictly contains.
    Cornerstone reports its statistics over the pixels of the slice it was drawn on;
    the equivalent here is to lay a sample grid on the polygon's plane at the volume's
    finest spacing, keep the samples inside the polygon, and take the voxel under each.

    Sampling at the *finest* spacing rather than the coarsest is deliberate: it
    oversamples along the coarse axes, and duplicate voxels are removed afterwards, so
    no voxel inside the ROI is missed. Undersampling would silently drop rows of voxels
    from the mean, which is invisible in the result.
    """
    points = np.asarray(points_lps, dtype=float)
    if len(points) < 3:
        return np.empty((0, 3), dtype=int)

    origin = points[0]
    normal, axis_u, axis_v = _plane_basis(points)
    if normal is None:
        return np.empty((0, 3), dtype=int)

    # Polygon in its own 2D plane coordinates.
    relative = points - origin
    planar = np.stack([relative @ axis_u, relative @ axis_v], axis=1)

    step = float(np.min(voxel_spacing(affine))) / 2
    if step <= 0:
        return np.empty((0, 3), dtype=int)

    low = planar.min(axis=0)
    high = planar.max(axis=0)
    us = np.arange(low[0], high[0] + step, step)
    vs = np.arange(low[1], high[1] + step, step)
    if us.size == 0 or vs.size == 0:
        return np.empty((0, 3), dtype=int)

    grid_u, grid_v = np.meshgrid(us, vs, indexing="ij")
    samples = np.stack([grid_u.ravel(), grid_v.ravel()], axis=1)
    inside = _points_in_polygon(samples, planar)
    if not np.any(inside):
        return np.empty((0, 3), dtype=int)

    kept = samples[inside]
    world = origin + kept[:, 0:1] * axis_u + kept[:, 1:2] * axis_v

    matrix = np.asarray(affine, dtype=float)
    inverse = np.linalg.inv(matrix[:3, :3])
    ras = np.stack([-world[:, 0], -world[:, 1], world[:, 2]], axis=1)
    voxels = np.rint((ras - matrix[:3, 3]) @ inverse.T).astype(int)

    shape = np.asarray(shape, dtype=int)
    within = np.all((voxels >= 0) & (voxels < shape), axis=1)
    voxels = voxels[within]
    if voxels.size == 0:
        return np.empty((0, 3), dtype=int)
    return np.unique(voxels, axis=0)


def _plane_basis(points):
    """An orthonormal basis for the plane the points lie in, or ``None`` if degenerate."""
    origin = points[0]
    normal = None
    for index in range(1, len(points) - 1):
        candidate = np.cross(points[index] - origin, points[index + 1] - origin)
        if np.linalg.norm(candidate) > 1e-9:
            normal = candidate / np.linalg.norm(candidate)
            break
    if normal is None:
        return None, None, None

    # Any vector not parallel to the normal will do to start the basis.
    seed = np.array([1.0, 0.0, 0.0])
    if abs(normal @ seed) > 0.9:
        seed = np.array([0.0, 1.0, 0.0])
    axis_u = np.cross(normal, seed)
    axis_u /= np.linalg.norm(axis_u)
    axis_v = np.cross(normal, axis_u)
    return normal, axis_u, axis_v


def _points_in_polygon(samples, polygon):
    """Even-odd ray casting, vectorised over the samples."""
    inside = np.zeros(len(samples), dtype=bool)
    x, y = samples[:, 0], samples[:, 1]
    count = len(polygon)
    for current in range(count):
        previous = (current - 1) % count
        x1, y1 = polygon[current]
        x2, y2 = polygon[previous]
        straddles = (y1 > y) != (y2 > y)
        # Guard the horizontal-edge division rather than relying on the straddle test:
        # `y1 == y2` makes `straddles` false for every sample, but NumPy evaluates the
        # expression for all of them anyway and would emit a divide-by-zero warning.
        denominator = np.where(y2 == y1, 1.0, y2 - y1)
        crossing = x < (x2 - x1) * (y - y1) / denominator + x1
        inside ^= straddles & crossing
    return inside


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def sample_values(volume, indices, *, rescale_slope=1.0, rescale_intercept=0.0):
    """The modality values at a set of voxel indices.

    The rescale is applied here, unconditionally, and the caller passes the **residual**
    LUT -- the one still outstanding after whatever the loader did. Applying the
    header's own would double the intercept on the two branches upstream already
    scaled. See ``frontend/imaging/metadata/modalityLutModule.js``; this is that rule
    on the server.
    """
    if len(indices) == 0:
        return np.empty(0, dtype=float)
    raw = np.asarray(volume)[indices[:, 0], indices[:, 1], indices[:, 2]].astype(float)
    return raw * float(rescale_slope) + float(rescale_intercept)


def statistics(values):
    """Mean, standard deviation, extremes and count over the sampled values.

    Non-finite samples are dropped rather than propagated -- a masked-out MRI
    background written as NaN would otherwise make every statistic NaN. The count
    reported is the number of samples actually used, so a caller can tell a full ROI
    from one that was mostly excluded.
    """
    values = np.asarray(values, dtype=float)
    finite = values[np.isfinite(values)]
    if finite.size < MINIMUM_VOXELS:
        return None
    return {
        "mean": float(finite.mean()),
        # Population standard deviation (ddof=0), matching what image viewers report;
        # a sample sd would differ visibly on a small ROI and match nothing on screen.
        "stddev": float(finite.std(ddof=0)),
        "min": float(finite.min()),
        "max": float(finite.max()),
        "count": int(finite.size),
    }


def indices_for_geometry(shape, affine, *, geometry_type, points, attributes=None):
    """Resolve one stored 3D geometry to the voxels it covers.

    :raises ValueError: for a geometry type with no defined voxel footprint.
    """
    attributes = attributes or {}
    points = np.asarray(points, dtype=float)

    if geometry_type == Geometry3DType.POINT:
        return point_indices(shape, affine, points[0])
    if geometry_type == Geometry3DType.SPHERE:
        radius = attributes.get("radius")
        if radius is None or not math.isfinite(float(radius)):
            raise ValueError("a sphere needs a finite radius in its attributes")
        return sphere_indices(shape, affine, points[0], float(radius))
    if geometry_type == Geometry3DType.BOX:
        return box_indices(shape, affine, points)
    if geometry_type == Geometry3DType.POLYLINE:
        # A closed planar shape -- Cornerstone's rectangle and ellipse arrive as four
        # coplanar handles. An open two-point polyline (a length) has no interior and
        # correctly yields nothing.
        return polygon_indices(shape, affine, points)

    raise ValueError(f"no voxel footprint defined for geometry type {geometry_type!r}")


def statistics_for_geometry(
    volume,
    affine,
    *,
    geometry_type,
    points,
    attributes=None,
    rescale_slope=1.0,
    rescale_intercept=0.0,
):
    """End to end: a stored geometry and a volume in, modality-unit statistics out.

    Returns ``None`` when the ROI covers no voxels -- outside the volume, or a shape
    with no interior. ``None`` is the honest answer; zero would be a measurement.
    """
    indices = indices_for_geometry(
        np.asarray(volume).shape,
        affine,
        geometry_type=geometry_type,
        points=points,
        attributes=attributes,
    )
    values = sample_values(
        volume, indices, rescale_slope=rescale_slope, rescale_intercept=rescale_intercept
    )
    return statistics(values)
