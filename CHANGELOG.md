# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.0.0] - TBD

### Added
- Folder-level access control for laparoscopy (`laparoscopy.FolderAccess`, mirroring brain).
  A data migration grants access on all laparoscopy folders to existing project members,
  preserving current visibility while making it revocable.
- `VERSION` file exposed as `settings.APP_VERSION` and shown in the site footer.
- This changelog.
- CI on self-hosted GitHub Actions runners (ruff + full suite against MySQL/Redis,
  `makemigrations --check` gate) and a release workflow on `v*` tags.
- Contract tests freezing the external runner HTTP API (claim/complete/fail).
- One-command local development bootstrap (`scripts/dev_bootstrap.sh`):
  standalone `docker-compose.dev.yml` with MySQL, Redis and a single-node
  Garage for object storage, plus an idempotent `manage.py seed_dev`.

### Changed
- The web container now serves with gunicorn and runs `migrate` on start
  (`AUTO_MIGRATE=0` to opt out, `RUN_DEV_SERVER=1` for the dev server).

### Fixed
- `common.permissions._namespace()` resolved every request-object call to `maxillo`,
  so brain and laparoscopy permission checks ran against the wrong domain.
- Folder permission edits from brain pages silently wrote maxillo `FolderAccess` rows;
  folder-tag views now resolve the model per namespace.
- `user_can_view_caption_content` ignored brain's `folders` M2M relation.
- `patient_volume_data` always returned 500: a dead filter block referenced an
  undefined `domain` variable and its NameError short-circuited the endpoint.

## [1.9.0]

Last pre-modernization production state (tag `v1.9.0`, commit `52d1557`).
No changelog was kept before this point.
