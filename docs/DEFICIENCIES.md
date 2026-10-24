# Deficiencies and todo

What is known to be wrong, missing, or untested, and why each one matters.
Kept in the repo rather than in an issue tracker so it travels with the code
and can be wrong in public. Every claim here was checked against the code or
a real run on the date noted; where something is believed rather than
verified, it says so.

Last reviewed: 2026-10-14, after `v1.0.0-alpha.7`. Status lines record what
changed since an entry was written, with the commit or release that did it.
CI runs the helper's suite on Windows, macOS and Linux under Python 3.9, 3.11
and 3.13, and the real-server suite in Chromium and Firefox against both ends
of the supported Audiobookshelf range (N12).

---

## Does this need a native helper at all?

Researched 2026-10-03, because the helper is the single biggest cost this
project imposes on a user: a Python install, a manifest in the right
per-browser directory, and a thing to keep updated.

**Conclusion: the helper cannot be removed. On Chromium it could become
optional; on Firefox there is no route at all, by Mozilla's deliberate
policy.**

What the helper actually does splits in two. Talking to Audiobookshelf over
HTTP is not privileged — the in-page UI already runs on the server's own
origin and could do all of it. The privileged half is the filesystem: list
removable volumes, read and write arbitrary paths under them, delete, and
notice volumes appearing and disappearing.

Measured in Chromium 141 by loading a probe extension and calling the API
in each context that matters:

| Context | `showDirectoryPicker` | Result |
|---|---|---|
| Extension options page | `function` | Native dialog opens |
| Extension popup document | `function` | Native dialog opens |
| MV3 service worker | `undefined` | No window, so no picker |
| Page on a secure origin | `function` | Native dialog opens |
| MAIN-world content script | `function` | It is the page's own `window` |

A `FileSystemDirectoryHandle` is structured-cloneable into IndexedDB, so a
grant can be remembered across sessions (checked with an OPFS handle as a
stand-in, since a headless browser cannot drive the native dialog).

So on Chromium a user could point the extension at the mounted player once
and never install Python. What that loses:

- **Firefox entirely.** Firefox has never shipped `showDirectoryPicker`,
  `showOpenFilePicker` or `showSaveFilePicker`, and Mozilla's standards
  position on the local-disk pickers is negative — they consider handing a
  web origin write access to arbitrary directories harmful. This is a
  decision, not a backlog item, so waiting for it is not a plan. Firefox
  extensions have no filesystem API either: `nativeMessaging` is the
  sanctioned mechanism, which is exactly what this project uses.
- **Finding the device.** `devices.py` lists and scores removable volumes so
  the user picks from a list. The web API has no equivalent — the user
  navigates a native dialog to the mount point themselves, and has to
  restore access whenever the grant lapses - which, measured in Chromium
  141, is on every restart and within about 1.5 seconds of the extension's
  last tab closing, unless the user chose "Allow on every visit".
- **Mount and unmount events.** There is no web API for this. The work in
  `absh/mounts.py` (POLLPRI on `/proc/self/mountinfo`, kqueue on
  `/Volumes`) has no browser counterpart, so the page would be back to
  asking on a timer — the exact thing that work removed.

Dead ends, so nobody re-investigates them:

- **WebUSB.** USB interface class `0x08` (mass storage) is on Chromium's
  protected-interface-class blocklist and cannot be claimed by a web origin
  at all. Firefox and Safari never shipped WebUSB. An MTP player is
  theoretically reachable (imaging class `0x06` is not blocked), but that
  means writing an MTP implementation against a device the OS has usually
  already claimed, for one browser.
- **WASM.** Irrelevant to this problem. WASM is a compute target; it has no
  more filesystem reach than the JS that hosts it. A FAT driver compiled to
  WASM still needs block-device access no browser grants.
- **Origin Private File System.** Firefox does ship OPFS, and it is useless
  here: it is a sandboxed filesystem private to the origin, not the USB
  device.

The reasonable version of this idea is therefore **a Chromium-only path that
skips the helper for the common case**, kept alongside it rather than
replacing it — and it is a second filesystem backend plus a permission
lifecycle, not a simplification. It is listed under todo as T7, deliberately
low.

---

## Deficiencies

