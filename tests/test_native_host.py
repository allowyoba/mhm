"""Profiles, storage and lifecycle regressions for mhm's actual native entry point."""

import base64
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "mhm_native", ROOT / "native-host/native_host.py"
)
nh = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = nh
spec.loader.exec_module(nh)
YAML = """proxies:
  - name: First
    type: socks5
    server: 127.0.0.1
    port: 65000
  - name: Second
    type: socks5
    server: 127.0.0.1
    port: 65001
proxy-groups:
  - name: Choose
    type: select
    proxies: [First, Second]
rules: [MATCH,Choose]
""".replace("rules: [MATCH,Choose]", 'rules: ["MATCH,Choose"]')
VLESS = "vless://11111111-1111-4111-8111-111111111111@example.org:443?security=reality&encryption=none&pbk=PUBLICKEY123456789&fp=chrome&type=tcp&flow=xtls-rprx-vision&sni=example.net&sid=0123abcd#Legacy"
MIHOMO_BINARY = (
    ROOT / ".build/validation" / ("mihomo.exe" if os.name == "nt" else "mihomo")
)
KEY = base64.b64encode(bytes(range(32))).decode()
WG = f"""[Interface]
PrivateKey = {KEY}
Address = 10.20.30.2/32
DNS = 1.1.1.1
[Peer]
PublicKey = {KEY}
Endpoint = 127.0.0.1:51820
AllowedIPs = 0.0.0.0/0
"""


class MemorySecrets:
    secure = True
    name = "test-secure"

    def __init__(self):
        self.values = {}

    def put(self, key, value):
        self.values[key] = value

    def get(self, key):
        if key not in self.values:
            raise nh.HostError("missing", "NOT_FOUND")
        return self.values[key]

    def delete(self, key):
        self.values.pop(key, None)

    def info(self):
        return {"secure": True, "backend": self.name}


class HostTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.secrets = MemorySecrets()
        self.patch = mock.patch.multiple(
            nh,
            BASE_DIR=self.base,
            PROFILES_PATH=self.base / "profiles.json",
            SETTINGS_PATH=self.base / "settings.json",
            SUBSCRIPTIONS_PATH=self.base / "subscriptions.json",
            DIAGNOSTIC_LOG=self.base / "log",
            SECRETS=self.secrets,
            TRAFFIC=nh.TrafficStore(self.base / "traffic.sqlite3"),
            PROFILES=nh.create_profile_store(
                self.base / "profiles.json",
                self.secrets,
                self.base / "subscriptions.json",
            ),
            CLIENT=nh.MihomoProcess(),
        )
        self.patch.start()
        self.validation = mock.patch.object(nh, "validate_mihomo")
        self.validation.start()

    def tearDown(self):
        nh.CLIENT.stop()
        self.validation.stop()
        self.patch.stop()
        self.tmp.cleanup()

    def save(self, **patch):
        return nh.handle(
            {
                "command": "saveMihomoProfile",
                "name": "Personal",
                "source": "local",
                "yaml": YAML,
                **patch,
            }
        )["profile"]


