# Handover

Written 2026-08-28. CI is green (18/18) but **nobody has ever used this
extension end to end**. That gap is the most important thing on this page.

---

## What it is

An extension (Firefox + Chrome) that lets you pick books in Audiobookshelf and
sync them to a USB player — renaming `.m4b` → `.m4a` on the way, because SanDisk
Sansa Clip/Fuze players ignore the `.m4b` extension but decode the AAC inside it
fine. A Python native-messaging host does the filesystem work.

Built for a specific 320-book Audiobookshelf library on a Proxmox host; nothing
in it is installation-specific.

---

## Status: proven vs unproven

**Proven by CI** (18 jobs, every push):

| Area | Coverage |
|---|---|
| Native host | 45 unittest cases — stdio protocol framing, copy semantics, m4b→m4a, disc-folder ordering, idempotency, ASCII transliteration, zip extraction, error paths |
| Cross-platform | macOS + Linux + Windows × Python 3.9 / 3.11 / 3.13, plus a real stdio round-trip per OS |
| Installer | Manifest generation and paths for all 6 OS × browser combinations, install/uninstall round-trip |
| Build | Per-browser bundles; both manifests valid and browser-appropriate |
| Extension logic | vitest over `lib.js` on node 20 + 22 |
| Lint | `web-ext lint` on the Firefox bundle |
| Content script | Playwright loads the **real built extension** in Chromium and Firefox and asserts the button lands in `#toolbar` |

**Not proven by anything:**

- **The popup has never been opened.** `popup.js` — library dropdown, filter,
  select-all, the sync call — has never run in a browser.
- **The options page has never been opened.** `options.js` likewise.
- **`background.js` message handlers have never been exercised.** CI tests
  `lib.js` (pure logic) but not the `browser.runtime.onMessage` switch, the
  `fetch` calls to Audiobookshelf, or `sendNativeMessage`.
- **Native messaging has never run through a browser.** The host is tested by
  driving its stdin/stdout directly. No browser has ever spawned it.
- **No book has ever reached a real device this way.** The copy logic is well
  covered against temp directories; a real Clip has never been plugged in.

The standalone `tools/clipsync.py` *has* been used against the real library and
a mock device, and implements the same copy semantics. It is the fallback if the
extension path turns out to be broken.

---

## Three constraints that justify the architecture

Do not "simplify" these away — each was verified, and the design collapses
without them.

1. **A browser extension cannot write to a USB device.**
   `downloads.download({filename})` — MDN: *"Absolute paths, empty paths, path
   components that start and/or end with a dot (.), and paths containing
   back-references (`../`) will cause an error."* Everything lands under the
   Downloads folder.
2. **Firefox has no `showDirectoryPicker()`.** The File System Access API that
   would grant write access to a chosen directory is Chromium-only. (On Chrome
   alone you *could* drop the native host; keeping one code path was a
   deliberate trade.)
3. **Audiobookshelf accepts `?token=` in the URL.** `Auth.js` uses
   `ExtractJwt.fromExtractors([fromAuthHeaderAsBearerToken(),
   fromUrlQueryParameter('token')])`. This is why download URLs can authenticate
   without headers — `downloads.download` cannot set them.

And the reason the rename works at all: an `.m4b` *is* an MP4/AAC container. The
Clip refuses the extension, not the codec. Renaming is lossless and instant.

---

## Things that look like bugs and are not

- **`content.js` does not read Audiobookshelf's multi-select state.** Book cards
  render as `#book-card-{index}`; the library item id appears nowhere in the DOM,
  and selection lives in Vue component state. Scraping it would break on any ABS
  update. The injected button opens the extension's own API-backed picker.
- **The Chromium e2e test sets `channel: "chromium"`.** MV3 extensions do not
  load under the old headless mode.
- **`tslib` is in devDependencies but nothing we wrote imports it.**
  `playwright-webextext@0.0.5` is compiled with TypeScript's `importHelpers` yet
  publishes `"dependencies": none`, so its `dist` does `require("tslib")` with
  nothing to resolve. Remove it and both e2e jobs fail at import.
  **Worth reporting upstream** — one line in their `package.json`.
- **`lib.js` is a classic script with a CommonJS tail.** It has to load three
  ways without a bundler: Firefox's MV3 `background.scripts`, Chrome's module
  service worker, and node for tests.
- **`package.json` has `"type": "module"`.** Required for Playwright to parse
  the `.js` config and spec as ESM.

---

## What is left to do

Roughly in priority order.

1. **Actually use it.** Load the unpacked extension, run `native/install.py`,
   open the popup, sync one book to a real device. Everything below is
   secondary to this.
2. **Cover the untested UI paths.** `popup.js` and `options.js` have no tests at
   all. A Playwright test that opens the popup against a mocked ABS API would
   close most of the gap.
3. **Chrome native messaging.** `install.py --browser chrome` needs
   `--chrome-id`, because unpacked extensions get a fresh id on every load.
   Nobody has verified the Chrome host path works.
4. **Bump the deprecated actions.** GitHub flags `checkout@v4`,
   `setup-node@v4`, `setup-python@v5`, `upload-artifact@v4` as targeting Node
   20, force-run on Node 24. Not breaking today.
5. **Consider a lockfile.** `cache: npm` was removed from `setup-node` because
   there is no `package-lock.json`. Committing one would let caching return and
   make builds reproducible.
6. **Packaging.** Temporary add-ons unload when Firefox restarts. Real installs
   need AMO signing (or Developer Edition with
   `xpinstall.signatures.required=false`).

---

## Working on it

```bash
python3 extension/build.py                                     # dist/{firefox,chrome}
python3 -m unittest discover -s tests/python -p 'test_*.py' -v  # no deps needed
npm install && npx vitest run
npx playwright install --with-deps chromium firefox && npx playwright test
```

Repo: `https://github.com/dadatuputi/audiobookshelf-helper` (public, `main`).
A working clone also sits at `/root/src/audiobookshelf-helper` on
`proxmox.shire.brd.la`; pushes from there rely on an SSH agent forwarded into
that session, so a fresh environment will need its own credentials.

**Reading CI without a token:** job logs return 403 anonymously, but check-run
annotations are public. The `Run Playwright` step captures output and, on
failure, emits the head of it as a `::error::` annotation for exactly this
reason. `GET /repos/:owner/:repo/commits/:sha/check-runs` then
`GET /repos/:owner/:repo/check-runs/:id/annotations`.

---

## How CI got green, in case it regresses

Eight runs. Worth skimming if something breaks in a similar way:

| Failure | Cause |
|---|---|
| All node jobs died in `setup-node` | `cache: npm` needs a lockfile that isn't committed |
| Both e2e legs failed | Spec launched both browsers, but CI installs one per matrix leg |
| Both e2e legs failed at import | `.js` files use `import` without `"type": "module"`; and `playwright-webextext` is CJS using `Object.defineProperty(exports, …)`, which node's ESM lexer cannot see — default-import and destructure |
| Both e2e legs failed at import | `tslib` missing (above) |

The symmetry was the clue throughout: Chromium never touches
`playwright-webextext`, so identical failures in both projects always meant a
load-time problem, never a browser-specific one.