### D1. `absh update` verifies integrity, not provenance — NARROWED, by choice
The digest came from GitHub, computed over the bytes GitHub was given, so
anyone who could publish a release published a matching digest.

**Status:** `a891beb` added signing. Releases carry a `SHA256SUMS` manifest
that binds the tag, and the updater checks its Ed25519 signature against keys
pinned in the *installed* copy (read with `ast`, never imported) before
downloading or swapping anything; unsigned releases are refused with no
override. The verifier is pure Python and accepts 60/60 OpenSSL-made
signatures and rejects 180/180 tampered ones in an independent check.

The maintainer chose to have CI hold the key, as the `RELEASE_SIGNING_KEY`
secret, so releasing needs no manual step. What that means plainly: a
signature proves a release came out of this repository's release workflow,
so an asset swapped in afterwards or a build from anywhere else is refused.
It does not hold against someone who can run workflows here or controls the
maintainer's GitHub account; they can read the secret and sign. Closing that
needs a key held off GitHub, which `tools/sign_release.py keygen` / `sign`
still support and which can be pinned beside the CI key.

### D2. A stale add-on is published under the wrong version — FIXED
**Status:** the maintainer deleted `cb73684229b84553a7b8-1.0.0.1.xpi` from
`v1.0.0-alpha.1`. `ca67dbf` stops a re-cut tag leaving an old xpi behind,
and `83edd53`'s release check would now flag one.

### D3. Windows device detection is a 2-second poll — MITIGATED
Still a poll; there is no event worth the ctypes. **Status:** `0138d94`
says so on the options page, only when the helper reports it is polling.

### D4. A test credential is in public history — ACCEPTED
`tests/real/state.json` (a JWT for a throwaway CI server, plus local
scratch paths) is in `274935f` and `d414b8a`, and has been gitignored since
`7bdc97a`. The server it authenticates to only ever existed inside CI, so
the maintainer chose not to rewrite history for it.

### D5. The Chrome Web Store path has never run — FIXED, unproven live
**Status:** `8b37a49`. Testing it found four bugs: publish dropped its body,
returned success on `ITEM_TAKEN_DOWN`, let a network error escape as a
traceback, and treated partial secrets as "not configured". It also spoke
the v1.1 API, which Google supports only until **15 Oct 2026**; it now uses
v2. 28 tests against a local stand-in. **Needs M2 before the first stable
tag.**

### D6. AMO's listed channel has never run — FIXED, unproven live
**Status:** `8ce0285`. The listed branch could never be reached: web-ext
waits up to 15 minutes for a signed file (`approvalCheckTimeout = 900000`
in its source), and a listed version is not signed until it is reviewed.
Listed submissions now return at submission. Every branch of the job's own
shell is tested, including against the real web-ext.

### D7. Nothing surfaces an available update — FIXED
**Status:** `0138d94`. See T1/T2.

---

## Found while fixing the above

### N1. `absh update` without `--tag` has never worked — FIXED
GitHub's `/releases/latest` omits prereleases, and every release so far is
one, so it returned 404. **Status:** `0138d94` picks the newest release by
semver through `absh/releases.py`, the same rule the page uses.

### N2. The options page threw away a saved folder and template — FIXED
A regression from `0b20110`: `load()` filled only empty fields, and those two
carry defaults in the markup, so a saved value was never shown and the next
Save wrote the default back. **Status:** `3dc138a`, with a test that fails
against the old code.

### N3. A helper swap could run stale bytecode, and lost execute bits — FIXED
**Status:** `0138d94`. `__pycache__` is cleared wherever a file lands; only
the execute bits an archive records are restored, on POSIX.

### N4. The privacy policy misdescribed the API key — FIXED (`c9ffff0`)
It said the key goes in download URLs and that nothing is written to the
server. Neither was true: every request authenticates with a header, and
uploads write to the server on request. The unused function that could
build a key-bearing URL is gone, and a test holds the code to the policy.