class ProfileTests(HostTest):
    def test_full_yaml_is_preserved_and_not_in_metadata(self):
        profile = self.save()
        self.assertEqual(nh.PROFILES.bundle(profile["id"])["yaml"], YAML)
        self.assertNotIn("127.0.0.1", (self.base / "profiles.json").read_text())
        self.assertNotIn("yaml", profile)
        self.assertNotIn("secretKey", profile)
        self.assertEqual(nh.profile_document(YAML)["rules"], ["MATCH,Choose"])

    def test_subscription_is_one_profile_with_headers_and_usage(self):
        with mock.patch.object(
            nh,
            "download_subscription",
            return_value=(
                YAML.encode(),
                {
                    "subscription-userinfo": "upload=4; download=6; total=100; expire=2000000000"
                },
            ),
        ) as fetch:
            profile = self.save(
                source="subscription",
                url="https://example.org/token",
                headers={"Authorization": "Bearer SECRET"},
                yaml=None,
            )
        self.assertEqual(len(nh.PROFILES.list()), 1)
        self.assertEqual(profile["usage"]["total"], 100)
        self.assertEqual(fetch.call_args.args[1], {"Authorization": "Bearer SECRET"})
        self.assertNotIn("SECRET", json.dumps(nh.PROFILES.list()))
        self.assertNotIn("token", json.dumps(nh.PROFILES.list()))

    def test_failed_update_keeps_revision_and_content(self):
        with mock.patch.object(
            nh, "fetch_mihomo_subscription", return_value=(YAML, None)
        ):
            profile = self.save(
                source="subscription", url="https://example.org/token", yaml=None
            )
        with mock.patch.object(
            nh, "fetch_mihomo_subscription", side_effect=nh.HostError("offline")
        ):
            with self.assertRaises(nh.HostError):
                nh.PROFILES.update(profile["id"])
        self.assertEqual(nh.PROFILES.bundle(profile["id"])["yaml"], YAML)
        self.assertEqual(
            nh.PROFILES.get_meta(profile["id"])["revision"], profile["revision"]
        )
        self.assertTrue(nh.PROFILES.get_meta(profile["id"])["lastError"])

    def test_failed_validation_does_not_commit(self):
        profile = self.save()
        with mock.patch.object(
            nh, "validate_mihomo", side_effect=nh.HostError("invalid")
        ):
            with self.assertRaises(nh.HostError):
                self.save(profileId=profile["id"], yaml=YAML.replace("First", "Other"))
        self.assertEqual(nh.PROFILES.bundle(profile["id"])["yaml"], YAML)

    def test_rollback_swaps_revisions(self):
        profile = self.save()
        self.save(profileId=profile["id"], yaml=YAML.replace("First", "Other"))
        nh.PROFILES.rollback(profile["id"])
        self.assertEqual(nh.PROFILES.bundle(profile["id"])["yaml"], YAML)

    def test_same_update_preserves_existing_rollback(self):
        profile = self.save()
        modified = YAML.replace("First", "Other")
        self.save(profileId=profile["id"], yaml=modified)
        self.save(profileId=profile["id"], yaml=modified)
        self.assertEqual(nh.PROFILES.bundle(profile["id"])["previousYaml"], YAML)

    def test_commit_failure_keeps_old_secret(self):
        profile = self.save()
        old = nh.PROFILES.get_meta(profile["id"])["secretKey"]
        with mock.patch.object(
            nh.PROFILES, "_save_metadata", side_effect=OSError("disk full")
        ):
            with self.assertRaises(OSError):
                self.save(profileId=profile["id"], yaml=YAML.replace("First", "Other"))
        self.assertIn(old, self.secrets.values)
        self.assertEqual(len(self.secrets.values), 1)

    def test_delete_active_is_rejected(self):
        profile = self.save()
        nh.CLIENT.profile_id = profile["id"]
        with mock.patch.object(nh.CLIENT, "running", return_value=True):
            with self.assertRaises(nh.HostError):
                nh.handle({"command": "deleteProfile", "profileId": profile["id"]})
        self.assertEqual(len(nh.PROFILES.list()), 1)

    def test_source_chunking_handles_large_profiles(self):
        profile = self.save(yaml=YAML + "# " + ("x" * 1100000))
        result = nh.handle({"command": "profileSource", "profileId": profile["id"]})
        self.assertNotIn("yaml", result)
        first = nh.handle(
            {"command": "profileContent", "profileId": profile["id"], "offset": 0}
        )
        self.assertEqual(len(first["text"]), 100000)

    def test_old_subscription_migrates_then_replaces_nodes_and_stays_deleted(self):
        self.secrets.put("node", VLESS)
        self.secrets.put("subscription:old-sub", "https://example.org/private-token")
        nh.PROFILES._save_metadata(
            [{"id": "node", "name": "Old node", "subscriptionId": "old-sub"}]
        )
        nh.SUBSCRIPTIONS_PATH.write_text(
            json.dumps(
                [{"id": "old-sub", "name": "Old subscription", "transport": "direct"}]
            )
        )
        first = nh.handle({"command": "bootstrap"})["profiles"]
        self.assertEqual(len(first), 2)
        subscription = next(p for p in first if p.get("legacySubscriptionId"))
        self.assertTrue(subscription["needsDownload"])
        with mock.patch.object(
            nh, "fetch_mihomo_subscription", return_value=(YAML, None)
        ):
            nh.PROFILES.update(subscription["id"])
        self.assertEqual(len(nh.PROFILES.list()), 1)
        nh.PROFILES.delete(subscription["id"])
        self.assertEqual(nh.handle({"command": "bootstrap"})["profiles"], [])

    def test_path_traversal_rejected(self):
        with self.assertRaises(nh.HostError):
            nh.PROFILES.get_meta("../outside")

    def test_legacy_profile_migrates_without_losing_id(self):
        self.secrets.put("legacy-id", VLESS)
        nh.PROFILES._save_metadata([{"id": "legacy-id", "name": "Legacy"}])
        result = nh.handle({"command": "bootstrap"})
        self.assertEqual(result["profiles"][0]["id"], "legacy-id")
        self.assertEqual(result["profiles"][0]["format"], "mihomo")
        self.assertIn("reality-opts", nh.PROFILES.bundle("legacy-id")["yaml"])

    def test_no_plaintext_fallback_in_product_commands(self):
        with mock.patch.object(self.secrets, "secure", False):
            with self.assertRaises(nh.HostError):
                nh.handle({"command": "bootstrap"})

    def test_protocol_validation(self):
        with self.assertRaises(nh.HostError):
            nh.handle({"command": "ping", "protocolVersion": 2, "requestId": "1"})
        with self.assertRaises(nh.HostError):
            nh.handle({"command": "ping", "protocolVersion": 1})


