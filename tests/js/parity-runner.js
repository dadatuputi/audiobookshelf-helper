/* Runs the parity fixtures (tests/fixtures/parity) against folder.js.
 *
 * One runner for both places the fixtures are checked: vitest, against the
 * in-memory handle in memfs.js, and tests/e2e/folder.spec.js, which evaluates
 * this file's source inside the extension's options page and runs it against
 * the origin private file system - a real FileSystemDirectoryHandle. So it is
 * a plain script that sets a global: no imports, no node APIs.
 *
 * Each function returns exactly the shape tools/parity_vectors.py records for
 * the same case, so the test is one deep equality per case. */
(function (g) {
  "use strict";

  const bytes = (b64) => Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  const plain = (x) => JSON.parse(JSON.stringify(x));

  async function buildTree(dir, tree) {
    for (const rel of Object.keys(tree).sort()) {
      const parts = rel.split("/");
      const isDir = tree[rel] === null;
      let d = dir;
      for (const p of parts.slice(0, isDir ? parts.length : -1)) {
        d = await d.getDirectoryHandle(p, { create: true });
      }
      if (!isDir) {
        const w = await (await d.getFileHandle(parts[parts.length - 1], { create: true })).createWritable();
        await w.write(bytes(tree[rel]));
        await w.close();
      }
    }
  }

  /** Every file (with its size) and folder, except the sidecar index. */
  async function readTree(F, dir) {
    const files = {};
    const dirs = [];
    const walk = async (d, parts) => {
      for await (const [name, h] of d.entries()) {
        const p = parts.concat(name);
        if (p[0] === ".absh") continue;
        if (h.kind === "directory") {
          dirs.push(p);
          await walk(h, p);
        } else {
          files[p.join("/")] = (await h.getFile()).size;
        }
      }
    };
    await walk(dir, []);
    return { files, dirs: dirs.sort(F.cmpParts).map((p) => p.join("/")) };
  }

  async function readIndex(F, dir) {
    const entries = (await F.loadIndex(dir)).entries;
    const out = {};
    for (const k of Object.keys(entries).sort(F.cmpCodePoints)) {
      const { syncedAt, ...rest } = entries[k];   // eslint-disable-line no-unused-vars
      out[k] = rest;
    }
    return out;
  }

  /** Serves a case's downloads exactly as fetch would hand them over. */
  function fakeClient(downloads) {
    const uploads = [];
    return {
      uploads,
      async download(id) {
        const d = downloads[id];
        return { contentType: d.contentType || "", disposition: d.disposition || "",
                 blob: new Blob([bytes(d.body)]) };
      },
      async upload(library, folder, title, author, files, series) {
        uploads.push({ library, folder, title, author, series,
                       files: files.map(([n, b]) => [n, b.size]) });
        return { id: "li_new" };
      },
    };
  }

  async function pull(F, dev, c) {
    await buildTree(dev, c.tree || {});
    const opts = { subdir: "AUDIOBOOKS", renameM4b: true,
                   folderTemplate: "{author} - {title}", ...c.opts };
    const events = [];
    const rep = await F.pull(fakeClient(c.downloads), dev, c.items, opts, (e) => events.push(e));
    return { report: plain(rep), tree: await readTree(F, dev), index: await readIndex(F, dev),
             events: plain(events) };
  }

  async function push(F, dev, c) {
    await buildTree(dev, c.tree);
    const opts = { subdir: "AUDIOBOOKS", renameM4b: true, restoreM4b: true,
                   folderTemplate: "{author} - {title}", libraryId: "lib1", folderId: "fol1" };
    const entries = await F.scan(dev, "AUDIOBOOKS", opts.folderTemplate);
    const want = new Set(c.push);
    const chosen = entries.filter((e) => !want.size || want.has(e.name));
    const client = fakeClient({});
    const events = [];
    const rep = await F.push(client, chosen, opts, (e) => events.push(e));
    return { report: plain(rep), uploads: client.uploads, events: plain(events) };
  }

  async function remove(F, dev, c) {
    await buildTree(dev, c.tree);
    const events = [];
    const rep = await F.remove(dev, c.names, { subdir: "AUDIOBOOKS" }, (e) => events.push(e));
    return { report: plain(rep), tree: await readTree(F, dev), index: await readIndex(F, dev),
             events: plain(events) };
  }

  async function device(F, dev, c, server) {
    await buildTree(dev, c.tree);
    const entries = await F.scan(dev, c.subdir, c.template, c.readTags);
    const d = F.diff(server, entries, c.template);
    return {
      scan: plain(entries),
      both: d.both.map((b) => ({ name: b.name, itemId: b.itemId, matchedBy: b.matchedBy })),
      serverOnly: d.serverOnly.map((i) => i.id),
      deviceOnly: d.deviceOnly.map((e) => e.name),
      index: await readIndex(F, dev),
    };
  }

  async function tags(F, t) {
    const files = {};
    for (const f of t.files) {
      const got = await F.readTags(new Blob([bytes(f.b64)]), f.name);
      files[f.name] = { title: got.title, author: got.author, album: got.album };
    }
    const byName = Object.fromEntries(t.files.map((f) => [f.name, f.b64]));
    const books = [];
    for (const b of t.books) {
      const got = await F.readBook(b.files.map((n) => ({ name: n, file: new Blob([bytes(byName[n])]) })));
      books.push({ title: got.title, author: got.author, album: got.album });
    }
    return { files, books };
  }

  g.PARITY = { buildTree, readTree, readIndex, pull, push, remove, device, tags };
})(typeof globalThis !== "undefined" ? globalThis : self);
