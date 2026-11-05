from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'prepare_release.py'
spec = importlib.util.spec_from_file_location('prepare_release', SCRIPT)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def test_collects_platform_packages_and_keeps_apt_separate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'artifacts'
            linux = source / 'mhm-linux-amd64'
            (linux / 'apt' / 'pool').mkdir(parents=True)
            (linux / 'mhm_0.1.0_amd64.deb').write_bytes(b'deb')
            (linux / 'apt' / 'pool' / 'mhm_0.1.0_amd64.deb').write_bytes(b'deb')
            (linux / 'SHA256SUMS').write_text('obsolete')
            output = root / 'dist'
            with patch.object(sys, 'argv', ['prepare_release', str(source), '--output', str(output)]):
                self.assertEqual(release.main(), 0)
            self.assertTrue((output / 'mhm-linux-amd64-apt.tar.gz').is_file())
            sums = (output / 'SHA256SUMS').read_text()
            self.assertIn('mhm_0.1.0_amd64.deb', sums)
            self.assertNotIn('obsolete', sums)

    def test_rejects_conflicting_platform_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name, data in [('one', b'one'), ('two', b'two')]:
                directory = root / 'artifacts' / name
                directory.mkdir(parents=True)
                (directory / 'mhm-setup-file').write_bytes(data)
            with patch.object(sys, 'argv', ['prepare_release', str(root / 'artifacts'), '--output', str(root / 'dist')]):
                with self.assertRaisesRegex(RuntimeError, 'conflicting artifact'):
                    release.main()
