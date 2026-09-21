"""Locate the committed Cornerstone3D bundle from ``static/vendor/cornerstone/``.

The bundle is emitted by ``scripts/build_frontend.sh`` into a *version-stamped
directory*, and ``manifest.json`` beside it names the current one. Per-file hashing is
not an option here: ``yggdrasil/settings.py:214-219`` deliberately uses
``CompressedStaticFilesStorage`` rather than manifest storage, because templates
reference assets by literal path -- so the directory carries the hash instead.

Read once at import, exactly like ``_read_app_version()`` at
``yggdrasil/settings.py:22-30``, and with the same rule: **a missing or broken manifest
must never prevent the app from booting, and must never raise from a template.** The
worst it may do is degrade to a console error in the browser, which is what
``common/templatetags/cornerstone.py`` does with a ``None`` from here.

See docs/cornerstone-roadmap.md, Phase 1.
"""

import json
import logging
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

#: Path of the manifest relative to any static-files root.
MANIFEST_RELATIVE_PATH = Path("vendor") / "cornerstone" / "manifest.json"

#: Directory, relative to a static root, holding the emitted per-surface entries.
_ENTRY_SUBDIR = "app"

_manifest = None


def _candidate_manifest_paths():
    """Yield every place the manifest could live, source tree before collected."""
    for directory in getattr(settings, "STATICFILES_DIRS", []) or []:
        yield Path(directory) / MANIFEST_RELATIVE_PATH
    static_root = getattr(settings, "STATIC_ROOT", None)
    if static_root:
        yield Path(static_root) / MANIFEST_RELATIVE_PATH


def _read_manifest():
    """Return the parsed manifest, or ``None`` if it is absent or unusable."""
    for path in _candidate_manifest_paths():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            # Present but unreadable or malformed: worth a log line, not a 500.
            logger.warning("Cornerstone manifest at %s is unusable: %s", path, exc)
            return None
        if not isinstance(data, dict) or not data.get("build"):
            logger.warning("Cornerstone manifest at %s has no build id", path)
            return None
        return data
    return None


def reload():
    """Re-read the manifest. Test seam; also useful after a local rebuild."""
    global _manifest
    _manifest = _read_manifest()
    return _manifest


def get_manifest():
    """Return the cached manifest dict, or ``None``."""
    if _manifest is None:
        reload()
    return _manifest


def get_build():
    """Return the current build id, or ``None`` if there is no usable manifest."""
    manifest = get_manifest()
    return manifest.get("build") if manifest else None


def get_entries():
    """Return the list of surface entry names the bundle provides."""
    manifest = get_manifest()
    entries = manifest.get("entries") if manifest else None
    return list(entries) if isinstance(entries, list) else []


def entry_static_path(name):
    """Return the ``{% static %}``-relative path of one entry, or ``None``.

    ``None`` means "do not emit a script tag": either there is no usable manifest, or
    the caller asked for a surface this bundle does not contain. Both are the caller's
    problem to report, not ours to guess around.
    """
    build = get_build()
    if not build:
        return None
    if name not in get_entries():
        logger.warning("Unknown Cornerstone entry %r (have: %s)", name, ", ".join(get_entries()))
        return None
    return "vendor/cornerstone/{}/{}/{}.js".format(build, _ENTRY_SUBDIR, name)


reload()
