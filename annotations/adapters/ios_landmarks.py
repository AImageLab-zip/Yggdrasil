"""IOS dental landmark documents in, 3D descriptors out -- and back again.

The canonical conversion, and the one this module exists to be: both the live editor and
``annotations_materialize_landmarks`` go through it, so what the converter wrote and what
the viewer writes are the same rows by construction rather than by two people agreeing.
The same argument :mod:`annotations.adapters.tooth_segmentation` makes for polygons, for
the same reason -- decision #6 keeps the legacy artifact readable for one release as a
cross-check, and two implementations of one conversion drift.

**The frame is ``resource_local``, and that is a claim about a specific mesh.** These
points come out of ``worldToLocal`` against one STL jaw scan: they are coordinates in that
mesh's own object space, and the mesh has no registration to the patient. Recording them
as ``patient_lps_mm`` would be a false statement that a later fusion or export would act
on. Two consequences the model already enforces rather than trusting:
``validate_geometry_3d`` refuses a ``frame_of_reference_uid`` alongside ``resource_local``,
and ``add_spatial_3d`` refuses ``resource_local`` without a resolved target resource. The
second is what makes "which mesh" un-skippable on the write path.

Pure: values in, descriptor dicts out. Nothing here queries or resolves a label; the FDI
code travels as ``label_code`` and :mod:`annotations.services.apply` looks it up in the
set's own schema, where an unknown code is *refused* rather than written unlabelled.
"""

import re

from django.core.exceptions import ValidationError

from annotations.adapters import descriptors
from annotations.constants import CoordinateSystem, Geometry3DType

#: The ``AnnotationSet.kind`` these landmarks are filed under. Already a valid choice and
#: already what the converter uses -- reusing it is what lets the cross-check find both
#: representations of one study.
LANDMARKS_KIND = "ios_landmarks"

#: The vocabulary the FDI codes resolve against, seeded by ``migrations/0002``.
FDI_SCHEMA_SLUG = "fdi-permanent"
FDI_SCHEMA_VERSION = 1

#: ``<patient_id>_<jaw>_FDI_<tooth>``, the key format the legacy editor wrote and the
#: legacy view validated on every save.
LANDMARK_KEY_RE = re.compile(r"^(\d+)_(upper|lower)_FDI_(\d{2})$")

#: Landmark entries holding a single point.
LANDMARK_POINT_TYPES = frozenset(
    {"incisal", "outer", "bracket", "gingival", "mesial", "distal", "inner", "facial"}
)
#: Landmark entries holding a list of points.
LANDMARK_MULTI_POINT_TYPES = frozenset({"cusps", "planar"})
#: The four named vectors of a tooth's base plane.
LANDMARK_PLANE_KEYS = ("origin", "xAxis", "yAxis", "zAxis")

#: ``in_<jaw>_FDI_<tooth>``, the key format the offline landmark worker emits.
#:
#: The worker names the arch and the tooth but not the patient, because its input is one
#: scan pair and it has no patient id to put there.
WORKER_LANDMARK_KEY_RE = re.compile(r"^in_(upper|lower)_FDI_(\d{2})$")

#: The attribute name carrying which landmark a point *is*.
#:
#: The type is an attribute rather than a label because the label slot is spent on the FDI
#: code, which is what decides the segment a point is exported under. Adding a second
#: vocabulary for ten type names would need a schema migration to say something the
#: document already says unambiguously.
LANDMARK_ATTRIBUTE = "landmark"

#: More points than this on one tooth's multi-point type is a client bug, not an anatomy.
#:
#: Mirrors the cap the legacy view enforced (``maxillo/views/patient_data.py``), so a
#: document that was accepted before is accepted now.
MAX_POINTS_PER_TYPE = 500


def _point(value, where):
    """One ``[x, y, z]``, refused rather than coerced.

    ``bool`` is rejected explicitly: it is an ``int`` in Python, so a stray ``True`` would
    otherwise store as a coordinate of 1.0 and read back as a plausible number.
    """
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValidationError(f"{where} must be a list of exactly three coordinates")
    out = []
    for ordinate in value:
        if isinstance(ordinate, bool) or not isinstance(ordinate, (int, float)):
            raise ValidationError(f"{where} has a non-numeric coordinate")
        out.append(float(ordinate))
    return out


def jaw_for_tooth(tooth):
    """Which arch an FDI code belongs to.

    Quadrants 1 and 2 are the upper arch, 3 and 4 the lower. The legacy document carried
    the jaw in its key *and* the FDI code, and the legacy view enforced that the two
    agreed, so the jaw was never independent information -- which is why the write path
    can derive it rather than trusting a client to send it.
    """
    return "upper" if str(tooth)[:1] in {"1", "2"} else "lower"


