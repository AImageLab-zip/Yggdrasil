#!/usr/bin/env bash
#
# Compile static/css/tailwind.src.css -> static/css/tailwind.css.
#
# tailwind.config.js has documented this script as the CSS pipeline since 2.0,
# but the file itself never existed (`git log -- scripts/build_css.sh` is empty),
# so for two releases the committed stylesheet could not be derived from its own
# input. A Tailwind class added to a template either happened to already be in
# the compiled output or silently did nothing. This is that missing step.
#
# Toolchain: the standalone Tailwind CLI, pinned, matching the header comment in
# tailwind.config.js ("no node/npm at deploy time"). The binary is downloaded on
# first use into scripts/.bin/, which .gitignore already excludes -- that entry
# predates this script and is the surviving evidence of what the pipeline was
# meant to be. Nothing here runs at deploy time: static/css/tailwind.css is
# committed and served by WhiteNoise.
#
#   scripts/build_css.sh            rebuild static/css/tailwind.css in place
#   scripts/build_css.sh --check    build to a temp file and diff; exit 1 on drift
#
# ---------------------------------------------------------------------------
# The committed static/css/tailwind.css is NOT reproducible by this script, and
# that is a fact about the committed file, not a bug here. Two independent
# reasons, both established by building HEAD in a pristine checkout:
#
#   1. It is stale. Rebuilding HEAD's own templates yields .absolute, .inline,
#      .pb-2 and .sr-only, which the committed file lacks, and drops .bg-danger,
#      .border-collapse, .ms-4 and .outline, which it carries. Every other rule
#      matches selector for selector, declaration for declaration.
#   2. It was minified by a different tool. Tailwind 3.4's --minify uses
#      lightningcss and emits declarations in source order; the committed file
#      has them sorted within each rule. Same rules, same values, ~69 bytes
#      apart, purely from ordering.
#
# So the first run of this script will produce a large but semantically
# equivalent diff, plus those eight utilities. Review it once, commit it, and
# from then on `--check` is meaningful.
# ---------------------------------------------------------------------------

set -euo pipefail

cd "$(dirname "$0")/.."

TAILWIND_VERSION="3.4.17"
BIN_DIR="scripts/.bin"
BIN="${BIN_DIR}/tailwindcss-${TAILWIND_VERSION}"

# Pinned so a compromised or swapped release cannot silently rewrite every
# stylesheet the platform serves. Update both lines together.
TAILWIND_SHA256_linux_x86_64="7d24f7fa191d2193b78cd5f5a42a6093e14409521908529f42d80b11fde1f1d4"

case "$(uname -s)-$(uname -m)" in
    Linux-x86_64)   ASSET="tailwindcss-linux-x64";   EXPECTED_SHA="$TAILWIND_SHA256_linux_x86_64" ;;
    Linux-aarch64)  ASSET="tailwindcss-linux-arm64"; EXPECTED_SHA="" ;;
    Darwin-arm64)   ASSET="tailwindcss-macos-arm64"; EXPECTED_SHA="" ;;
    Darwin-x86_64)  ASSET="tailwindcss-macos-x64";   EXPECTED_SHA="" ;;
    *)
        echo "No pinned Tailwind CLI for $(uname -s)-$(uname -m)." >&2
        exit 1
        ;;
esac

if [ ! -x "$BIN" ]; then
    mkdir -p "$BIN_DIR"
    url="https://github.com/tailwindlabs/tailwindcss/releases/download/v${TAILWIND_VERSION}/${ASSET}"
    echo "Downloading ${ASSET} v${TAILWIND_VERSION} -> ${BIN}"
    curl -sSfL -o "${BIN}.tmp" "$url"
    if [ -n "$EXPECTED_SHA" ]; then
        actual="$(sha256sum "${BIN}.tmp" | cut -d' ' -f1)"
        if [ "$actual" != "$EXPECTED_SHA" ]; then
            rm -f "${BIN}.tmp"
            echo "Checksum mismatch for ${ASSET}: got ${actual}, expected ${EXPECTED_SHA}" >&2
            exit 1
        fi
    else
        echo "warning: no pinned checksum for ${ASSET}; downloaded without verification." >&2
    fi
    chmod +x "${BIN}.tmp"
    mv "${BIN}.tmp" "$BIN"
fi

SRC="static/css/tailwind.src.css"
OUT="static/css/tailwind.css"

if [ "${1:-}" = "--check" ]; then
    tmp="$(mktemp)"
    trap 'rm -f "$tmp"' EXIT
    "$BIN" -c tailwind.config.js -i "$SRC" -o "$tmp" --minify >/dev/null
    if cmp -s "$tmp" "$OUT"; then
        echo "$OUT is up to date."
        exit 0
    fi
    echo "$OUT differs from a fresh build of $SRC. Run scripts/build_css.sh." >&2
    exit 1
fi

"$BIN" -c tailwind.config.js -i "$SRC" -o "$OUT" --minify
echo "Wrote $OUT"
