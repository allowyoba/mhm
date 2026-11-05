import base64
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "setup" / "runtime.py"
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("setup_runtime", MODULE_PATH)
setup_runtime = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(setup_runtime)


class ExtensionIdentityTests(unittest.TestCase):
    def test_id_can_be_extracted_from_id_or_store_url(self):
        extension_id = "abcdefghijklmnopabcdefghijklmnop"
        self.assertEqual(
            setup_runtime.extension_id_from_text(extension_id), extension_id
        )
        self.assertEqual(
            setup_runtime.extension_id_from_text(
                f"https://chromewebstore.google.com/detail/example/{extension_id}"
            ),
            extension_id,
        )
        self.assertIsNone(
            setup_runtime.extension_id_from_text("https://example.com/no-id")
        )

    def test_id_is_calculated_from_manifest_public_key(self):
        public_key = bytes(range(1, 80))
        encoded = base64.b64encode(public_key).decode("ascii")
        digest = hashlib.sha256(public_key).hexdigest()[:32]
        expected = "".join(chr(ord("a") + int(ch, 16)) for ch in digest)
        self.assertEqual(
            setup_runtime.chrome_extension_id_from_public_key(encoded), expected
        )
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "manifest.json"
            manifest.write_text(json.dumps({"key": encoded}), encoding="utf-8")
            self.assertEqual(
                setup_runtime.extension_id_from_manifest(manifest), expected
            )

    def test_installed_marker_detection(self):
        extension_id = "abcdefghijklmnopabcdefghijklmnop"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "User Data"
            version = root / "Default" / "Extensions" / extension_id / "0.1.1_0"
            version.mkdir(parents=True)
            (version / setup_runtime.PRODUCT_MARKER).write_text(
                json.dumps({"product": setup_runtime.PRODUCT_MARKER_ID, "schema": 1}),
                encoding="utf-8",
            )
            with mock.patch.object(
                setup_runtime,
                "browser_user_data_roots",
                return_value=[("chrome", root)],
            ):
                found = setup_runtime.detect_installed_extensions()
            self.assertEqual(found[0]["extensionId"], extension_id)
            self.assertEqual(found[0]["browser"], "chrome")


