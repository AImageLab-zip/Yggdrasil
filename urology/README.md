# `urology/`

Urological imaging domain, mounted at `/urology/`. Supporting prostate MRI, digital
pathology Whole Slide Images (WSI), and confocal laser endomicroscopy. It reuses
the platform's shared patient / folder / job / export architecture under its own
namespace and database tables.

## What it owns

- **Its own domain tables**: `Patient`, `Folder`, `Tag`, `Dataset`, `VoiceCaption`,
  `Export`, `UrologyProject` — subclasses of the abstract bases in
  `common/base_models.py`, with `Patient` tailored for urological imaging workflows.
- **The urology modalities**:
  - `urology-mri`: Prostate MRI sequences (.nii, .nii.gz)
  - `urology-wsi`: Digital Pathology Whole Slide Images (.svs, .tiff, .tif)
  - `urology-confocal`: Confocale Laser Endomicroscopy (.svs, .tiff, .tif)
  Registered via `manage.py setup_urology_modalities`.
- **Specialized viewers**:
  - Volumetric viewer for MRI scans.
  - Multi-resolution deep-zoom pyramidal TIFF / SVS tile reader (`wsi_reader.py`)
    and OpenSeadragon viewer (`wsi_views.py`).
  - Confocale microscopy multi-frame viewer and playback surfaces.
- **Domain upload and management forms** (`forms.py`, `file_utils.py`) with
  project-scoped permissions and collision-free file naming.
- **Voice captioning and export wiring** for the domain.

## What it must NOT own

- **Generic infrastructure.** File storage, authentication, project access, and
  permissions live in `common/`.
- **The annotation record.** Durable annotations, contours, measurements, and
  label schemas are stored in `annotations/` and edited via Cornerstone3D / OpenSeadragon
  adapters.
- **A private pipeline runner.** Processing tasks are tracked via `common.Job`
  and dispatched through platform workers.

## The boundary with `common/`

`urology` imports `common`; `common` never directly imports `urology`. Its domain
slug is registered once in `common/domains.py`, where its per-domain foreign key
columns (`urology_patient`, `urology_caption`) are declared. Any platform-level
code (e.g. export processing, file access validation) resolves Urology models
dynamically via `apps.get_model("urology", ...)` or `fk_fields_for("urology")`.
