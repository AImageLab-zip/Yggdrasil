# Yggdrasil

A Django web application for managing and processing medical imaging data across multiple research projects, each mounted as its own app: **Maxillo** (`/maxillo/`), **Brain** (`/brain/`), and **Laparoscopy** (`/laparoscopy/`).

## Main Features

### Maxillo (dental/maxillofacial imaging)

- **Bite Classification**: Automatic and manual classification of dental occlusion (sagittal, vertical, transverse, midline)
- **AI-Powered Captioning**: IOS and CBCT annotation using speech-to-text technology, with editable/versioned transcriptions
- **CBCT Panoramic Extraction**: Automated extraction of panoramic views from CBCT scans
- **IOS Normalization**: Standardized processing of intraoral scan data
- **Multi-Modality Support**: Handle IOS, intraoral photos, teleradiography, panoramic images, and CBCT
- **Data Export**: Structured, shareable export of patient data and imaging files

### Brain (brain tumor MRI imaging)

`brain/` reuses the same patient/folder/export workflow as `maxillo/` for a separate project namespace, with its own database tables, folders, and modalities.

- **Multi-Modality Support**: Handle brain tumor MRI sequences (T1, T2, FLAIR, T1c)
- **AI-Powered Captioning**: Speech-to-text annotation with editable/versioned transcriptions
- **Data Export**: Structured, shareable export of patient data and imaging files

### Laparoscopy (surgical video annotation)

- **Video Upload & Organization**: Upload laparoscopic surgery videos into folders, datasets, and tags
- **Frame-Accurate Annotation**: Brush/eraser/polygon region annotation and time-stamped quadrant markers, with per-project, per-user color schemes
- **AI-Assisted Segmentation**: Point-prompt segmentation proxied to an external worker service (requires `WORKER_BASE_URL` — see [docs/setup.md](docs/setup.md))
- **Voice Captioning**: Speech-to-text clinical notes, same as Maxillo/Brain
- **Data Export**: Subsampled video frames plus per-frame annotation masks (NPZ) as ZIP archives

## Description

Yggdrasil is a comprehensive platform designed for medical imaging research. It provides tools for uploading, processing, annotating, and exporting imaging data with support for multiple modalities across its three projects. The application features a modern web interface with 3D visualization capabilities and automated processing workflows.

Live instance: [https://yggdrasil.ing.unimore.it](https://yggdrasil.ing.unimore.it)

## Documentation

- [docs/setup.md](docs/setup.md) — first-time setup: `.env`, `DOCKER_SUFFIX`, Docker networks
- [docs/running.md](docs/running.md) — day-to-day commands: start/stop, logs, migrations, shell access
- [docs/runners.md](docs/runners.md) — distributed Celery runners and the runner callback API
- [docs/admin-tasks.md](docs/admin-tasks.md) — one-off ops scripts (superuser, DB import)
- [docs/new-project-type.md](docs/new-project-type.md) — adding a new project app (like Maxillo, Brain, or Laparoscopy)

Notes:

- Django accepts either `DB_NAME/DB_USER/DB_PASSWORD` or the `MYSQL_*` variables.
- Object storage is S3-compatible (Garage/MinIO) via `OBJECT_STORAGE_*`.

## Contact

For more information or to request an account, please contact:

Email: [yggdrasil@unimore.it](mailto:yggdrasil@unimore.it)