class ExtensionProvisioningTests(unittest.TestCase):
    def test_web_store_url_is_validated(self):
        extension_id = "abcdefghijklmnopabcdefghijklmnop"
        url = f"https://chromewebstore.google.com/detail/example/{extension_id}"
        self.assertEqual(setup_runtime.normalize_web_store_url(url), url)
        with self.assertRaises(setup_runtime.SetupError):
            setup_runtime.normalize_web_store_url(
                f"https://example.com/detail/{extension_id}"
            )
        with self.assertRaises(setup_runtime.SetupError):
            setup_runtime.normalize_web_store_url(
                f"http://chromewebstore.google.com/detail/example/{extension_id}"
            )

    def test_explicit_id_must_match_store_url(self):
        extension_id = "abcdefghijklmnopabcdefghijklmnop"
        other_id = "bcdefghijklmnopabcdefghijklmnopa"
        url = f"https://chromewebstore.google.com/detail/example/{other_id}"
        with self.assertRaises(setup_runtime.SetupError):
            setup_runtime.resolve_extension_id(
                extension_id, url, None, interactive=False
            )

    def test_external_extension_file_uses_official_store_update_url(self):
        extension_id = "abcdefghijklmnopabcdefghijklmnop"
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / f"{extension_id}.json"
            setup_runtime._write_external_extension_file(target, extension_id)
            data = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(
            data, {"external_update_url": setup_runtime.CHROME_WEB_STORE_UPDATE_URL}
        )
        self.assertEqual(
            setup_runtime.CHROME_WEB_STORE_UPDATE_URL,
            "https://clients2.google.com/service/update2/crx",
        )

    def test_windows_elevation_is_limited_to_machine_phase(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("def _run_windows_elevated_self", source)
        self.assertIn('"--machine-phase"', source)
        self.assertIn("Keeping the main setup process unelevated", source)
        self.assertNotIn("def _relaunch_windows_as_admin", source)

    def test_build_scripts_do_not_embed_an_extension_id(self):
        text = (ROOT / "scripts" / "build_setup.py").read_text(encoding="utf-8")
        self.assertIsNone(
            __import__("re").search(r"(?<![a-p])[a-p]{32}(?![a-p])", text)
        )
        runtime = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("resolve_extension_id", runtime)
        self.assertIn("--extension-id", runtime)
        self.assertIn("--extension-manifest", runtime)
        self.assertIn("provision_external_extension", runtime)


class MihomoInstallerTests(unittest.TestCase):
    def test_release_asset_is_official_and_digest_is_required(self):
        release = {
            "prerelease": False,
            "tag_name": "v1.14.0",
            "assets": [
                {
                    "name": "mihomo-windows-amd64-compatible-v1.14.0.zip",
                    "browser_download_url": "https://github.com/MetaCubeX/mihomo/releases/download/v1.14.0/mihomo-windows-amd64-compatible-v1.14.0.zip",
                    "digest": "sha256:" + "a" * 64,
                }
            ],
        }
        with mock.patch.object(
            setup_runtime, "_http_json", return_value=release
        ), mock.patch.object(
            setup_runtime,
            "_platform_asset_parts",
            return_value=("windows", "amd64", "zip"),
        ):
            asset = setup_runtime.resolve_mihomo_asset("latest")
        self.assertEqual(asset["version"], "1.14.0")
        self.assertEqual(asset["sha256"], "a" * 64)

        release["assets"][0]["digest"] = None
        with mock.patch.object(
            setup_runtime, "_http_json", return_value=release
        ), mock.patch.object(
            setup_runtime,
            "_platform_asset_parts",
            return_value=("windows", "amd64", "zip"),
        ):
            with self.assertRaises(setup_runtime.SetupError):
                setup_runtime.resolve_mihomo_asset("latest")

    def test_manifest_is_created_only_after_runtime_id_is_known(self):
        extension_id = "abcdefghijklmnopabcdefghijklmnop"
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            host = tmp / "native_host"
            host.write_bytes(b"x")
            manifest = tmp / "host.json"
            setup_runtime._write_manifest(manifest, extension_id, host)
            data = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(
                data["allowed_origins"], [f"chrome-extension://{extension_id}/"]
            )
            self.assertEqual(data["path"], str(host.resolve()))

    def test_firefox_native_manifest_uses_allowed_extensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            host = tmp / "native_host"
            host.write_bytes(b"x")
            manifest = tmp / "host-firefox.json"
            setup_runtime._write_manifest(
                manifest, setup_runtime.FIREFOX_EXTENSION_ID, host
            )
            data = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(
                data["allowed_extensions"], [setup_runtime.FIREFOX_EXTENSION_ID]
            )
            self.assertNotIn("allowed_origins", data)
            self.assertEqual(data["path"], str(host.resolve()))


class LocalRuntimeDiscoveryTests(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "Unix executable fixture")
    def test_probe_and_detect_mihomo_verify_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "mihomo"
            binary.write_text(
                "#!/bin/sh\necho 'mihomo version 1.2.3'\n", encoding="utf-8"
            )
            binary.chmod(0o755)
            info = setup_runtime.probe_mihomo(binary)
            self.assertIsNotNone(info)
            self.assertEqual(info["version"], "1.2.3")
            with mock.patch.object(
                setup_runtime, "app_data_dir", return_value=Path(tmp) / "data"
            ), mock.patch.object(
                setup_runtime.shutil, "which", return_value=str(binary)
            ):
                detected = setup_runtime.detect_mihomo()
            self.assertEqual(detected[0]["path"], str(binary.resolve()))

    def test_user_native_host_install_is_versioned_and_pointer_based(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            payload = tmp / "payload"
            payload.mkdir()
            name = (
                "native_host.exe"
                if setup_runtime.sys.platform == "win32"
                else "native_host"
            )
            (payload / name).write_bytes(b"host")
            installed = setup_runtime.install_user_native_host(payload, tmp / "data")
            self.assertTrue(installed.is_file())
            self.assertIn("releases", installed.parts)
            pointer = tmp / "data" / "native_host" / "current.json"
            self.assertTrue(pointer.is_file())
            self.assertEqual(
                json.loads(pointer.read_text(encoding="utf-8"))["path"],
                str(installed.resolve()),
            )
            with mock.patch.object(
                setup_runtime, "app_data_dir", return_value=tmp / "data"
            ), mock.patch.object(
                setup_runtime, "embedded_native_host_archive", return_value=None
            ):
                self.assertEqual(setup_runtime.locate_native_host(), installed)

    def test_zipped_native_host_payload_is_extracted_without_outer_rewriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            payload = tmp / "payload"
            payload.mkdir()
            name = (
                "native_host.exe"
                if setup_runtime.sys.platform == "win32"
                else "native_host"
            )
            (payload / name).write_bytes(b"host")
            archive = Path(
                shutil.make_archive(str(tmp / "native_host"), "zip", payload)
            )
            installed = setup_runtime.install_user_native_host(archive, tmp / "data")
            self.assertEqual(installed.read_bytes(), b"host")

    def test_uninstall_can_preserve_or_remove_user_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data"
            (base / "native_host").mkdir(parents=True)
            (base / "mihomo").mkdir()
            (base / "profiles.json").write_text("[]", encoding="utf-8")
            manifest = base / f"{setup_runtime.HOST_NAME}-chrome.json"
            manifest.write_text("{}", encoding="utf-8")
            with mock.patch.object(
                setup_runtime, "_native_messaging_targets", return_value=[manifest]
            ), mock.patch.object(
                setup_runtime, "_configured_extension_ids", return_value=set()
            ):
                result = setup_runtime.uninstall_user_components(base=base)
            self.assertTrue(result["keptUserData"])
            self.assertTrue((base / "profiles.json").is_file())
            self.assertFalse((base / "native_host").exists())
            with mock.patch.object(
                setup_runtime, "_native_messaging_targets", return_value=[]
            ), mock.patch.object(
                setup_runtime, "_configured_extension_ids", return_value=set()
            ):
                result = setup_runtime.uninstall_user_components(
                    base=base, remove_user_data=True
                )
            self.assertFalse(result["keptUserData"])
            self.assertFalse(base.exists())

    def test_setup_transaction_restores_snapshotted_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data"
            target = base / "settings.json"
            target.parent.mkdir(parents=True)
            target.write_text('{"before":true}\n', encoding="utf-8")
            transaction = setup_runtime.SetupTransaction("test", base)
            transaction.snapshot(target)
            created = base / "new.json"
            transaction.snapshot(created)
            target.write_text('{"after":true}\n', encoding="utf-8")
            created.write_text("{}\n", encoding="utf-8")
            transaction.rollback("expected test failure")
            self.assertEqual(target.read_text(encoding="utf-8"), '{"before":true}\n')
            self.assertFalse(created.exists())
            journal = json.loads(
                (transaction.directory / "journal.json").read_text(encoding="utf-8")
            )
            self.assertEqual(journal["state"], "rolled-back")

    def test_diagnostics_do_not_export_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "profiles.json").write_text(
                '[{"url":"vless://secret"}]', encoding="utf-8"
            )
            with mock.patch.object(
                setup_runtime,
                "load_browser_channels",
                return_value={"chrome": {}, "opera": {}, "firefox": {}},
            ):
                report = setup_runtime.diagnose_system(base)
            encoded = json.dumps(report)
            self.assertNotIn("vless://", encoded)
            self.assertNotIn("secret", encoded)

    def test_uninstall_removes_profile_and_subscription_keychain_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "profiles.json").write_text(
                json.dumps([{"id": "profile-one"}]), encoding="utf-8"
            )
            (base / "subscriptions.json").write_text(
                json.dumps([{"id": "subscription-one"}]), encoding="utf-8"
            )
            with mock.patch.object(
                setup_runtime.sys, "platform", "darwin"
            ), mock.patch.object(
                setup_runtime.shutil, "which", return_value="/usr/bin/security"
            ), mock.patch.object(
                setup_runtime.subprocess, "run"
            ) as run:
                setup_runtime._remove_saved_secrets(base)
            accounts = [call.args[0][3] for call in run.call_args_list]
            self.assertEqual(accounts, ["profile-one", "subscription:subscription-one"])


