# `urology/`

Urological imaging domain, mounted at `/urology/`. Supporting prostate multiparametric MRI (mpMRI), digital pathology Whole Slide Images (WSI), and confocal laser endomicroscopy. It implements the platform's domain contract, reusing shared patient, folder, job, and export infrastructure under its own namespace and database tables.

---

## 1. What It Owns

- **Domain Database Tables**:
  - `Patient`: Domain-specific patient record tailored for multimodal urological workflows (mpMRI, histopathology, confocal). Soft-deleted via `ActivePatientManager`.
  - `Folder`, `FolderAccess`: Hierarchical study folder organisation with project-scoped access control.
  - `Tag`: Patient tagging and filtering.
  - `VoiceCaption`: Audio recording and transcription attachments linked to modalities.
  - `Export`: Asynchronous dataset export generation and public/token-protected share links.
  - `UrologyProject`: Project proxy model bound to the `urology` domain.
- **The Urology Modalities**:
  - `urology-mri`: Prostate MRI sequences (`.nii`, `.nii.gz`).
  - `urology-wsi`: Digital Pathology Whole Slide Images (`.svs`, `.tiff`, `.tif`).
  - `urology-confocal`: Confocal Laser Endomicroscopy (`.svs`, `.tiff`, `.tif`, `.mp4`).
  - Configured and seeded via `python manage.py setup_urology_modalities`.
- **Specialized Viewers**:
  - **mpMRI Viewer**: High-performance volumetric 3D viewer built with Cornerstone3D.
  - **WSI Deep-Zoom Viewer**: Multi-resolution pyramidal tile engine (`wsi_reader.py`, `wsi_views.py`) integrated with OpenSeadragon and Cornerstone annotation schemas.
  - **WSI Segmentation Overlay**: Dual-canvas mask rendering on both the primary slide viewport and the bottom-right minimap navigator, toggling synchronously with the segmentation control.
  - **Confocale Viewer**: Microscopy playback and multi-frame inspection.
- **In-Browser Gigapixel Converter**:
  - Client-side WebAssembly VIPS (`wasm-vips`) pipeline in `static/js/wsi_convert.js` and `static/js/worker/wsi_convert_worker.js`.
  - Transforms ultra-high-resolution JPEG/PNG pathology images into tiled pyramidal BigTIFF directly in the user's browser before transmission to the server.
- **Project-Scoped Security & File Management**:
  - Upload handling (`forms.py`, `file_utils.py`) enforcing unique, collision-resistant filenames.
  - Strict project-scoped authorization (`user_is_project_admin`, `user_can_write_patient_annotations`, folder ACLs).
  - Raw data immutability: preventing modification or deletion of raw files when dependent annotations or segmentations exist (`annotation_lock_reasons`).

---

## 2. What It Must NOT Own

- **Generic Platform Infrastructure**:
  - Authentication, global user profiles, project membership, and base permissions live in `common/`.
- **The Durable Annotation Record**:
  - Annotation sets, revisions, geometries, and contour items are stored in `annotations/`. Viewers serialize and deserialize measurements using standardized Cornerstone3D schemas (`annotations/adapters/cornerstone.py`).
- **A Private Pipeline Runner**:
  - Asynchronous compute tasks and background workers use `common.Job` and the central Celery runner infrastructure.

---

## 3. The Boundary with `common/`

Import direction is strictly **one-way**:

```
urology  ──▶  annotations  ──▶  common
```

- `urology` imports from `common`.
- **`common` never imports `urology` directly.**
- The `urology` domain is registered in `common/domains.py` (`DOMAIN_CHOICES`, `DOMAIN_FK_FIELDS`).
- Shared tables (`Job`, `ProcessingJob`, `FileRegistry`) declare nullable foreign keys (`urology_patient`, `urology_voice_caption`).
- Platform services (e.g. `common/export_processing.py`, `common/file_access.py`, admin site registration) resolve Urology models dynamically via `apps.get_model("urology", ...)` or `fk_fields_for("urology")`.

---

## 4. Key Subsystems

### A. WSI Deep-Zoom & Tiling Engine (`wsi_reader.py`, `wsi_views.py`)
- **Pyramidal BigTIFF/SVS Parser**: Extracts resolution pyramids from multi-IFD TIFF/SVS files using Pillow and BigTIFF chunking.
- **Virtual Downsampling**: Calculates intermediate downsample levels dynamically when native IFD pyramids have sparse steps.
- **Zero-Padding Edge Stripping**: Scans and strips artificial black/blank margin tiles at slide boundaries to avoid visual rendering seams.
- **Multi-Tier Caching**:
  1. *In-Memory Frame Cache* (`_FRAME_CACHE`): Retains open image handlers and header metadata.
  2. *Disk Tile Cache* (`WSI_TILES_DIR`): Persists 256×256 JPEG/PNG tiles on local SSD storage for instant repeated access.
  3. *LRU Encoded Byte Cache* (`_MEM_TILE_CACHE`): Thread-safe 2,048-item LRU cache serving hottest tiles in sub-millisecond time.
- **OpenSeadragon + Cornerstone Bridge**: OpenSeadragon handles high-LOD deep-zoom rendering; annotations serialize to Cornerstone schemas for unified persistence in `annotations`.

### B. Dual-Canvas Segmentation Mask Overlay
- Pathology segmentation masks (`urology_wsi_seg`) are served as transparent PNG overlays or geo-referenced masks.
- The viewer renders the segmentation layer synchronously across:
  1. The **primary OpenSeadragon viewport** (scaled and anchored to the active zoom level and pan coordinate).
  2. The **bottom-right minimap navigator** (scaled to the slide overview thumbnail).
- Toggling the segmentation visibility control seamlessly updates both overlays in lockstep.

### C. Client-Side Gigapixel Converter (`static/js/wsi_convert.js`)
- Standard pathology workflows often produce single-plane giant JPEGs (e.g., 20,000 × 20,000 px).
- The in-browser converter uses `wasm-vips` inside a dedicated Web Worker:
  - Generates a multi-resolution pyramidal BigTIFF with 256×256 JPEG tile compression and sub-IFD directory structures.
  - Runs entirely client-side without consuming server RAM or CPU.
  - Automatically feeds the resulting `.tiff` into the WSI upload dropzone.

### D. Security, Permissions, and Raw Data Locks
- Views enforce domain project scoping: users cannot access patients, folders, or uploads across project boundaries without explicit `ProjectAccess` grants.
- Raw scan files (`urology-mri.raw`, `urology-wsi.raw`, `urology-confocal.raw`) are locked from modification or deletion if annotations, measurements, or segmentation masks are associated with them, adhering to medical-imaging data integrity requirements.

---

## 5. Verification & Tests

To execute the test suite:

```bash
docker compose -f docker-compose.dev.yml exec -T web python manage.py test urology --keepdb
```

The test suite covers:
- Patient CRUD, folder hierarchy, tagging, and project access controls.
- Multimodal file uploads (MRI, WSI, Confocale) and validation.
- WSI metadata generation, tile serving, and downsample math.
- Voice captioning endpoints and audio file management.
- Dataset export generation, ZIP packaging, and shared landing tokens.
- Raw file protection and annotation lock verification.
