## What and why

<!-- The problem, the root cause, and what this changes. One paragraph is fine. -->

## How it was checked

- [ ] Tests added or updated for the change (a regression test fails before, passes after)
- [ ] `python manage.py test` passes (against **MySQL** if models or constraints changed)
- [ ] `ruff check .` and `lint-imports` pass
- [ ] `npm run build` leaves no diff (only if `frontend/` changed)

## Core paths

- [ ] This touches a path in `.github/CODEOWNERS` (models, migrations, permissions,
      storage, file serving, the runner API, imaging invariants, settings, deployment).
      If so, a maintainer has signed off: <!-- link or @mention -->
- [ ] New URLs that name an object (patient, file, caption, export...) authorize
      against that object's own project (`get_patient_for`); the cross-project
      matrix test passes.

## Data and privacy

- [ ] No real patient data, names or identifiers in code, fixtures, screenshots or
      this description.
