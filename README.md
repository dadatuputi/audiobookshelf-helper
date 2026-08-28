# Audiobookshelf Helper

Pick books in [Audiobookshelf](https://audiobookshelf.org) and sync them to a
USB audio player — renaming `.m4b` to `.m4a` on the way, because SanDisk Sansa
Clip/Fuze players ignore the `.m4b` extension but play the AAC inside it
perfectly well.

Firefox and Chrome. Native host runs on macOS, Linux and Windows.

---

See [HANDOVER.md](HANDOVER.md) for current status, what is and is not
tested, and what to pick up next.

## Why there is a native helper

A browser extension **cannot** write to a USB device. Two hard limits:

1. **`downloads.download({filename})`** — per MDN: *"Absolute paths, empty
   paths, path components that start and/or end with a dot (.), and paths
   containing back-references (`../`) will cause an error."* Everything lands
   under the browser's Downloads folder.
2. **`showDirectoryPicker()`** — the File System Access API that would grant
   write access to a chosen folder — is not implemented in Firefox.

So the extension is the UI, and a small Python native-messaging host does the
filesystem work. The host is the part that can destroy data, so it carries the
heavier test suite.

## Why the m4b → m4a rename works

An `.m4b` *is* an MP4/AAC container. The Clip's decoder handles AAC-LC; it is
the file extension it refuses. Renaming is lossless and instant — no
transcoding, no quality change.

## Layout

```
extension/
  src/            single source tree
    manifest.base.json   shared manifest; build.py adds the per-browser bits
    lib.js               pure logic (unit tested, no browser needed)
    background.js        API client + native-messaging bridge
    content.js           injects the toolbar button
    popup.*  options.*
  build.py        emits dist/firefox and dist/chrome
native/
  absh_host.py    stdio native messaging host
  install.py      registers the host (6 OS × browser combinations)
tests/
  python/         unittest — host, build, installer
  js/             vitest — pure logic
  e2e/            playwright — content script in a real browser
```

Firefox and Chrome disagree on exactly two manifest keys, so `build.py` emits
both rather than shipping a lowest-common-denominator manifest:

| | Firefox | Chrome |
|---|---|---|
| background | `{"scripts": [...]}` | `{"service_worker": "...", "type": "module"}` |
| identity | `browser_specific_settings.gecko.id` | id derived from the CRX key |

## Install

```bash
python3 extension/build.py          # -> extension/dist/{firefox,chrome}
python3 native/install.py           # register the native host
```

Then load the unpacked extension:

- **Firefox** — `about:debugging#/runtime/this-firefox` → Load Temporary Add-on
  → `extension/dist/firefox/manifest.json`
- **Chrome** — `chrome://extensions` → Developer mode → Load unpacked →
  `extension/dist/chrome`

Chrome assigns unpacked extensions a fresh id on each load, and the native host
manifest must name it. Grab it from `chrome://extensions`, then:

```bash
python3 native/install.py --browser chrome --chrome-id <id>
```

`--dry-run` shows what would be written; `--uninstall` removes it.

## Configure

Toolbar icon → ⚙.

| Setting | Notes |
|---|---|
| Audiobookshelf URL | e.g. `http://media.local:13378` |
| API key | Settings → API Keys. Sent as a Bearer header, and as `?token=` on downloads — ABS accepts both (`ExtractJwt.fromUrlQueryParameter`). |
| Player mount path | Absolute, e.g. `/Volumes/CLIP` or `E:\` |
| Folder on device | `AUDIOBOOKS` — SanDisk players give this folder resume + bookmarks |
| Folder template | `{author}` `{title}` `{series}` |
| Rename m4b → m4a | Leave on unless your player handles `.m4b` |
| Source | `auto` prefers a mounted SMB share (much faster) and falls back to HTTP |

## Use

A **⤓ Sync to device** button appears in the ABS library toolbar. It opens the
picker: choose a library, filter, tick books, **Sync selected**.

The button deliberately does **not** read Audiobookshelf's own multi-select
state. Book cards render as `#book-card-{index}` — the library item id never
appears in the DOM, and selection lives in Vue component state. Scraping it
would break on any ABS update, so the picker queries the API instead.

Re-running is cheap: files already on the device with a matching size are
skipped.

## Tests

```bash
python3 -m unittest discover -s tests/python -p 'test_*.py' -v   # no deps
npm install && npx vitest run                                    # unit
npx playwright install --with-deps && npx playwright test        # e2e
```

CI runs on every push:

| Job | Coverage |
|---|---|
| `native` | ubuntu × macos × windows, Python 3.9 / 3.11 / 3.13 |
| `native-smoke` | real stdio protocol round-trip per OS |
| `extension-lint` | `web-ext lint` + `addons-linter` |
| `extension-unit` | vitest on node 20 / 22 |
| `e2e` | Playwright, chromium + firefox |
| `package` | zips both bundles as artefacts |

All 18 jobs green. Note `playwright-webextext` is compiled with TypeScript's
`importHelpers` but ships no `dependencies`, so its `dist` requires `tslib`
without installing it — `tslib` is in our devDependencies to compensate.

Both browsers load the **real built extension**. Chromium uses core
Playwright's `--load-extension`. Firefox goes through
[`playwright-webextext`](https://github.com/ueokande/playwright-webextext),
which installs a temporary add-on over the remote debugging protocol — core
Playwright ignores extensions in `launchPersistentContext` on Firefox. For an
MV3 add-on webextext needs `browser_specific_settings.gecko.id`, which
`build.py` already emits for the Firefox target.

## Notes

- Multi-file books copy as `001 - …`, `002 - …` so they sort correctly; disc
  subfolders are flattened in order.
- Names are transliterated to ASCII and stripped of `<>:"/\|?*`.
- A single long `.m4a` gives no chapter navigation on the Clip. Keep a book as
  multiple files if you want skippable chapters.
- Older Clip+/Clip Zip: set **Settings → USB Mode → MSC** or it won't mount.

## Licence

MIT.
