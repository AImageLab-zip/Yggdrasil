from types import SimpleNamespace

from django.test import TestCase

from common.permissions import _folder_access_model, _namespace


def _fake_request(namespace):
    return SimpleNamespace(resolver_match=SimpleNamespace(namespace=namespace))


class NamespaceResolutionTests(TestCase):
    def test_string_namespaces(self):
        self.assertEqual(_namespace("maxillo"), "maxillo")
        self.assertEqual(_namespace("brain"), "brain")
        self.assertEqual(_namespace("laparoscopy"), "laparoscopy")
        self.assertEqual(_namespace("unknown"), "maxillo")

    def test_request_namespace_resolves_to_its_own_domain(self):
        self.assertEqual(_namespace(_fake_request("brain")), "brain")
        self.assertEqual(_namespace(_fake_request("laparoscopy")), "laparoscopy")
        self.assertEqual(_namespace(_fake_request("maxillo")), "maxillo")

    def test_request_without_namespace_falls_back_to_maxillo(self):
        self.assertEqual(_namespace(_fake_request("")), "maxillo")
        self.assertEqual(_namespace(SimpleNamespace(resolver_match=None)), "maxillo")

    def test_folder_access_model_per_domain(self):
        self.assertEqual(_folder_access_model("maxillo")._meta.app_label, "maxillo")
        self.assertEqual(_folder_access_model("brain")._meta.app_label, "brain")
        self.assertEqual(_folder_access_model("laparoscopy")._meta.app_label, "laparoscopy")
