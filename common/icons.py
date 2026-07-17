"""Icon name resolution for the ``{% icon %}`` template tag.

The 2.0 rebuild standardises on **Font Awesome 6** (self-hosted, see
static/vendor/fontawesome), matching the design handoff, which draws every glyph
from FA. The database has always stored Font Awesome class strings in
``Project.icon`` / ``Modality.icon`` (e.g. "fas fa-tooth", "fas fa-brain"), so
those values now render natively and need no translation.

This module exists for two remaining jobs:

1. Normalise the several shorthands templates pass — bare ("tooth"),
   FA-prefixed ("fa-tooth"), or a full class string ("fas fa-tooth") — into one
   renderable class string.
2. Alias the handful of names that changed between FA5 and FA6 (e.g.
   "sign-in-alt" -> "right-to-bracket") plus the Lucide-era names left behind by
   the interim sprite (e.g. "layout-dashboard" -> "gauge-high"), so existing call
   sites and stored DB values keep working.

Anything not listed is passed through unchanged, which is the common case.
"""

# Names that must be rewritten: FA5 -> FA6 renames, and Lucide-era leftovers
# from the sprite that briefly replaced Font Awesome.
ICON_ALIASES = {
    # --- FA5 -> FA6 renames ---
    "sign-in-alt": "right-to-bracket",
    "sign-out-alt": "arrow-right-from-bracket",
    "heartbeat": "heart-pulse",
    "save": "floppy-disk",
    "plus-circle": "circle-plus",
    "user-circle": "circle-user",
    "users-cog": "users-gear",
    "cog": "gear",
    "mobile-alt": "mobile-screen-button",
    "sync": "rotate",
    "sync-alt": "rotate",
    "exclamation-triangle": "triangle-exclamation",
    "check-circle": "circle-check",
    "times": "xmark",
    "times-circle": "circle-xmark",
    "file-alt": "file-lines",
    "file-archive": "file-zipper",
    "external-link-alt": "up-right-from-square",
    "compress-arrows-alt": "compress",
    "expand-arrows-alt": "expand",
    "tachometer-alt": "gauge-high",
    "search": "magnifying-glass",
    "trash-alt": "trash-can",
    # --- Lucide-era names (interim sprite) -> Font Awesome ---
    "layout-dashboard": "gauge-high",
    "circle-alert": "triangle-exclamation",
    "menu": "bars",
    "settings": "gear",
    "box": "cube",
    "message-square": "comment",
    "chart-column": "chart-bar",
    "pen-tool": "draw-polygon",
    "crosshair": "crosshairs",
    "square-check": "check-square",
    "calendar-days": "calendar-week",
    "trending-up": "arrow-trend-up",
}

# Glyphs that live in the FA "brands" family rather than "solid".
BRAND_ICONS = {"github", "gitlab", "google", "twitter", "linkedin", "docker", "python"}


def resolve(name):
    """Normalise any supported icon shorthand into a Font Awesome class string.

    Accepts "tooth", "fa-tooth", "fas fa-tooth" or "fa-brands fa-github" and
    returns a complete class string such as "fas fa-tooth".
    """
    if not name:
        return ""

    raw = str(name).strip()

    # Already a full class string (e.g. the DB's "fas fa-brain", or an explicit
    # family like "fa-brands fa-github") — trust it and pass it through.
    if " " in raw:
        return raw

    # Strip an "fa-" prefix to get at the bare glyph name.
    bare = raw[3:] if raw.startswith("fa-") else raw
    bare = ICON_ALIASES.get(bare, bare)

    family = "fa-brands" if bare in BRAND_ICONS else "fas"
    return "{} fa-{}".format(family, bare)