### N5. Two editions of one book overwrote each other on the player — FIXED
A full-cast recording and a single-narrator one share an author and a title,
so the folder template gave them one name. Copying the second replaced a
single-file first, merged into a folder first, or - at the same size - was
skipped yet recorded as copied; the index then credited the name to the
second, and the first showed as not on the player. Found on 2026-10-13 from
a question about the full-cast Harry Potter, not from a report.
**Status:** fixed after `v1.0.0-alpha.5`, in the helper and the Chrome folder
backend alike. A name another library item holds (per the index, and only
while its book is still on the player) is never reused: the second edition
gets its narrator added, or a number, compared ignoring case as FAT32 and
exFAT do. Something on the player this tool did not put there keeps the old
behaviour, since it is most likely the same book copied by hand. Templates
gain `{narrator}`, and the popup shows "read by" on each row. Five copy
scenarios are pinned in the shared vectors, both languages are held to them,
and `test_parity_vectors` asserts the outcome rather than only the agreement.
**Not handled:** a player already damaged by the old behaviour; remove the
affected book and copy both editions again.

### N6. Reported on alpha.5 (2026-10-13), from a fresh Firefox profile
1. **Settings had no visible way in.** Only the Library card opened them, and
   nothing said so. Now a gear on that card (44px target, `,` key).
2. **Dead controls with no helper.** Tabs, filter and "Select books" showed
   though none could act; the Player card read "…" for good. Now a setup card
   replaces them: "Install the helper app", three steps, and a button that
   downloads the helper app from the release this extension was built for
   (`config.js` carries the tag). The same card covers an unset server and a
   Chrome folder that is missing or paused.
3. **Firefox never said why the helper failed.** The reason is on the port
   (`port.error`), and only `runtime.lastError` was read, which Chrome alone
   sets, so Firefox always got "native helper disconnected". Both are read
   now, and "never installed" is told apart from "installed but failing".
4. **Release files did not say what they were for.** "native" meant nothing
   to a user. Now `-app-` (the helper), `-firefox-extension-` (.xpi),
   `-chrome-extension-`, `-firefox-extension-unsigned-`, `-source-code-`, named
   once in `tools/release_version.py`, and the release notes open with a
   which-file table. Helpers from before alpha.6 look for `-native-` and so
   cannot update themselves to it; reinstall the helper app by hand once.
   Accepted by the maintainer: nobody runs the old versions.
Also: "Options" is "Settings" everywhere, matching the page.
**Status:** fixed after `v1.0.0-alpha.5`.

