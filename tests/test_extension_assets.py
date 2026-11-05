import importlib.util
import json
from pathlib import Path
import re
import subprocess
import shutil
import unittest
import zipfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "extension"


class ExtensionContractTests(unittest.TestCase):
    def test_dom_and_locale_references_resolve(self):
        html = (EXT / "popup.html").read_text(encoding="utf-8")
        js = "\n".join(
            path.read_text(encoding="utf-8") for path in [EXT / "popup.js", *(EXT / "lib").glob("*.js")]
        )
        ids = re.findall(r'id="([^"]+)"', html)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertFalse(set(re.findall(r'\$\("([^"]+)"\)', js)) - set(ids))
        keys = set(re.findall(r'data-i18n(?:-[a-z-]+)?="([^"]+)"', html)) | set(
            re.findall(r'\bt\("([^"]+)"', js)
        )
        locales = [
            json.loads((EXT / f"_locales/{locale}/messages.json").read_text(encoding="utf-8"))
            for locale in ("en", "ru", "zh_CN")
        ]
        for locale in locales:
            self.assertFalse(keys - set(locale))
            self.assertEqual(set(locale), set(locales[0]))

    def test_mhm_brand_and_packaged_files(self):
        manifest = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], "0.1.1")
        self.assertEqual(manifest["action"]["default_popup"], "popup.html?popup=1")
        self.assertEqual(
            json.loads((EXT / "_locales/en/messages.json").read_text(encoding="utf-8"))["appName"][
                "message"
            ],
            "mhm",
        )
        self.assertTrue((EXT / "icons/logo.svg").exists())
        for page in ("popup.html", "setup.html"):
            for asset in re.findall(
                r'(?:src|href)="([^"#]+)"', (EXT / page).read_text(encoding="utf-8")
            ):
                self.assertTrue((EXT / asset).exists(), asset)

    def test_browser_packages(self):
        spec = importlib.util.spec_from_file_location(
            "packager", ROOT / "scripts/build_browser_packages.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(set(module.BROWSERS), {"chrome", "edge", "firefox", "opera"})
        with tempfile.TemporaryDirectory() as directory:
            for browser in module.BROWSERS:
                path = module.build(browser, Path(directory), "test")
                with zipfile.ZipFile(path) as archive:
                    manifest = json.loads(archive.read("manifest.json"))
                    self.assertEqual(manifest["version"], module.VERSION)
                    if browser == "firefox":
                        self.assertEqual(
                            manifest["background"],
                            {
                                "scripts": [
                                    "background/identity.js",
                                    "background/native.js",
                                    "background/proxy.js",
                                    "background/action.js",
                                    "service-worker.js",
                                ]
                            },
                        )
                        self.assertEqual(
                            manifest["browser_specific_settings"]["gecko"]["id"],
                            "mhm@allowyoba.github.io",
                        )
                    else:
                        self.assertIn("service_worker", manifest["background"])
                    self.assertNotIn("setup.js", archive.namelist())

    @unittest.skipUnless(shutil.which("node"), "Node required")
    def test_javascript_syntax(self):
        for file in EXT.rglob("*.js"):
            result = subprocess.run(
                ["node", "--check", str(file)], capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
