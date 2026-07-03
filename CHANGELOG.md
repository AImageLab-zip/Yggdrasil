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

### Changed
- The web container now serves with gunicorn and runs `migrate` on start
  (`AUTO_MIGRATE=0` to opt out, `RUN_DEV_SERVER=1` for the dev server).

### Fixed
- `common.permissions._namespace()` resolved every request-object call to `maxillo`,
  so brain and laparoscopy permission checks ran against the wrong domain.
- Folder permission edits from brain pages silently wrote maxillo `FolderAccess` rows;
  folder-tag views now resolve the model per namespace.
- `user_can_view_caption_content` ignored brain's `folders` M2M relation.

## [1.9.0]

Last pre-modernization production state (tag `v1.9.0`, commit `52d1557`).
No changelog was kept before this point.
