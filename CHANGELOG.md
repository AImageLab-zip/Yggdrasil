# Changelog

All notable changes to this project will be documented in this file.

It is written for the people who use Yggdrasil — clinicians, researchers and
students — rather than for its developers, so each entry says what changed on
screen and what it means for your data, not how it was built. The same text is
rendered in the application at `/changelog/`, reachable by clicking the version
number in the footer.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [3.0.0] - 2026-09-02

The imaging release: every viewer rebuilt on one engine, and annotations turned
into a record you can trust.

### Added
- **One viewer engine for every kind of image.** CBCT volumes, panoramic
  reconstructions, 3D intraoral scans, clinical photographs, teleradiography and
  laparoscopy video are now all displayed by the same imaging engine, so the
  controls, the measurements and the annotation tools behave the same way
  wherever you are. Previously each modality had its own viewer, with its own
  quirks and its own bugs.
- **Annotations are a versioned record.** Every save is a numbered revision that
  keeps who made it, when, and which image it was drawn on. Nothing is silently
  overwritten, nothing is left floating without the picture it belongs to, and
  earlier revisions remain available. Predictions produced by the automatic
  pipelines are stored in the same record, marked as predictions, so a
  hand-corrected result is never confused with a raw one.
- **Automatic analysis of laparoscopy video.** Uploaded video is prepared for
  review automatically — a compressed copy for smooth playback and a
  frame-per-second track for annotation — and the analysis runs on the compute
  cluster instead of on the web server.
- **Automatic analysis of intraoral photographs.** New models recognise which
  view a photograph shows and outline the teeth in it; the result opens in the
  annotator as an ordinary editable annotation.
- **The panoramic reconstruction is now live.** The dental arch can be adjusted
  directly on the CBCT slice and the panoramic strip redraws as you move it.

### Changed
- **The CBCT export offers the uploaded volume and the segmentation, and nothing
  else.** Intermediate pipeline files that nobody left with have been removed
  from the list.
- **The export form names one thing per checkbox.** Duplicated and near-identical
  options were merged — bite classification in particular was offered twice
  under two names — and the explanatory paragraphs are gone because the labels
  now say it. Saved exports keep working.
- **Images are stored and exchanged as NIfTI.** Yggdrasil is a NIfTI-only
  platform: DICOM upload and DICOM interchange are no longer part of it.
- **Clearer modality labels** across the patient tabs.

### Fixed
- Many viewer and annotation defects found by working through the migrated
  screens: brush strokes lost on mouse-up in the video annotator, tooth outlines
  drifting away from rotated photographs, measurements that could not be made
  visible again, a locked panoramic arch after a prediction, blank image stacks,
  panels that named the wrong role, masks too faint to see, and pages with
  several modalities running out of graphics contexts and blanking a viewer.
- Turning on HTTPS no longer discards a deployment's configured browser access
  rules.
- Projects and folders can now be created and deleted from the administration
  screens in every workflow; several combinations previously failed silently.

## [2.0.0] - 2026-08-26

The release that made the platform Yggdrasil: a new identity, a public window
onto it, and the access-control and operations work behind both.

### Added
- **A name, a logo and a look.** The platform became Yggdrasil, the world tree,
  with an original logo, a full icon set and a consistent interface. Fonts are
  served from our own servers, so viewing a page makes no request to any
  third-party font provider.
- **A public, read-only demo.** Curated folders can be published at `/demo/` for
  anyone to browse without logging in. Only anonymised or synthetic studies are
  ever flagged for it, and the demo can never reach the working application.
- **Share links that expire.** Export links can be given a lifetime — 7, 30 or
  90 days, or never for administrators — and default to 30 days. Expired links
  say so instead of serving the file.
- **Nightly backups and a status page.** The database is dumped, verified and
  stored off-machine every night, with a staff status page that warns when the
  most recent good backup is too old, and a simple health check for monitoring.
- **The version number in the footer**, and this changelog.
- **Per-folder access control for laparoscopy**, matching what the other
  workflows already had, so access can be granted and revoked folder by folder.

### Changed
- The platform now runs behind a production web server and applies its own
  database updates on start, which removes a manual step from every deployment.

### Fixed
- **A member of one project could read another project's files.** File access
  was checked against a single hard-coded project, which let members of one
  workflow fetch images belonging to another and refused some users their own
  files. Access is now decided by the file's own project, everywhere. After
  upgrading, review who has access to what: some cross-project reads that used
  to succeed were never intended.
- **Parts of the brain workflow's data were reachable without logging in.**
  Those endpoints now require a login, and the monitoring ones require staff.
- Several permission checks resolved to the wrong workflow, so folder
  permissions edited in one place were written in another.
- An administration page was reachable without signing in; it now requires a
  staff login.
- Assorted page errors, including a patient view that always failed to load its
  volume information.

## [1.9.0]

The last release of the original application, kept as the reference point for
the upgrade to 2.0. Everything from this version forward is recorded above; no
changelog was kept before it.

## [1.0.0]

The original application — first named ToothFairy4M — built between 2025 and
2026 and grown over roughly 520 changes into the platform 2.0 renamed. Summarised
here in one entry because it predates this changelog.

### Added
- Upload and browse dental studies, organised into projects and folders, with
  registration by invitation only.
- CBCT volumes viewable in the browser, without installing anything.
- Voice captioning: dictate a note against a study and edit the transcription
  afterwards.
- Intraoral scan support and automatic bite classification.
- A background job queue for the automatic analyses, which later moved out to
  external compute runners so heavy work no longer competed with the website.
- Growth from a single workflow to three, with per-folder access control, object
  storage for the images, and ZIP exports of a study's files.
