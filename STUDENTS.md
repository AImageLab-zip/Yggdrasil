# Where your thesis code goes

Yggdrasil is a clinical research platform with real patient data behind it. Most
contributions do not need to touch its core, and the ones that do go through a
maintainer. This page says which is which.

## Free: no core change needed

- **A processing algorithm.** Algorithms run outside the web app. On the AImageLab
  cluster that is a SLURM job (`slurm/README.md`): your `run.sbatch` gets its inputs
  with `ygg-stage pull` and returns outputs with `ygg-stage push`, using per-job
  grants, so it never needs, and never gets, storage credentials. Register it with a
  `ProcessingStep` row in the admin (queue, `algo_name`, dependencies).
- **Analysis on exports.** Export a dataset from the UI and work on the ZIP.
- **Configuration.** Projects, modalities, processing steps, annotation methods and
  laparoscopy region/quadrant types are admin data, not code.

## Through review: code outside the core

A new view, template, viewer surface, export artifact or management command in a
domain app (`maxillo/`, `brain/`, `laparoscopy/`, `urology/`). Before you start,
read that app's `README.md` (what it owns and must not own) and `CONTRIBUTING.md`.
The rules that catch most first PRs:

- Load patients with `common.permissions.get_patient_for(...)`. It checks the
  patient's own project. Never authorize against `request` or "the current project".
- Serve stored files through `common.file_access`, never with a hand-built response.
- Imports go one way: domain app → `annotations` → `common`, and domain apps do not
  import each other. `lint-imports` fails the build otherwise.
- No `if domain == "..."` in `common/`. Use the registry (`common/domains.py`).
- Object-storage work is a management command, never a migration or a request.
- Touching `frontend/` means rebuilding and committing the bundle (`npm run build`).

## Core: maintainers only

Anything listed in `.github/CODEOWNERS`: data models and migrations, permissions,
storage and file serving, the runner HTTP API and its frozen tests, the SLURM
executor, imaging-invariant files, settings, deployment and CI. If your work needs a
change there, open an issue or ask a maintainer first; a PR that edits a core path
without that conversation will be sent back.

## Data

Use the dev stack (`./scripts/dev_bootstrap.sh`) and synthetic or de-identified data.
You should never need real patient data to develop or test. Never put patient names,
identifiers, scans or screenshots of them in code, fixtures, issues or PRs; the
repository is public.

## Before you open a PR

```bash
python manage.py test --settings=yggdrasil.settings_sqlite_test   # quick, no MySQL needed
ruff check . && lint-imports
npm test                                                          # if frontend/ changed
```

CI also runs the suite against MySQL. Fill in the PR template honestly; "how it was
checked" is the part reviewers read first.
