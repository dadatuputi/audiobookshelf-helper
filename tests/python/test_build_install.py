"""Tests for the per-browser build and the native-host installer."""
import importlib.util, json, os, platform, shutil, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


BUILD = load(ROOT / "extension" / "build.py", "absh_build")
INSTALL = load(ROOT / "native" / "install.py", "absh_install")


class TestManifests(unittest.TestCase):
    def test_firefox_uses_background_scripts(self):
        m = BUILD.manifest_for("firefox")
        self.assertIn("scripts", m["background"])
        self.assertNotIn("service_worker", m["background"])

    def test_firefox_declares_gecko_id(self):
        m = BUILD.manifest_for("firefox")
        self.assertEqual(m["browser_specific_settings"]["gecko"]["id"], BUILD.GECKO_ID)

    def test_chrome_uses_service_worker_module(self):
        m = BUILD.manifest_for("chrome")
        self.assertEqual(m["background"]["service_worker"], "background.js")
        self.assertEqual(m["background"]["type"], "module")
        self.assertNotIn("scripts", m["background"])

    def test_chrome_omits_gecko_settings(self):
        self.assertNotIn("browser_specific_settings", BUILD.manifest_for("chrome"))

    def test_both_are_manifest_v3(self):
        for t in ("firefox", "chrome"):
            self.assertEqual(BUILD.manifest_for(t)["manifest_version"], 3)

    def test_shared_keys_survive(self):
        for t in ("firefox", "chrome"):
            m = BUILD.manifest_for(t)
            self.assertIn("nativeMessaging", m["permissions"])
            self.assertIn("storage", m["permissions"])
            self.assertTrue(m["content_scripts"][0]["js"])

    def test_unknown_target_rejected(self):
        with self.assertRaises(SystemExit):
            BUILD.manifest_for("safari")


class TestBuildOutput(unittest.TestCase):
    def test_builds_produce_all_files(self):
        for t in ("firefox", "chrome"):
            out = BUILD.build(t)
            self.assertTrue((out / "manifest.json").exists())
            for f in BUILD.SHARED_FILES:
                self.assertTrue((out / f).exists(), f"{t}: missing {f}")

    def test_chrome_background_imports_shim(self):
        out = BUILD.build("chrome")
        self.assertTrue((out / "background.js").read_text()
                        .startswith('import "./browser-polyfill.js";'))

    def test_firefox_background_not_rewritten(self):
        out = BUILD.build("firefox")
        self.assertFalse((out / "background.js").read_text().startswith("import "))

    def test_manifest_is_valid_json(self):
        for t in ("firefox", "chrome"):
            json.loads((BUILD.build(t) / "manifest.json").read_text())


class TestInstallerPaths(unittest.TestCase):
    def test_each_os_browser_combination_resolves(self):
        for system in ("Darwin", "Linux"):
            for browser in ("firefox", "chrome"):
                dirs = INSTALL.manifest_dirs(system, browser)
                self.assertTrue(dirs, f"{system}/{browser}")
                for d in dirs:
                    self.assertTrue(d.is_absolute())

    def test_windows_returns_a_directory(self):
        dirs = INSTALL.manifest_dirs("Windows", "firefox")
        self.assertEqual(len(dirs), 1)

    def test_unsupported_os_raises(self):
        with self.assertRaises(SystemExit):
            INSTALL.manifest_dirs("Plan9", "firefox")

    def test_firefox_and_chrome_paths_differ(self):
        for system in ("Darwin", "Linux"):
            f = INSTALL.manifest_dirs(system, "firefox")
            c = INSTALL.manifest_dirs(system, "chrome")
            self.assertFalse(set(f) & set(c), system)


class TestInstallerManifest(unittest.TestCase):
    def test_firefox_uses_allowed_extensions(self):
        m = INSTALL.build_manifest("firefox", [])
        self.assertEqual(m["allowed_extensions"], [INSTALL.GECKO_ID])
        self.assertNotIn("allowed_origins", m)

    def test_chrome_uses_allowed_origins(self):
        m = INSTALL.build_manifest("chrome", ["aaaabbbbccccddddeeeeffffgggghhhh"])
        self.assertEqual(m["allowed_origins"],
                         ["chrome-extension://aaaabbbbccccddddeeeeffffgggghhhh/"])
        self.assertNotIn("allowed_extensions", m)

    def test_type_is_stdio_and_path_absolute(self):
        for b in ("firefox", "chrome"):
            m = INSTALL.build_manifest(b, ["x" * 32])
            self.assertEqual(m["type"], "stdio")
            self.assertTrue(Path(m["path"]).is_absolute())

    def test_registry_keys_differ_per_browser(self):
        self.assertNotEqual(INSTALL.registry_key("firefox"),
                            INSTALL.registry_key("chrome"))
        self.assertIn("Mozilla", INSTALL.registry_key("firefox"))
        self.assertIn("Chrome", INSTALL.registry_key("chrome"))


class TestInstallDryRun(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            with mock.patch.object(INSTALL, "manifest_dirs", return_value=[tmp]):
                INSTALL.install("firefox", [], dry=True, remove=False)
            self.assertEqual(list(tmp.iterdir()), [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_real_install_then_uninstall(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            with mock.patch.object(INSTALL, "manifest_dirs", return_value=[tmp]), \
                 mock.patch.object(INSTALL.platform, "system", return_value="Linux"):
                INSTALL.install("firefox", [], dry=False, remove=False)
                target = tmp / f"{INSTALL.HOST_NAME}.json"
                self.assertTrue(target.exists())
                data = json.loads(target.read_text())
                self.assertEqual(data["name"], INSTALL.HOST_NAME)
                INSTALL.install("firefox", [], dry=False, remove=True)
                self.assertFalse(target.exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