### N7. "Checking the helper…" and "Looking…" with no end (alpha.5, macOS, Firefox)
Seen: with the helper installed, the popup sat at "Checking the helper…"
while the player list said "Looking…", indefinitely. Not reproduced here (no
Mac). **Cause, believed not confirmed:** the helper ran every request in turn
on one loop, and the device list searched each volume under /Volumes for audio
with no bound - a network share or backup disk there takes minutes, or hangs
on a dead mount - so the list, and every request queued behind it, waited.
Separately, the extension had no limit on how long it waited for a ping.
**Guards, added anyway:** requests are read on their own thread, and ping and
devices answer at once rather than in turn (except behind a queued update, so
a replaced helper never answers with its old code); the audio search stops at
2000 entries or 3 folders deep; each volume gets 4 seconds in all and one that
has not answered is listed as "Not answering"; the extension gives a ping 15
seconds, then drops the connection so the next try starts a fresh helper, and
says "Helper not answering" with Look again. Tests: a server that never
answers holds `status`, and ping and devices still come back (fails against
the old loop); a big volume and a dead one are bounded.
**Measured on the Mac (2026-10-13):** a network share was mounted under
`/Volumes` when the popup hung. When measured later it was gone: `/Volumes`
held `Macintosh HD`, `MUSICSD` and `SANSA CLIPZ`, and
`python3 -m absh.cli devices` from Terminal was not slow. That fits the cause
above: the unbounded audio search ran over the share. The guards bound it.
Network shares are now left out of the list altogether, since a player is
never one (maintainer's decision): by filesystem type from `mount` on macOS
and `/proc/self/mountinfo` on Linux, before anything inside is touched.
Windows already listed only removable drives, but asked whether each letter
existed first, which can block on a disconnected network drive; it now asks
the type first. A second possible cause stays open:
the macOS prompt "Firefox would like to access files on a removable volume",
which blocks the helper's first touch of a USB volume until answered. It
cannot be prevented from here, but it is no longer silent: on a Mac, when
Detect finds a player that does not answer, the popup says to look for that
prompt, click Allow, and detect again (`ABSH.slowPlayerHint`).

### N8. Reported on alpha.6 (2026-10-13): telling versions of a book apart
1. **Not enough detail per book.** Seen: filtering "Harry Potter" shows seven
   rows whose titles and narrator lists are cut off ("Harry Potter and the
   Deathly Hallows,…", "read by Hugh Laurie, Matthe…"), with several versions
   of the same books in the library. Expected: a way to see the whole title,
   every narrator, and what tells versions apart (length, series number,
   year). Where: `extension/src/popup.js` `rowsFor`, and the item fields in
   `absh/abs_api.py` `normalize_item` (no duration, subtitle or year yet).
   **DECIDED 1A** (maintainer, 2026-10-13): the second line is what tells
   versions apart, narrator then length; a 44px chevron opens the rest
   (title, every narrator, series and number, length and files, year) under
   the row. Rejected: grouping versions (1B), wrapping every row (1C).
   **Status:** shipped in `v1.0.0-alpha.7`.
2. **The tab count ignores the filter.** "To player 345" over seven filtered
   rows. A count must agree with what is on screen: "7 of 345" while a filter
   is on, from the same predicate as the list. Where: `popup.js` `render`.
   **Status:** shipped in `v1.0.0-alpha.7`.

### N9. Reported on alpha.6 (2026-10-13): a copy disappears from view
Seen: during a copy, reopening the popup shows "Checking the helper…" over an
empty shelf, as if stalled. Expected: the copy's state from start to finish -
what is copying, how far along, done or failed - whenever the popup opens,
and an alert when it finishes. **Cause:** progress goes only to the popup
that started the copy, and is lost when it closes; a new popup asks the
helper for the shelf, which waits behind the copy, while the status line
still says "Checking the helper…" though the helper has answered. Where:
`popup.js` (progress held in the page), `background.js` `route` (no record
of a running job), `absh/host.py` (status queued behind pull).
**DECIDED 2A, 3a** (maintainer, 2026-10-13): the copy belongs to the
extension's background, not a popup. Each row being copied says so (copied,
a bar, waiting) and the button becomes the overall progress; the shelf stays
usable and more books join the queue. Finished: the footer says what was
copied; the toolbar icon carries a count while copying and a check when done;
a system notification says it finished (needs the notifications permission).
Rejected: a separate copy card (2B). **Status:** shipped in `v1.0.0-alpha.7`, with
one deviation: a book that is one file reports no progress until it lands,
so its bar is indeterminate rather than a percentage.

### N10. Reported on alpha.6 (2026-10-13): nothing shows in Audiobookshelf
Seen: no change to the Audiobookshelf pages; the maintainer wants to browse
there and send books to the player. What exists: on `<server>/library/*`
only, a badge on each book card and a "Sync to device" toolbar button, and
only once Access is granted in Settings. Nothing on a book's own page
(`/item/<id>`). The maintainer sees no buttons at all, so injection itself is
in question as well as the missing page button. **DECIDED 4A** (maintainer,
2026-10-13): a "Copy to <player>" button beside Play on a book's page with
the player's state under it, and on every cover a full-width strip saying
on the player, copying, or Copy to player, the whole strip the target.
Rejected: the book page only (4B). **Cause** of nothing showing, found
against a real v2.37.1: the page script was registered for
`<server>/library/*`, and the app reaches the library and every book by
in-app navigation, which never matches a registration; only a full load of a
`/library/` address did. Two more, found once it ran: a cover's strip was
attached to the card, which includes the title under it, and the book page
followed the first green Play in the document, which is a hidden one in the
select-many bar. **Status:** shipped in `v1.0.0-alpha.7`. Registered for the whole
origin; covers carry their book's id from the app's own state, so two
versions of one title are told apart; the strip sits on the cover; the page
follows the Play that is drawn. The real-server suite checks each of these.
After alpha.7 (maintainer, 2026-10-14): **Remove** on a book's page asks
once more, in place ("Yes, remove" / "Keep", lapsing after six seconds),
since it deletes from the player in one click; and the old "Sync to device"
button in Audiobookshelf's toolbar now says how the player stands ("SANSA ·
3 of 28 on it", or "Player not connected") and still opens the picker. Not
yet confirmed on the maintainer's own server.

### N11. The upstream watch never reported a release — FIXED
Audiobookshelf shipped v2.36.1, v2.37.0 and v2.37.1 while the tests stayed on
v2.36.0, with no issue opened. The watch kept its own "last seen" record and
deliberately said nothing on its first run; the workflow saved that record
only when there was news. So it never saved one, and every weekly run was a
first run. **Status:** the pinned version (`tests/real/abs-version.txt`, since N12 the newest in
`extension/abs-versions.json`) is
now the only record: anything newer is news. Daily, `upstream.yml` runs the
full real-server suite (Chromium and Firefox) against the newest release, via
the same `real-abs.yml` CI uses; if it passes it moves the pin and starts CI,
and if it fails it opens an issue with the failing run and the changelog
lines that touch what this extension uses. The pin moved to v2.37.1 by hand.

---

### N12. Support was "whatever the latest passes" — DECIDED
The suite ran against one pinned release, and the watch moved it forward, so
what the extension supported was never stated and an older server was never
tested. The tests also connected with the browser's session token, while
every user pastes an API key. **DECIDED** (maintainer, 2026-10-13): a stated
range, tested at both ends. `extension/abs-versions.json` names the oldest
and newest; CI runs the real-server suite against both, in Chromium and
Firefox. The oldest is **v2.26.0**, the first release with API keys, and moves
only by decision; Settings names it when a server is older (`/status` gives
the version without signing in). The newest moves when a new release passes
(N11). The tests now create an API key and use it everywhere. Rejected:
testing every release on every push, since breaks come at the ends and each
release between was tested as it shipped.

### N13. The API contract described code that no longer existed — FIXED
`tests/fixtures/abs/contract.json` said downloads send the API key as
`?token=`, and the README explained "why the token goes in the URL"; the
code sends it only as a header, as the privacy policy says. Both, and the
upstream report, named `tests/js/contract.test.js`, which never existed. The
contract now lists every endpoint in use, the fields alpha.7 added, and the
parts of Audiobookshelf's pages the strips and buttons rely on; it points at
`TestContract` in `tests/python/test_absh.py`, which checks the new fields
and that no endpoint carries a key in its URL.

### N14. The helper ran from wherever it was unzipped (alpha.8, macOS) — FIXED
Seen: Settings at "Helper · Not starting", "native helper disconnected",
"regularly". The Browser Console had the cause: the launcher ran
`~/Downloads/audiobookshelf-helper-app-1.0.0-alpha.6/absh_host.py`, a folder
since deleted. `install.py` registered the folder it was run from, so tidying
Downloads broke the helper, and an update replaced files in Downloads too.
**Status:** fixed after `v1.0.0-alpha.8`. A release copies itself to
`app_home()` (Application Support on a Mac, `~/.local/share` on Linux,
`%LOCALAPPDATA%` on Windows), swapping a whole fresh copy in on a reinstall,
and registers that; it says the download can be deleted, and `--uninstall`
removes the copy. A checkout still runs in place. The launcher falls back to
`/usr/bin/python3` if the recorded interpreter goes, and says on stderr if the
helper's files are missing. A helper that quits before replying is now named
as such in Settings and the popup ("quit as soon as it started … install it
again"), instead of "native helper disconnected" or Chrome's "Native host has
exited.". Tests install an unpacked release into a scratch home, delete the
download, and ping through the launcher. An existing install stays where it
is until `install.py` is run again from a new download.
Windows got the same guards in its `.bat`: `py -3` if the recorded Python is
gone, and a line on stderr if the helper is. It is written as exact UTF-8
bytes with `chcp 65001` first (written as text, every line ended `\r\r\n`,
and a non-ASCII user name was garbled), and with labels rather than `( )`
blocks, which a ")" in a path would close early. A reinstall that cannot
rename the old folder - Windows refuses while a file in it is open - copies
over it instead. `TestPlacedOnEveryOS` runs the copy and the launcher on the
Windows, macOS and Linux runners alike, without touching the registry.

### N15. Snap and Flatpak browsers on Linux were never considered — ADDRESSED
A sandboxed browser cannot start the helper itself. **Snap Firefox**
(Ubuntu's default) asks Ubuntu's WebExtensions desktop portal, which reads
the manifest install.py already writes to `~/.mozilla/native-messaging-hosts`,
asks the user once, and starts the helper outside the sandbox. **Flatpak
Firefox** has that portal only where the distribution carries Ubuntu's patch,
which most do not; the known workaround lets Firefox run anything on the
host, so it is described, not set up (maintainer, 2026-10-23). **Sandboxed
Chromium** cannot start the helper at all; the helper-free folder mode is
the answer. **Status:** install.py recognises each from what is on disk,
checks a Snap's portal for WebExtensions support over D-Bus, and says what
to expect, including the `gdbus` command that undoes "Don't allow". On Linux,
Settings and the popup link the README's "Sandboxed browsers on Linux" from
their install steps. The location rule the portal states - the helper out
of the confined browser's reach - holds since N14 put it under a hidden
folder, which a Snap cannot write. `sandboxed-firefox.yml` installs the Snap and a
packaged helper on Ubuntu 24.04, allows it with the printed command, and
drives Settings until the Helper row shows that copy answering through the
portal; weekly, and on changes to what it covers, outside ci.yml. It also
requires Firefox's own log of the portal call. **Proven** on `ac0e0db`
(Ubuntu 24.04, xdg-desktop-portal 1.18.4-1ubuntu2.24.04.3): the portal found
the manifest, started `io.github.abshelper` for this add-on, and Settings
showed the installed 1.0.0-rc.99 answering. Getting there on a runner took
handing the virtual display to the user session (the portal's GTK backend
died without it), `devtools.chrome.enabled` for the debugger, and a file
rather than a data: address to mark the tab. Flatpak remains untested end
to end: there is no supported path to test. *(Superseded below: the same
job now covers Flatpak too.)* Flatpak, added after: the same workflow runs
Flathub's Firefox twice - with Ubuntu's portal, expecting the installed helper
to answer through it, and with the portal switched off, as on most systems a
Flatpak runs on, expecting Settings to say "Not installed" and link the
README. Writing it found that the "undo Don't allow" command named the Snap
alone; the portal keys the choice by app, so a Flatpak gets one naming
`org.mozilla.firefox`. Its first run found more: Flathub's Firefox logs
"NativeMessagingPortal will not be used" - it leaves
`widget.use-xdg-desktop-portal.native-messaging` off, where Ubuntu's Snap
turns it on - so even with the portal it could not reach the helper.
install.py now tells a Flatpak user on a system with the portal to set it
to 1 in about:config (which, unlike the flatpak-spawn workaround, opens
nothing else), then choose Allow. The job runs the Flatpak as installed
(expects "Not installed"), with that setting (expects the helper answering
through the portal), and with no portal (expects "Not installed").

### N16. Reported on alpha.10 (2026-10-23): copies count wrong, and do not survive an unplug
1. **"Copying 1 of 34", then "1 of 33".** Each cover clicked queues a job
   of one book; the footer counted only what was running and waiting, so a
   finished book left the count. **Fixed:** the background keeps a run -
   everything queued from idle until idle again - and counts and sums it up
   as one. Also found: the helper reports books in library order and the
   background matched them by position, so a book's progress could carry
   another's title; matched by id or name now.
2. **Unplugging mid-copy.** A file was written under its real name as it
   went, so a cut-off copy stayed as half a book that the next copy skipped;
   the helper made the player's missing folders again (on a Mac, a
   /Volumes/<player> folder on the main disk); and every book left failed in
   turn. **Fixed:** each file is written under a hidden `.absh-part-` name
   and renamed into place, so one that exists is whole and a cut-off
   multi-file book carries on at its next file; a vanished player stops the
   run with what is left in `interrupted`, failing nothing, and nothing is
   recreated. Chrome's folder mode does the same (its writes were already
   whole). The background pauses ("Paused · <player> disconnected"),
   resumes when the player answers again - on a device event, every 15s, or
   when a page asks after the shelf - keeps the queue in storage so a
   browser restart resumes it, and offers "Cancel the rest". A helper that
   stops pauses the run the same way. Tests: unplugging during a download in
   Python, folder mode and a real browser; a browser quit and relaunched on
   the same profile mid-pause.
3. **One click for a series or author.** **DECIDED** (maintainer): on
   Audiobookshelf's own series, author, collection and playlist pages,
   in the page's own button row (mockup option A). **Done:** the helper's
   new `group` command lists what the page lists, asked as the page asks
   (a series filters its library by base64 id; an author comes with their
   books; a collection and a playlist list theirs, playlist podcast
   episodes left out), and Chrome's folder mode asks the same. The button
   copies what is not on the player yet, asks again in place above ten
   books, and says "Needs X; <player> has Y free" rather than starting a
   copy that cannot fit. Tests: the endpoints in Python and folder mode;
   the confirm and too-big states on a stub page; a real Audiobookshelf
   with a series, an author and a collection, checked against the files
   that land on the player. The endpoints are in the API contract.

## Todo

### T1. Consume ping's `release` and offer the update — DONE (`0138d94`)
### T2. Make `absh update` reachable without the CLI — DONE (`0138d94`)
The options page shows the installed version, the newer release, and why a
copy cannot update itself when it can't; the Update button installs the
release it showed and picks up the new helper without a browser restart.
Every check goes through the helper, so no new permission.

### T3. Test that a published release matches its tag — DONE (`83edd53`)
A `verify-release` job checks the asset set, digests against what CI built,
stale assets, the native stamp, and opens the xpi to check AMO's signature,
add-on id and version. Run by hand against the live `v1.0.0-alpha.3`: it
passes, and it rejects a changed byte, another tag's build, and broken
job outputs. First ran in Actions on `v1.0.0-alpha.4`, and passed.

### T4. Make the update self-check prove more than "it starts" — DONE (`af77013`)
The new helper must also list devices, against a stand-in folder so it
reads nothing real and gives the same answer with nothing mounted.

### T5. Sign releases — DONE, with a CI-held key by choice (`a891beb`)
See D1.

### T6. Drive the Firefox options page in a real browser — DONE (`86f76bb`)
Over Firefox's remote debugging protocol. Proven in CI: all five tests ran
and passed in a real Firefox. The Grant button still needs a trusted user
gesture, so permission granting is covered only in Chromium.

### T7. Optional helper-free mode on Chromium — DONE (`6e80c35`)
Chrome users can pick the player's folder instead of installing the helper.
The helper still wins whenever it answers; Firefox's bundle carries none of
it and no manifest gained a permission. Every helper operation a browser can
do is ported (status, copy, remove, upload, and the MP4/ID3 tag readers),
held to the helper's exact results by shared test vectors that both the
Python and JS suites check. The cost is the grant lifecycle above: without
"Allow on every visit", the options tab has to stay open while copying.
**Unverified:** a real removable disk, Chrome's real permission prompts
(including whether "Allow on every visit" is offered to an extension), real
Chrome rather than Chromium, and Windows/macOS.

### T8. Surface that Windows is polling — DONE (`0138d94`)

---


## Needs the maintainer

### M1. Set up the signing key — DONE
The `RELEASE_SIGNING_KEY` secret is set and its public half is pinned
(`7178cc7`, key id `f8c85757eb1dbc99`). `v1.0.0-alpha.4` was the first release
signed by CI; `tools/sign_release.py verify` against the published release
confirms an installed helper will accept it.

### M2. Before the first stable tag
Add the `CWS_PUBLISHER_ID` secret; create the Chrome Web Store item by hand
(the v2 API cannot create items); run a dispatched dry run, which signs in
and reads the item without publishing.

### M3. The `claude/plugin-release-pipeline-kzlr09` branch
Claude's sessions are told to push there, so it is kept level with `main`
(every push goes to both) rather than deleted. Nothing under `.claude/` has
ever been committed on any ref, and `.claude/` is ignored since `22711c5`.

---

## Still unverified

- Chrome Web Store v2: the exact upload-state spellings and error shapes;
  Google's reference pages were unreachable when this was written.
- Whether AMO's first listed submission needs listing details (summary,
  categories, licence), and whether its refusal of a deleted version number
  says "already exists".
- The "Pin release-signing key" workflow has run only in tests; the key was
  pinned by hand with the same code, from the line the release guard printed.
- alpha.1 to alpha.3 were deleted from the releases (tags kept) on
  2026-10-13; nobody runs them, and a helper from before alpha.6 cannot find
  the renamed release files, so it is reinstalled by hand once.
