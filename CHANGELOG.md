# Changelog

All notable changes to this project will be documented in this file.

It is written for the people who use Yggdrasil — clinicians, researchers and
students — rather than for its developers, so each entry says what changed on
screen and what it means for your data, not how it was built. The same text is
rendered in the application at `/changelog/`, reachable by clicking the version
number in the footer.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [3.3.0] - 2026-09-24

Yggdrasil now works on a phone -- as the public demo.

### Added
- **The public demo on your phone.** Opening any Yggdrasil link on a phone takes you
  straight into the read-only demo, no account needed, on the page the link points to.
  Signing in and registering stay on computers and tablets; on a phone you are shown the
  demo instead of a sign-in form.
- **Viewers that follow your fingers.** On a phone one finger scrolls the page, even over
  an image, and two fingers pinch to zoom and drag to move it -- in the CBCT and MRI
  grids, the photographs, the panoramic, the 3D intraoral scans and the pathology slides.
- **Drag a brain series onto a window with your finger**, or tap the series and then the
  window it should open in.

### Changed
- **Pages laid out for a phone.** The side menu opens from a button at the top and names
  each entry, including Home; patients are listed as cards you can tap anywhere; and the
  modality tabs and viewer controls stay at the top of the screen while you scroll.
- **"Best viewed on a bigger screen" is now shown only to signed-in users** on a phone,
  for the pages that still need a computer. Visitors and the demo no longer see it.

### Fixed
- **Administrators are notified of new registrations again.** The notification email
  failed silently on every sign-up.

## [3.2.1] - 2026-09-23

Cardiology joins the platform, inviting people is easy to find again, and the
home page fits the areas you actually work in.

### Added
- **Cardiology.** A new area at `/cardiology/` for reviewing ECG recordings. Upload
  one recording per patient, or a whole folder at once if you administer the
  project. Each recording is drawn on the familiar clinical grid (25 mm/s, 10 mm/mV)
  and can be panned by dragging or with the slider, so a 12-lead strip stays
  readable on a phone. Classify each recording as AF, NSR, Other or NI and add
  notes; a **Next** button walks you through a folder in list order. The list can
  be filtered to classified recordings, recordings with notes, or recordings with
  neither. Exports can include the raw recordings, the plots and the
  classifications.
- **Every change to an ECG classification is kept.** If someone else changes it
  while you have the page open, your change is refused and you are asked to reload,
  instead of silently replacing theirs.
- **A recording that cannot be plotted is refused at upload**, with the reason:
  malformed files, non-numeric samples, and recordings too long to draw at clinical
  scale (about three minutes for 12 leads).
- **The invitations page lists every user with their email.** Staff can see each
  account's name, email, projects and role, when they joined and last signed in, and
  copy the addresses of all active users in one click to contact them.

### Changed
- **Invitations are back in the navigation**: an envelope icon in the left bar and
  an **Invitations** button in the Control Panel (staff only).
- **Invitation emails look the part.** They now carry the Yggdrasil logo, a short
  summary of the projects, role and expiry, and an **Accept invitation** button,
  with a plain-text version for mail clients that do not show formatting.
- **The home page shows only your areas, centred.** Someone with access to a single
  area now sees one card in the middle of the page instead of a lone card in the
  first of four columns; on a wide screen administrators see all five areas in one
  row.

### Fixed
- **Deleting an unused invitation works again**; the button used to lead to an
  error page.
- **"Generate the missing default panoramics" now works.** The batch page loads each
  patient in a hidden frame, and the site's security settings refused to be framed,
  even by itself, so every patient timed out. The patient page now allows framing by
  Yggdrasil's own pages only. The new ECG plot batch page relies on the same fix.

## [3.2.0] - 2026-09-23

A security and reliability release. Most of it is invisible when things work; where
you will notice it, it is because something that used to be allowed is now refused.

### Security
- **Every remaining screen decides access by the record's own project.** Viewing,
  editing, deleting, re-running and moving patients, their files, captions,
  measurements, segmentations and landmarks are all judged against the project the
  patient belongs to, never against the project you happen to have open. A patient in
  a project you cannot see now answers "not found". **After upgrading, some actions
  that used to succeed will be refused; that is the fix.**
- **Only project administrators can publish an export link**, and a link that
  anyone can open always expires. Existing never-expiring public links now expire 30
  days after the upgrade. Link addresses are no longer written to the server logs.
- **Uploaded files are served so they cannot run in your browser.** Photos, videos
  and scans display as before; anything else is downloaded instead of opened.
  Photo uploads accept image formats only (including HEIC, BMP and TIFF).
- **Slide labels and macro images are never shown.** Pathology slide files carry a
  photo of the slide label, which often shows the patient's name or barcode; it is
  no longer offered as a zoom level or used for the minimap.
- **Repeated failed sign-ins lock that account from that address for 30 minutes**,
  and you stay signed in for up to a week instead of two.
- **Processing jobs on the cluster no longer receive the platform's storage keys**;
  each job can read only its own inputs and write only its own results.

### Changed
- **Large downloads no longer strain the server.** Scans, videos and export ZIPs are
  sent as they are read, so several multi-gigabyte downloads at once no longer risk
  running a server process out of memory.
- **Re-running a setup command keeps what administrators changed.** Modality names,
  settings and project links edited in the admin are no longer reset.

### Fixed
- **Urology is included everywhere the other workflows are**: processing that
  should hide raw files until it finishes now does so for urology too, and urology
  annotations are filed under the right patient.
