import contextlib
import importlib.util
import io
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "make_profile", ROOT / "scripts" / "make-profile.py"
)
make_profile = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(make_profile)

UUID = "00000000-0000-4000-8000-000000000000"
ADDRESS = "198.51.100.20"
SNI = "cdn.example.com"
PATH = "/api-stream"


class PresetTests(unittest.TestCase):
    def test_yandex_preset_is_get_header(self):
        preset = make_profile.preset("yandex-get")
        self.assertEqual(preset["mode"], "packet-up")
        self.assertEqual(preset["uplinkHTTPMethod"], "GET")
        self.assertEqual(preset["uplinkDataPlacement"], "header")
        self.assertEqual(preset["uplinkChunkSize"], "3000-4000")

    def test_minimal_preset_keeps_required_keys(self):
        preset = make_profile.preset("minimal")
        self.assertEqual(preset["uplinkHTTPMethod"], "GET")
        self.assertIn("scMaxEachPostBytes", preset)

    def test_unknown_preset_exits(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                make_profile.preset("nope")


class UriTests(unittest.TestCase):
    def build(self):
        return make_profile.build_uri(
            ADDRESS, SNI, SNI, PATH, 443, UUID, "CDN pinned", make_profile.preset("yandex-get")
        )

    def test_uri_shape(self):
        uri = self.build()
        parsed = urlsplit(uri)
        self.assertEqual(parsed.scheme, "vless")
        self.assertEqual(parsed.username, UUID)
        self.assertEqual(parsed.hostname, ADDRESS)
        self.assertEqual(parsed.port, 443)
        self.assertEqual(parsed.fragment, "CDN%20pinned")

    def test_extra_is_flat_json(self):
        query = parse_qs(urlsplit(self.build()).query)
        extra = json.loads(query["extra"][0])
        self.assertEqual(extra["mode"], "packet-up")
        self.assertEqual(extra["uplinkHTTPMethod"], "GET")
        self.assertNotIn("xhttpSettings", extra)

    def test_path_is_encoded(self):
        raw = self.build()
        self.assertIn("path=%2Fapi-stream", raw)
        query = parse_qs(urlsplit(raw).query)
        self.assertEqual(query["path"][0], PATH)
        self.assertEqual(query["sni"][0], SNI)
        self.assertEqual(query["type"][0], "xhttp")


class ClientConfigTests(unittest.TestCase):
    def test_config_matches_preset(self):
        config = make_profile.build_client_config(
            ADDRESS, SNI, SNI, PATH, 443, UUID, make_profile.preset("yandex-get"), 11096
        )
        inbound = config["inbounds"][0]
        self.assertEqual(inbound["port"], 11096)
        self.assertEqual(inbound["protocol"], "socks")
        outbound = config["outbounds"][0]
        self.assertEqual(outbound["protocol"], "vless")
        vnext = outbound["settings"]["vnext"][0]
        self.assertEqual(vnext["address"], ADDRESS)
        self.assertEqual(vnext["users"][0]["id"], UUID)
        stream = outbound["streamSettings"]
        self.assertEqual(stream["network"], "xhttp")
        self.assertEqual(stream["tlsSettings"]["serverName"], SNI)
        settings = stream["xhttpSettings"]
        self.assertEqual(settings["path"], PATH)
        self.assertEqual(settings["host"], SNI)
        self.assertEqual(settings["uplinkHTTPMethod"], "GET")
        self.assertEqual(settings["sessionPlacement"], "header")
        self.assertEqual(settings["sessionKey"], "X-Auth-Token")
        self.assertNotIn("sessionIDPlacement", settings)
        self.assertNotIn("enableXmux", settings)
        self.assertIn("maxConnections", settings["xmux"])

    def test_ipv6_address_is_bracketed_in_uri(self):
        uri = make_profile.build_uri(
            "2001:db8::1", SNI, SNI, PATH, 443, UUID, "v6", make_profile.preset("minimal")
        )
        parsed = urlsplit(uri)
        self.assertEqual(parsed.hostname, "2001:db8::1")
        self.assertIn("@[2001:db8::1]:443", uri)


class CliTests(unittest.TestCase):
    def test_writes_private_files(self):
        with tempfile.TemporaryDirectory() as directory:
            uri_path = Path(directory) / "pinned.vless"
            config_path = Path(directory) / "pinned.json"
            code = make_profile.main(
                [
                    "--uuid",
                    UUID,
                    "--address",
                    ADDRESS,
                    "--sni",
                    SNI,
                    "--path",
                    PATH,
                    "--out",
                    str(uri_path),
                    "--client-config",
                    str(config_path),
                    "--quiet",
                ]
            )
            self.assertEqual(code, 0)
            self.assertEqual(stat.S_IMODE(uri_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)
            self.assertTrue(uri_path.read_text().startswith("vless://"))
            json.loads(config_path.read_text())

    def test_json_summary_does_not_leak_uuid(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            make_profile.main(
                [
                    "--uuid",
                    UUID,
                    "--address",
                    ADDRESS,
                    "--sni",
                    SNI,
                    "--json",
                    "--quiet",
                ]
            )
        output = buffer.getvalue()
        self.assertNotIn(UUID, output)
        self.assertEqual(json.loads(output)["sni"], SNI)

    def test_rejects_bad_uuid(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                make_profile.main(
                    ["--uuid", "not-a-uuid", "--address", ADDRESS, "--sni", SNI]
                )

    def test_rejects_bad_path(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                make_profile.main(
                    [
                        "--uuid",
                        UUID,
                        "--address",
                        ADDRESS,
                        "--sni",
                        SNI,
                        "--path",
                        "api-stream",
                    ]
                )

    def test_rejects_stdout_uri_and_json_together(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                make_profile.main(
                    [
                        "--uuid",
                        UUID,
                        "--address",
                        ADDRESS,
                        "--sni",
                        SNI,
                        "--out",
                        "-",
                        "--json",
                    ]
                )


if __name__ == "__main__":
    unittest.main()
