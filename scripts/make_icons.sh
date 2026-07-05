#!/usr/bin/env bash
# Regenerate the raster favicon set from the SVG master in static/icons/.
# Source of truth: static/icons/favicon.svg (the ink+green monogram square),
# used for the favicon PNGs, the .ico, and the apple-touch icon.
#
# Prefers rsvg-convert or ImageMagick if installed; otherwise falls back to a
# Python (cairosvg + Pillow) pipeline. Outputs are committed to the repo, so this
# only needs re-running when the SVG masters change.
set -euo pipefail

cd "$(dirname "$0")/.."
ICONS=static/icons
FAVICON_SVG="$ICONS/favicon.svg"

render() { # <svg> <size> <out.png>
  local svg="$1" size="$2" out="$3"
  if command -v rsvg-convert >/dev/null 2>&1; then
    rsvg-convert -w "$size" -h "$size" "$svg" -o "$out"
  elif command -v magick >/dev/null 2>&1; then
    magick -background none -density 384 "$svg" -resize "${size}x${size}" "$out"
  elif command -v convert >/dev/null 2>&1; then
    convert -background none -density 384 "$svg" -resize "${size}x${size}" "$out"
  else
    python3 - "$svg" "$size" "$out" <<'PY'
import sys, cairosvg
svg, size, out = sys.argv[1], int(sys.argv[2]), sys.argv[3]
cairosvg.svg2png(url=svg, write_to=out, output_width=size, output_height=size)
PY
  fi
}

render "$FAVICON_SVG" 16  "$ICONS/favicon-16.png"
render "$FAVICON_SVG" 32  "$ICONS/favicon-32.png"
# apple-touch needs an opaque background (iOS fills transparency with black),
# so it renders from the dark-square favicon master, not the transparent logo.
render "$FAVICON_SVG" 180  "$ICONS/apple-touch-icon.png"

# favicon.ico (16 + 32) from the two PNGs.
if command -v magick >/dev/null 2>&1; then
  magick "$ICONS/favicon-16.png" "$ICONS/favicon-32.png" "$ICONS/favicon.ico"
elif command -v convert >/dev/null 2>&1; then
  convert "$ICONS/favicon-16.png" "$ICONS/favicon-32.png" "$ICONS/favicon.ico"
else
  python3 - "$ICONS/favicon-32.png" "$ICONS/favicon.ico" <<'PY'
import sys
from PIL import Image
src, out = sys.argv[1], sys.argv[2]
Image.open(src).save(out, sizes=[(16, 16), (32, 32)])
PY
fi

echo "Icons regenerated in $ICONS/"
