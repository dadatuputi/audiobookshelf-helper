# Audiobookshelf Helper

Pick books in [Audiobookshelf](https://audiobookshelf.org) and sync them to a
USB audio player — renaming `.m4b` to `.m4a` on the way, because SanDisk Sansa
Clip/Fuze players ignore the `.m4b` extension but play the AAC inside it
perfectly well.

An **On device** shelf shows what is already on the player, how much space it
uses, and lets you delete a book from it without going through the filesystem.

Firefox and Chrome. Native helper runs on macOS, Linux and Windows.

---

## Why there is a native helper

A browser extension **cannot** write to a USB device. Two hard limits, both
verified — the architecture collapses without them, so don't "simplify" them
away:

1. **`downloads.download({filename})`** — per MDN: *"Absolute paths, empty
   paths, path components that start and/or end with a dot (.), and paths
   containing back-references (`../`) will cause an error."* Everything lands
   under the browser's Downloads folder.
2. **`showDirectoryPicker()`** — the File System Access API that would grant
   write access to a chosen folder — is not implemented in Firefox. (On Chrome
   alone you *could* drop the native helper; keeping one code path was a
   deliberate trade.)

So the extension is the UI, and a small Python helper does the filesystem work.
The helper is the part that can destroy data, so it carries the heavier tests.

## Why the m4b → m4a rename works

An `.m4b` *is* an MP4/AAC container. The Clip's decoder handles AAC-LC; it is
the file extension it refuses. Renaming is lossless and instant — no
transcoding, no quality change.

## Why the token goes in the URL

Audiobookshelf accepts the JWT as `?token=` as well as a Bearer header —
`Auth.js` uses `ExtractJwt.fromExtractors([fromAuthHeaderAsBearerToken(),
fromUrlQueryParameter('token')])`. That is what lets the helper authenticate a
download without setting headers.

---

## Install

Grab the [latest release](https://github.com/dadatuputi/audiobookshelf-helper/releases),
or build it yourself — see [Building locally](#building-locally). Either way you
also need the native helper:

```bash
python3 native/install.py               # register the native helper
```

Then load the extension:

- **Firefox** — `about:debugging#/runtime/this-firefox` → Load Temporary Add-on
  → `extension/dist/firefox/manifest.json`. Temporary add-ons unload on
  restart; a signed `.xpi` from the releases page installs permanently.
- **Chrome** — `chrome://extensions` → Developer mode → Load unpacked →
  `extension/dist/chrome`

`install.py` takes `--dry-run` and `--uninstall`. It no longer needs
`--chrome-id`: the Chrome extension id is pinned by the public key in
`extension/identity.json`, so it is the same on every load and every machine.

On Windows the helper manifest points at a generated `.bat`, because Windows
browsers cannot execute a `.py` directly.

## Building locally

Nothing here needs a bundler, and the extension itself builds with **Python
alone** — no `npm install` required. Node is only for the tests and linters.

### The unpacked extension, for development

```bash
python3 extension/build.py            # -> extension/dist/{firefox,chrome}
python3 extension/build.py --target chrome   # just one
```

Load `extension/dist/firefox` or `extension/dist/chrome` as an unpacked/temporary
extension (see [Install](#install)). Re-run after any change to `extension/src/`
and press reload in the browser — there is no watch mode, because the build is a
file copy plus a generated manifest and takes about a tenth of a second.

### The release artefacts, byte-for-byte

`tools/package.py` is the same script the release workflow runs, so you can
produce locally exactly what a tag would publish:

```bash
python3 tools/package.py                       # stamped v0.0.0, into release/
python3 tools/package.py --tag v1.0.0-alpha.1  # exactly what that tag ships
python3 tools/package.py --tag v1.0.0 --out /tmp/rc
```

That writes four archives plus a `release-info.json`:

| Archive | What it is |
|---|---|
| `...-firefox-<ver>.zip` | Firefox bundle, `manifest.json` at the **root** — what AMO accepts |
| `...-chrome-<ver>.zip` | Chrome bundle, same shape — what the Web Store accepts |
| `...-native-<ver>.zip` | `absh_host.py`, `install.py`, `identity.json`; unzip and run `python3 install.py` |
| `...-source-<ver>.zip` | Source for AMO's source-code review |

The version stamped into each manifest is derived from the tag, so a local
`--tag v1.0.0-alpha.1` build carries `1.0.0a1` for Firefox and `1.0.0.1` for
Chrome, just as the published one does. See [Releasing](#releasing).

### If you changed the icons

```bash
python3 extension/icons/make_icons.py    # regenerates the PNG set
```

The PNGs are committed, but CI regenerates them before packaging so a stale
one cannot ship.

### npm scripts, if you prefer them

```bash
npm install                    # only needed for tests and linters
npm run build                  # python3 extension/build.py
npm run package                # python3 tools/package.py  (add -- --tag vX.Y.Z)
npm run icons
npm test                       # python + unit + e2e
npm run test:py                # no node needed
npm run lint:ext               # web-ext lint, the check AMO runs
npm run lint:chrome
```

## Configure

Toolbar icon → ⚙.

| Setting | Notes |
|---|---|
| Audiobookshelf URL | e.g. `http://media.local:13378` |
| **Grant access** | Required. See below. |
| API key | Settings → API Keys |
| Player mount path | Absolute, e.g. `/Volumes/CLIP` or `E:\` |
| Folder on device | `AUDIOBOOKS` — SanDisk players give this folder resume + bookmarks |
| Folder template | `{author}` `{title}` `{series}`. Also how a book is recognised on the device, so changing it makes synced books look absent |
| Rename m4b → m4a | Leave on unless your player handles `.m4b` |
| Source | `auto` prefers a mounted SMB share (much faster) and falls back to HTTP |

### The permission grant

The extension ships with **no host permissions and no content script**. On a
fresh install it can reach no site at all — check `chrome://extensions`, site
access is empty.

Because Audiobookshelf is self-hosted, its address isn't knowable at build
time. So *Grant access* computes the single origin pattern for the URL you
typed (`http://media.local:13378/*`) and requests only that. The toolbar button
is then registered at runtime for that origin's `/library/*` pages.

This is why the manifest lists `*://*/*` under `optional_host_permissions` and
never requests it as written.

## Use

A **⤓ Sync to device** button appears in the Audiobookshelf library toolbar, and
opens the picker. Or just click the toolbar icon.

- **Library** — filter, tick books, **Sync selected**. Books already on the
  device carry an *on device* chip. Progress streams per file while it copies.
- **On device** — what is actually on the player, with sizes and free space.
  **Remove** takes two clicks; deleting from a device shouldn't be one stray
  click. Anything on the device that isn't in this library is listed separately
  under *Not in this library*.

Re-running is cheap: files already present with a matching size are skipped.

The button deliberately does **not** read Audiobookshelf's own multi-select
state. Book cards render as `#book-card-{index}` — the library item id never
appears in the DOM, and selection lives in Vue component state. Scraping it
would break on any ABS update, so the picker queries the API instead.

## Releasing

```bash
git tag v1.0.0-alpha.1 && git push --tags     # prerelease
git tag v1.0.0         && git push --tags     # stable
```

`.github/workflows/release.yml` runs the whole CI suite, then builds, packages
and publishes. A prerelease is **never** submitted to a public store — it gets
signed through AMO's unlisted channel instead, which needs no review and
installs permanently. Stable tags go to both stores.

Every publishing step skips cleanly when its credentials are missing, so the
pipeline is safe to run before any store account exists.

| Secret | For |
|---|---|
| `AMO_JWT_ISSUER`, `AMO_JWT_SECRET` | Firefox signing and submission |
| `CWS_CLIENT_ID`, `CWS_CLIENT_SECRET`, `CWS_REFRESH_TOKEN`, `CWS_ITEM_ID` | Chrome Web Store |

Versions are derived from the tag, because the stores disagree about what a
version may look like — Chrome takes 1–4 integers and rejects `1.0.0-alpha.1`
outright, while Firefox sorts `1.0.0a1` *below* `1.0.0`. So `v1.0.0-alpha.1`
becomes `1.0.0a1` for Firefox and `1.0.0.1` for Chrome. See
`tools/release_version.py`.

Store listing copy, permission justifications and the privacy policy live in
[`store/`](store/), written once so the two submissions can't disagree.

## Watching upstream

`.github/workflows/upstream.yml` checks weekly for new Audiobookshelf releases
and opens an issue listing what this extension depends on, with the changelog
lines that mention any of it. `tests/fixtures/abs/contract.json` is that
dependency list; `tests/js/contract.test.js` asserts the shapes against
recorded fixtures on every push. The workflow is the heads-up; the test is the
tripwire.

If an upgrade does break something, re-record the fixtures from the new server
and fix `lib.js` until they pass — don't loosen the assertions.

## Layout

```
extension/
  identity.json   the add-on id, host name and Chrome key - one source of truth
  identity.py     reads it; derives the Chrome id from the key
  icons/          make_icons.py generates the PNG set with no dependencies
  src/            single source tree
    manifest.base.json   shared manifest; build.py adds the per-browser bits
    lib.js               pure logic (unit tested, no browser needed)
    background.js        API client + native-messaging bridge
    content.js           injects the toolbar button
    popup.*  options.*
  build.py        emits dist/firefox and dist/chrome
native/
  absh_host.py    stdio native messaging host
  install.py      registers the helper (6 OS x browser combinations)
tools/
  package.py            release artefacts
  release_version.py    tag -> per-store versions
  publish_cws.py        Chrome Web Store upload
  check_upstream.py     Audiobookshelf release watch
  clipsync.py           standalone CLI, no browser involved
store/            privacy policy and store listing copy
tests/            python (host, build, installer, packaging) | js | e2e
```

Firefox and Chrome disagree on exactly two manifest keys, so `build.py` emits
both rather than shipping a lowest-common-denominator manifest:

| | Firefox | Chrome |
|---|---|---|
| background | `{"scripts": [...]}` | `{"service_worker": "...", "type": "module"}` |
| identity | `browser_specific_settings.gecko.id` | id derived from `key` |

## Tests

```bash
python3 -m unittest discover -s tests/python -p 'test_*.py' -v   # no deps
npm install && npx vitest run                                    # unit
npx playwright install --with-deps chromium firefox
npx playwright test                                              # e2e
```

`tests/e2e/extension.spec.js` is the one that matters: a real Chromium loads the
built extension, spawns the real native helper, lists a stand-in Audiobookshelf,
syncs a book to a temp "device", sees it on the shelf and deletes it — all
asserted against files on disk.

Headless Chromium draws no permission bubble, so `permissions.request()` never
resolves there. The granted state is seeded into the test profile instead, and
the *un*-granted first-run experience gets its own context.

Set `ABSH_CHROMIUM_PATH` / `ABSH_FIREFOX_PATH` if your environment ships a
browser at a fixed path rather than the revision Playwright downloads.

| CI job | Coverage |
|---|---|
| `native` | ubuntu × macos × windows, Python 3.9 / 3.11 / 3.13 |
| `native-smoke` | real stdio round-trip of sync → list → remove, per OS |
| `extension-lint` | `web-ext lint` (zero errors, warnings and notices) + `addons-linter` |
| `extension-unit` | vitest on node 20 / 22 |
| `e2e` | Playwright, chromium (full loop) + firefox (DOM contract) |
| `package` | the real release packaging, so a tag can't fail on it |

## Notes

- Multi-file books copy as `001 - …`, `002 - …` so they sort correctly; disc
  subfolders are flattened in order.
- Names are transliterated to ASCII and stripped of `<>:"/\|?*`.
- A single long `.m4a` gives no chapter navigation on the Clip. Keep a book as
  multiple files if you want skippable chapters.
- Older Clip+/Clip Zip: set **Settings → USB Mode → MSC** or it won't mount.
- `playwright-webextext` (and the `tslib` it needed but did not declare) are
  gone. It installed the built add-on into a real Firefox, but it cannot handle
  this manifest: `playwright-webextext@0.0.5` crashes on any MV3 add-on with no
  `content_scripts`, because `overridePermissions()` short-circuits into
  `manifest.optional_permissions.length` when that key is absent — and it means
  `optional_host_permissions` anyway. Worth reporting upstream. Firefox manifest
  validation is covered by `web-ext lint`, which is Mozilla's own addons-linter
  and the same check AMO runs on submission.
- `lib.js` is a classic script with a CommonJS tail: it has to load three ways
  without a bundler — Firefox's MV3 `background.scripts`, Chrome's module
  service worker, and node for tests.
- `package.json` has `"type": "module"` so Playwright can parse the config and
  specs as ESM.

## Licence

MIT.
