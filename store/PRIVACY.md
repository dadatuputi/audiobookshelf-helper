# Privacy Policy — Audiobookshelf Helper

Last updated: 2026-08-28

## The short version

This add-on collects nothing, sends nothing to us, and has no servers. There is
no analytics, no telemetry, no crash reporting, no advertising, and no third
party of any kind.

## What it stores, and where

Everything is kept in the browser's own extension storage, on your machine:

| Stored | Why |
|---|---|
| Your Audiobookshelf server URL | To know which server to talk to |
| Your Audiobookshelf API key | To authenticate to *your* server |
| The path your player mounts at | To know where to copy books |
| Folder name, folder template, rename and source preferences | Your settings |
| The last library you picked | So the picker opens where you left it |

None of it leaves your computer. It is removed when you uninstall the add-on.

## What it talks to

Two things, both yours:

1. **Your Audiobookshelf server**, at the address you enter. The add-on reads
   your libraries and book list, and downloads books you select. Nothing is
   written back to the server.
2. **A native helper program on your own computer**, installed by you, which
   copies files onto your USB player. It receives the book details and the
   download URL for the books you selected, and reports back what it copied,
   what is already on the device, and what it deleted when you ask it to.

There is no third destination. The add-on has no ability to reach any server
you have not configured: it ships with no host permissions at all and asks for
access to exactly one origin — yours — which you grant explicitly.

## Your API key

Your API key is stored in extension storage and sent to your own server in two
ways: as an `Authorization: Bearer` header on API requests, and as a `?token=`
query parameter on download URLs. The second form exists because the browser
cannot attach headers to the download the helper performs, and Audiobookshelf
accepts the token either way.

That means your API key appears in the download URL handed to the local helper.
It stays on your machine and is not logged by the add-on. Treat it as you would
any credential: if your server is exposed to the internet, prefer a scoped key.

## Permissions, and why each is needed

| Permission | Why |
|---|---|
| `storage` | To keep the settings listed above |
| `nativeMessaging` | To talk to the local helper, which is the only component able to write to a USB device |
| `scripting` | To add the "Sync to device" button to your Audiobookshelf pages, registered only for your server |
| Host access to your server | To read your library and download the books you pick |

Host access is **not** requested in the manifest. It is requested at runtime,
for your server's origin only, when you press *Grant access*.

## Data deletion

Uninstalling the add-on removes everything it stored. The helper can be removed
with `python3 install.py --uninstall`. Books already copied to your player are
files on your player; delete them there, or from the add-on's *On device* shelf.

## Contact

Issues and questions: https://github.com/dadatuputi/audiobookshelf-helper/issues
