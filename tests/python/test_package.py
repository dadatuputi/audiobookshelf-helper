"""Release artefacts.

The store-rejection this guards against is silent and slow: a zip whose
manifest sits one directory down is refused at upload with a message that
never mentions nesting.
"""
import importlib.util, json, os, shutil, sys, tempfile, unittest, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("packager", ROOT / "tools" / "package.py")
PK = importlib.util.module_from_spec(spec)
spec.loader.exec_module(PK)


class PackageBase(unittest.TestCase):
    TAG = "v1.0.0-alpha.1"

    @classmethod
    def setUpClass(cls):
        cls.out = Path(tempfile.mkdtemp())
        import subprocess, sys
        subprocess.run([sys.executable, str(ROOT / "tools" / "package.py"),
                        "--tag", cls.TAG, "--out", str(cls.out)], check=True,
                       capture_output=True)
        cls.zips = {p.name: p for p in cls.out.glob("*.zip")}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out, ignore_errors=True)

    def find(self, kind):
        """The archive of one kind, by the exact name the release gives it."""
        name = PK.RV.asset_name("app" if kind == "native" else kind, self.TAG.lstrip("v"))
        if name in self.zips:
            return self.zips[name]
        self.fail(f"no {name} in {sorted(self.zips)}")


class TestNames(unittest.TestCase):
    """What someone choosing a file reads - so it says what each one is for."""

    def test_each_file_is_named_for_what_it_is(self):
        self.assertEqual(
            {k: PK.RV.asset_name(k, "1.0.0") for k in PK.RV.ASSETS},
            {"app": "audiobookshelf-helper-app-1.0.0.zip",
             "chrome": "audiobookshelf-helper-chrome-extension-1.0.0.zip",
             "firefox": "audiobookshelf-helper-firefox-extension-unsigned-1.0.0.zip",
             "source": "audiobookshelf-helper-source-code-1.0.0.zip"})
        self.assertEqual(PK.RV.xpi_name("1.0.0"),
                         "audiobookshelf-helper-firefox-extension-1.0.0.xpi")

    def test_the_extension_links_to_the_helper_by_the_same_name(self):
        lib = (ROOT / "extension" / "src" / "lib.js").read_text()
        self.assertIn("`audiobookshelf-helper-app-${", lib)
        self.assertEqual(PK.RV.asset_name("app", "1.0.0"), "audiobookshelf-helper-app-1.0.0.zip")

    def test_the_updater_finds_it_by_the_same_name(self):
        upd = (ROOT / "absh" / "update.py").read_text()
        self.assertIn(f'"{PK.RV.PROJECT}-{PK.RV.ASSETS["app"]}-"', upd)


class TestBundles(PackageBase):
    def test_all_four_archives_are_produced(self):
        for kind in ("firefox", "chrome", "native", "source"):
            self.assertTrue(self.find(kind).exists())

    def test_manifest_is_at_the_archive_root(self):
        for kind in ("firefox", "chrome"):
            with zipfile.ZipFile(self.find(kind)) as z:
                names = z.namelist()
            self.assertIn("manifest.json", names,
                          f"{kind}: manifest must be at the root, got {names[:5]}")
            self.assertFalse([n for n in names if n.startswith(f"{kind}/")],
                             f"{kind}: bundle must not be nested in a folder")

    def test_bundles_carry_the_code_and_icons(self):
        for kind in ("firefox", "chrome"):
            with zipfile.ZipFile(self.find(kind)) as z:
                names = set(z.namelist())
            for f in ("background.js", "popup.html", "popup.js", "options.html",
                      "lib.js", "config.js", "content.js", "icons/icon-128.png"):
                self.assertIn(f, names, f"{kind}: missing {f}")

    def test_versions_are_stamped_per_browser(self):
        with zipfile.ZipFile(self.find("firefox")) as z:
            self.assertEqual(json.loads(z.read("manifest.json"))["version"], "1.0.0.1")
        with zipfile.ZipFile(self.find("chrome")) as z:
            self.assertEqual(json.loads(z.read("manifest.json"))["version"], "1.0.0.1")

    def test_no_source_tree_leaks_into_a_bundle(self):
        with zipfile.ZipFile(self.find("chrome")) as z:
            names = z.namelist()
        self.assertFalse([n for n in names if "identity.json" in n or n.endswith(".py")],
                         "build inputs must not ship to users")


