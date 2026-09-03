#!/usr/bin/env bash
#
# Build the vendored Cornerstone3D bundle into static/vendor/cornerstone/.
#
# Dev-only by design: the emitted tree is
# committed, so nothing here runs at deploy time. CI re-runs it and requires
# `git diff --exit-code` to be clean, which is what keeps the committed bundle
# honest -- see CONTRIBUTING.md.
#
# Deliberately NOT regenerated here:
#
#   static/js/nifti-reader.js       is esbuild output, but of a custom entry that
#                                   assigns `window.nifti = {...}`. That entry was
#                                   never committed, and the source version it was
#                                   built from is unrecorded. Re-authoring it would
#                                   swap the header parser under volume_metadata.js,
#                                   cbct_convert.js and the panorex path for no gain.
#   static/js/vendor/fflate-0.8.2.min.js
#                                   is not esbuild output at all -- its own header
#                                   says "Original file: /npm/fflate@0.8.2/umd/index.js",
#                                   i.e. a jsdelivr copy of the prebuilt UMD file.
#
# Both are already self-hosted and working. Phase 3 revisits nifti-reader.js when it
# re-wires volume_metadata.js (finding F2).

set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -d node_modules ]; then
    echo "node_modules/ missing -- run 'npm ci' first (needs registry egress)." >&2
    exit 1
fi

node scripts/build_frontend.mjs
