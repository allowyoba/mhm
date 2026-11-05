import contextlib
import io
import unittest
from unittest import mock
from scripts import install
from setup import runtime

ID = "abcdefghijklmnopabcdefghijklmnop"


class InstallPreparationTests(unittest.TestCase):
    def test_missing_identity_fails_before_build_or_install(self):
        with mock.patch.object(
            runtime, "detect_installed_extensions", return_value=[]
        ), mock.patch.object(install.subprocess, "run") as build, mock.patch.object(
            install.cli, "main"
        ) as backend, contextlib.redirect_stderr(
            io.StringIO()
        ) as error:
            self.assertEqual(install.main([]), 1)
        build.assert_not_called()
        backend.assert_not_called()
        self.assertIn("chrome://extensions", error.getvalue())
        self.assertIn("make install ARGS=", error.getvalue())

    def test_ready_payload_is_reused_with_detected_id(self):
        with mock.patch.object(
            runtime,
            "detect_installed_extensions",
            return_value=[{"browser": "opera", "extensionId": ID}],
        ), mock.patch.object(
            install, "payload_is_current", return_value=True
        ), mock.patch.object(
            install.subprocess, "run"
        ) as build, mock.patch.object(
            install.cli, "main", return_value=0
        ) as backend:
            self.assertEqual(install.main(["--browser", "opera"]), 0)
        build.assert_not_called()
        backend.assert_called_once_with(
            [
                "install",
                "--browser",
                "opera",
                "--extension-id",
                ID,
                "--extension-install",
                "skip",
            ]
        )

    def test_missing_payload_builds_after_validation(self):
        with mock.patch.object(
            install, "payload_is_current", return_value=False
        ), mock.patch.object(install.subprocess, "run") as build, mock.patch.object(
            install.cli, "main", return_value=0
        ) as backend, contextlib.redirect_stdout(
            io.StringIO()
        ):
            self.assertEqual(install.main(["--extension-id", ID]), 0)
        build.assert_called_once()
        backend.assert_called_once()

    def test_multiple_detected_id_error_is_distinct(self):
        with mock.patch.object(
            runtime,
            "detect_installed_extensions",
            return_value=[
                {"browser": "chrome", "extensionId": ID},
                {"browser": "chrome", "extensionId": "b" * 32},
            ],
        ), self.assertRaisesRegex(runtime.SetupError, "Multiple"):
            runtime.resolve_extension_id(None, None, None, interactive=False)

    def test_invalid_binary_fails_before_build(self):
        with mock.patch.object(
            runtime, "probe_mihomo", return_value=None
        ), mock.patch.object(install.subprocess, "run") as build, mock.patch.object(
            install.cli, "main"
        ) as backend, contextlib.redirect_stderr(
            io.StringIO()
        ):
            self.assertEqual(
                install.main(["--extension-id", ID, "--mihomo", "/missing"]), 1
            )
        build.assert_not_called()
        backend.assert_not_called()