class InstallerAcceptanceRegressionTests(unittest.TestCase):
    def test_embedded_payload_is_installed_before_old_host_is_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "payload"
            source.mkdir()
            host = source / (
                "native_host.exe" if sys.platform == "win32" else "native_host"
            )
            host.write_bytes(b"new bundled host")
            with mock.patch.object(
                setup_runtime, "app_data_dir", return_value=Path(tmp) / "data"
            ), mock.patch.object(
                setup_runtime, "embedded_native_host_archive", return_value=None
            ), mock.patch.object(
                setup_runtime, "embedded_native_host_dir", return_value=source
            ):
                installed = setup_runtime.locate_native_host()
            self.assertEqual(installed.read_bytes(), b"new bundled host")

    def test_system_package_host_is_pinged_from_saved_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            host = base / "system-host"
            host.write_bytes(b"host")
            (base / "setup-state.json").write_text(
                json.dumps({"nativeHostPath": str(host), "mihomoPath": str(host)})
            )
            with mock.patch.object(
                setup_runtime, "native_host_ping", return_value={"protocolVersion": 1}
            ):
                report = setup_runtime.diagnose_system(base)
            check = next(c for c in report["checks"] if c["id"] == "native-host")
            self.assertEqual(check["status"], "ok")

    @unittest.skipIf(sys.platform == "win32", "shell fixture")
    def test_an_arbitrary_executable_is_not_accepted_as_mihomo(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "wrong-tool"
            binary.write_text("#!/bin/sh\necho unrelated tool\n")
            binary.chmod(0o755)
            self.assertIsNone(setup_runtime.probe_mihomo(binary))

    def test_operation_lock_rejects_overlap_and_recovers_after_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data"
            first = setup_runtime.OperationLock(base)
            try:
                with self.assertRaises(setup_runtime.SetupError):
                    setup_runtime.OperationLock(base)
            finally:
                first.close()
            setup_runtime.OperationLock(base).close()

    def test_failed_upgrade_restores_previous_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data"
            base.mkdir()
            settings = base / "settings.json"
            original = '{"mihomoPath":"/previous","custom":true}'
            settings.write_text(original)
            with mock.patch.object(
                setup_runtime, "app_data_dir", return_value=base
            ), mock.patch.object(
                setup_runtime, "_native_messaging_targets", return_value=[]
            ), mock.patch.object(
                setup_runtime, "extension_is_installed", return_value=True
            ), mock.patch.object(
                setup_runtime, "locate_native_host", return_value=base / "host"
            ), mock.patch.object(
                setup_runtime,
                "install_mihomo",
                side_effect=setup_runtime.SetupError("download failed"),
            ):
                code = setup_runtime.main(
                    [
                        "--extension-id",
                        "abcdefghijklmnopabcdefghijklmnop",
                        "--extension-install",
                        "skip",
                        "--non-interactive",
                        "--no-open-browser",
                    ]
                )
            self.assertEqual(code, 1)
            self.assertEqual(settings.read_text(), original)
            journals = list((base / "transactions").glob("*/journal.json"))
            self.assertEqual(
                json.loads(journals[0].read_text())["state"], "rolled-back"
            )


class BrowserRegistrationRegressionTests(unittest.TestCase):
    def test_linux_firefox_uses_the_mozilla_native_manifest_location(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            setup_runtime.sys, "platform", "linux"
        ), mock.patch.object(Path, "home", return_value=Path(tmp)):
            targets = setup_runtime._native_messaging_targets(
                "firefox", Path(tmp) / "data"
            )
            self.assertIn(
                Path(tmp)
                / ".mozilla"
                / "native-messaging-hosts"
                / "io.mhm.browser.json",
                targets,
            )

    def test_upgrade_keeps_releases_referenced_by_another_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data"
            payload = Path(tmp) / "payload"
            payload.mkdir()
            name = "native_host.exe" if sys.platform == "win32" else "native_host"
            (payload / name).write_bytes(b"fixture")

            def targets(browser, base=None):
                return [base / f"io.mhm.browser-{browser}.json"]

            with mock.patch.object(
                setup_runtime, "_native_messaging_targets", side_effect=targets
            ):
                first = setup_runtime.install_user_native_host(payload, base)
                (base / "io.mhm.browser-chrome.json").write_text(
                    json.dumps({"path": str(first)})
                )
                setup_runtime.install_user_native_host(payload, base)
                setup_runtime.install_user_native_host(payload, base)
                setup_runtime.install_user_native_host(payload, base)
            self.assertTrue(
                first.is_file(), "another browser still points at this release"
            )

    def test_windows_registry_default_value_is_restored_after_failure(self):
        registry = mock.MagicMock()
        registry.QueryValueEx.return_value = ("previous-manifest.json", 1)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            setup_runtime.sys, "platform", "win32"
        ), mock.patch.dict(sys.modules, {"winreg": registry}):
            transaction = setup_runtime.SetupTransaction("test", Path(tmp) / "data")
            transaction.snapshot_registry(
                "Software\\Mozilla\\NativeMessagingHosts\\io.mhm.browser"
            )
            transaction.rollback("failed upgrade")
        registry.SetValueEx.assert_called_once()
        self.assertEqual(
            registry.SetValueEx.call_args.args[-2:], (1, "previous-manifest.json")
        )


