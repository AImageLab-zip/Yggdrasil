# Urology Domain Architecture & Multimodal Design

This document details the architectural design, multimodal imaging pipelines, and platform integration standards of the **Urology** domain (`urology/`) in Yggdrasil.

---

## 1. Domain Overview & Placement in Yggdrasil

Yggdrasil structures medical research imaging across dedicated domain apps (`maxillo/`, `brain/`, `laparoscopy/`, `urology/`), built on shared infrastructure (`common/`) and versioned annotations (`annotations/`).

The Urology domain, mounted at `/urology/`, supports research and clinical workflows for:
1. **Multiparametric Prostate MRI (mpMRI)**: Volumetric scans (T2-weighted, diffusion-weighted imaging DWI, apparent diffusion coefficient ADC).
2. **Digital Pathology Whole Slide Images (WSI)**: Gigapixel histopathology biopsies scanned at 20× or 40× optical magnification.
3. **Confocal Laser Endomicroscopy (CLE)**: Real-time high-magnification cellular optical biopsies.

```
                         ┌────────────────────────────────────────┐
                         │               Yggdrasil                │
                         └───────────────────┬────────────────────┘
                                             │
      ┌──────────────────┬───────────────────┼────────────────────┬─────────────────┐
      ▼                  ▼                   ▼                    ▼                 ▼
 ┌─────────┐       ┌───────────┐       ┌───────────┐       ┌─────────────┐   ┌─────────────┐
 │ common/ │◀──────│ maxillo/  │       │  brain/   │       │laparoscopy/ │   │  urology/   │
 └────▲────┘       └───────────┘       └───────────┘       └─────────────┘   └──────┬──────┘
      │                                                                             │
      └─────────────────────────────────── annotations/ ────────────────────────────┘
```

### Architectural Invariant: Strict One-Way Decoupling
- **`urology` depends on `common` and `annotations`**: It imports models, permissions, base classes, and utilities.
- **`common` never imports `urology`**: Platform modules resolve Urology resources dynamically via `common/domains.py` registry entries, `apps.get_model("urology", ...)`, or `fk_fields_for("urology")`.
- **Annotation schemas are unified**: WSI measurements and contours serialize into the standard Cornerstone3D coordinate format understood by `annotations/adapters/cornerstone.py`.

---

## 2. Domain Data Model

Urology leverages the abstract base patterns defined in `common/base_models.py` while tailoring domain-specific entities:

| Model | Inherits From | Purpose |
|---|---|---|
| `Patient` | `models.Model` | Central patient entity. Owns modalities, tags, visibility, folder link, and project scope. Managed by `ActivePatientManager` (soft-deletion). |
| `Folder` | `FolderBase` | Hierarchical study folders (e.g. cohorts, trial arms). |
| `FolderAccess` | `FolderAccessBase` | Granular per-user permissions on folders (viewer / annotator / admin). |
| `Tag` | `TagBase` | Clinical tags (e.g., `Gleason-7`, `PIRADS-4`). |
| `VoiceCaption` | `VoiceCaptionBase` | Audio recordings and transcription notes attached to patient studies. |
| `Export` | `ExportBase` | Multimodal dataset export bundles, tracking job status and share tokens. |
| `UrologyProject` | `common.models.Project` (Proxy) | Domain-scoped project representation in Django admin. |

Shared relations on `common.Job`, `common.ProcessingJob`, and `common.FileRegistry` link via nullable foreign keys:
- `urology_patient` (FK to `urology.Patient`)
- `urology_voice_caption` (FK to `urology.VoiceCaption`)

---

## 3. Multimodal Ingestion & Modality Definitions

Urology defines three core modalities registered in the database via `manage.py setup_urology_modalities`:

```
                                      Upload Interface (/urology/upload/)
                                                        │
                      ┌─────────────────────────────────┼─────────────────────────────────┐
                      ▼                                 ▼                                 ▼
             [urology-mri]                       [urology-wsi]                   [urology-confocal]
         Prostate mpMRI Volume               Digital Pathology WSI              Confocal Microscopy
         (.nii, .nii.gz, .mha)              (.tiff, .tif, .svs)                 (.tiff, .svs, .mp4)
                      │                                 │                                 │
                      ▼                                 ▼                                 ▼
           FileRegistry Record:              FileRegistry Record:              FileRegistry Record:
            `urology_mri_raw`                 `urology_wsi_raw`                `urology_confocal_raw`
```

### Deterministic & Collision-Free File Storage
Incoming files are saved using `urology.file_utils.generate_urology_file_path`:
- Path format: `urology/patients/<patient_id>/<modality_folder>/<timestamp>_<uuid4>_<filename>`
- Files are verified for SHA-256 integrity and registered in `common.FileRegistry` with explicit domain scoping (`domain="urology"`).

---

## 4. The WSI & Pyramidal Pathology Pipeline

Whole Slide Images represent ultra-high-resolution imagery (often exceeding 40,000 × 40,000 pixels). Loading them entirely into browser memory is impossible. Urology solves this with a multi-resolution pyramidal tiling architecture.

