"""Brain view helper utilities.

`render_with_fallback` / `redirect_with_namespace` live in
`common/view_helpers.py` and are re-exported here so brain call sites keep their
import path. The old brain-local copies hardcoded the "brain" namespace; the
shared ones read it off ``request.resolver_match``, which is what brain's URLs
supply anyway.
"""

from common.view_helpers import redirect_with_namespace, render_with_fallback

__all__ = ["render_with_fallback", "redirect_with_namespace"]
