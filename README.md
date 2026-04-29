# ToothFairy4M

A Django web application for managing and processing dental and medical imaging data, including Intraoral Scans (IOS) and Cone Beam Computed Tomography (CBCT).

## Main Features

- **Bite Classification**: Automatic and manual classification of dental occlusion
- **AI-Powered Captioning**: IOS and CBCT annotation using speech-to-text technology
- **CBCT Panoramic Extraction**: Automated extraction of panoramic views from CBCT scans
- **IOS Normalization**: Standardized processing of intraoral scan data
- **Multi-Modality Support**: Handle IOS, intraoral photos, teleradiography, and panoramic images
- **Data Export**: Structured export of patient data and imaging files
- **User Management**: Role-based access control and project organization

## Description

ToothFairy4M is a comprehensive platform designed for dental and maxillofacial imaging research. It provides tools for uploading, processing, annotating, and exporting medical imaging data with support for multiple modalities. The application features a modern web interface with 3D visualization capabilities and automated processing workflows.

Live instance: [https://toothfairy4m.ing.unimore.it](https://toothfairy4m.ing.unimore.it)

## Environment variables

Minimum required for the web stack:

- `SECRET_KEY` (Django)
- `MYSQL_DATABASE`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_ROOT_PASSWORD` (MySQL)
- `RUNNER_API_TOKENS` (token(s) accepted by runner callback API)

Notes:

- Django accepts either `DB_NAME/DB_USER/DB_PASSWORD` or the `MYSQL_*` variables above.
- `CELERY_BROKER_URL` and `CELERY_RESULT_BACKEND` must point to Redis reachable by both web app and runners.
- Object storage is S3-compatible (Garage/MinIO) via `OBJECT_STORAGE_*`.

## Running web stack

```bash
docker compose --env-file .env up -d
```

This starts Django, MySQL, and Redis for distributed runners.

## Distributed runners (hard cutover)

CBCT/IOS preprocessing is executed by external Celery runners.

- Web app enqueues `RUNNER_TASK_NAME` (default: `toothfairy4m_runner.process_job`).
- Job routing uses `RUNNER_DEFAULT_QUEUE` with optional `RUNNER_QUEUE_BY_MODALITY` / `RUNNER_QUEUE_BY_PROJECT`.
- For two-stage IOS workflows and speech-to-text, map queues by modality, for example:
  - `{"ios":"runner_ios_dev","bite_classification":"runner_bite_dev","cbct":"runner_cbct_dev","audio":"runner_audio_dev"}`
- External runner claims/completes/fails through token-protected endpoints:
  - `POST /api/runner/jobs/<id>/claim/`
  - `POST /api/runner/jobs/<id>/complete/`
  - `POST /api/runner/jobs/<id>/fail/`

Use `toothfairy4m-runner` (cookiecutter template) to run worker nodes.

## Contact

For more information or to request an account, please contact:

**Luca Lumetti**  
Email: [luca.lumetti@unimore.it](mailto:luca.lumetti@unimore.it)
