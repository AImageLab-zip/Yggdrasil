"""Cross-origin isolation for the in-browser converters.

The upload page runs two WebAssembly converters that need ``SharedArrayBuffer``,
so the document must be cross-origin isolated. The part that is easy to get
wrong -- and was, once -- is that **the worker scripts need the headers too**:
a dedicated worker whose own script response lacks the owner's COEP is refused
at load, and the browser reports that as an ErrorEvent with an empty message,
so the page says "unknown error" and never attempts the upload.

These tests pin both halves, and pin that the headers stay *off* everything
else: ``require-corp`` blocks any cross-origin subresource that does not opt in
with CORP, which is why this is not applied site-wide.
"""

from django.test import RequestFactory, SimpleTestCase
from django.http import HttpResponse

from yggdrasil.middleware import CrossOriginIsolationMiddleware

COEP = "Cross-Origin-Embedder-Policy"
COOP = "Cross-Origin-Opener-Policy"
CORP = "Cross-Origin-Resource-Policy"


class _Match:
    def __init__(self, url_name):
        self.url_name = url_name


def _response_for(path, url_name=None):
    request = RequestFactory().get(path)
    if url_name is not None:
        request.resolver_match = _Match(url_name)
    middleware = CrossOriginIsolationMiddleware(lambda r: HttpResponse())
    return middleware(request)


class IsolatedSurfacesTests(SimpleTestCase):
    def test_the_upload_page_is_isolated(self):
        response = _response_for("/urology/upload/", url_name="upload_patient")
        self.assertEqual(response[COOP], "same-origin")
        self.assertEqual(response[COEP], "require-corp")
        self.assertEqual(response[CORP], "same-origin")

    def test_every_domains_upload_page_is_isolated(self):
        # The rule keys on the URL name, which every domain's app_urls.py uses,
        # so no domain is named here and a new one is covered on arrival.
        for path in (
            "/maxillo/upload/",
            "/brain/upload/",
            "/urology/upload/",
            "/laparoscopy/upload/",
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    _response_for(path, url_name="upload_patient")[COEP], "require-corp"
                )

    def test_worker_scripts_carry_the_headers(self):
        """Without this the worker is refused and the failure is unreadable."""
        for path in (
            "/static/js/worker/wsi_convert_worker.js",
            "/static/js/worker/cbct_convert_worker.js",
            # wasm-vips spawns its pthread pool as nested workers from this URL.
            "/static/vendor/vips/vips.js",
        ):
            with self.subTest(path=path):
                response = _response_for(path)
                self.assertEqual(response[COEP], "require-corp")
                self.assertEqual(response[COOP], "same-origin")


class EverythingElseIsUntouchedTests(SimpleTestCase):
    def test_ordinary_pages_are_not_isolated(self):
        for path, url_name in (
            ("/", "landing"),
            ("/urology/patients/", "patient_list"),
            ("/maxillo/patient/1/", "patient_detail"),
            ("/changelog/", "changelog_page"),
        ):
            with self.subTest(path=path):
                self.assertNotIn(COEP, _response_for(path, url_name=url_name))

    def test_ordinary_static_files_are_not_isolated(self):
        for path in (
            "/static/js/cbct_upload.js",
            "/static/css/tokens.css",
            "/static/vendor/cornerstone/manifest.json",
        ):
            with self.subTest(path=path):
                self.assertNotIn(COEP, _response_for(path))

    def test_an_unresolved_request_is_not_isolated(self):
        # resolver_match is None for anything that never reached a view.
        self.assertNotIn(COEP, _response_for("/nope/"))

    def test_an_existing_header_is_not_overwritten(self):
        request = RequestFactory().get("/static/js/worker/wsi_convert_worker.js")
        middleware = CrossOriginIsolationMiddleware(
            lambda r: HttpResponse(headers={CORP: "cross-origin"})
        )
        self.assertEqual(middleware(request)[CORP], "cross-origin")
