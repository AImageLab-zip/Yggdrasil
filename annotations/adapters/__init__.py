"""Pure translation between outside representations and the annotation model.

An adapter reads a legacy row, a viewer's serialized state or an interchange
document and returns descriptor dicts saying what to write. It never queries,
never opens a file and never saves -- ``annotations.services.apply`` does that
part, because resolving a label code or reusing a selector needs the database.

The split is what keeps a conversion testable with a literal input, and it is
what makes decision #6's cross-check window meaningful: the same pure function
that produced the converted rows can be re-run against the legacy row to see
whether the two still agree.
"""

from annotations.adapters import descriptors

__all__ = ["descriptors"]