def landmark_key(patient_id, tooth):
    """The document key for one tooth, in the format the legacy editor wrote."""
    return f"{patient_id}_{jaw_for_tooth(tooth)}_FDI_{tooth}"


def ios_landmarks(document, *, patient_id):
    """Convert the IOS landmark JSON document into 3D descriptors.

    Each tooth's ``basePlane`` becomes one ``plane`` item from its origin and two axis
    endpoints, with the third axis kept in ``attributes`` -- the model stores a plane as
    three points, and z is derivable but not identical to a recomputed cross product once
    floats are involved, so it is preserved rather than reconstructed.

    :param document: the FDI-keyed map, exactly as the legacy artifact stored it.
    :param patient_id: every key must name this patient; a key that does not is refused
        rather than skipped, because a document holding another patient's landmarks is a
        document nobody should act on.
    :returns: descriptors ordered by key, then type, then point index, so two conversions
        of one study produce identical output and the cross-check compares like with like.
    """
    if not isinstance(document, dict):
        raise ValidationError("the landmark document must be a JSON object")

    out = []
    for key in sorted(document):
        match = LANDMARK_KEY_RE.match(str(key))
        if not match:
            raise ValidationError(f"landmark key {key!r} is not in the FDI format")
        if match.group(1) != str(patient_id):
            raise ValidationError(
                f"landmark key {key!r} belongs to patient {match.group(1)}, not {patient_id}"
            )
        jaw, tooth = match.group(2), match.group(3)
        if jaw != jaw_for_tooth(tooth):
            raise ValidationError(
                f"landmark key {key!r} puts tooth {tooth} in the {jaw} jaw, but its FDI "
                f"quadrant makes it {jaw_for_tooth(tooth)}"
            )
        entry = document[key]
        if not isinstance(entry, dict):
            raise ValidationError(f"landmark entry {key!r} must be an object")

        shared = {"jaw": jaw, "fdi": tooth, "legacy_key": str(key)}

        for name in sorted(LANDMARK_POINT_TYPES & set(entry)):
            out.append(
                descriptors.spatial_3d(
                    geometry_type=Geometry3DType.POINT,
                    coordinate_system=CoordinateSystem.RESOURCE_LOCAL,
                    points=[_point(entry[name], f"{key}.{name}")],
                    label_code=tooth,
                    attributes={**shared, LANDMARK_ATTRIBUTE: name},
                )
            )

        for name in sorted(LANDMARK_MULTI_POINT_TYPES & set(entry)):
            points = entry[name]
            if not isinstance(points, list):
                raise ValidationError(f"{key}.{name} must be a list of points")
            if len(points) > MAX_POINTS_PER_TYPE:
                raise ValidationError(
                    f"{key}.{name} has {len(points)} points, more than the "
                    f"{MAX_POINTS_PER_TYPE} limit"
                )
            # One item per point rather than a polyline: cusps are unordered landmarks
            # that happen to be stored in a list, and a polyline would assert an order and
            # a connectivity the original never had.
            for index, point in enumerate(points):
                out.append(
                    descriptors.spatial_3d(
                        geometry_type=Geometry3DType.POINT,
                        coordinate_system=CoordinateSystem.RESOURCE_LOCAL,
                        points=[_point(point, f"{key}.{name}[{index}]")],
                        label_code=tooth,
                        order=index,
                        attributes={**shared, LANDMARK_ATTRIBUTE: name, "index": index},
                    )
                )

        plane = entry.get("basePlane")
        if plane is not None:
            if not isinstance(plane, dict) or set(plane) != set(LANDMARK_PLANE_KEYS):
                raise ValidationError(
                    f"{key}.basePlane must hold exactly {list(LANDMARK_PLANE_KEYS)}"
                )
            out.append(
                descriptors.spatial_3d(
                    geometry_type=Geometry3DType.PLANE,
                    coordinate_system=CoordinateSystem.RESOURCE_LOCAL,
                    points=[
                        _point(plane["origin"], f"{key}.basePlane.origin"),
                        _point(plane["xAxis"], f"{key}.basePlane.xAxis"),
                        _point(plane["yAxis"], f"{key}.basePlane.yAxis"),
                    ],
                    label_code=tooth,
                    attributes={
                        **shared,
                        LANDMARK_ATTRIBUTE: "basePlane",
                        "zAxis": _point(plane["zAxis"], f"{key}.basePlane.zAxis"),
                    },
                )
            )

    return out


