"""Release artefacts.

The store-rejection this guards against is silent and slow: a zip whose
manifest sits one directory down is refused at upload with a message that
never mentions nesting.
"""
import importlib.util, json, shutil, tempfile, unittest, zipfile
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
        for name, p in self.zips.items():
            if f"-{kind}-" in name:
                return p
        self.fail(f"no {kind} archive in {sorted(self.zips)}")


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
            self.assertEqual(json.loads(z.read("manifest.json"))["version"], "1.0.0a1")
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
            self.assertTrue(any("firefox-1.0.0.zip" in n for n in zips), zips)
        finally:
            shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