class ConfigTests(HostTest):
    def test_nodes_only_subscription_gets_group(self):
        doc = nh.load_mihomo_yaml(
            "proxies:\n- {name: A, type: socks5, server: example.org, port: 1080}"
        )
        self.assertEqual(doc["rules"], ["MATCH,mhm"])

    def test_aliases_and_merge_keys_work(self):
        doc = nh.load_mihomo_yaml(
            "base: &base {type: socks5, server: example.org, port: 1080}\nproxies:\n- <<: *base\n  name: Test"
        )
        self.assertEqual(doc["proxies"][0]["server"], "example.org")

    def test_recursive_aliases_duplicate_keys_and_deep_nesting_rejected(self):
        for payload in [
            "a: &a [*a]\nproxies: []",
            "proxies: []\nproxies: []",
            "a: " + ("[" * 45) + "0" + ("]" * 45),
        ]:
            with self.subTest(payload=payload), self.assertRaises(nh.HostError):
                nh.load_mihomo_yaml(payload)

    def test_local_listeners_and_controllers_are_overridden(self):
        doc = nh.profile_document(YAML)
        doc.update(
            {
                "allow-lan": True,
                "tun": {"enable": True},
                "listeners": [{"port": 8080}],
                "external-controller-unix": "/tmp/attack",
                "external-ui-url": "https://example.org",
            }
        )
        config = nh.build_mihomo_config(doc, self.base, 10808, 19090, "secret")
        self.assertEqual(config["bind-address"], "127.0.0.1")
        self.assertFalse(config["tun"]["enable"])
        self.assertNotIn("listeners", config)
        self.assertNotIn("external-controller-unix", config)
        self.assertEqual(config["rules"], ["MATCH,Choose"])

    def test_provider_paths_are_owned(self):
        doc = nh.profile_document(YAML)
        doc["proxy-providers"] = {
            "Remote": {
                "type": "http",
                "url": "https://example.org/sub",
                "path": "../../arbitrary",
            }
        }
        config = nh.build_mihomo_config(doc, self.base, 10808, 19090, "secret")
        self.assertEqual(
            Path(config["proxy-providers"]["Remote"]["path"]).parent, self.base
        )

    def test_file_provider_is_explicitly_rejected(self):
        doc = nh.profile_document(YAML)
        doc["proxy-providers"] = {"File": {"type": "file", "path": "/etc/passwd"}}
        with self.assertRaises(nh.HostError):
            nh.build_mihomo_config(doc, self.base, 10808, 19090, "secret")

    def test_wireguard_accepts_split_routes_and_preserves_keepalive(self):
        proxy = nh.wireguard_proxy(WG)
        self.assertEqual(proxy["private-key"], KEY)
        self.assertTrue(proxy["remote-dns-resolve"])
        split = nh.wireguard_proxy(
            WG.replace("0.0.0.0/0", "172.31.253.0/24, 192.168.8.0/24")
            + "PersistentKeepalive = 25\n"
        )
        self.assertEqual(
            split["peers"][0]["allowed-ips"], ["172.31.253.0/24", "192.168.8.0/24"]
        )
        self.assertEqual(split["persistent-keepalive"], 25)
        with self.assertRaisesRegex(nh.HostError, "PublicKey is invalid Base64"):
            nh.wireguard_proxy(
                WG.replace("PublicKey = " + KEY, "PublicKey = " + KEY[:-2] + "=")
            )
        for bad in [
            WG.replace("0.0.0.0/0", "invalid-network"),
            WG.replace(KEY, "invalid"),
            WG + "PostUp = touch /tmp/attack\n",
        ]:
            with self.subTest(bad=bad), self.assertRaises(nh.HostError):
                nh.wireguard_proxy(bad)

    def test_wireguard_reports_missing_field_without_disclosing_keys(self):
        missing = WG.replace("Address = 10.20.30.2/32\n", "")
        with self.assertRaisesRegex(nh.HostError, "missing Address") as caught:
            nh.wireguard_proxy(missing)
        self.assertNotIn(KEY, str(caught.exception))

    def test_wireguard_accepts_utf8_bom_and_windows_newlines(self):
        proxy = nh.wireguard_proxy("\ufeff" + WG.replace("\n", "\r\n"))
        self.assertEqual(proxy["ip"], "10.20.30.2")

    def test_wireguard_failure_does_not_download_directly(self):
        with mock.patch.object(
            nh.MihomoProcess,
            "start_document",
            side_effect=nh.HostError("handshake failed"),
        ), mock.patch.object(nh, "download_subscription") as fetch:
            with self.assertRaises(nh.HostError):
                nh.fetch_mihomo_subscription(
                    {"wireguard": WG, "url": "https://example.org"}, "wireguard"
                )
            fetch.assert_not_called()

    def test_wireguard_download_uses_tunnel_and_always_stops(self):
        with mock.patch.object(nh.MihomoProcess, "start_document"), mock.patch.object(
            nh.MihomoProcess, "stop"
        ) as stop, mock.patch.object(
            nh, "download_subscription", return_value=(YAML.encode(), {})
        ) as fetch:
            nh.fetch_mihomo_subscription(
                {"wireguard": WG, "url": "https://example.org"}, "wireguard"
            )
            stop.assert_called_once()
            self.assertEqual(len(fetch.call_args.args), 3)

    def test_wireguard_uses_internal_dns_without_public_resolver(self):
        with mock.patch.object(
            nh.MihomoProcess, "start_document"
        ) as start, mock.patch.object(nh.MihomoProcess, "stop"), mock.patch.object(
            nh, "download_subscription", return_value=(YAML.encode(), {})
        ):
            nh.fetch_mihomo_subscription(
                {
                    "wireguard": WG.replace("1.1.1.1", "10.20.30.1"),
                    "url": "https://panel.example.invalid/sub",
                },
                "wireguard",
            )
        document = start.call_args.args[0]
        self.assertFalse(document["dns"]["enable"])
        self.assertEqual(document["proxies"][0]["dns"], ["10.20.30.1"])

    def test_https_certificate_error_is_actionable_and_redacted(self):
        with mock.patch.object(
            nh.http.client,
            "HTTPSConnection",
            side_effect=nh.ssl.SSLCertVerificationError("PRIVATE-URL-TOKEN"),
        ):
            with self.assertRaisesRegex(
                nh.HostError, "certificate is not trusted"
            ) as caught:
                nh.download_subscription("https://example.org/private-token", {})
        self.assertNotIn("PRIVATE-URL-TOKEN", str(caught.exception))
        self.assertNotIn("private-token", str(caught.exception))

    def test_wireguard_timeout_is_distinct_from_certificate_failure(self):
        with mock.patch.object(
            nh.transport, "_open_socks5_tunnel", side_effect=TimeoutError()
        ):
            with self.assertRaisesRegex(nh.HostError, "timed out through WireGuard"):
                nh.download_subscription("https://example.org/sub", {}, 10808)

    def test_subscription_redirects_cannot_downgrade_https(self):
        response = mock.Mock(status=302)
        response.getheader.return_value = "http://example.org/plain"
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.object(
            nh.http.client, "HTTPSConnection", return_value=connection
        ):
            with self.assertRaises(nh.HostError):
                nh.download_subscription("https://example.org/secret", {})

    def test_authorization_not_forwarded_to_other_origin(self):
        first = mock.Mock(status=302)
        first.getheader.return_value = "https://other.example.org/sub"
        second = mock.Mock(status=200)
        second.read.return_value = YAML.encode()
        second.getheaders.return_value = []
        a = mock.Mock()
        a.getresponse.return_value = first
        b = mock.Mock()
        b.getresponse.return_value = second
        with mock.patch.object(nh.http.client, "HTTPSConnection", side_effect=[a, b]):
            nh.download_subscription(
                "https://example.org/sub", {"Authorization": "SECRET"}
            )
        self.assertNotIn("Authorization", b.request.call_args.kwargs["headers"])


