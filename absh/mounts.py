"""Wait for volumes to be mounted or unmounted, without polling where the OS
will simply tell us.

The extension used to find out that a player had been plugged in only by asking
again on a timer. That is the wrong shape: the answer changes when the user
plugs something in, which the kernel already knows about the moment it happens.
The host holds a long-lived connection to the extension anyway, so it can watch
and push instead of being asked.

Two of the three platforms have a real event for this and need no dependency:

  Linux   /proc/self/mountinfo becomes readable-with-priority whenever the
          mount table changes. poll() for POLLPRI and the kernel wakes us.
  macOS   /Volumes gains and loses a directory entry per volume, so a kqueue
          vnode watch on that directory fires on mount and unmount alike.

Windows has no equivalent that does not involve creating a window and pumping
a message loop for WM_DEVICECHANGE. That is a lot of ctypes for one signal, so
there we compare the drive bitmask instead - one syscall, no allocation, and
nothing crosses into the browser until it actually changes. It is still a poll,
and it is named one.
"""
import os
import select
import sys
import threading
import time

from . import devices

# How often the fallback re-checks. Short enough that plugging a player in feels
# immediate, cheap enough to be uninteresting: a bitmask read, or one listdir
# per root directory.
FALLBACK_INTERVAL = 2.0


def _snapshot(system=None):
    """What is mounted right now, as a comparable value.

    Used by the fallback watcher, and by every watcher as the thing that decides
    whether a wake-up was real - the mount table changes for reasons that have
    nothing to do with removable media (a container layer, a network share),
    and waking the page for those would be no better than the timer this
    replaces.
    """
    try:
        return tuple(sorted(str(p) for p in devices.roots(system)))
    except OSError:
        return ()


def _watch_linux(changed, stop):
    """Block until the mount table changes.

    /proc/self/mountinfo is the documented way to be told: it never becomes
    readable in the ordinary sense, but poll() reports POLLPRI (and POLLERR on
    some kernels) on every change to the table.
    """
    try:
        fd = open("/proc/self/mountinfo", "rb")
    except OSError:
        return False                    # not Linux enough; caller falls back
    with fd:
        poller = select.poll()
        poller.register(fd, select.POLLPRI | select.POLLERR)
        while not stop.is_set():
            # A timeout, so a stopped watcher exits promptly instead of sitting
            # in the kernel until something happens to be mounted.
            if poller.poll(500):
                fd.seek(0)
                fd.read()               # consume, or poll() reports it forever
                _settle(changed, stop)
    return True


def _watch_kqueue(changed, stop, base="/Volumes"):
    """Block until /Volumes gains or loses an entry.

    Mounting a volume creates a directory here and unmounting removes it, so a
    vnode watch on the directory itself covers both without naming any volume.
    """
    if not hasattr(select, "kqueue") or not os.path.isdir(base):
        return False
    fd = os.open(base, os.O_RDONLY)
    try:
        kq = select.kqueue()
        ev = select.kevent(
            fd,
            filter=select.KQ_FILTER_VNODE,
            flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR,
            fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND
                   | select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME,
        )
        while not stop.is_set():
            if kq.control([ev], 1, 0.5):
                _settle(changed, stop)
        kq.close()
    finally:
        os.close(fd)
    return True


def _settle(changed, stop, delay=0.4):
    """Look now, and once more after the dust settles.

    The kernel event and the thing we measure are not the same event. An
    automounter unmounts the filesystem and then removes the directory it was
    mounted at, so a snapshot taken the instant the mount table changes can
    still show the volume - and on the platforms with real events there is no
    timer coming along afterwards to notice. Looking twice costs nothing and
    closes the gap.
    """
    changed()
    if not stop.wait(delay):
        changed()


def _fallback_reader(system=None):
    """What the timer should compare, which is not always the same thing.

    On Windows the cheap answer is the drive bitmask: this runs every couple of
    seconds for the life of the browser session, and listing every removable
    drive that often is not free - it would keep waking a sleeping USB disk.

    But the bitmask only answers the question when the question is about drive
    letters. Point ABSH_DEVICE_ROOTS at a folder and the answer turns into
    "does this path exist", which no drive letter appearing or disappearing
    will ever reflect - so compare the roots themselves instead.
    """
    system = system or sys.platform
    if system.startswith("win") and devices._env_roots() is None:
        return _drive_bitmask
    return _snapshot


def _watch_fallback(changed, stop, interval=None):
    """Compare what is mounted, on a timer. Windows, and anything unusual."""
    interval = interval or FALLBACK_INTERVAL
    read = _fallback_reader()
    last = read()
    while not stop.wait(interval):
        now = read()
        if now != last:
            last = now
            changed()
    return True


def _drive_bitmask():
    """Which drive letters exist, as one integer. One syscall, no I/O."""
    try:
        import ctypes
        return int(ctypes.windll.kernel32.GetLogicalDrives())
    except Exception:
        return _snapshot()


def is_polling(system=None):
    """Whether this platform is on the timer rather than a real event.

    Worth reporting rather than hiding: it is the difference between the page
    reacting the instant a player is plugged in and reacting within a couple of
    seconds, and it is the only thing the caller might want to say out loud.
    """
    system = system or sys.platform
    if devices._env_roots() is not None:
        return True                     # named roots are watched by looking
    if system == "linux":
        return not os.path.exists("/proc/self/mountinfo")
    if system == "darwin":
        return not (hasattr(select, "kqueue") and os.path.isdir("/Volumes"))
    return True


def watch(on_change, stop=None, system=None):
    """Call on_change() whenever the set of mounted volumes changes.

    Blocks. Returns when `stop` is set. on_change is called with no arguments
    and only for changes that alter which volumes devices.roots() would return,
    so a caller can treat every call as worth acting on.
    """
    stop = stop or threading.Event()
    system = system or sys.platform
    last = [_snapshot(system)]

    def changed():
        # The wake-up says something moved, not that it matters. Compare, so a
        # mount the user does not care about does not reach the page.
        now = _snapshot(system)
        if now != last[0]:
            last[0] = now
            on_change()

    # A named root is a question about directories, not about the mount table:
    # ABSH_DEVICE_ROOTS says "the player is here", and that path can appear
    # without anything being mounted. Watch what the answer actually depends on.
    if devices._env_roots() is None:
        if system == "linux" and _watch_linux(changed, stop):
            return
        if system == "darwin" and _watch_kqueue(changed, stop):
            return
    _watch_fallback(changed, stop)


def watch_in_background(on_change, system=None):
    """Same, on a daemon thread. Returns the Event that stops it."""
    stop = threading.Event()
    t = threading.Thread(
        target=watch, args=(on_change,), kwargs={"stop": stop, "system": system},
        name="absh-mounts", daemon=True)
    t.start()
    return stop