def landmarks_from_items(items, *, patient_id):
    """The inverse: canonical rows back into the document the editor and export read.

    Rebuilt from the *items*, not from a payload, because for this surface the items are
    the only representation -- a landmark is a point, and there is no handle convention the
    model cannot express, so a second copy would only ever go stale.

    Multi-point types are re-ordered by the stored ``index`` rather than by row order: a
    revision's rows carry forward as fresh inserts, so their ids say when the last save
    happened and not which cusp came first.

    :param items: ``SpatialAnnotation3DItem`` rows, or anything with ``points``,
        ``attributes`` and a ``label``.
    :returns: the FDI-keyed document, keys sorted, ready to compare byte for byte with
        what the legacy artifact held.
    """
    document = {}
    for item in items:
        attributes = item.attributes if isinstance(item.attributes, dict) else {}
        name = attributes.get(LANDMARK_ATTRIBUTE)
        if not name:
            continue

        # The label is authoritative when present; `attributes['fdi']` is the fallback for
        # a row written before the label existed.
        tooth = None
        label = getattr(item, "label", None)
        if label is not None and label.code:
            tooth = str(label.code)
        elif attributes.get("fdi"):
            tooth = str(attributes["fdi"])
        if not tooth:
            continue

        points = item.points or []
        entry = document.setdefault(landmark_key(patient_id, tooth), {})

        if name == "basePlane":
            if len(points) != 3:
                continue
            entry["basePlane"] = {
                "origin": list(points[0]),
                "xAxis": list(points[1]),
                "yAxis": list(points[2]),
                # Preserved on the way in precisely so it does not have to be recomputed
                # here; a cross product of floats is not the number that was stored.
                "zAxis": list(attributes.get("zAxis") or [0.0, 0.0, 0.0]),
            }
        elif name in LANDMARK_MULTI_POINT_TYPES:
            if not points:
                continue
            index = attributes.get("index")
            entry.setdefault(name, []).append(
                (index if isinstance(index, int) else item.order, list(points[0]))
            )
        elif name in LANDMARK_POINT_TYPES:
            if not points:
                continue
            entry[name] = list(points[0])

    for entry in document.values():
        for name in LANDMARK_MULTI_POINT_TYPES:
            if name in entry:
                entry[name] = [point for _, point in sorted(entry[name], key=lambda p: p[0])]

    return {key: document[key] for key in sorted(document)}


def normalize_worker_document(payload, *, patient_id):
    """A landmark job's output, as this patient's canonical document.

    The worker emits ``in_upper_FDI_11`` keys -- its input is one scan pair, so it has no
    patient id to put in them -- and may wrap the map in a ``{"landmarks": ...}`` envelope.
    Both are accepted here and rewritten to canonical keys.

    **Keys naming another patient are dropped rather than refused**, which is the opposite
    of :func:`ios_landmarks`. That asymmetry is deliberate and is the one the legacy read
    path had: a *human* save carrying another patient's key is a client bug worth
    refusing, while a worker aggregate legitimately covers several scans and the caller
    wants this patient's share of it. It is also why
    ``annotations_materialize_landmarks`` does not use this function -- a worker-shaped
    document in an ``ios_landmarks`` row is a row that should not exist, and accepting one
    there would convert model output as if a person had placed it.

    :returns: the canonical ``{<patient_id>_<jaw>_FDI_<tooth>: entry}`` document. Entries
        are passed through untouched; :func:`ios_landmarks` is what validates them, and it
        runs before anything is written.
    """
    if isinstance(payload, dict) and isinstance(payload.get("landmarks"), dict):
        payload = payload["landmarks"]
    if not isinstance(payload, dict):
        raise ValidationError("the landmark document must be a JSON object")

    document = {}
    for key, entry in payload.items():
        match = LANDMARK_KEY_RE.match(str(key))
        # Compared as a *number*, not a string: a worker that zero-pads writes
        # ``012_upper_FDI_11`` for patient 12, and the legacy read path accepted it. The
        # strict converter compares as text, which is right there -- an ``ios_landmarks``
        # row is written by this application and has no business being padded -- but
        # dropping a whole arch of predictions over a leading zero is not.
        if match and int(match.group(1)) == int(patient_id):
            tooth = match.group(3)
        else:
            worker_match = WORKER_LANDMARK_KEY_RE.match(str(key))
            if not worker_match:
                continue
            tooth = worker_match.group(2)
        document[landmark_key(patient_id, tooth)] = entry
    return document


def by_jaw(document):
    """Split a canonical document into the FDI-keyed, per-arch wire format.

    The inverse of what the save service does on the way in, and the shape both the editor
    and the prediction writer hand to :func:`~annotations.services.ios_landmarks.save_ios_landmarks`.
    """
    jaws = {"upper": {}, "lower": {}}
    for key, entry in document.items():
        match = LANDMARK_KEY_RE.match(str(key))
        if match:
            jaws[match.group(2)][match.group(3)] = entry
    return jaws