@unittest.skipUnless(
    MIHOMO_BINARY.is_file(), "Set up the verified mihomo validation binary"
)
class RealMihomoTests(HostTest):
    def setUp(self):
        super().setUp()
        self.validation.stop()
        self.binary = mock.patch.object(
            nh, "find_mihomo", return_value=str(MIHOMO_BINARY)
        )
        self.binary.start()

    def tearDown(self):
        self.binary.stop()
        super().tearDown()

    def test_normal_entry_point_connect_select_mode_telemetry_disconnect(self):
        profile = self.save()
        result = nh.handle({"command": "connect", "profileId": profile["id"]})
        self.assertEqual(result["status"], "connected")
        self.assertEqual(
            nh.handle({"command": "groups"})["groups"][0]["name"], "Choose"
        )
        nh.handle({"command": "selectProxy", "group": "Choose", "proxy": "Second"})
        self.assertEqual(nh.CLIENT.groups()[0]["now"], "Second")
        nh.handle({"command": "setMode", "mode": "global"})
        self.assertTrue(any(g["name"] == "GLOBAL" for g in nh.CLIENT.groups()))
        port = result["localPort"]
        nh.handle({"command": "disconnect"})
        self.assertTrue(nh.is_port_available(port))
        self.assertEqual(list((self.base / "runtime").iterdir()), [])
        nh.handle({"command": "connect", "profileId": profile["id"]})
        self.assertEqual(nh.CLIENT.groups()[0]["now"], "Second")

    def test_real_wireguard_configuration_passes_engine_validation(self):
        doc = {"proxies": [nh.wireguard_proxy(WG)], "rules": ["MATCH,mhm WireGuard"]}
        nh.validate_mihomo(doc, "wg-check")
        split = nh.wireguard_proxy(
            WG.replace("0.0.0.0/0", "172.31.253.0/24, 192.168.8.0/24")
            + "PersistentKeepalive = 25\n"
        )
        nh.validate_mihomo(
            {"proxies": [split], "rules": ["MATCH,mhm WireGuard"]}, "wg-split-check"
        )

    def test_real_load_balance_group_without_now_can_measure_delay(self):
        import threading
        import urllib.parse
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(204)
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            content = (
                "proxies:\n- {name: Local, type: direct}\n"
                "proxy-groups:\n- name: RU-ACTIVE\n  type: load-balance\n  strategy: round-robin\n  proxies: [Local]\n"
                'rules: ["MATCH,RU-ACTIVE"]\n'
            )
            profile = self.save(yaml=content)
            nh.handle({"command": "connect", "profileId": profile["id"]})
            group = nh.CLIENT.groups()[0]
            self.assertEqual(group["name"], "RU-ACTIVE")
            self.assertIsNone(group["now"])
            query = urllib.parse.urlencode(
                {"url": f"http://127.0.0.1:{server.server_port}/", "timeout": 3000}
            )
            result = nh.CLIENT.api("GET", "/proxies/RU-ACTIVE/delay?" + query)
            self.assertGreaterEqual(result["delay"], 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_invalid_engine_config_preserves_saved_profile(self):
        profile = self.save()
        with self.assertRaises(nh.HostError):
            self.save(
                profileId=profile["id"],
                yaml=YAML.replace("type: socks5", "type: nonexistent"),
            )
        self.assertEqual(nh.PROFILES.bundle(profile["id"])["yaml"], YAML)


class EncryptionAndResourcesTests(HostTest):
    def test_mac_encrypted_storage_detects_tampering(self):
        key = bytes(range(32))
        backend = nh.MacKeychainBackend("/usr/bin/security", self.base)
        with mock.patch.object(backend, "_key", return_value=key):
            backend.put("profile", "secret-subscription-token")
            self.assertNotIn(b"secret-subscription-token", backend.path.read_bytes())
            self.assertEqual(backend.get("profile"), "secret-subscription-token")
            data = bytearray(base64.b64decode(backend.path.read_bytes()))
            data[-1] ^= 1
            backend.path.write_bytes(base64.b64encode(data))
            with self.assertRaises(nh.HostError):
                backend.get("profile")

    def test_provider_cache_is_encrypted_and_restores_binary_files(self):
        profile = self.save()
        directory = self.base / "runtime" / "mhm-fixture"
        directory.mkdir(parents=True)
        name = "a" * 64 + ".yaml"
        (directory / name).write_bytes(b"\x00\xffprovider-secret")
        nh.CLIENT.directory = directory
        nh.CLIENT.profile_id = profile["id"]
        nh.CLIENT.stop()
        self.assertFalse(directory.exists())
        self.assertIn("resources-" + profile["id"], self.secrets.values)
        restored = self.base / "runtime" / "mhm-restored"
        restored.mkdir()
        nh.CLIENT.directory = restored
        nh.CLIENT.restore_resources(profile["id"])
        self.assertEqual((restored / name).read_bytes(), b"\x00\xffprovider-secret")

    def test_failed_candidate_attempts_previous_running_document(self):
        profile = self.save()
        nh.CLIENT.profile_id = "previous"
        nh.CLIENT.revision = "old"
        nh.CLIENT._document = nh.profile_document(YAML)
        with mock.patch.object(
            nh.CLIENT, "running", return_value=True
        ), mock.patch.object(
            nh.CLIENT,
            "start_document",
            side_effect=[nh.HostError("failed"), {"status": "connected"}],
        ) as start:
            with self.assertRaises(nh.HostError):
                nh.CLIENT.connect(profile["id"])
        self.assertEqual(start.call_args_list[-1].args[1:3], ("previous", "old"))

    def test_runtime_cleanup_cannot_delete_arbitrary_directory(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "keep").write_text("keep")
        nh.cleanup_runtime_directory(str(outside))
        self.assertTrue((outside / "keep").exists())


class SubscriptionCertificateTests(HostTest):
    @classmethod
    def setUpClass(cls):
        import datetime
        import ipaddress
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name(
            [x509.NameAttribute(x509.NameOID.COMMON_NAME, "mhm local test CA")]
        )
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(root_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=None), critical=True
            )
            .add_extension(
                x509.KeyUsage(
                    False, False, False, False, False, True, True, False, False
                ),
                critical=True,
            )
            .sign(root_key, hashes.SHA256())
        )
        cls.pem = cert.public_bytes(serialization.Encoding.PEM).decode()
        cls.der = cert.public_bytes(serialization.Encoding.DER)
        leaf = (
            x509.CertificateBuilder()
            .subject_name(
                x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "localhost")])
            )
            .issuer_name(name)
            .public_key(cls.key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None), critical=True
            )
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False
            )
            .add_extension(
                x509.ExtendedKeyUsage([x509.ExtendedKeyUsageOID.SERVER_AUTH]),
                critical=False,
            )
            .add_extension(
                x509.KeyUsage(
                    True, False, True, False, False, False, False, False, False
                ),
                critical=True,
            )
            .sign(root_key, hashes.SHA256())
        )
        cls.server_pem = (
            leaf.public_bytes(serialization.Encoding.PEM).decode() + cls.pem
        )
        cls.private_pem = cls.key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )

    def test_pem_der_import_and_private_key_rejection(self):
        self.assertEqual(nh.normalize_subscription_ca(self.pem), self.pem)
        self.assertEqual(
            nh.normalize_subscription_ca("der:" + base64.b64encode(self.der).decode()),
            self.pem,
        )
        for invalid in ["not a certificate", self.private_pem.decode()]:
            with self.assertRaises(nh.HostError):
                nh.normalize_subscription_ca(invalid)

    def test_certificate_settings_survive_updates_and_remain_encrypted(self):
        with mock.patch.object(
            nh, "fetch_mihomo_subscription", return_value=(YAML, None)
        ) as fetch:
            profile = self.save(
                source="subscription",
                url="https://example.org/sub",
                yaml=None,
                tlsMode="custom",
                caCertificate=self.pem,
            )
            nh.PROFILES.update(profile["id"])
        self.assertEqual(fetch.call_args.args[0]["caCertificate"], self.pem)
        self.assertEqual(nh.PROFILES.bundle(profile["id"])["tlsMode"], "custom")
        self.assertNotIn("BEGIN CERTIFICATE", (self.base / "profiles.json").read_text())
        source = nh.handle({"command": "profileSource", "profileId": profile["id"]})
        self.assertEqual(source["caCertificate"], self.pem)

    def test_real_https_default_custom_and_insecure_modes(self):
        import ssl
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        pem_path = self.base / "test-cert.pem"
        pem_path.write_text(self.server_pem)
        key_path = self.base / "test-key.pem"
        key_path.write_bytes(self.private_pem)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(YAML.encode())

        class TestHTTPServer(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                if isinstance(sys.exception(), (ConnectionResetError, ssl.SSLError)):
                    return
                super().handle_error(request, client_address)

        server = TestHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(pem_path, key_path)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"https://localhost:{server.server_port}/sub"
            with self.assertRaisesRegex(nh.HostError, "certificate is not trusted"):
                nh.download_subscription(url, {})
            body, _ = nh.download_subscription(
                url, {}, tls_options={"mode": "custom", "caCertificate": self.pem}
            )
            self.assertEqual(body.decode(), YAML)
            wrong_name = f"https://127.0.0.1:{server.server_port}/sub"
            with self.assertRaisesRegex(nh.HostError, "certificate is not trusted"):
                nh.download_subscription(
                    wrong_name,
                    {},
                    tls_options={"mode": "custom", "caCertificate": self.pem},
                )
            body, _ = nh.download_subscription(
                wrong_name, {}, tls_options={"mode": "insecure"}
            )
            self.assertEqual(body.decode(), YAML)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_insecure_mode_does_not_follow_cross_origin_redirect(self):
        first = mock.Mock(status=302)
        first.getheader.return_value = "https://other.example.org/sub"
        second = mock.Mock(status=200)
        second.read.return_value = YAML.encode()
        second.getheaders.return_value = []
        a, b = mock.Mock(), mock.Mock()
        a.getresponse.return_value = first
        b.getresponse.return_value = second
        with mock.patch.object(
            nh.http.client, "HTTPSConnection", side_effect=[a, b]
        ), mock.patch.object(nh.transport, "subscription_tls_context") as context:
            nh.download_subscription(
                "https://example.org/sub", {}, tls_options={"mode": "insecure"}
            )
        self.assertEqual(context.call_args_list[0].args, ({"mode": "insecure"},))
        self.assertEqual(context.call_args_list[1].args, (None,))


if __name__ == "__main__":
    unittest.main()
