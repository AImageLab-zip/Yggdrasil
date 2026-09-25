"""Helper utilities for views.

`render_with_fallback` / `redirect_with_namespace` / `bulk_upload_url_for` now
live in `common/view_helpers.py` (brain and cardiology both need them too) and
are re-exported here so existing import paths keep working.
"""
from common.view_helpers import bulk_upload_url_for, redirect_with_namespace, render_with_fallback

__all__ = ["render_with_fallback", "redirect_with_namespace", "bulk_upload_url_for"]
