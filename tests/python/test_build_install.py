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

    def test_firefox_declares_no_data_collection(self):
        """AMO is making this key mandatory; the add-on genuinely collects
        nothing, so saying so avoids a consent prompt on the listing."""
        g = BUILD.manifest_for("firefox")["browser_specific_settings"]["gecko"]
        self.assertEqual(g["data_collection_permissions"], {"required": ["none"]})

    def test_firefox_min_version_supports_optional_host_permissions(self):
        """optional_host_permissions landed in 128; an earlier floor lets the
        add-on install into a Firefox where its permission model cannot work."""
        g = BUILD.manifest_for("firefox")["browser_specific_settings"]["gecko"]
        self.assertGreaterEqual(int(g["strict_min_version"].split(".")[0]), 128)

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
            # scripting replaces the declared content_scripts block: the
            # toolbar button is registered at runtime for the user's own
            # server rather than matching every site in the manifest.
            self.assertIn("scripting", m["permissions"])
            self.assertNotIn("content_scripts", m)

    def test_no_host_permissions_are_requested_up_front(self):
        for t in ("firefox", "chrome"):
            m = BUILD.manifest_for(t)
            self.assertNotIn("host_permissions", m)
            self.assertEqual(m["optional_host_permissions"], ["*://*/*"])

    def test_icons_are_declared_and_present(self):
        for t in ("firefox", "chrome"):
            out = BUILD.build(t)
            m = json.loads((out / "manifest.json").read_text())
            for size in ("16", "48", "128"):
                self.assertTrue((out / m["icons"][size]).exists(), f"{t}: icon {size}")

    def test_version_can_be_stamped_from_a_tag(self):
        self.assertEqual(BUILD.manifest_for("firefox", "2.3.4")["version"], "2.3.4")
        self.assertEqual(BUILD.manifest_for("chrome", "2.3.4")["version"], "2.3.4")

    def test_chrome_pins_its_id_with_a_key(self):
        """Without a pinned key an unpacked load gets a fresh id every time,
        which the native host's allowed_origins cannot name in advance."""
        m = BUILD.manifest_for("chrome")
        self.assertTrue(m.get("key"))
        self.assertRegex(BUILD.CHROME_ID, r"^[a-p]{32}$")

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


class TestPathReporting(unittest.TestCase):
    """The build used to print a path relative to extension/, so running it
    from the repo root reported "dist/firefox" for extension/dist/firefox -
    and the obvious next command, `cd dist`, failed."""

    def test_path_is_relative_to_where_you_ran_it(self):
        cwd = Path.cwd()
        shown = BUILD.show(cwd / "extension" / "dist" / "firefox")
        self.assertEqual(Path(shown), Path("extension/dist/firefox"))
        self.assertFalse(Path(shown).is_absolute())

    def test_path_outside_the_cwd_is_printed_absolute(self):
        shown = BUILD.show(Path("/somewhere/else/dist/firefox"))
        self.assertTrue(Path(shown).is_absolute())

    def test_the_shown_path_actually_resolves_to_the_build(self):
        out = BUILD.build("firefox")
        self.assertTrue((Path(BUILD.show(out)) / "manifest.json").exists())


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

    def test_windows_paths_differ_per_browser(self):
        """Sharing one path meant installing both browsers clobbered Firefox."""
        f = INSTALL.manifest_dirs("Windows", "firefox")
        c = INSTALL.manifest_dirs("Windows", "chrome")
        self.assertFalse(set(f) & set(c))

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


class TestWindowsLauncher(unittest.TestCase):
    """Windows browsers cannot exec a .py directly; the manifest needs a .bat."""

    def test_manifest_points_at_a_bat_on_windows(self):
        d = Path("C:/x") if os.name == "nt" else Path("/x")
        self.assertEqual(INSTALL.host_command_path("Windows", d).name,
                         INSTALL.LAUNCHER_NAME)

    def test_manifest_points_at_the_py_elsewhere(self):
        for system in ("Darwin", "Linux"):
            self.assertEqual(INSTALL.host_command_path(system, Path("/x")).suffix, ".py")

    def test_launcher_invokes_the_interpreter_with_the_host(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            body = INSTALL.write_windows_launcher(tmp / "l.bat").read_text()
            self.assertIn(sys.executable, body)
            self.assertIn("absh_host.py", body)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_windows_install_writes_manifest_and_launcher(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            with mock.patch.object(INSTALL, "manifest_dirs", return_value=[tmp]):
                INSTALL.install("firefox", [], dry=False, remove=False, system="Windows")
            self.assertTrue((tmp / f"{INSTALL.HOST_NAME}.json").exists())
            self.assertTrue((tmp / INSTALL.LAUNCHER_NAME).exists())
            m = json.loads((tmp / f"{INSTALL.HOST_NAME}.json").read_text())
            self.assertTrue(m["path"].endswith(INSTALL.LAUNCHER_NAME))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestIdentity(unittest.TestCase):
    """build.py and install.py must never disagree about who the add-on is."""

    def test_gecko_id_is_shared(self):
        self.assertEqual(BUILD.GECKO_ID, INSTALL.GECKO_ID)

    def test_gecko_id_looks_like_an_amo_id(self):
        self.assertRegex(BUILD.GECKO_ID, r"^[^@\s]+@[^@\s]+$")

    def test_firefox_manifest_id_matches_allowed_extensions(self):
        built = BUILD.manifest_for("firefox")["browser_specific_settings"]["gecko"]["id"]
        self.assertEqual(INSTALL.build_manifest("firefox", [])["allowed_extensions"], [built])


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
