import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "cdn_probe", ROOT / "scripts" / "cdn-probe.py"
)
cdn_probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cdn_probe)


class FakeSocket:
    def __init__(self, cert):
        self._cert = cert

    def getpeercert(self):
        return self._cert


class FakeIpTests(unittest.TestCase):
    def test_fake_ranges(self):
        self.assertEqual(cdn_probe.fake_ip_reason("198.18.6.185"), "198.18.0.0/15")
        self.assertEqual(cdn_probe.fake_ip_reason("10.1.2.3"), "10.0.0.0/8")
        self.assertEqual(cdn_probe.fake_ip_reason("127.0.0.1"), "127.0.0.0/8")
        self.assertEqual(cdn_probe.fake_ip_reason("fc00::1"), "fc00::/7")

    def test_public_ip(self):
        self.assertIsNone(cdn_probe.fake_ip_reason("8.8.8.8"))
        self.assertIsNone(cdn_probe.fake_ip_reason("198.51.100.20"))

    def test_invalid_value(self):
        self.assertIsNone(cdn_probe.fake_ip_reason("not-an-ip"))


class PathTests(unittest.TestCase):
    def test_unique_path(self):
        value = cdn_probe.unique_path("/api-stream")
        self.assertTrue(value.startswith("/api-stream?probe="))

    def test_unique_path_with_query(self):
        value = cdn_probe.unique_path("/api-stream?x=1")
        self.assertTrue(value.startswith("/api-stream?x=1&probe="))


class RequestTests(unittest.TestCase):
    def test_request_line_and_host(self):
        raw = cdn_probe._request_bytes("cdn.example.com", "GET", "/api-stream", b"")
        self.assertIn(b"GET /api-stream HTTP/1.1", raw)
        self.assertIn(b"Host: cdn.example.com", raw)
        self.assertNotIn(b"Content-Length", raw)

    def test_body_sets_content_length(self):
        raw = cdn_probe._request_bytes("cdn.example.com", "POST", "/api-stream", b"abc")
        self.assertIn(b"Content-Length: 3", raw)
        self.assertTrue(raw.endswith(b"abc"))


class StatusClassTests(unittest.TestCase):
    def test_routed(self):
        for code in (200, 204, 302, 400, 404):
            self.assertEqual(cdn_probe.status_class(code), "ROUTED")

    def test_blocked(self):
        for code in (403, 405, 413, 501):
            self.assertEqual(cdn_probe.status_class(code), "BLOCKED")

    def test_errors(self):
        self.assertEqual(cdn_probe.status_class(502), "ERROR")
        self.assertEqual(cdn_probe.status_class(None), "ERR")


class CertificateTests(unittest.TestCase):
    def test_certificate_fields(self):
        sock = FakeSocket(
            {
                "subject": ((("commonName", "cdn.example.com"),),),
                "issuer": ((("organizationName", "Example CA"),),),
                "notAfter": "Dec 25 22:56:35 2026 GMT",
                "subjectAltName": (("DNS", "cdn.example.com"),),
            }
        )
        result = cdn_probe._certificate(sock)
        self.assertEqual(result["subject"], "commonName=cdn.example.com")
        self.assertEqual(result["issuer"], "organizationName=Example CA")
        self.assertEqual(result["san"], ["cdn.example.com"])
        self.assertIsNotNone(result["days_left"])

    def test_empty_certificate(self):
        self.assertEqual(cdn_probe._certificate(FakeSocket({})), {})


if __name__ == "__main__":
    unittest.main()