class TestNativeArchive(PackageBase):
    def test_contains_the_host_the_installer_and_the_engine(self):
        with zipfile.ZipFile(self.find("native")) as z:
            names = set(z.namelist())
        for f in ("absh_host.py", "install.py", "identity.json", "identity.py"):
            self.assertIn(f, names)
        # The host is a shim over the package now; without it the browser
        # launches a helper that cannot import itself.
        for mod in ("absh/host.py", "absh/sync.py", "absh/abs_api.py", "absh/tags.py"):
            self.assertIn(mod, names)

    def test_entry_points_sit_at_the_root_so_install_is_one_command(self):
        with zipfile.ZipFile(self.find("native")) as z:
            top = [n for n in z.namelist() if "/" not in n]
        self.assertIn("install.py", top)
        self.assertIn("absh_host.py", top)

    def test_the_unpacked_archive_can_actually_run_the_host(self):
        """The layout differs from the checkout, which has broken this before."""
        import subprocess, sys, struct, json, tempfile
        out = Path(tempfile.mkdtemp())
        try:
            with zipfile.ZipFile(self.find("native")) as z:
                z.extractall(out)
            body = json.dumps({"cmd": "ping"}).encode()
            p = subprocess.run([sys.executable, str(out / "absh_host.py")],
                               input=struct.pack("<I", len(body)) + body,
                               capture_output=True, timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr.decode()[:400])
            n = struct.unpack("<I", p.stdout[:4])[0]
            self.assertTrue(json.loads(p.stdout[4:4 + n])["ok"])
        finally:
            shutil.rmtree(out, ignore_errors=True)


    def test_the_installer_in_a_download_says_what_to_do_with_a_download(self):
        """It used to print a checkout's instructions - load extension/dist -
        to someone holding a release zip, where no such folder exists."""
        import subprocess, sys, tempfile
        out = Path(tempfile.mkdtemp())
        try:
            with zipfile.ZipFile(self.find("native")) as z:
                z.extractall(out)
            # A dry run: a real one writes to the registry on Windows.
            p = subprocess.run([sys.executable, str(out / "install.py"), "--dry-run"],
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr[:400])
            self.assertNotIn("extension/dist", p.stdout)
            self.assertIn("Quit and reopen your browser", p.stdout)
            semver = self.TAG.lstrip("v")
            self.assertIn(f"audiobookshelf-helper-firefox-extension-{semver}.xpi", p.stdout)
            self.assertIn(f"audiobookshelf-helper-chrome-extension-{semver}.zip", p.stdout)
        finally:
            shutil.rmtree(out, ignore_errors=True)


