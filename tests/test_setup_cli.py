import contextlib
import io
import unittest
from unittest import mock
from setup import main, runtime


class CommandLineTests(unittest.TestCase):
    def test_commands_preserve_paths_and_exit_status(self):
        for args, expected in [
            (["install", "--mihomo", "/a path/mihomo"], ["--mihomo", "/a path/mihomo"]),
            (["repair", "--json"], ["--repair", "--json"]),
            (["inspect"], ["--inspect"]),
        ]:
            with self.subTest(args=args), mock.patch.object(
                runtime, "main", return_value=7
            ) as backend:
                self.assertEqual(main.main(args), 7)
                backend.assert_called_once_with([*expected, "--non-interactive"])

    def test_empty_invocation_shows_help_without_installing(self):
        with mock.patch.object(
            runtime, "main", return_value=0
        ) as backend, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main.main([]), 0)
        backend.assert_called_once_with(["--help"])

    def test_removed_interfaces_are_rejected(self):
        for flag in ("--ui", "--wizard", "--backend"):
            with self.subTest(flag=flag), contextlib.redirect_stderr(
                io.StringIO()
            ), self.assertRaises(SystemExit) as error:
                main.main([flag])
            self.assertEqual(error.exception.code, 2)

    def test_version_does_not_invoke_backend(self):
        with mock.patch.object(runtime, "main") as backend, contextlib.redirect_stdout(
            io.StringIO()
        ) as output:
            self.assertEqual(main.main(["--version"]), 0)
        self.assertEqual(output.getvalue().strip(), runtime.PRODUCT_VERSION)
        backend.assert_not_called()
