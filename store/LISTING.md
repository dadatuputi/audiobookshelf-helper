# Store listing copy

Everything a submission form asks for, written once. Paste from here rather
than improvising in the form — the two stores ask overlapping questions and
answering them differently is the sort of thing reviewers notice.

Privacy policy URL (both stores):
`https://github.com/dadatuputi/audiobookshelf-helper/blob/main/store/PRIVACY.md`

---

## Name

Audiobookshelf Helper

## Summary (Chrome: 132 char limit)

> Pick books in Audiobookshelf and sync them to a USB player, renaming .m4b to
> .m4a on the way.

(92 characters.)

## Description

> Audiobookshelf Helper copies audiobooks from your own Audiobookshelf server
> onto a plain USB audio player — the kind that has no apps, no account, and no
> idea what a media server is.
>
> Pick books from your library in a filterable list, press Sync, and they are
> written to the device with tidy folder names. An "On device" shelf shows what
> is already there, how much space it uses, and lets you delete a book from the
> player without hunting through folders.
>
> It exists because many simple players refuse to play a file called .m4b,
> while playing the AAC inside it perfectly well. An .m4b *is* an MP4/AAC container; only the extension is
> unfamiliar. This add-on renames as it copies, which is lossless and instant.
> Nothing is transcoded and no quality is lost.
>
> A browser extension cannot write to a USB device, so the file copying is done
> by a small open-source helper program you install on your own computer. The
> add-on is the interface; the helper does the work.
>
> Requires: an Audiobookshelf server you control, Python 3.9 or newer, and the
> helper from the project's releases page.
>
> This add-on has no servers of its own. It talks to your Audiobookshelf server
> and to the local helper, and to nothing else. It collects no data whatsoever.

## Category

Chrome Web Store: **Workflow & Planning**
AMO: **Other** (secondary: Download Management)

## Support / homepage

`https://github.com/dadatuputi/audiobookshelf-helper`

---

## Permission justifications

Chrome asks for these per permission in the dashboard; AMO asks in review
notes. Same answers, deliberately.

**`nativeMessaging`**
> A browser extension cannot write files to a removable USB device. The
> WebExtension `downloads` API rejects absolute paths and `../`, so every
> download lands in the browser's Downloads folder, and `showDirectoryPicker()`
> is not available in Firefox. The extension therefore delegates file copying
> to a small Python helper the user installs themselves. The helper is
> open-source and part of the same repository. It accepts four commands: ping,
> list what is on the device, copy selected books, and delete a named book from
> the device.

**`storage`**
> Stores the user's own settings: their Audiobookshelf server URL and API key,
> the mount path of their player, and their naming preferences. Local only.

**`scripting`**
> Used with `scripting.registerContentScripts` to add a "Sync to device" button
> to the toolbar of the user's Audiobookshelf library pages. It is registered at
> runtime for the single origin the user has granted, rather than declared in
> the manifest against every site.

**Optional host permission (`*://*/*` in `optional_host_permissions`)**
> Audiobookshelf is self-hosted, so the server's address is not knowable at
> build time — it is whatever the user types in, commonly a private hostname or
> IP on their LAN. The broad pattern appears only in `optional_host_permissions`
> and is never requested as written. At runtime the extension computes the
> single origin pattern for the URL the user configured (for example
> `http://media.local:13378/*`) and requests only that, from a button press on
> the options page. The extension declares no `host_permissions` and no
> `content_scripts`, so a fresh install has access to no site at all. This can
> be verified: install it and check `chrome://extensions` — site access is empty
> until the user grants their own server.

**Remote code**
> None. No code is loaded from any remote source. All logic ships in the
> package; there is no `eval`, no remote script tag, and no CDN.

---

## Data collection disclosures

Chrome Web Store data-use form — answer **no** to every collection category,
and tick all three certifications:

- Not being sold to third parties, outside of approved use cases — **yes**
- Not being used or transferred for purposes unrelated to the item's single
  purpose — **yes**
- Not being used or transferred to determine creditworthiness or for lending
  purposes — **yes**

AMO — the manifest declares
`browser_specific_settings.gecko.data_collection_permissions.required = ["none"]`,
which matches the above.

---

## Single-purpose statement (Chrome requires one)

> The single purpose of this extension is to copy audiobooks from the user's
> own Audiobookshelf server to a USB audio player attached to their computer.

---

## Screenshots to capture before submitting

Chrome wants at least one 1280×800 or 640×400; AMO wants at least one. Take
them against a real library:

1. The popup's **Library** tab — filtered list, a couple of books ticked, the
   "on device" chip visible on one row.
2. The popup's **On device** tab — two or three books with sizes, free space
   showing in the tab bar.
3. The **options page**, with *Access granted* showing.
4. The **Sync to device** button in the Audiobookshelf toolbar.

A sync in progress, with the progress bar mid-copy, makes a good fifth if the
timing cooperates.

---

## Review notes (paste into both submissions)

> This extension requires a companion native helper to function, because a
> browser extension cannot write to a removable drive. Reviewers can exercise
> the full flow without hardware:
>
> 1. Clone https://github.com/dadatuputi/audiobookshelf-helper
> 2. `python3 native/install.py` registers the helper.
> 3. Any local directory works as the "player mount path" — a USB device is not
>    required to test.
> 4. An Audiobookshelf server is needed for the library list. The repository's
>    test suite includes a stand-in server
>    (`tests/e2e/extension.spec.js`) that can be run instead.
>
> The helper's full source is `native/absh_host.py` (under 400 lines, no
> dependencies beyond the Python standard library). It only ever writes beneath
> the directory the user configured, and its delete command refuses any name
> that is not a single entry resolving inside that directory, and refuses
> symlinks.