@unittest.skipIf(sys.platform.startswith("win"), "a real install writes the registry")
class TestReleaseInstallsItself(PackageBase):
    """A release used to register itself wherever it was unzipped. Once that
    folder in Downloads was tidied away, Firefox launched a host that was not
    there and said only "native helper disconnected". It copies itself to a
    home of its own now, so the download can go."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith("XDG_")} | {"HOME": str(self.home)}

    def unzip(self):
        dl = self.tmp / "Downloads" / "app"
        with zipfile.ZipFile(self.find("native")) as z:
            z.extractall(dl)
        return dl

    def run_installer(self, folder, *args):
        import subprocess
        return subprocess.run([sys.executable, str(folder / "install.py"),
                               "--browser", "firefox", *args],
                              env=self.env, capture_output=True, text=True,
                              timeout=60, check=True)

    def manifest(self):
        found = list(self.home.rglob("io.github.abshelper.json"))
        self.assertEqual(len(found), 1, found)
        return json.loads(found[0].read_text())

    def ping(self, launcher):
        import subprocess, struct
        body = json.dumps({"cmd": "ping"}).encode()
        p = subprocess.run([launcher], input=struct.pack("<I", len(body)) + body,
                           capture_output=True, timeout=60,
                           env={"PATH": "/nonexistent", "HOME": str(self.home)})
        self.assertEqual(p.returncode, 0, p.stderr.decode()[:400])
        n = struct.unpack("<I", p.stdout[:4])[0]
        return json.loads(p.stdout[4:4 + n])

    def test_it_still_starts_after_the_download_is_deleted(self):
        dl = self.unzip()
        out = self.run_installer(dl).stdout
        self.assertIn(f"You can delete {dl}", out)
        shutil.rmtree(dl.parent)
        launcher = self.manifest()["path"]
        self.assertNotIn("Downloads", Path(launcher).read_text())
        self.assertTrue(self.ping(launcher)["ok"])

    def test_a_reinstall_replaces_the_copy_rather_than_layering_on_it(self):
        self.run_installer(self.unzip())
        home = next(self.home.rglob("absh_host.py")).parent
        (home / "absh" / "stale_module.py").write_text("raise SystemExit")
        shutil.rmtree(self.tmp / "Downloads")
        self.run_installer(self.unzip())
        self.assertFalse((home / "absh" / "stale_module.py").exists())
        self.assertTrue(self.ping(self.manifest()["path"])["ok"])

    def test_uninstall_takes_the_copy_too(self):
        self.run_installer(self.unzip())
        home = next(self.home.rglob("absh_host.py")).parent
        import subprocess
        subprocess.run([sys.executable, str(home / "install.py"), "--uninstall"],
                       env=self.env, capture_output=True, timeout=60, check=True)
        self.assertFalse(home.exists())
        self.assertEqual(list(self.home.rglob("io.github.abshelper.json")), [])

    def test_a_gone_helper_says_so_where_firefox_shows_it(self):
        self.run_installer(self.unzip())
        launcher = self.manifest()["path"]
        shutil.rmtree(next(self.home.rglob("absh_host.py")).parent)
        import subprocess
        p = subprocess.run([launcher], input=b"", capture_output=True, timeout=30)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn(b"run install.py again", p.stderr)


class TestManifest(PackageBase):
    """SHA256SUMS is what the maintainer signs and `absh update` checks."""

    def test_lists_every_archive_under_the_tag_it_was_built_for(self):
        import hashlib, sys
        sys.path.insert(0, str(ROOT))
        from absh import signing
        tag, digests = signing.parse_manifest((self.out / "SHA256SUMS").read_bytes())
        self.assertEqual(tag, self.TAG)
        self.assertEqual(sorted(digests), sorted(self.zips))
        for name, p in self.zips.items():
            self.assertEqual(digests[name], hashlib.sha256(p.read_bytes()).hexdigest())

    def test_the_native_archive_carries_the_verifier_and_the_trust_list(self):
        # Without these an installed copy cannot check the update after it.
        with zipfile.ZipFile(self.find("native")) as z:
            names = set(z.namelist())
        for f in ("absh/ed25519.py", "absh/signing.py", "absh/release_keys.py"):
            self.assertIn(f, names)


class TestSourceArchive(PackageBase):
    def test_carries_the_real_source(self):
        with zipfile.ZipFile(self.find("source")) as z:
            names = set(z.namelist())
        for f in ("extension/src/background.js", "native/absh_host.py",
                  "extension/build.py", "README.md", "package.json"):
            self.assertIn(f, names)

    def test_excludes_build_output_and_dependencies(self):
        with zipfile.ZipFile(self.find("source")) as z:
            names = z.namelist()
        for junk in ("node_modules", "__pycache__", "extension/dist", "release/"):
            self.assertFalse([n for n in names if junk in n], f"leaked {junk}")


class TestReleaseInfo(PackageBase):
    def test_info_is_written_for_the_workflow_to_read(self):
        info = json.loads((self.out / "release-info.json").read_text())
        self.assertTrue(info["prerelease"])
        self.assertEqual(info["semver"], "1.0.0-alpha.1")


class TestLocalDefault(unittest.TestCase):
    """A local build should not require inventing a version number."""

    def test_no_tag_produces_a_placeholder_build(self):
        out = Path(tempfile.mkdtemp())
        try:
            import subprocess, sys
            subprocess.run([sys.executable, str(ROOT / "tools" / "package.py"),
                            "--out", str(out)], check=True, capture_output=True)
            info = json.loads((out / "release-info.json").read_text())
            self.assertEqual(info["semver"], "0.0.0")
            self.assertFalse(info["prerelease"])
            self.assertEqual(len(list(out.glob("*.zip"))), 4)
        finally:
            shutil.rmtree(out, ignore_errors=True)


class TestStableTag(unittest.TestCase):
    def test_a_stable_tag_is_not_marked_prerelease(self):
        out = Path(tempfile.mkdtemp())
        try:
            import subprocess, sys
            subprocess.run([sys.executable, str(ROOT / "tools" / "package.py"),
                            "--tag", "v1.0.0", "--out", str(out)], check=True,
                           capture_output=True)
            info = json.loads((out / "release-info.json").read_text())
            self.assertFalse(info["prerelease"])
            zips = {p.name for p in out.glob("*.zip")}
            self.assertIn("audiobookshelf-helper-firefox-extension-unsigned-1.0.0.zip", zips)
        finally:
            shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
