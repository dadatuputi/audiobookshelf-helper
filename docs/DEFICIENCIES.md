# Deficiencies and todo

What is known to be wrong, missing, or untested, and why each one matters.
Kept in the repo rather than in an issue tracker so it travels with the code
and can be wrong in public. Every claim here was checked against the code or
a real run on the date noted; where something is believed rather than
verified, it says so.

Last reviewed: 2026-10-03, against `cfc0cd9`.

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
  navigates a native dialog to the mount point themselves, every time the
  grant is lost.
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

### D1. `absh update` verifies integrity, not provenance
`absh/update.py` says this itself. The digest comes from GitHub, computed
over the bytes GitHub was given, so anyone who can publish a release
publishes a matching digest. It catches a corrupted download and nothing
else. The helper runs outside the browser sandbox with the user's rights and
writes to their filesystem, so this is the gap that matters most: a
self-updater whose check establishes no provenance. The fix is signing with
a key that does not live on GitHub, and verifying it in the updater.

**Severity: high.** Everything else here is an inconvenience.

### D2. A stale add-on is published under the wrong version
`v1.0.0-alpha.1` still carries `cb73684229b84553a7b8-1.0.0.1.xpi`, built
2026-09-01, beside zips rebuilt 2026-09-07 from different code and labelled
the same version. 2 downloads. `ca67dbf` stops this recurring, but does not
clean up the instance that already shipped — that needs deleting by hand.

### D3. Windows device detection is a 2-second poll
Linux and macOS get real kernel events. Windows compares the drive bitmask
on a timer, because the alternative is creating a window and pumping a
message loop for `WM_DEVICECHANGE`. `mounts.is_polling()` reports this
honestly and `cmd_watch` returns it as `polls`, so the fact already crosses
the wire — `polls` appears nowhere in `extension/src/`, so the UI drops it.

### D4. A test credential is in public history
`tests/real/state.json` holds a JWT for a throwaway local Audiobookshelf
instance and was committed in `274935f` and `d414b8a`. It is gitignored now.
The server it authenticates to never existed outside CI, so the practical
risk is low; a history rewrite was considered and declined.

### D5. The Chrome Web Store path has never run
The `chrome-web-store` job is skipped for every prerelease and its secrets
are unset, so the first stable tag will exercise it for the first time —
the same way the AMO path was first exercised in production, which cost
three failed releases.

### D6. AMO's listed channel has never run
Only the unlisted channel has. The listed branch — submission accepted, no
artefact in the run, signed after review — is covered by a stubbed `gh` and
nothing else.

### D7. Nothing surfaces an available update
`absh/host.py` reports `release` in its ping reply *specifically* so the
options page can say the helper is behind. Nothing reads it: `release` does
not appear in `extension/src/options.js`. So `absh update` exists and the
only way to learn it is needed is to already know.

---

## Todo

### T1. Consume ping's `release` and offer the update
The data already crosses the wire. Needs the options page to compare it
against the latest release and say so. Pairs with T2. *(Closes D7.)*

### T2. Make `absh update` reachable without the CLI
Today it is `python3 -m absh.cli update` from the directory you unzipped.
A user who installed by double-clicking does not have that context.

### T3. Test that a published release matches its tag
Packaging has real coverage; publishing is only stub-tested. Both release
failures this cycle lived in that gap: a stale asset surviving a replace,
and a version AMO already held. A post-publish check that asserts the
release's assets, names and versions agree with the tag would have caught
both.

### T4. Make the update self-check prove more than "it starts"
`_self_check` sends a `ping` and requires an answer. A build that starts and
answers but cannot read a device passes. Running `devices` would cost
nothing and prove the part that matters.

### T5. Sign releases with a key GitHub does not hold
The real fix for D1. Scope: a signing key, a published public key, a
signature asset, and verification in `update.apply` before the swap.

### T6. Drive the Firefox options page in a real browser
Playwright's Firefox cannot drive `moz-extension://` documents, worked
around by seeding config into the profile. So that UI is covered by unit
tests and seeded state rather than by clicking it. DuckDuckGo's harness
patches `omni.ja` to enable this; that is the known route if it becomes
worth the cost.

### T7. Optional helper-free mode on Chromium
See the research section. A second filesystem backend behind the same
interface the helper implements, plus a permission lifecycle, plus a user
story for when the grant lapses. Real work, Chromium-only, and it removes
the install step for the majority of users. Low priority precisely because
it cannot replace the helper — only sit beside it.

### T8. Surface that Windows is polling
`cmd_watch` already returns `polls`; the UI ignores it. The page could say device changes take a
moment here, instead of looking slow for no stated reason. *(Mitigates D3.)*
