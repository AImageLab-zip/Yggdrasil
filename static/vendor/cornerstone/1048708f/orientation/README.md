# `Human.vtp` — the 3D orientation marker

The human figure shown in the corner of the volume render, the same one 3D Slicer
uses. It is **Cornerstone's own choice of asset**, not ours:
`OrientationMarkerTool`'s `CUSTOM` overlay type defaults to exactly this file
(`@cornerstonejs/tools/dist/esm/tools/OrientationMarkerTool.js`, `polyDataURL`).

It is vendored here rather than fetched from that default URL because the default is a
runtime request to `raw.githubusercontent.com`, and `templates/base.html` states that
no third-party CDN may be contacted at runtime. This is the same treatment finding F5
gave itk-wasm's jsdelivr default: keep the upstream feature, remove the upstream
network call.

- Source: <https://github.com/Slicer/Slicer/blob/80ad0a04dacf134754459557bf2638c63f3d1d1b/Base/Logic/Resources/OrientationMarkers/Human.vtp>
- Upstream project: 3D Slicer, distributed under the Slicer License (a BSD-style
  licence permitting redistribution with attribution). See
  <https://github.com/Slicer/Slicer/blob/main/License.txt>.

Pinned to that commit deliberately: the file is geometry, it does not need updating,
and a moving reference would change what clinicians see with no diff to review.