class InstallerTrustTests(unittest.TestCase):
    def test_release_metadata_uses_system_trust(self):
        import io
        import ssl
        import truststore

        with mock.patch.object(setup_runtime.urllib.request, "urlopen") as open_url:
            open_url.return_value.__enter__.return_value = io.BytesIO(
                b'{"tag_name":"v1.0.0"}'
            )
            self.assertEqual(
                setup_runtime._http_json("https://api.github.com/test")["tag_name"],
                "v1.0.0",
            )
            context = open_url.call_args.kwargs["context"]
            self.assertIsInstance(
                context,
                (
                    ssl.SSLContext
                    if sys.platform.startswith("linux")
                    else truststore.SSLContext
                ),
            )
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(context.check_hostname)

    def test_engine_download_uses_system_trust_and_checks_digest(self):
        import io
        import ssl
        import truststore

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            setup_runtime.urllib.request, "urlopen"
        ) as open_url:
            target = Path(directory) / "engine"
            data = b"verified engine"
            open_url.return_value.__enter__.side_effect = lambda: io.BytesIO(data)
            setup_runtime._download_verified(
                "https://github.com/test", target, hashlib.sha256(data).hexdigest()
            )
            self.assertEqual(target.read_bytes(), data)
            context = open_url.call_args.kwargs["context"]
            self.assertIsInstance(
                context,
                (
                    ssl.SSLContext
                    if sys.platform.startswith("linux")
                    else truststore.SSLContext
                ),
            )
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(context.check_hostname)
            with self.assertRaisesRegex(setup_runtime.SetupError, "SHA-256"):
                setup_runtime._download_verified(
                    "https://github.com/test", target, "0" * 64
                )
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
