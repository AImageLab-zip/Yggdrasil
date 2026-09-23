"""Views that are the same in every domain.

Folders, tags, voice captions and exports differ between domains only in which
model classes they touch, and ``common.domain_models.get_domain_models`` answers
that from the URL namespace. So they live here, once, and every domain routes at
them -- rather than each domain keeping its own copy, which is how brain and
urology ended up with nine near-identical export views apiece.

Named ``domain_views`` and not ``views`` because ``common/views.py`` already
holds this app's *own* pages (the changelog, the status and control panels).
Nothing here imports a domain app; domain-specific screens -- the patient detail
page, imaging APIs, the runner API -- stay in their own app.
"""
