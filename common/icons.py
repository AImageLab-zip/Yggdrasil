"""Font Awesome -> Lucide icon name mapping.

Single source of truth for the 2.0 icon system. The project migrated from
Font Awesome (CDN) to a self-hosted Lucide SVG sprite. Icon names are still
stored as Font Awesome classes in the database (Project.icon / Modality.icon,
e.g. "fas fa-tooth"), so the ``{% icon %}`` template tag resolves those legacy
strings through this map. Values are Lucide icon names; the sprite ships the
matching ``<symbol id="lc-NAME">`` in static/icons/lucide-sprite.svg.

To add an icon: add the fa->lucide entry here, then regenerate the sprite with
scripts/build_sprite.py (which reads this map).
"""

FA_TO_LUCIDE = {
    "adjust": "contrast",
    "arrow-down": "arrow-down",
    "arrow-left": "arrow-left",
    "arrow-right": "arrow-right",
    "arrow-up": "arrow-up",
    "brain": "brain",
    "calendar-week": "calendar-days",
    "camera": "camera",
    "chart-line": "chart-line",
    "check": "check",
    "check-circle": "circle-check",
    "check-square": "square-check",
    "chevron-down": "chevron-down",
    "chevron-left": "chevron-left",
    "chevron-right": "chevron-right",
    "chevron-up": "chevron-up",
    "circle": "circle",
    "circle-dot": "circle-dot",
    "clipboard-check": "clipboard-check",
    "clipboard-list": "clipboard-list",
    "clock": "clock",
    "cog": "settings",
    "comment": "message-square",
    "compass": "compass",
    "compress-arrows-alt": "minimize-2",
    "copy": "copy",
    "crosshairs": "crosshair",
    "cube": "box",
    "database": "database",
    "download": "download",
    "draw-polygon": "pen-tool",
    "edit": "pencil",
    "envelope": "mail",
    "eraser": "eraser",
    "exclamation-circle": "circle-alert",
    "exclamation-triangle": "triangle-alert",
    "eye": "eye",
    "eye-slash": "eye-off",
    "file": "file",
    "file-alt": "file-text",
    "file-archive": "file-archive",
    "file-export": "file-output",
    "file-import": "file-input",
    "file-medical": "file-plus",
    "file-medical-alt": "file-text",
    "file-text": "file-text",
    "filter": "filter",
    "folder": "folder",
    "folder-open": "folder-open",
    "folder-tree": "folder-tree",
    "github": "github",
    "hand-paper": "hand",
    "hand-pointer": "pointer",
    "hdd": "hard-drive",
    "heartbeat": "activity",
    "home": "house",
    "hourglass-half": "hourglass",
    "inbox": "inbox",
    "info-circle": "info",
    "layer-group": "layers",
    "link": "link",
    "list": "list",
    "lock": "lock",
    "magic": "sparkles",
    "map-pin": "map-pin",
    "microphone": "mic",
    "microphone-slash": "mic-off",
    "mobile-alt": "smartphone",
    "mouse": "mouse",
    "paint-brush": "brush",
    "palette": "palette",
    "panorama": "image",
    "paper-plane": "send",
    "pause": "pause",
    "play": "play",
    "plus": "plus",
    "plus-circle": "circle-plus",
    "project-diagram": "workflow",
    "question-circle": "circle-help",
    "robot": "bot",
    "ruler": "ruler",
    "save": "save",
    "search-minus": "zoom-out",
    "search-plus": "zoom-in",
    "shapes": "shapes",
    "sign-in-alt": "log-in",
    "sign-out-alt": "log-out",
    "skull": "skull",
    "spinner": "loader-circle",
    "square": "square",
    "step-backward": "skip-back",
    "step-forward": "skip-forward",
    "stop": "square",
    "stream": "align-left",
    "sync": "refresh-cw",
    "table": "table",
    "tachometer-alt": "gauge",
    "tag": "tag",
    "tags": "tags",
    "teeth-open": "tooth",
    "th": "grid-3x3",
    "times": "x",
    "tooth": "tooth",
    "trash": "trash-2",
    "upload": "upload",
    "user": "user",
    "user-circle": "circle-user",
    "user-plus": "user-plus",
    "users": "users",
    "users-cog": "user-cog",
    "user-secret": "user-round",
    "video": "video",
    "x-ray": "scan",
}

# Lucide names known to ship in the sprite (used directly in 2.0 templates,
# no Font Awesome ancestry). Kept here so resolve() passes them through.
_LUCIDE_PASSTHROUGH = {
    "layout-dashboard",
    "server",
    "shield-check",
    "archive",
    "menu",
}

# Fallback Lucide glyph when a name can't be resolved.
FALLBACK = "circle"


def resolve(value: str) -> str:
    """Resolve any icon reference to a Lucide sprite name.

    Accepts a Font Awesome class string ("fas fa-tooth", "fa-tooth"), a bare
    Font Awesome base name ("tooth"), or a Lucide name ("layout-dashboard").
    """
    if not value:
        return FALLBACK
    base = ""
    for token in str(value).split():
        if token.startswith("fa-"):
            base = token[3:]
            break
        base = token  # last non-prefixed token wins (bare name / lucide name)
    base = base or FALLBACK
    if base in FA_TO_LUCIDE:
        return FA_TO_LUCIDE[base]
    if base in _LUCIDE_PASSTHROUGH:
        return base
    # Assume it's already a valid lucide name; the sprite <use> degrades to
    # empty if not, which is preferable to raising in a template.
    return base