```
                      +------------------------------------------+
                      |         WSI File (BigTIFF / SVS)         |
                      +------------------------------------------+
                                           |
                                           v
                             urology/wsi_reader.py
                     - Parse TIFF/SVS IFD Directory Levels
                     - Compute Virtual Downsamples
                     - Strip Zero-Padding Margins
                                           |
                                           v
                             urology/wsi_views.py
                          Multi-Tier Tile Serving Cache
                     +--------------------------------------------+
                     | 1. Memory Byte Cache (LRU, 2,048 items)    |
                     | 2. Local Disk Cache (WSI_TILES_DIR, JPEG)  |
                     | 3. Raw IFD Slice Decompression             |
                     +--------------------------------------------+
                                           |
                                           v
                       HTTP GET /urology/api/wsi/<id>/tile/...
                                           |
                                           v
                       OpenSeadragon + Cornerstone Layer
                     - Hardware-accelerated 2D Canvas Tiling
                     - Dual-Canvas Segmentation Mask Overlay
                     - Cornerstone-compatible Annotation Schema
```

### A. Resolution Pyramids & Virtual Downsampling (`wsi_reader.py`)
- Standard BigTIFF/SVS slides contain multiple Image File Directories (IFDs), each storing a resolution tier (e.g., Level 0 = 40×, Level 1 = 10×, Level 2 = 2.5×).
- When native IFD steps have large magnification jumps, `wsi_reader.py` synthesizes **virtual downsample levels**, ensuring smooth zooming without visual popping or excessive bandwidth use.
- Slide metadata (dimensions, levels, microns per pixel `mpp_x`/`mpp_y`) is exposed via `GET /urology/api/wsi/<file_id>/metadata/`.

### B. Zero-Padding Edge Stripping
Scanned pathology slides often contain artificial black or white padding margins where the scanner hardware padded partial tiles at the slide edge.
- `wsi_reader.py` dynamically computes the real tissue bounding envelope, stripping away blank padding borders.
- This prevents black seams and alignment artifacts when tiles are stitched in OpenSeadragon.

### C. Multi-Tiered High-Throughput Caching
To maintain 60 FPS viewport navigation at 40× zoom:
1. **Tier 1 (LRU In-Memory Byte Cache)**: 2,048 recently accessed 256×256 encoded tile byte arrays are stored in memory (`_MEM_TILE_CACHE`), achieving sub-millisecond response times.
2. **Tier 2 (On-Disk Tile Cache)**: Rendered tiles are saved as compressed JPEGs under `WSI_TILES_DIR/<file_hash>/<level>/<col>_<row>.jpg`. Subsequent visits read directly from fast local disk storage.
3. **Tier 3 (Open Image Handler Cache)**: Open file handles and directory trees are cached in `_FRAME_CACHE` to avoid repeated filesystem opens.

---

## 5. Synchronized Dual-Canvas Segmentation Overlay

When a pathology segmentation mask (`urology_wsi_seg`) is present:
1. **Slide Viewport Canvas**: The mask is rendered on an overlay canvas perfectly registered to OpenSeadragon's world coordinates. As the user zooms and pans, OpenSeadragon's transform matrix maintains pixel-perfect alignment with the underlying tissue.
2. **Minimap Navigator Canvas**: In the bottom-right overview thumbnail, a second synchronized canvas renders the mask scaled to the full slide overview.
3. **Lockstep Visibility Control**: Toggling the segmentation button (`toggleWsiSegmentation`) synchronously reveals or hides both overlays.

---

## 6. Client-Side Gigapixel Converter (`wasm-vips`)

Single-plane ultra-high-resolution JPEG/PNG images (e.g. 20,000 × 20,000 px exported from optical microscopes) cannot be directly navigated efficiently as flat images. Uploading uncompressed gigapixel images to the server for processing is slow and risks exhausting server RAM.

Urology introduces an in-browser WebAssembly converter:
- **Engine**: `wasm-vips` (Libvips compiled to WebAssembly with SIMD and WebWorker threading).
- **Execution**: Runs inside a dedicated Web Worker (`static/js/worker/wsi_convert_worker.js`) instantiated from `static/js/wsi_convert.js`.
- **Output**: Multi-resolution pyramidal BigTIFF with 256×256 tiled JPEG compression.
- **Workflow**:
  1. The user drags a giant JPEG/PNG into the converter dropzone.
  2. The worker processes the image in memory streaming chunks, generating the pyramidal TIFF.
  3. The resulting `.tiff` is passed directly into the WSI upload form for immediate storage.

---

## 7. Security, Permissions, and Raw Data Locking

The Urology domain enforces medical-grade multi-tenancy and raw data immutability:

### A. Project Scoping
- All views verify that the requested patient, folder, or scan belongs to an active project accessible by the authenticated user (`user_is_project_admin` or `user_can_write_patient_annotations`).
- Direct URL manipulation targeting another project's patient ID returns `403 Forbidden` or `404 Not Found`.

### B. Raw Data Immutability (`annotation_lock_reasons`)
- Raw imaging files (`urology-mri.raw`, `urology-wsi.raw`, `urology-confocal.raw`) represent the primary clinical record.
- If annotations, contour sets, or segmentation masks reference a patient or file, `common.annotation_lock.annotation_lock_reasons` flags the file as locked.
- Deletion or replacement endpoints reject requests with `409 Conflict`, preserving forensic data integrity.

---

## 8. Guidelines for Maintaining System Design

When adding features to `urology/`:
1. **Do not create cycles**: Keep all imports pointing towards `common/` and `annotations/`. Never add direct imports of `urology` to `common/`.
2. **Use the domain registry**: Domain settings belong in `common/domains.py`.
3. **Respect project scoping**: Never write queries like `Patient.objects.get(pk=id)` without checking project access.
4. **Preserve viewer contract**: All new annotation tools must serialize into the Cornerstone3D schema for cross-modality uniformity.
5. **Keep migrations linear**: Always verify migration graph consistency with `python manage.py makemigrations --check --dry-run`.
