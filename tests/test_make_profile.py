import contextlib
import importlib.util
import io
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

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

# The `extra` blob of a share link that was imported and used on a phone.
# The generator must keep reproducing it byte for byte.
VERIFIED_EXTRA = (
    "%257B%250A%2520%2520%2522scMaxEachPostBytes%2522%2520:%252032768,"
    "%250A%2520%2520%2522scMinPostsIntervalMs%2522%2520:%252010,"
    "%250A%2520%2520%2522uplinkChunkSize%2522%2520:%2520%252216384%2522,"
    "%250A%2520%2520%2522uplinkDataPlacement%2522%2520:%2520%2522header%2522,"
    "%250A%2520%2520%2522uplinkHTTPMethod%2522%2520:%2520%2522GET%2522%250A%257D"
)


def decode_extra(value):
    """Decode a share-link `extra` blob the way a client does."""
    for _ in range(3):
        try:
            return json.loads(value)
        except ValueError:
            value = unquote(value)
    raise AssertionError("extra blob does not decode to JSON")


class PresetTests(unittest.TestCase):
    def test_yandex_preset_is_get_header(self):
        preset = make_profile.preset("yandex-get")
        self.assertEqual(preset["uplinkHTTPMethod"], "GET")
        self.assertEqual(preset["uplinkDataPlacement"], "header")
        self.assertEqual(preset["uplinkChunkSize"], 4000)

    def test_happ_parity_preset_matches_verified_profile(self):
        preset = make_profile.preset("happ-parity")
        self.assertEqual(
            set(preset),
            {
                "scMaxEachPostBytes",
                "scMinPostsIntervalMs",
                "uplinkChunkSize",
                "uplinkDataPlacement",
                "uplinkHTTPMethod",
            },
        )
        self.assertEqual(preset["uplinkChunkSize"], 16384)
        self.assertNotIn("mode", preset)
        self.assertNotIn("uplinkDataKey", preset)
        self.assertNotIn("scMaxBufferedPosts", preset)

    def test_preset_is_accepted_by_old_client_cores(self):
        for name in ("happ-parity", "yandex-get", "minimal"):
            preset = make_profile.preset(name)
            self.assertIsInstance(preset["uplinkChunkSize"], int)
            self.assertGreater(preset["scMaxEachPostBytes"], 8192)
            for key in (
                "sessionPlacement",
                "sessionKey",
                "seqPlacement",
                "seqKey",
                "xPaddingKey",
                "xPaddingHeader",
                "xPaddingPlacement",
                "xPaddingMethod",
                "xPaddingObfsMode",
                "xPaddingBytes",
            ):
                self.assertNotIn(key, preset)

    def test_minimal_preset_keeps_required_keys(self):
        preset = make_profile.preset("minimal")
        self.assertEqual(preset["uplinkHTTPMethod"], "GET")
        self.assertIn("scMaxEachPostBytes", preset)

    def test_unknown_preset_exits(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                make_profile.preset("nope")


class UriTests(unittest.TestCase):
    def build(self, preset="yandex-get", **kwargs):
        return make_profile.build_uri(
            ADDRESS, SNI, SNI, PATH, 443, UUID, "CDN pinned",
            make_profile.preset(preset), **kwargs
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
        extra = decode_extra(query["extra"][0])
        self.assertEqual(extra["uplinkHTTPMethod"], "GET")
        self.assertNotIn("xhttpSettings", extra)
        self.assertNotIn("mode", extra)

    def test_share_link_extra_is_encoded_twice(self):
        raw = dict(
            pair.split("=", 1) for pair in urlsplit(self.build()).query.split("&")
        )
        self.assertIn("%25", raw["extra"])
        extra = decode_extra(raw["extra"])
        self.assertEqual(extra["uplinkChunkSize"], "4000")
        self.assertIsInstance(extra["uplinkChunkSize"], str)

    def test_parity_preset_reproduces_verified_extra_blob(self):
        uri = make_profile.build_uri(
            ADDRESS, SNI, SNI, PATH, 443, UUID, "pinned",
            make_profile.preset("happ-parity"),
        )
        raw = dict(
            pair.split("=", 1) for pair in urlsplit(uri).query.split("&")
        )
        self.assertEqual(raw["extra"], VERIFIED_EXTRA)

    def test_plain_extra_encoding_decodes_once(self):
        raw = dict(
            pair.split("=", 1)
            for pair in urlsplit(self.build(extra_encoding="plain")).query.split("&")
        )
        self.assertNotIn("%25", raw["extra"])
        self.assertEqual(decode_extra(raw["extra"])["uplinkChunkSize"], "4000")

    def test_mode_is_a_top_level_field(self):
        query = parse_qs(urlsplit(self.build()).query)
        self.assertEqual(query["mode"][0], "packet-up")

    def test_path_is_encoded(self):
        raw = self.build()
        self.assertIn("path=/api-stream", raw)
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
        self.assertEqual(settings["mode"], "packet-up")
        self.assertEqual(settings["uplinkHTTPMethod"], "GET")
        self.assertEqual(settings["uplinkChunkSize"], 4000)
        self.assertNotIn("sessionPlacement", settings)
        self.assertNotIn("sessionKey", settings)
        self.assertNotIn("seqPlacement", settings)
        self.assertNotIn("sessionIDPlacement", settings)
        self.assertNotIn("enableXmux", settings)
        self.assertIn("maxConnections", settings["xmux"])

    def test_encryption_is_always_explicit(self):
        config = make_profile.build_client_config(
            ADDRESS, SNI, SNI, PATH, 443, UUID, make_profile.preset("happ-parity"), 11096
        )
        user = config["outbounds"][0]["settings"]["vnext"][0]["users"][0]
        self.assertEqual(user["encryption"], "none")

    def test_parity_preset_client_config_has_no_xmux(self):
        config = make_profile.build_client_config(
            ADDRESS, SNI, SNI, PATH, 443, UUID, make_profile.preset("happ-parity"), 11096,
            include_xmux=make_profile.PRESET_CLIENT_XMUX["happ-parity"],
        )
        settings = config["outbounds"][0]["streamSettings"]["xhttpSettings"]
        self.assertNotIn("xmux", settings)
        self.assertEqual(settings["uplinkChunkSize"], 16384)
        self.assertNotIn("uplinkDataKey", settings)
        self.assertNotIn("scMaxBufferedPosts", settings)

    def test_ipv6_address_is_bracketed_in_uri(self):
        uri = make_profile.build_uri(
            "2001:db8::1", SNI, SNI, PATH, 443, UUID, "v6", make_profile.preset("minimal")
        )
        parsed = urlsplit(uri)
        self.assertEqual(parsed.hostname, "2001:db8::1")
        self.assertIn("@[2001:db8::1]:443", uri)


class EncryptionTests(unittest.TestCase):
    KEY = "mlkem768x25519plus.native.0rtt.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

    def test_uri_carries_encryption_key(self):
        uri = make_profile.build_uri(
            ADDRESS, SNI, SNI, PATH, 443, UUID, "enc", make_profile.preset("yandex-get"),
            self.KEY,
        )
        query = parse_qs(urlsplit(uri).query)
        self.assertEqual(query["encryption"][0], self.KEY)

    def test_default_encryption_is_omitted(self):
        uri = make_profile.build_uri(
            ADDRESS, SNI, SNI, PATH, 443, UUID, "plain", make_profile.preset("yandex-get")
        )
        self.assertNotIn("encryption", parse_qs(urlsplit(uri).query))

    def test_client_config_carries_encryption_key(self):
        config = make_profile.build_client_config(
            ADDRESS, SNI, SNI, PATH, 443, UUID, make_profile.preset("yandex-get"), 11096,
            self.KEY,
        )
        user = config["outbounds"][0]["settings"]["vnext"][0]["users"][0]
        self.assertEqual(user["encryption"], self.KEY)

    def test_validator_accepts_none_and_keys(self):
        self.assertEqual(make_profile.validate_vless_encryption("none"), "none")
        self.assertEqual(make_profile.validate_vless_encryption(""), "none")
        self.assertEqual(make_profile.validate_vless_encryption(self.KEY), self.KEY)

    def test_validator_rejects_bad_keys(self):
        for value in ("aes-128-gcm", "mlkem768x25519plus", "mlkem768x25519plus.native", "x " + self.KEY):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    make_profile.validate_vless_encryption(value)


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

    def test_chunk_size_override_is_numeric(self):
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
                    "--chunk-size",
                    "2500",
                    "--out",
                    str(uri_path),
                    "--client-config",
                    str(config_path),
                    "--quiet",
                ]
            )
            self.assertEqual(code, 0)
            query = parse_qs(urlsplit(uri_path.read_text().strip()).query)
            extra = decode_extra(query["extra"][0])
            self.assertEqual(extra["uplinkChunkSize"], "2500")
            config = json.loads(config_path.read_text())
            settings = config["outbounds"][0]["streamSettings"]["xhttpSettings"]
            self.assertEqual(settings["uplinkChunkSize"], 2500)

    def test_chunk_size_range_warns_about_old_cores(self):
        with tempfile.TemporaryDirectory() as directory:
            uri_path = Path(directory) / "pinned.vless"
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                make_profile.main(
                    [
                        "--uuid",
                        UUID,
                        "--address",
                        ADDRESS,
                        "--sni",
                        SNI,
                        "--chunk-size",
                        "3000-4000",
                        "--out",
                        str(uri_path),
                        "--quiet",
                    ]
                )
            self.assertIn("uint32", errors.getvalue())
            query = parse_qs(urlsplit(uri_path.read_text().strip()).query)
            self.assertEqual(
                decode_extra(query["extra"][0])["uplinkChunkSize"], "3000-4000"
            )

    def test_chunk_size_rejects_bad_values(self):
        for value in ("0", "999999", "4000-3000", "abc"):
            with self.assertRaises(SystemExit):
                make_profile.main(
                    [
                        "--uuid",
                        UUID,
                        "--address",
                        ADDRESS,
                        "--sni",
                        SNI,
                        "--chunk-size",
                        value,
                        "--out",
                        "-",
                    ]
                )

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

    def test_writes_encryption_to_uri_and_config(self):
        with tempfile.TemporaryDirectory() as directory:
            uri_path = Path(directory) / "encrypted.vless"
            config_path = Path(directory) / "encrypted.json"
            code = make_profile.main(
                [
                    "--uuid", UUID,
                    "--address", ADDRESS,
                    "--sni", SNI,
                    "--vless-encryption", EncryptionTests.KEY,
                    "--out", str(uri_path),
                    "--client-config", str(config_path),
                    "--quiet",
                ]
            )
            self.assertEqual(code, 0)
            query = parse_qs(urlsplit(uri_path.read_text()).query)
            self.assertEqual(query["encryption"][0], EncryptionTests.KEY)
            config = json.loads(config_path.read_text())
            user = config["outbounds"][0]["settings"]["vnext"][0]["users"][0]
            self.assertEqual(user["encryption"], EncryptionTests.KEY)

    def test_json_summary_does_not_leak_encryption_key(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            make_profile.main(
                [
                    "--uuid", UUID,
                    "--address", ADDRESS,
                    "--sni", SNI,
                    "--vless-encryption", EncryptionTests.KEY,
                    "--json",
                    "--quiet",
                ]
            )
        output = buffer.getvalue()
        self.assertNotIn(EncryptionTests.KEY, output)
        self.assertTrue(json.loads(output)["vless_encryption_enabled"])

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
