"""Watching for volumes coming and going.

The point of absh/mounts.py is that the page stops asking. So the test that
matters is not "does the code run" but "does a real mount actually wake it" -
which is why the Linux case here mounts something for real and waits, rather
than calling the callback itself and checking it was called.
"""
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from absh import mounts as M  # noqa: E402


def can_mount():
    """Whether this machine will let the test mount anything."""
    if not sys.platform.startswith("linux") or not shutil_which("mount"):
        return False
    src = Path("/tmp/absh-mount-probe-src")
    dst = Path("/tmp/absh-mount-probe-dst")
    src.mkdir(exist_ok=True)
    dst.mkdir(exist_ok=True)
    ok = subprocess.run(["mount", "--bind", str(src), str(dst)],
                        capture_output=True).returncode == 0
    if ok:
        subprocess.run(["umount", str(dst)], capture_output=True)
    dst.rmdir()
    return ok


def shutil_which(name):
    from shutil import which
    return which(name)


class Snapshot(unittest.TestCase):
    def test_reports_the_roots_it_would_offer(self):
        """The thing compared is what the user would be offered as a device."""
        base = Path(os.environ.get("TMPDIR", "/tmp")) / "absh-snap"
        one, two = base / "one", base / "two"
        one.mkdir(parents=True, exist_ok=True)
        if two.exists():                 # a previous run, or a previous test
            two.rmdir()
        os.environ["ABSH_DEVICE_ROOTS"] = f"{one}{os.pathsep}{two}"
        try:
            self.assertEqual(M._snapshot(), (str(one),))
            two.mkdir(exist_ok=True)
            self.assertEqual(M._snapshot(), tuple(sorted([str(one), str(two)])))
        finally:
            os.environ.pop("ABSH_DEVICE_ROOTS", None)
            if two.exists():
                two.rmdir()

    def test_says_when_it_is_reduced_to_polling(self):
        # The caller reports this to the extension, so it has to be honest
        # rather than optimistic: Windows has no event worth the ctypes.
        self.assertTrue(M.is_polling("win32"))
        self.assertTrue(M.is_polling("sunos5"))


class Fallback(unittest.TestCase):
    """The timer path, which is what Windows runs."""

    def test_fires_only_when_the_set_changes(self):
        base = Path(os.environ.get("TMPDIR", "/tmp")) / "absh-fallback"
        one, two = base / "one", base / "two"
        one.mkdir(parents=True, exist_ok=True)
        if two.exists():
            two.rmdir()
        os.environ["ABSH_DEVICE_ROOTS"] = f"{one}{os.pathsep}{two}"
        hits, stop = [], threading.Event()
        t = threading.Thread(
            target=M._watch_fallback,
            args=(lambda: hits.append(1), stop), kwargs={"interval": 0.05},
            daemon=True)
        t.start()
        try:
            time.sleep(0.2)
            self.assertEqual(hits, [], "fired without anything changing")
            two.mkdir()
            deadline = time.time() + 3
            while not hits and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(hits, "a new volume did not wake it")
        finally:
            stop.set()
            os.environ.pop("ABSH_DEVICE_ROOTS", None)
            if two.exists():
                two.rmdir()


@unittest.skipUnless(can_mount(), "needs to be able to mount (root, Linux)")
class RealMount(unittest.TestCase):
    """A real mount and a real unmount, through the real kernel event.

    Everything else about this feature can be right while the one thing that
    matters is wrong, and only an actual mount tells you. /mnt is one of the
    directories absh looks in for a player, so mounting there is also what the
    user's automounter effectively does.
    """

    def setUp(self):
        self.src = Path("/tmp/absh-real-src")
        self.dst = Path("/mnt/absh-real-player")
        self.src.mkdir(exist_ok=True)
        self.hits = []
        self.stop = M.watch_in_background(lambda: self.hits.append(time.time()))
        time.sleep(0.4)                  # let the watcher reach the kernel

    def tearDown(self):
        self.stop.set()
        subprocess.run(["umount", str(self.dst)], capture_output=True)
        if self.dst.exists():
            self.dst.rmdir()

    def wait(self, seconds=5):
        deadline = time.time() + seconds
        while not self.hits and time.time() < deadline:
            time.sleep(0.05)
        return bool(self.hits)

    def test_a_mount_wakes_it_and_an_unmount_wakes_it_again(self):
        self.dst.mkdir(exist_ok=True)
        subprocess.run(["mount", "--bind", str(self.src), str(self.dst)],
                       check=True, capture_output=True)
        self.assertTrue(self.wait(), "mounting a volume did not wake the watcher")

        self.hits.clear()
        subprocess.run(["umount", str(self.dst)], check=True, capture_output=True)
        # The automounter removes the directory after unmounting, which is the
        # change that matters and lands after the kernel event - the reason
        # _settle looks a second time.
        self.dst.rmdir()
        self.assertTrue(self.wait(), "unmounting a volume did not wake the watcher")


if __name__ == "__main__":
    unittest.main()