- **A processing job is only handed to the cluster once it has been saved**, so a
  job can no longer be picked up before it exists.

## [3.1.0] - 2026-09-22

The Urology multimodal imaging release: digital pathology Whole Slide Images, multiparametric prostate MRI, and confocal endomicroscopy.

### Added
- **The Urology domain (`/urology/`).** Clinicians and research teams can now manage, view, and annotate urological studies spanning prostate MRI, digital pathology biopsies, and confocal microscopy.
- **Whole Slide Image (WSI) deep-zoom viewer.** Native pyramidal tile streaming supporting BigTIFF and SVS slide formats with smooth navigation up to 40× magnification, physical scale calibration, and vector measurements.
- **In-browser gigapixel converter.** Pathology scans in flat JPEG/PNG format can be converted into pyramidal tiled BigTIFF files directly in the browser via WebAssembly (`wasm-vips`), completely offloading heavy conversion compute from the server.
- **Dual-canvas pathology segmentation overlays.** Histological segmentation masks render synchronously on both the primary slide viewport and the overview minimap navigator, toggling cleanly with a single control.
- **Zero-padding edge stripping.** The WSI reader automatically detects and removes artificial black margin strips from scanner tiling boundaries for seamless pathology viewing.
- **Raw data immutability locks.** Scans associated with clinical annotations or segmentation masks are protected against deletion or overwrite, safeguarding research data integrity.

### Changed
- **Unified with main v3.0.1.** Upstream improvements to the public demo, permissions, and index grouping are fully integrated.
- **Dropped obsolete `Dataset` model** in favor of project-scoped study management across the platform.
- **The Rerun action offers processing steps that have never run.** Registering a new analysis step no longer leaves existing patients behind: any patient whose uploaded images the new step applies to now offers it in the Rerun picker, on both the patient page and the patient list, and picking it runs the step for the first time. Previously only steps that had already run at least once could be started again, and only in the dental workflow.
- **The modality switcher is the same control in every workflow.** Urology had its own; it now uses the one the other workflows use, so a modality added to a project appears without anything else changing, and a voice caption recorded while viewing a slide is filed against that slide's modality. The tabs are listed in the same order as everywhere else, which for urology means alphabetically rather than MRI first.
- **Turning voice captions off for a project now hides them in urology too.** The setting was ignored on the patient page, which always offered captions even where the project had disabled them.
- **The urology patient page loads about 4 MB less.** It was downloading the shared imaging engine for a viewer that never used it.

### Fixed
- **Access to folders, patients and exports is judged by the project they belong to.** Several actions checked your rights against the project you happened to have open rather than against the project the folder or patient actually belongs to. An administrator of one project could therefore act on another project's data: read a folder's patient count, move patients into their own project's folder — which also reassigns those patients — and delete patients singly or in bulk. Each of these is now decided by the record's own project. **After upgrading, some actions that used to succeed will be refused; that is the fix.** This is the same class of problem as the cross-project file access corrected in 2.0.0, in screens that were missed then.
- **Your profile page opens.** In the dental workflow it had been failing with a server error, and the brain and urology workflows showed a cut-down stand-in rather than the real page. All four now open the same page with their own figures, and a link to somebody else's profile shows theirs instead of always your own.
- **Export previews count what the export will actually contain.** Outside the dental workflow the file totals and the text captions both came back empty, so the size and file count shown before starting an export were wrong.
- **The person who created an export can manage its share link** without also needing permission to create new exports.
- **Text captions have to say something.** A one-character caption was accepted everywhere except urology; ten characters are now the minimum in every workflow. The modality a caption is filed under is checked against the workflow you are in, rather than against every modality on the platform, and a modality registered moments earlier is recognised immediately instead of after a delay.

## [3.0.1] - 2026-09-07

The public demo becomes a real feature, and Brain upload stops crying wolf.

### Added
- **A live public demo.** Anyone can now explore Yggdrasil without an account.
  The front page leads with it: one button opens the real platform — the same
  viewers, patients and annotations the research teams use — strictly read-only.
  Nothing can be uploaded, edited or exported from the demo.
- **Publishing to the demo is a project decision.** A project appears in the
  public demo when the shared `guest` account is given the Viewer role on it,
  granted on the project page like any other person's access. The project page
  states plainly, for each project, whether it is readable by anyone on the
  internet.

### Changed
- **The demo opens on the domain chooser** instead of dropping visitors into
  whichever domain happened to come first, so a demo spanning several domains
  shows all of them.
- **The front page only offers domains you can actually open.** Cards for
  domains you hold no project in are no longer shown, and the patient count on
  each card now counts the patients you will actually find behind it.

### Fixed
- **Uploading a patient in Brain no longer reports a failure that did not
  happen.** A successful upload showed a red "Upload failed" message and left
  you on the form, even though the patient had been created; the page now goes
  to the patient list as it does elsewhere. Real upload problems are reported
  with the actual reason instead of a status code.
- **Creating a folder in Brain works.** It previously failed with a server
  error every time.
- **The upload page in Brain no longer errors when no project is selected.**
- **Access is judged by the project a folder belongs to.** Access to one project
  could allow reading, and in some places annotating, a folder belonging to a
  different project while you had the first one open.

### Removed
- **The per-folder "is demo" checkbox.** Demo access is decided by project
  access now. As part of this upgrade the shared `guest` account's existing
  project access is cleared and must be granted again deliberately, so nothing
  is published to the internet by accident.

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
