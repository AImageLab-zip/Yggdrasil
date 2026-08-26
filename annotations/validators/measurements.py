"""Measurement rules: a number, a unit, and whether the unit is earned.

The database refuses a millimetre value on an uncalibrated measurement, which is
the backstop. These rules are the same statement made earlier and in more
detail, so a caller gets "an area is reported in mm2 or px2, not mm" instead of
an ``IntegrityError`` with a constraint name in it -- and so the kind/unit
pairing, which the database does not check, is checked somewhere.

Pure: values in, ``ValidationError`` out.
"""

import math

from django.core.exceptions import ValidationError

from annotations.constants import MeasurementKind, MeasurementUnit

#: Units each measurement kind may be reported in. A kind absent from this table
#: is unknown and rejected outright, so adding a kind means deciding its units.
UNITS_BY_KIND = {
    MeasurementKind.LENGTH: frozenset({MeasurementUnit.MM, MeasurementUnit.PX}),
    MeasurementKind.PERIMETER: frozenset({MeasurementUnit.MM, MeasurementUnit.PX}),
    MeasurementKind.DIAMETER: frozenset({MeasurementUnit.MM, MeasurementUnit.PX}),
    MeasurementKind.AREA: frozenset({MeasurementUnit.MM2, MeasurementUnit.PX2}),
    MeasurementKind.VOLUME: frozenset({MeasurementUnit.MM3, MeasurementUnit.PX3}),
    MeasurementKind.ANGLE: frozenset({MeasurementUnit.DEG}),
    # Intensity statistics carry the modality's own unit, or none where the
    # modality has no calibrated scale (which is most of this system's CBCT).
    MeasurementKind.MEAN: frozenset({MeasurementUnit.HU, MeasurementUnit.NONE}),
    MeasurementKind.STDDEV: frozenset({MeasurementUnit.HU, MeasurementUnit.NONE}),
    MeasurementKind.MIN: frozenset({MeasurementUnit.HU, MeasurementUnit.NONE}),
    MeasurementKind.MAX: frozenset({MeasurementUnit.HU, MeasurementUnit.NONE}),
    MeasurementKind.COUNT: frozenset({MeasurementUnit.NONE}),
}

#: Kinds whose value is a magnitude and therefore cannot be negative. Intensity
#: statistics are absent on purpose: -412 HU is air, not an error.
NON_NEGATIVE_KINDS = frozenset(
    {
        MeasurementKind.LENGTH,
        MeasurementKind.PERIMETER,
        MeasurementKind.DIAMETER,
        MeasurementKind.AREA,
        MeasurementKind.VOLUME,
        MeasurementKind.COUNT,
    }
)

#: Kinds computed over a sample, which have to say how large the sample was.
#: A mean over an unstated number of voxels is not a reportable figure.
SAMPLED_KINDS = frozenset(
    {
        MeasurementKind.MEAN,
        MeasurementKind.STDDEV,
        MeasurementKind.MIN,
        MeasurementKind.MAX,
    }
)


def validate_measurement(*, kind, value, unit, is_calibrated, sample_count=None):
    """Validate one measurement's kind, unit, value and calibration together."""
    if kind not in MeasurementKind.ALL:
        raise ValidationError(f"unknown measurement kind {kind!r}")
    if unit not in MeasurementUnit.ALL:
        raise ValidationError(f"unknown measurement unit {unit!r}")

    allowed = UNITS_BY_KIND[kind]
    if unit not in allowed:
        raise ValidationError(
            f"a {kind} is reported in {' or '.join(sorted(allowed))}, not {unit}"
        )

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"a measurement value must be a number, got {value!r}")
    value = float(value)
    if not math.isfinite(value):
        raise ValidationError("a measurement value must be finite")
    if kind in NON_NEGATIVE_KINDS and value < 0:
        raise ValidationError(f"a {kind} cannot be negative, got {value}")

    # The rule this model exists for, restated ahead of the database's own
    # check so the caller gets a sentence instead of a constraint name.
    if unit in MeasurementUnit.REQUIRES_CALIBRATION and not is_calibrated:
        raise ValidationError(
            f"{unit} claims a real-world size, so the measurement has to be "
            "calibrated; report an uncalibrated one in pixels instead"
        )

    if kind in SAMPLED_KINDS:
        if sample_count is None:
            raise ValidationError(f"a {kind} must record how many samples it covers")
        if isinstance(sample_count, bool) or not isinstance(sample_count, int):
            raise ValidationError("sample_count must be an integer")
        if sample_count <= 0:
            raise ValidationError("a statistic over zero samples is not a measurement")
    elif sample_count is not None:
        raise ValidationError(f"a {kind} is not computed over a sample")

    return value
