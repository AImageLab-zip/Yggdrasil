"""Pure validation for annotation content.

Nothing in this package imports a model, opens a file or touches the database.
A validator takes plain values and either returns cleaned ones or raises
``django.core.exceptions.ValidationError``. That constraint is what makes the
same rule usable from a service before a write, from a management command
sweeping legacy rows, and from a test with a literal dict -- and it is what
keeps the rules readable, because a function that cannot query has to state its
requirement rather than look it up.

Anything needing the database (does this label belong to this set's schema? is
this target's resource the one the geometry was measured against?) belongs in
``annotations.services``, which is the only layer allowed to write.
"""

from annotations.validators.geometry import validate_geometry_2d, validate_geometry_3d
from annotations.validators.measurements import validate_measurement
from annotations.validators.selectors import (
    validate_bounds,
    validate_item_selector_pairing,
    validate_selector,
)

__all__ = [
    "validate_bounds",
    "validate_geometry_2d",
    "validate_geometry_3d",
    "validate_item_selector_pairing",
    "validate_measurement",
    "validate_selector",
]
