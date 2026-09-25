# Changelog

All notable changes to this project will be documented in this file.

It is written for the people who use Yggdrasil — clinicians, researchers and
students — rather than for its developers, so each entry says what changed on
screen and what it means for your data, not how it was built. The same text is
rendered in the application at `/changelog/`, reachable by clicking the version
number in the footer.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [3.2.0] - 2026-09-23

Reporting templates are edited rather than deployed, a caption can be filed into its template with one button, and the services behind both are configured in the administration panel.

### Added
- **Report templates are edited in the administration panel.** The reporting checklist shown beside the voice-caption controls used to be written into the software, so changing a word — a wording your specialty prefers, a corrected Italian translation, a field nobody uses — meant asking a developer and waiting for a release. It is now ordinary content: *Clinical data → Report templates*, one row per field, English, Italian and German side by side, with a description that becomes the guidance clinicians expand while dictating. The checklists you have been reading are unchanged; they were moved across word for word.
- **A template can belong to one project.** By default a template covers its whole workflow, and urology keeps one per modality. A project that reports differently can be given its own, and it applies only there.
- **"Structure" turns a dictation into the report template.** Once a caption is complete, one button sends it to a language model with an instruction to file what was said under the template's headings, fix the mistakes speech recognition makes, and change nothing else. The report appears section by section as it is written, and can be run again as many times as you like — each run is kept, with the time, the model and the attempt number. **Your dictation is never touched.** The structured version is stored beside it, and the words you recorded stay exactly as you said them.
- **Structuring can be re-run from the patient list.** A patient's *Rerun* button now offers *Report structuring* alongside the processing steps, which files each of that patient's finished captions into its template again, one after another, and reports how many succeeded. Each run is a new attempt kept beside the earlier ones. A caption that is still being processed is never structured, from the list or from the patient page: a report filed from half a dictation would read as the whole of it.
- **Structuring is enabled per project.** It is off everywhere until a project administrator ticks *Report structuring* on the project, in the same list as voice captions. Until then the button does not appear. This matters: structuring sends the dictated text to whichever language-model service the platform is pointed at, so turning it on is a deliberate decision about where that text may go.
- **A warning when the report may have lost something.** If the structured version appears to be missing a chunk of what was dictated, it says so instead of quietly presenting a shorter report, and anything that fits no field is kept under a final *Other findings* heading rather than dropped.
- **External services are configured in the administration panel.** Speech-to-text and the language model are now rows under *Processing → External services*: address, model, languages, timeouts and tuning, editable without a deploy, with a check that reports whether each one is actually usable. Passwords and API keys are **not** stored there — each row names the environment variable to read, so no key ends up in the database or in a backup of it.

### Changed
- **Dictation messages no longer name a service that is not at fault.** "Live Whisper is not configured" became "Speech transcription is not configured on this server", and so on.

### Upgrading
- Run `python manage.py seed_external_services`, `python manage.py seed_report_templates` and `python manage.py seed_llm_prompts` once after upgrading. All three are safe to re-run and none of them overwrites anything an administrator has edited.
- *Report structuring* appears in the project’s annotation-method list after the
  migration runs; tick it on a project to turn the feature on there.
- Report structuring stays off until a project is opted in and an API key is configured, so upgrading changes nothing on its own.

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
