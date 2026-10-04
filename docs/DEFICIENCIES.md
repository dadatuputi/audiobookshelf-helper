# Deficiencies and todo

What is known to be wrong, missing, or untested, and why each one matters.
Kept in the repo rather than in an issue tracker so it travels with the code
and can be wrong in public. Every claim here was checked against the code or
a real run on the date noted; where something is believed rather than
verified, it says so.

Last reviewed: 2026-10-03, against `0138d94`. Status lines record what
changed since the list was written, with the commit that did it. CI at
`0138d94` is green on every job, including the helper's suite on Windows,
macOS and Linux under Python 3.9, 3.11 and 3.13.

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

---

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
job outputs. **It has not yet run inside Actions.**

### T4. Make the update self-check prove more than "it starts" — DONE (`af77013`)
The new helper must also list devices, against a stand-in folder so it
reads nothing real and gives the same answer with nothing mounted.

### T5. Sign releases — DONE, with a CI-held key by choice (`a891beb`)
See D1.

### T6. Drive the Firefox options page in a real browser — DONE (`86f76bb`)
Over Firefox's remote debugging protocol. Proven in CI: all five tests ran
and passed in a real Firefox. The Grant button still needs a trusted user
gesture, so permission granting is covered only in Chromium.

### T7. Optional helper-free mode on Chromium — IN PROGRESS
See the research section. Being built as a second backend beside the helper.

### T8. Surface that Windows is polling — DONE (`0138d94`)

---

## Needs the maintainer

### M1. Set up the signing key — once, before the next tag of any kind
1. `openssl rand -base64 32`.
2. Add the output as the repository secret `RELEASE_SIGNING_KEY`.
3. Run the **Pin release-signing key** workflow (Actions tab); it commits the
   key's public half to `absh/release_keys.py`.

Tag builds stop without this, because a helper released with no trusted key
can never update itself. After it, every release is signed by CI; there is no
per-release step.

### M2. Before the first stable tag
Add the `CWS_PUBLISHER_ID` secret; create the Chrome Web Store item by hand
(the v2 API cannot create items); run a dispatched dry run, which signs in
and reads the item without publishing.

### M3. Optionally, delete the stale `claude/plugin-release-pipeline-kzlr09` branch
Every commit on it reached main through PRs #1–#6, and `git diff` against
its squash commit `044bd74` is empty, so nothing is lost. Deleting it only
tidies the branch list. Nothing under `.claude/` has ever been committed on
any ref, and `.claude/` is ignored since `22711c5`.

---

## Still unverified

- The `verify-release` job has never run in Actions.
- Chrome Web Store v2: the exact upload-state spellings and error shapes;
  Google's reference pages were unreachable when this was written.
- Whether AMO's first listed submission needs listing details (summary,
  categories, licence), and whether its refusal of a deleted version number
  says "already exists".
- CI signing (`ci-sign`) and the pin workflow have run only in tests, against
  the workflow's own step scripts; the first tag after M1 is their first live run.
- Users on alpha.2 and alpha.3 run the old updater, which does not check
  signatures: they trust GitHub once more, for the first signed release.
  alpha.1 shipped no updater and must install the next release by hand.
