/* The player as a folder Chrome can write to - without the helper.
 *
 * Chromium only. build.py copies this into the Chrome bundle and nowhere
 * else: Firefox has no showDirectoryPicker and Mozilla does not intend to
 * ship one, so there the helper stays the only way to reach the device.
 *
 * It is a second implementation of what absh does - pull, push, remove, scan
 * and the three-way match - against a FileSystemDirectoryHandle instead of a
 * path. Two copies of a rule drift, so this one is held to the Python one by
 * tests/fixtures/parity/*.json, which tools/parity_vectors.py generates by
 * running absh itself. Each function below names the Python it mirrors;
 * change both, regenerate, and both test suites have to agree again.
 *
 * Where it runs: the background service worker, which reads the handle the
 * options page stored in this extension's IndexedDB. Measured in Chromium
 * 141: the worker can list, read and write through such a handle, but cannot
 * show a picker or answer a permission prompt. Chrome keeps the grant only
 * while one of the extension's own tabs is open - it withdraws it within a
 * second or two of the last one closing; neither the toolbar popup nor an
 * offscreen document counts - unless the user chose "Allow on every visit",
 * which survives tabs closing and restarts. So access has to be restored
 * from the options page, with a click, and the UI says so.
 *
 * Same classic-script-with-a-CommonJS-tail shape as lib.js, so it loads in
 * the module service worker, in the options page, and in node for tests. */
(function (root) {
  "use strict";

  /* ------------------------------------------------------------- naming
   * absh/naming.py. */

  const AUDIO_EXT = new Set([".m4b", ".m4a", ".mp3", ".flac", ".wav", ".ogg", ".opus"]);
  const RESERVED = /[<>:"/\\|?*\x00-\x1f]/g;

  /* Python's str.strip() and \s cover a different set from String.trim():
   * they include \x1c-\x1f and \x85, and not the BOM. A title read from a
   * tag keeps a BOM in Python, so it has to here too. */
  const PY_WS = "\\t\\n\\x0b\\x0c\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
  const PY_WS_RUN = new RegExp(`[${PY_WS}]+`, "g");
  const PY_WS_EDGES = new RegExp(`^[${PY_WS}]+|[${PY_WS}]+$`, "g");

  function pyStrip(s, chars) {
    s = String(s);
    if (chars === undefined) return s.replace(PY_WS_EDGES, "");
    let a = 0;
    let b = s.length;
    while (a < b && chars.includes(s[a])) a++;
    while (b > a && chars.includes(s[b - 1])) b--;
    return s.slice(a, b);
  }

  /** unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode() */
  function asciiFold(s) {
    return String(s).normalize("NFKD").replace(/[^\x00-\x7f]/g, "");
  }

  function clean(s) {
    s = asciiFold(s || "").replace(RESERVED, "");
    return pyStrip(s.replace(PY_WS_RUN, " ")) || "Untitled";
  }

  /* pathlib, POSIX flavour: the name is the last part once empty and "."
   * parts are gone; the suffix needs a dot that is neither first nor last. */
  function pathName(p) {
    const parts = String(p).split("/").filter((x) => x !== "" && x !== ".");
    return parts.length ? parts[parts.length - 1] : "";
  }
  function suffix(p) {
    const name = pathName(p);
    const i = name.lastIndexOf(".");
    return i > 0 && i < name.length - 1 ? name.slice(i) : "";
  }
  function stem(p) {
    const name = pathName(p);
    const i = name.lastIndexOf(".");
    return i > 0 && i < name.length - 1 ? name.slice(0, i) : name;
  }

  function outExt(path, renameM4b = true) {
    const e = suffix(path).toLowerCase();
    return renameM4b && e === ".m4b" ? ".m4a" : e;
  }

  function sourceExt(name) {
    const e = suffix(name).toLowerCase();
    return e === ".m4a" ? ".m4b" : e;
  }

  /** The books folder as a list of parts. Never empty, never escapes. */
  function safeSubdir(s) {
    const parts = [];
    for (const raw of String(s == null ? "" : s).split(/[\\/]+/)) {
      if (!raw || raw === "." || raw === "..") continue;
      const p = clean(raw);
      if (p && p !== "Untitled") parts.push(p);
    }
    return parts.length ? parts : ["AUDIOBOOKS"];
  }

  function targetName(book, template) {
    let name = template || "{author} - {title}";
    // In order, like str.replace in a loop: a placeholder that arrives inside
    // a value is itself replaced by the next pass.
    for (const key of ["author", "title", "series"]) {
      const v = book[key];
      name = name.split(`{${key}}`).join(v ? String(v) : "");
    }
    return pyStrip(clean(name), " -") || "Untitled";
  }

  function fold(s) {
    s = asciiFold(s || "").toLowerCase();
    s = pyStrip(s);
    s = s.replace(new RegExp(`,[${PY_WS}]*(the|a|an)[${PY_WS}]*$`), "");
    s = s.replace(/[^a-z0-9 ]+/g, " ");
    s = pyStrip(s.replace(PY_WS_RUN, " "));
    s = s.replace(/^(the|a|an) /, "");
    return s.replace(new RegExp(`[${PY_WS}]+(the|a|an)$`), "");
  }

  function normKey(title, author) {
    return [fold(title), fold(author).replace(/ /g, "")];
  }

  /** absh/abs_api.py normalize_item. */
  function normalizeItem(it) {
    const media = it.media || {};
    const meta = media.metadata || {};
    let authors = meta.authorName;
    if (!authors && Array.isArray(meta.authors)) {
      authors = meta.authors.filter((a) => a && a.name).map((a) => a.name).join(", ");
    }
    let series = meta.seriesName;
    if (!series && Array.isArray(meta.series)) {
      series = meta.series.filter((s) => s)
        .map((s) => (typeof s === "object" ? s.name : String(s))).join(", ");
    }
    let tracks = media.numTracks;
    if (!tracks && Array.isArray(media.audioFiles)) tracks = media.audioFiles.length;
    return {
      id: it.id === undefined ? null : it.id,
      title: meta.title || it.relPath || "(untitled)",
      author: authors || "",
      series: series || "",
      relPath: it.relPath || "",
      numTracks: tracks || 0,
      size: it.size || media.size || 0,
    };
  }

  /* Python compares str by code point; JavaScript's < compares UTF-16 code
   * units, which disagree once a name has a character above U+FFFF. */
  function cmpCodePoints(a, b) {
    const x = Array.from(a);
    const y = Array.from(b);
    for (let i = 0; i < Math.min(x.length, y.length); i++) {
      if (x[i] !== y[i]) return x[i].codePointAt(0) < y[i].codePointAt(0) ? -1 : 1;
    }
    return x.length - y.length;
  }

  /** sorted() over pathlib paths: part by part, so "Disc 1/01" comes before
   *  "Disc 1.m4a", where a plain string sort puts it after. */
  function cmpParts(a, b) {
    for (let i = 0; i < Math.min(a.length, b.length); i++) {
      const c = cmpCodePoints(a[i], b[i]);
      if (c) return c;
    }
    return a.length - b.length;
  }

  /* ------------------------------------------------------------- tags
   * absh/tags.py, the built-in readers only.
   *
   * Without mutagen the helper reads MP4 atoms and ID3v2 frames and nothing
   * else, and that is what this matches - title, artist and album, which is
   * all the matching uses. What the helper with mutagen also reads (FLAC, Ogg
   * and Opus comments, ID3v2.2, odd MP4 layouts) a book here is identified
   * by its folder or file name instead, as the helper without mutagen does.
   * Falling back to names alone was the other option, and it would offer a
   * hand-copied book the server already has for upload - the one mistake the
   * tag match exists to prevent. */

  const EMPTY_TAGS = () => ({ title: "", author: "", album: "" });

  const UTF8 = () => new TextDecoder("utf-8", { ignoreBOM: true });

  function latin1(bytes) {
    // Not TextDecoder("latin1"): the web maps that label to windows-1252,
    // which turns 0x80-0x9f into curly quotes and the like.
    let s = "";
    for (let i = 0; i < bytes.length; i += 8192) {
      s += String.fromCharCode.apply(null, bytes.subarray(i, i + 8192));
    }
    return s;
  }

  function* mp4Atoms(d, start, end) {
    const view = new DataView(d.buffer, d.byteOffset, d.byteLength);
    let i = start;
    while (i + 8 <= end) {
      let size = view.getUint32(i);
      const name = latin1(d.subarray(i + 4, i + 8));
      let body;
      if (size === 1) {
        if (i + 16 > end) return;
        size = Number(view.getBigUint64(i + 8));
        body = i + 16;
      } else if (size === 0) {
        size = end - i;
        body = i + 8;
      } else {
        body = i + 8;
      }
      if (size < 8 || i + size > end) return;
      yield [name, body, i + size];
      i += size;
    }
  }

  function mp4Find(d, path, start, end) {
    for (const [name, b, stop] of mp4Atoms(d, start, end)) {
      if (name !== path[0]) continue;
      if (path.length === 1) return [b, stop];
      // meta carries 4 bytes of version and flags before its children.
      return mp4Find(d, path.slice(1), name === "meta" ? b + 4 : b, stop);
    }
    return null;
  }

  function mp4Text(d, start, end) {
    for (const [name, b, stop] of mp4Atoms(d, start, end)) {
      if (name === "data" && stop - b > 8) {
        return pyStrip(UTF8().decode(d.subarray(b + 8, stop)));
      }
    }
    return "";
  }

  const MP4_KEYS = { "\xa9nam": "title", "\xa9ART": "author", aART: "albumartist",
                     "\xa9alb": "album", "\xa9wrt": "composer" };

  /* The Python reads the whole file. A book can be hundreds of megabytes and
   * its moov atom is often at the end, so this walks the top level reading
   * only headers, then reads moov alone - the same atoms, the same bounds. */
  async function readMp4(file) {
    const size = file.size;
    let i = 0;
    let moov = null;
    while (i + 8 <= size) {
      const h = new Uint8Array(await file.slice(i, Math.min(i + 16, size)).arrayBuffer());
      const view = new DataView(h.buffer);
      let n = view.getUint32(0);
      const name = latin1(h.subarray(4, 8));
      let b;
      if (n === 1) {
        if (i + 16 > size) break;
        n = Number(view.getBigUint64(8));
        b = i + 16;
      } else if (n === 0) {
        n = size - i;
        b = i + 8;
      } else {
        b = i + 8;
      }
      if (n < 8 || i + n > size) break;
      if (name === "moov") { moov = [b, i + n]; break; }
      i += n;
    }
    const out = EMPTY_TAGS();
    if (!moov) return out;
    const d = new Uint8Array(await file.slice(moov[0], moov[1]).arrayBuffer());
    const found = mp4Find(d, ["udta", "meta", "ilst"], 0, d.length);
    if (!found) return out;
    const raw = {};
    for (const [name, b, stop] of mp4Atoms(d, found[0], found[1])) {
      const key = MP4_KEYS[name];
      if (key) raw[key] = mp4Text(d, b, stop);
    }
    out.title = raw.title || "";
    out.author = raw.albumartist || raw.author || raw.composer || "";
    out.album = raw.album || "";
    return out;
  }

  const syncsafe = (b, i) => (b[i] << 21) | (b[i + 1] << 14) | (b[i + 2] << 7) | b[i + 3];

  function id3Text(p) {
    if (!p.length) return "";
    const enc = p[0];
    let body = p.subarray(1);
    let s;
    if (enc === 0) {
      s = latin1(body);
    } else if (enc === 1) {
      // Python's "utf-16": a BOM picks the order and is dropped; without
      // one it is little-endian on every machine this runs on.
      let order = "utf-16le";
      if (body.length >= 2 && body[0] === 0xfe && body[1] === 0xff) { order = "utf-16be"; body = body.subarray(2); }
      else if (body.length >= 2 && body[0] === 0xff && body[1] === 0xfe) body = body.subarray(2);
      s = new TextDecoder(order, { ignoreBOM: true }).decode(body);
    } else if (enc === 2) {
      s = new TextDecoder("utf-16be", { ignoreBOM: true }).decode(body);
    } else {
      s = UTF8().decode(body);
    }
    return pyStrip(s.replace(/\x00/g, " "));
  }

  const ID3_KEYS = { TIT2: "title", TPE1: "author", TALB: "album", TPE2: "albumartist" };

  async function readId3(file) {
    const out = EMPTY_TAGS();
    const head = new Uint8Array(await file.slice(0, 10).arrayBuffer());
    if (head.length < 10 || latin1(head.subarray(0, 3)) !== "ID3") return out;
    const major = head[3];
    const end = Math.min(10 + syncsafe(head, 6), file.size);
    const d = new Uint8Array(await file.slice(0, end).arrayBuffer());
    const view = new DataView(d.buffer);
    const raw = {};
    let i = 10;
    while (i + 10 <= end) {
      const fid = d.subarray(i, i + 4);
      if (fid.every((x) => x === 0)) break;
      // v2.4 frame sizes are syncsafe; v2.3 are plain big-endian.
      const fsize = major >= 4 ? syncsafe(d, i + 4) : view.getUint32(i + 4);
      i += 10;
      if (fsize <= 0 || i + fsize > end) break;
      const key = ID3_KEYS[latin1(fid)];
      if (key) raw[key] = id3Text(d.subarray(i, i + fsize));
      i += fsize;
    }
    out.title = raw.title || "";
    out.author = raw.albumartist || raw.author || "";
    out.album = raw.album || "";
    return out;
  }

  /** tags.read: best effort for one file, never throws. */
  async function readTags(file, name) {
    try {
      const ext = suffix(name).toLowerCase();
      if ([".m4a", ".m4b", ".mp4", ".m4p"].includes(ext)) return await readMp4(file);
      if (ext === ".mp3") return await readId3(file);
    } catch { /* a bad file is blank, not an error */ }
    return EMPTY_TAGS();
  }

  /** tags.read_book. `files` is [{name, file}] - file a Blob, or null if
   *  it could not be opened. */
  async function readBook(files) {
    if (!files.length) return EMPTY_TAGS();
    const first = files[0].file ? await readTags(files[0].file, files[0].name) : EMPTY_TAGS();
    const out = { ...first };
    if (files.length > 1 && first.album) out.title = first.album;
    else if (!out.title) out.title = first.album || stem(files[0].name);
    return out;
  }

  /* -------------------------------------------------------------- zip
   *
   * Audiobookshelf sends a multi-file book as one zip. The helper opens it
   * with zipfile; this reads the central directory itself, which is enough
   * for what a server sends - stored or deflated members, ZIP64 records, and
   * the cp437 or UTF-8 names zipfile would decode. Each member is checked
   * against its CRC-32 as it is written, as zipfile does, so a damaged
   * download fails rather than landing on the player as a corrupt track. */

  const CP437_HIGH =
    "ÇüéâäàåçêëèïîìÄÅÉæÆôöòûùÿÖÜ¢£¥₧ƒáíóúñÑªº¿⌐¬½¼¡«»░▒▓│┤╡╢╖╕╣║╗╝╜╛┐└┴┬├─┼╞╟╚╔╩╦╠═╬╧╨╤╥╙╘╒╓╫╪┘┌█▄▌▐▀" +
    "αßΓπΣσµτΦΘΩδ∞φε∩≡±≥≤⌠⌡÷≈°∙·√ⁿ²■ ";

  function cp437(bytes) {
    let s = "";
    for (const b of bytes) s += b < 0x80 ? String.fromCharCode(b) : CP437_HIGH[b - 0x80];
    return s;
  }

  class ZipError extends Error {}

  async function zipEntries(blob) {
    const size = blob.size;
    const tailLen = Math.min(size, 22 + 0xffff);
    const tail = new Uint8Array(await blob.slice(size - tailLen).arrayBuffer());
    const tv = new DataView(tail.buffer);
    let eocd = -1;
    for (let i = tail.length - 22; i >= 0; i--) {
      if (tv.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
    }
    if (eocd < 0) throw new ZipError("File is not a zip file");
    let count = tv.getUint16(eocd + 10, true);
    let cdSize = tv.getUint32(eocd + 12, true);
    let cdOffset = tv.getUint32(eocd + 16, true);
    const locator = eocd - 20;
    if (locator >= 0 && tv.getUint32(locator, true) === 0x07064b50) {
      const at = Number(tv.getBigUint64(locator + 8, true));
      const z = new DataView(await blob.slice(at, at + 56).arrayBuffer());
      if (z.byteLength >= 56 && z.getUint32(0, true) === 0x06064b50) {
        count = Number(z.getBigUint64(32, true));
        cdSize = Number(z.getBigUint64(40, true));
        cdOffset = Number(z.getBigUint64(48, true));
      }
    }
    const cd = new Uint8Array(await blob.slice(cdOffset, cdOffset + cdSize).arrayBuffer());
    const v = new DataView(cd.buffer);
    const out = [];
    let i = 0;
    for (let n = 0; n < count; n++) {
      if (i + 46 > cd.length || v.getUint32(i, true) !== 0x02014b50) {
        throw new ZipError("Bad magic number for central directory");
      }
      const flags = v.getUint16(i + 8, true);
      const method = v.getUint16(i + 10, true);
      const crc = v.getUint32(i + 16, true);
      let compSize = v.getUint32(i + 20, true);
      let fileSize = v.getUint32(i + 24, true);
      const nameLen = v.getUint16(i + 28, true);
      const extraLen = v.getUint16(i + 30, true);
      const commentLen = v.getUint16(i + 32, true);
      let offset = v.getUint32(i + 42, true);
      const rawName = cd.subarray(i + 46, i + 46 + nameLen);
      let name = flags & 0x800 ? new TextDecoder("utf-8").decode(rawName) : cp437(rawName);
      const nul = name.indexOf("\x00");
      if (nul >= 0) name = name.slice(0, nul);
      // ZIP64: whichever fields overflowed are in extra field 0x0001, in order.
      let e = i + 46 + nameLen;
      const eEnd = e + extraLen;
      while (e + 4 <= eEnd) {
        const id = v.getUint16(e, true);
        const len = v.getUint16(e + 2, true);
        if (id === 0x0001) {
          let p = e + 4;
          if (fileSize === 0xffffffff) { fileSize = Number(v.getBigUint64(p, true)); p += 8; }
          if (compSize === 0xffffffff) { compSize = Number(v.getBigUint64(p, true)); p += 8; }
          if (offset === 0xffffffff) { offset = Number(v.getBigUint64(p, true)); }
        }
        e += 4 + len;
      }
      out.push({ name, flags, method, crc, compSize, fileSize, offset });
      i = eEnd + commentLen;
    }
    return out;
  }

  const CRC_TABLE = (() => {
    const t = new Uint32Array(256);
    for (let n = 0; n < 256; n++) {
      let c = n;
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      t[n] = c >>> 0;
    }
    return t;
  })();

  function crcStream(expect, size, name) {
    let crc = 0xffffffff;
    let seen = 0;
    return new TransformStream({
      transform(chunk, ctl) {
        for (let i = 0; i < chunk.length; i++) crc = CRC_TABLE[(crc ^ chunk[i]) & 0xff] ^ (crc >>> 8);
        seen += chunk.length;
        ctl.enqueue(chunk);
      },
      flush() {
        if (((crc ^ 0xffffffff) >>> 0) !== expect || seen !== size) {
          throw new ZipError(`Bad CRC-32 for file '${name}'`);
        }
      },
    });
  }

  /** One member's bytes, decompressed and verified, as a stream. */
  async function zipMemberStream(blob, ent) {
    if (ent.flags & 0x1) throw new ZipError(`File '${ent.name}' is encrypted`);
    const local = new DataView(await blob.slice(ent.offset, ent.offset + 30).arrayBuffer());
    if (local.byteLength < 30 || local.getUint32(0, true) !== 0x04034b50) {
      throw new ZipError("Bad magic number for file header");
    }
    const start = ent.offset + 30 + local.getUint16(26, true) + local.getUint16(28, true);
    let s = blob.slice(start, start + ent.compSize).stream();
    if (ent.method === 8) s = s.pipeThrough(new DecompressionStream("deflate-raw"));
    else if (ent.method !== 0) throw new ZipError(`compression type ${ent.method} is not supported`);
    return s.pipeThrough(crcStream(ent.crc, ent.fileSize, ent.name));
  }

  /* --------------------------------------------------- the device folder */

  /** A failure the UI acts on, not just shows: `code` picks the remedy. */
  function coded(code, message) {
    const e = new Error(message);
    e.code = code;
    return e;
  }

  const isNotFound = (e) => e && (e.name === "NotFoundError" || e.name === "TypeMismatchError");

  async function dirAt(base, parts, create = false) {
    let d = base;
    for (const p of parts) {
      try {
        d = await d.getDirectoryHandle(p, { create });
      } catch (e) {
        if (isNotFound(e)) return null;
        throw e;
      }
    }
    return d;
  }

  /** What `name` is inside `dir`: a handle and its kind, or null. */
  async function child(dir, name) {
    if (!dir) return null;
    try {
      return await dir.getFileHandle(name);
    } catch (e) {
      if (e && e.name === "TypeError") return null;          // not a valid name here
      if (!isNotFound(e)) throw e;
    }
    try {
      return await dir.getDirectoryHandle(name);
    } catch (e) {
      if (isNotFound(e) || (e && e.name === "TypeError")) return null;
      throw e;
    }
  }

  async function sortedChildren(dir) {
    const out = [];
    for await (const [name, h] of dir.entries()) out.push([name, h]);
    return out.sort((a, b) => cmpCodePoints(a[0], b[0]));
  }

  /** device._dir_stats: every file under a folder, at any depth. */
  async function dirStats(dir, prefix) {
    const all = [];
    const walk = async (d, parts) => {
      for (const [name, h] of await sortedChildren(d)) {
        if (h.kind === "directory") await walk(h, parts.concat(name));
        else all.push({ parts: parts.concat(name), handle: h, size: (await h.getFile()).size });
      }
    };
    await walk(dir, prefix);
    all.sort((a, b) => cmpParts(a.parts, b.parts));
    let total = 0;
    for (const f of all) total += f.size;
    const files = all.filter((f) => AUDIO_EXT.has(suffix(f.parts[f.parts.length - 1]).toLowerCase()));
    return { total, count: all.length, files };
  }

  async function writeBlob(fileHandle, data) {
    const w = await fileHandle.createWritable();
    try {
      await w.write(data);
      await w.close();
    } catch (e) {
      await w.abort().catch(() => {});
      throw e;
    }
  }

  /** Create `name` and fill it, or leave no trace of it.
   *
   * Asking for a file handle with create:true makes an empty file at once,
   * before a byte is written. If the write then failed - a damaged member, a
   * full player - that empty file would be taken for the finished one next
   * time, since a part that exists is skipped, and the book would stay one
   * track short for good. (The helper has the same hole with a half-written
   * file; this is where the browser can do better for free.) */
  async function createWhole(dir, name, fill) {
    const fh = await dir.getFileHandle(name, { create: true });
    try {
      await fill(fh);
    } catch (e) {
      await dir.removeEntry(name).catch(() => {});
      throw e;
    }
  }

  /* -------------------------------------------------------------- index
   * absh/index.py: <device>/.absh/index.json, written the same way, so a
   * player synced from here and from the helper keeps one record. */

  const INDEX_DIR = ".absh";
  const INDEX_NAME = "index.json";

  const isPlainObject = (o) => o !== null && typeof o === "object" && !Array.isArray(o);

  async function loadIndex(dev) {
    const fresh = { version: 1, entries: {} };
    try {
      const d = await dirAt(dev, [INDEX_DIR]);
      const fh = d && (await child(d, INDEX_NAME));
      if (!fh || fh.kind !== "file") return fresh;
      // Strict, and BOM kept: Python's read_text and json.loads refuse what
      // these refuse, and a refused index is an empty one.
      const text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true })
        .decode(await (await fh.getFile()).arrayBuffer());
      const data = JSON.parse(text);
      if (!isPlainObject(data) || !isPlainObject(data.entries)) return fresh;
      if (!("version" in data)) data.version = 1;
      return data;
    } catch {
      return fresh;
    }
  }

  function sortKeys(v) {
    if (Array.isArray(v)) return v.map(sortKeys);
    if (!isPlainObject(v)) return v;
    const out = {};
    for (const k of Object.keys(v).sort(cmpCodePoints)) out[k] = sortKeys(v[k]);
    return out;
  }

  const utcNow = () => new Date().toISOString().replace(/\.\d+Z$/, "Z");

  /** Never throws: a player that refuses the bookkeeping must not turn a
   *  copy that worked into one reported as failed. createWritable writes to a
   *  scratch file and swaps it in on close, which is the atomic replace the
   *  Python gets from os.replace. */
  async function saveIndex(dev, data) {
    try {
      const d = await dev.getDirectoryHandle(INDEX_DIR, { create: true });
      const fh = await d.getFileHandle(INDEX_NAME, { create: true });
      const body = { version: 1, updatedAt: utcNow(), entries: data.entries || {} };
      const text = JSON.stringify(sortKeys(body), null, 1)
        .replace(/[\u0080-￿]/g, (c) => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"));
      await writeBlob(fh, text);
      return true;
    } catch {
      return false;
    }
  }

  async function recordIndex(dev, name, item, files, kind, srcExt) {
    const data = await loadIndex(dev);
    data.entries[name] = {
      itemId: item.id === undefined ? null : item.id,
      title: item.title || "", author: item.author || "", series: item.series || "",
      kind, sourceExt: srcExt || "",
      bytes: files.reduce((n, f) => n + (f.size || 0), 0),
      files, syncedAt: utcNow(),
    };
    await saveIndex(dev, data);
  }

  async function forgetIndex(dev, names) {
    const data = await loadIndex(dev);
    const dropped = [];
    for (const n of names) {
      if (Object.prototype.hasOwnProperty.call(data.entries, n) && data.entries[n] !== null) {
        delete data.entries[n];
        dropped.push(n);
      } else if (Object.prototype.hasOwnProperty.call(data.entries, n)) {
        delete data.entries[n];
      }
    }
    if (dropped.length) await saveIndex(dev, data);
    return dropped;
  }

  async function pruneIndex(dev, present) {
    const data = await loadIndex(dev);
    const stale = Object.keys(data.entries).filter((n) => !present.has(n));
    for (const n of stale) delete data.entries[n];
    if (stale.length) await saveIndex(dev, data);
    return stale;
  }

  /* ---------------------------------------------------- scan and diff
   * absh/device.py. Paths are "/"-joined and relative to the device root,
   * which is what the parity fixtures record. */

  const recOf = (idx, name) => {
    if (!Object.prototype.hasOwnProperty.call(idx, name)) return null;
    const r = idx[name];
    // Python's `if rec:` - an empty record is no record.
    return isPlainObject(r) && Object.keys(r).length ? r : null;
  };

  async function scan(dev, subdir, template, readTagsToo = true) {
    const sub = safeSubdir(subdir);
    const rootDir = await dirAt(dev, sub);
    if (!rootDir) return [];
    const idx = (await loadIndex(dev)).entries;
    const out = [];
    for (const [name, h] of await sortedChildren(rootDir)) {
      if (name.startsWith(".") || name === INDEX_DIR) continue;
      let entry;
      let files;
      if (h.kind === "directory") {
        const st = await dirStats(h, sub.concat(name));
        if (!st.files.length) continue;
        files = st.files;
        entry = { name, kind: "dir", bytes: st.total, files: st.count,
                  paths: files.map((f) => f.parts.join("/")) };
      } else if (AUDIO_EXT.has(suffix(name).toLowerCase())) {
        const size = (await h.getFile()).size;
        files = [{ parts: sub.concat(name), handle: h, size }];
        entry = { name, kind: "file", bytes: size, files: 1, paths: [files[0].parts.join("/")] };
      } else {
        continue;
      }
      Object.defineProperty(entry, "handles", { value: files.map((f) => f.handle) });

      const rec = recOf(idx, name);
      if (rec) {
        Object.assign(entry, {
          itemId: rec.itemId === undefined ? null : rec.itemId,
          title: rec.title || stem(name), author: rec.author || "",
          series: rec.series || "", source: "index" });
      } else if (readTagsToo) {
        const book = [];
        for (const f of files) {
          let blob = null;
          try { blob = await f.handle.getFile(); } catch { /* blank, like a bad file */ }
          book.push({ name: f.parts[f.parts.length - 1], file: blob });
        }
        const t = await readBook(book);
        // For a folder, the per-file title is usually a chapter; the album is
        // the book, and failing that the folder is a better guess than "Part 1".
        const title = entry.kind === "dir" ? (t.album || name) : t.title;
        Object.assign(entry, {
          itemId: null, title: title || stem(name), author: t.author || "", series: "",
          source: t.title || t.author ? "tags" : "name" });
      } else {
        Object.assign(entry, { itemId: null, title: stem(name), author: "", series: "",
                               source: "name" });
      }
      out.push(entry);
    }
    // Anything the index still claims but the folder no longer has was
    // deleted outside this tool.
    await pruneIndex(dev, new Set(out.map((e) => e.name)));
    return out;
  }

  function diff(serverItems, entries, template) {
    const byId = new Map();
    const byName = new Map();
    const byKey = new Map();
    for (const i of serverItems) if (i.id) byId.set(i.id, i);
    for (const i of serverItems) {
      const n = targetName(i, template);
      if (!byName.has(n)) byName.set(n, i);
      const k = JSON.stringify(normKey(i.title, i.author));
      if (!byKey.has(k)) byKey.set(k, i);
    }
    const both = [];
    const deviceOnly = [];
    const matched = new Set();
    for (const e of entries) {
      let item = null;
      let how = null;
      if (e.itemId && byId.has(e.itemId)) { item = byId.get(e.itemId); how = "id"; }
      if (!item) {
        const s = e.kind === "file" ? stem(e.name) : e.name;
        if (byName.has(s)) { item = byName.get(s); how = "name"; }
      }
      if (!item && (e.title || e.author)) {
        const k = JSON.stringify(normKey(e.title, e.author));
        if (byKey.has(k)) { item = byKey.get(k); how = "tags"; }
      }
      if (item) {
        matched.add(item.id);
        const b = { ...e, item, itemId: item.id, matchedBy: how };
        Object.defineProperty(b, "handles", { value: e.handles });
        both.push(b);
      } else {
        deviceOnly.push(e);
      }
    }
    return { both, serverOnly: serverItems.filter((i) => !matched.has(i.id)), deviceOnly };
  }

  /** host._serialisable: what the popup and the page are sent. */
  function serialisable(st) {
    const pick = (o, keys) => Object.fromEntries(keys.map((k) => [k, o[k] === undefined ? null : o[k]]));
    const item = (i) => pick(i, ["id", "title", "author", "series", "size", "numTracks"]);
    const entry = (e) => pick(e, ["name", "kind", "bytes", "files", "title", "author",
                                  "series", "itemId", "source", "matchedBy"]);
    return {
      both: st.both.map((b) => ({ ...entry(b), item: item(b.item) })),
      serverOnly: st.serverOnly.map(item),
      deviceOnly: st.deviceOnly.map(entry),
      free: st.free || {},
      onDeviceBytes: st.onDeviceBytes || 0,
    };
  }

  /* ------------------------------------------------------- the server
   * absh/abs_api.py over fetch. Chrome makes these requests itself, so it
   * needs the host permission the options page grants for the one server -
   * the helper never did. */

  class AbsError extends Error {}

  function client(cfg, fetchImpl) {
    const doFetch = fetchImpl || ((...a) => fetch(...a));
    const base = String(cfg.absUrl || "").replace(/\/+$/, "");
    const key = cfg.apiKey || "";
    if (!base) throw new AbsError("Audiobookshelf URL is not set");
    let scheme = "";
    try { scheme = new URL(base).protocol.replace(/:$/, "").toLowerCase(); } catch { /* below */ }
    if (scheme !== "http" && scheme !== "https") {
      throw new AbsError(`server URL must be http or https, got '${scheme || "nothing"}'`);
    }
    const q = encodeURIComponent;

    async function request(path, init = {}) {
      let r;
      try {
        r = await doFetch(base + path, {
          ...init, cache: "no-store",
          headers: { Authorization: "Bearer " + key, Accept: "application/json" },
        });
      } catch (e) {
        throw new AbsError(`cannot reach Audiobookshelf at ${base}: ${(e && e.message) || e}`);
      }
      if (r.status >= 400) {
        let detail = "";
        try { detail = (await r.text()).slice(0, 400); } catch { /* none */ }
        const hint = r.status === 401 || r.status === 403 ? " - check the API key"
          : r.status === 404 ? " - check the server URL" : "";
        throw new AbsError(`Audiobookshelf ${path} responded ${r.status}${hint}. ${detail}`.trim());
      }
      return r;
    }

    async function json(path) {
      const body = await (await request(path)).text();
      if (!body.trim()) return {};
      try {
        return JSON.parse(body);
      } catch {
        throw new AbsError(`Audiobookshelf ${path} did not return JSON (is the URL the server root?)`);
      }
    }

    return {
      async libraries() {
        const d = await json("/api/libraries");
        const libs = Array.isArray(d) ? d : d.libraries || [];
        return libs.filter((l) => l.mediaType === "book")
          .map((l) => ({ id: l.id, name: l.name, mediaType: l.mediaType }));
      },
      async libraryFolders(id) {
        const d = await json(`/api/libraries/${q(id)}`);
        const lib = d.library !== undefined ? d.library : d;
        return (lib.folders || []).map((f) => ({ id: f.id, fullPath: f.fullPath }));
      },
      async items(id) {
        const d = await json(`/api/libraries/${q(id)}/items?limit=0&minified=1`);
        return (d.results || []).map(normalizeItem);
      },
      async download(id) {
        const r = await request(`/api/items/${q(id)}/download`);
        return { contentType: r.headers.get("Content-Type") || "",
                 disposition: r.headers.get("Content-Disposition") || "",
                 blob: await r.blob() };
      },
      async upload(libraryId, folderId, title, author, files, series) {
        if (!files.length) throw new AbsError("nothing to upload");
        const form = new FormData();
        form.append("library", libraryId);
        form.append("folder", folderId);
        form.append("title", title);
        form.append("author", author || "");
        if (series) form.append("series", series);
        files.forEach(([name, blob], i) => form.append(`file${i}`, blob, name));
        const raw = await (await request("/api/upload", { method: "POST", body: form })).text();
        try { return raw.trim() ? JSON.parse(raw) : { ok: true }; } catch { return { ok: true, raw: raw.slice(0, 200) }; }
      },
    };
  }

  /* --------------------------------------------------------------- sync
   * absh/sync.py. None of these throws for one book: a batch of 200 must
   * not stop because one book is odd. */

  function report() {
    const r = { copied: 0, skipped: 0, uploaded: 0, removed: [], freed: 0, errors: [], books: 0 };
    Object.defineProperty(r, "fail", { value: (title, why) => r.errors.push(`${title}: ${why}`) });
    return r;
  }

  const DISPOSITION = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/;

  function explain(e) {
    if (e && e.name === "NotAllowedError") {
      return "Chrome withdrew access to the player's folder part-way through. Open the " +
             "extension's Options and allow access again";
    }
    if (e && e.name === "QuotaExceededError") return "the player is full";
    return String((e && e.message) || e);
  }

  async function alreadyOnDevice(dev, root, item) {
    const idx = (await loadIndex(dev)).entries;
    for (const [name, rec] of Object.entries(idx)) {
      if (rec && rec.itemId && rec.itemId === item.id && (await child(root, name))) return name;
    }
    return null;
  }

  async function pullBook(c, dev, item, opts, rep, emit) {
    const sub = safeSubdir(opts.subdir);
    const name = targetName(item, opts.folderTemplate);
    const rename = opts.renameM4b !== false;
    const title = item.title || name;
    const step = (done, total, label) =>
      emit({ event: "progress", id: item.id, title, file: label, done, total });

    const have = await alreadyOnDevice(dev, await dirAt(dev, sub), item);
    if (have) {
      rep.skipped += 1;
      step(1, 1, have);
      return have;
    }

    let got;
    try {
      step(0, 1, "downloading");
      const r = await c.download(item.id);
      const m = DISPOSITION.exec(r.disposition);
      const srcname = m ? m[1] : title + ".bin";
      const root = await dirAt(dev, sub, true);
      if (r.contentType.toLowerCase().includes("zip") || srcname.toLowerCase().endsWith(".zip")) {
        got = await unzipBook(r.blob, root, name, rename, rep, title, step);
      } else {
        const srcExt = suffix(srcname).toLowerCase();
        const dstName = `${name}${outExt(srcname, rename)}`;
        step(1, 1, dstName);
        const there = await child(root, dstName);
        if (there && there.kind === "file" && (await there.getFile()).size === r.blob.size) {
          rep.skipped += 1;
        } else if (there) {
          await writeBlob(there, r.blob);
          rep.copied += 1;
        } else {
          await createWhole(root, dstName, (fh) => writeBlob(fh, r.blob));
          rep.copied += 1;
        }
        got = { written: [[root, dstName]], entry: dstName, kind: "file", srcExt };
      }
    } catch (e) {
      rep.fail(title, `download failed - ${explain(e)}`);
      return null;
    }
    if (!got) return null;
    const files = [];
    for (const [d, n] of got.written) {
      const h = await child(d, n);
      if (h && h.kind === "file") files.push({ name: n, size: (await h.getFile()).size });
    }
    await recordIndex(dev, got.entry, item, files, got.kind, got.srcExt);
    rep.books += 1;
    return got.entry;
  }

  async function unzipBook(blob, root, name, rename, rep, title, step) {
    const target = await root.getDirectoryHandle(name, { create: true });
    const all = await zipEntries(blob);
    // zipfile.getinfo: the last member of a given name is the one opened.
    const byName = new Map(all.map((e) => [e.name, e]));
    const members = all.map((e) => e.name)
      .filter((n) => AUDIO_EXT.has(suffix(n).toLowerCase()))
      .sort(cmpCodePoints);
    if (!members.length) {
      rep.fail(title, "download contained no audio files");
      return null;
    }
    const written = [];
    let srcExt = "";
    for (let i = 0; i < members.length; i++) {
      const member = members[i];
      srcExt = srcExt || suffix(member).toLowerCase();
      // Names are rebuilt, never taken from the archive, so a crafted zip
      // cannot reach outside the book's folder.
      const dst = `${String(i + 1).padStart(3, "0")} - ${clean(stem(member))}${outExt(member, rename)}`;
      step(i + 1, members.length, dst);
      if (await child(target, dst)) {
        rep.skipped += 1;
      } else {
        await createWhole(target, dst, async (fh) => {
          // pipeTo closes the file on success and aborts it on failure.
          await (await zipMemberStream(blob, byName.get(member))).pipeTo(await fh.createWritable());
        });
        rep.copied += 1;
      }
      written.push([target, dst]);
    }
    return { written, entry: name, kind: "dir", srcExt };
  }

  async function pull(c, dev, items, opts, emit = () => {}) {
    const rep = report();
    for (let n = 0; n < items.length; n++) {
      const item = items[n];
      emit({ event: "item", id: item.id, title: item.title, index: n + 1, count: items.length, op: "pull" });
      try {
        await pullBook(c, dev, item, opts, rep, emit);
      } catch (e) {
        rep.fail(item.title || "?", explain(e));
      }
    }
    return rep;
  }

  async function pushEntry(c, entry, opts, rep, emit) {
    const title = entry.title || stem(entry.name);
    const author = entry.author || "";
    if (!opts.libraryId || !opts.folderId) {
      rep.fail(title, "no target library/folder chosen for upload");
      return null;
    }
    const files = [];
    for (const h of entry.handles || []) {
      try { files.push([h.name, await h.getFile()]); } catch { /* gone since the scan */ }
    }
    if (!files.length) {
      rep.fail(title, "no audio files found on the device");
      return null;
    }
    // Undo the rename on the way back: it was .m4b on the server, and only
    // called .m4a so the player would touch it.
    const upload = files.map(([name, blob], i) => {
      let uploadName = name;
      if (opts.restoreM4b !== false) {
        const ext = sourceExt(name);
        if (ext !== suffix(name).toLowerCase()) uploadName = stem(name) + ext;
      }
      emit({ event: "progress", title, file: uploadName, done: i + 1, total: files.length });
      return [uploadName, blob];
    });
    try {
      await c.upload(opts.libraryId, opts.folderId, title, author, upload, entry.series || null);
    } catch (e) {
      if (!(e instanceof AbsError)) throw e;
      rep.fail(title, `upload failed - ${e.message}`);
      return null;
    }
    rep.uploaded += 1;
    rep.books += 1;
    return title;
  }

  async function push(c, entries, opts, emit = () => {}) {
    const rep = report();
    for (let n = 0; n < entries.length; n++) {
      const e = entries[n];
      emit({ event: "item", title: e.title, index: n + 1, count: entries.length, op: "push" });
      try {
        await pushEntry(c, e, opts, rep, emit);
      } catch (err) {
        rep.fail(e.title || "?", explain(err));
      }
    }
    return rep;
  }

  async function treeSize(h) {
    if (h.kind === "file") return (await h.getFile()).size;
    let n = 0;
    for await (const [, k] of h.entries()) n += await treeSize(k);
    return n;
  }

  /* sync.remove. The only destructive path. The handle API cannot name
   * anything outside the folder it was given, and a name with a separator in
   * it is refused before it is looked up - which is also why a backslash is
   * refused here on every OS, where the helper refuses it only on Windows. */
  async function remove(dev, names, opts, emit = () => {}) {
    const root = await dirAt(dev, safeSubdir(opts.subdir));
    const rep = report();
    for (let name of names) {
      name = String(name);
      if (!name || name === "." || name === ".." || pathName(name) !== name || name.includes("\\")) {
        rep.fail(name, "refusing a name that is not a single entry");
        continue;
      }
      const h = await child(root, name);
      if (!h) {
        rep.fail(name, "not on the device");
        continue;
      }
      try {
        emit({ event: "item", title: name, op: "remove" });
        const size = await treeSize(h);
        await root.removeEntry(name, { recursive: true });
        rep.freed += size;
        rep.removed.push(name);
      } catch (e) {
        rep.fail(name, explain(e));
      }
    }
    if (rep.removed.length) await forgetIndex(dev, rep.removed);
    return rep;
  }

  /* -------------------------------------------------- the stored handle
   *
   * IndexedDB of this extension's origin: the options page writes it, the
   * service worker reads it. A FileSystemDirectoryHandle survives a restart
   * there; the permission to use it does not. */

  const DB = "absh-folder";
  const STORE = "handles";
  const KEY = "device";

  function db() {
    return new Promise((res, rej) => {
      const r = indexedDB.open(DB, 1);
      r.onupgradeneeded = () => r.result.createObjectStore(STORE);
      r.onsuccess = () => res(r.result);
      r.onerror = () => rej(r.error);
    });
  }

  async function tx(mode, fn) {
    const d = await db();
    try {
      return await new Promise((res, rej) => {
        const t = d.transaction(STORE, mode);
        const out = fn(t.objectStore(STORE));
        t.oncomplete = () => res(out && "result" in out ? out.result : undefined);
        t.onerror = () => rej(t.error);
      });
    } finally {
      d.close();
    }
  }

  const loadHandle = () => tx("readonly", (s) => s.get(KEY)).then((h) => h || null);
  const saveHandle = (h) => tx("readwrite", (s) => s.put(h, KEY));
  const forgetHandle = () => tx("readwrite", (s) => s.delete(KEY));

  /** Where the folder stands, without asking for anything:
   *  none | granted | prompt | missing. "prompt" is the state Chrome leaves
   *  a grant in after a restart, or once the extension's last tab closes. */
  async function handleState(h) {
    h = h === undefined ? await loadHandle() : h;
    if (!h) return { state: "none", name: "" };
    let perm;
    try { perm = await h.queryPermission({ mode: "readwrite" }); } catch { perm = "prompt"; }
    if (perm !== "granted") return { state: "prompt", name: h.name };
    try {
      for await (const _ of h.keys()) break;   // eslint-disable-line no-unused-vars
    } catch (e) {
      if (e && e.name === "NotAllowedError") return { state: "prompt", name: h.name };
      return { state: "missing", name: h.name };
    }
    return { state: "granted", name: h.name };
  }

  /** host.require_device: the folder, usable, or the reason it is not. */
  async function openDevice() {
    const h = await loadHandle();
    const st = await handleState(h);
    if (st.state === "none") {
      throw coded("folder-none", "No folder is chosen for your player. Open the extension's " +
                                 "Options and choose the player's folder.");
    }
    if (st.state === "prompt") {
      throw coded("folder-access", `Chrome needs your OK again to use the folder “${st.name}”. ` +
                                   "Open the extension's Options and click Allow access.");
    }
    if (st.state === "missing") {
      throw coded("folder-missing", `The folder “${st.name}” isn't there. Plug the player in ` +
                                    "and try again - Chrome can't tell when it arrives.");
    }
    return h;
  }

  /* ----------------------------------------------------------- commands
   * host.py's cmd_*: the same names, settings and answers, so the popup
   * and the page cannot tell which one answered except where they ask. */

  const DEFAULTS = { absUrl: "", apiKey: "", libraryId: "", folderId: "",
                     subdir: "AUDIOBOOKS", folderTemplate: "{author} - {title}",
                     renameM4b: true, restoreM4b: true };

  /** host.settings: what the request says, unless it says nothing. */
  function settings(msg) {
    const cfg = { ...DEFAULTS };
    for (const k of Object.keys(DEFAULTS)) {
      if (msg[k] !== undefined && msg[k] !== null && msg[k] !== "") cfg[k] = msg[k];
    }
    return cfg;
  }

  function missing(cfg) {
    const gaps = [];
    if (!cfg.absUrl) gaps.push("absUrl (your Audiobookshelf URL)");
    if (!cfg.apiKey) gaps.push("apiKey (Settings -> API Keys)");
    return gaps;
  }

  /* The helper reaches the server from outside the browser. Here it is
   * Chrome making the request, so the one-origin grant has to exist. */
  async function serverAccess(cfg) {
    const perms = root.browser && root.browser.permissions;
    if (!perms || !cfg.absUrl) return true;
    let origin;
    try {
      const u = new URL(String(cfg.absUrl).replace(/\/+$/, ""));
      origin = `${u.protocol}//${u.host}/*`;
    } catch {
      return true;   // client() reports a bad URL in its own words
    }
    if (await perms.contains({ origins: [origin] })) return true;
    throw coded("folder-server", `Without the helper, Chrome itself has to reach ${origin.slice(0, -2)}. ` +
                                 "Grant access to your server in the extension's Options.");
  }

  async function clientFor(cfg, fetchImpl) {
    const gaps = missing(cfg);
    if (gaps.length) throw new Error("not configured: missing " + gaps.join(", "));
    await serverAccess(cfg);
    return client(cfg, fetchImpl);
  }

  async function libraryId(c, cfg, msg) {
    if (msg.libraryId) return msg.libraryId;
    if (cfg.libraryId) return cfg.libraryId;
    const libs = await c.libraries();
    if (!libs.length) throw new Error("the server reports no book libraries");
    return libs[0].id;
  }

  async function ping(msg) {
    const cfg = settings(msg);
    const st = await handleState();
    const gaps = missing(cfg);
    return { ok: true, backend: "folder", tags: "builtin", folder: st.name, access: st.state,
             configured: !gaps.length, missing: gaps };
  }

  /** Every command the helper answers that the folder can, by name.
   *  `fetchImpl` is for node tests only; the extension never passes one. */
  async function run(cmd, msg, emit = () => {}, fetchImpl) {
    const cfg = settings(msg);
    switch (cmd) {
      case "ping": return ping(msg);
      case "libraries": return { libraries: await (await clientFor(cfg, fetchImpl)).libraries() };
      case "folders": {
        const c = await clientFor(cfg, fetchImpl);
        return { folders: await c.libraryFolders(await libraryId(c, cfg, msg)) };
      }
      case "status": {
        const dev = await openDevice();
        const c = await clientFor(cfg, fetchImpl);
        const items = await c.items(await libraryId(c, cfg, msg));
        const entries = await scan(dev, cfg.subdir, cfg.folderTemplate, msg.readTags !== false);
        const d = diff(items, entries, cfg.folderTemplate);
        // Chrome does not say how much room a folder's drive has left.
        d.free = {};
        d.onDeviceBytes = entries.reduce((n, e) => n + e.bytes, 0);
        return { ...serialisable(d), backend: "folder", folder: dev.name };
      }
      case "pull": {
        const dev = await openDevice();
        const c = await clientFor(cfg, fetchImpl);
        const lib = await libraryId(c, cfg, msg);
        const wanted = new Set(msg.ids || []);
        const items = (await c.items(lib)).filter((i) => !wanted.size || wanted.has(i.id));
        if (!items.length) throw new Error("no matching books to pull");
        return pull(c, dev, items, cfg, emit);
      }
      case "push": {
        const dev = await openDevice();
        const c = await clientFor(cfg, fetchImpl);
        const lib = await libraryId(c, cfg, msg);
        let folder = msg.folderId || cfg.folderId;
        if (!folder) {
          const folders = await c.libraryFolders(lib);
          if (!folders.length) throw new Error("that library has no folder to upload into");
          folder = folders[0].id;
        }
        const entries = await scan(dev, cfg.subdir, cfg.folderTemplate);
        const names = new Set(msg.names || []);
        const chosen = entries.filter((e) => !names.size || names.has(e.name));
        if (!chosen.length) throw new Error("no matching books on the device to push");
        return push(c, chosen, { ...cfg, libraryId: lib, folderId: folder }, emit);
      }
      case "remove": {
        const dev = await openDevice();
        const names = msg.names || [];
        if (!names.length) throw new Error("nothing to remove");
        return remove(dev, names, cfg, emit);
      }
      default:
        throw new Error(`unknown cmd '${cmd}'`);
    }
  }

  const api = {
    // naming
    AUDIO_EXT, clean, pathName, suffix, stem, outExt, sourceExt, safeSubdir, targetName,
    normKey, normalizeItem, pyStrip, cmpCodePoints, cmpParts,
    // tags and zip
    readTags, readBook, zipEntries, zipMemberStream,
    // the device
    scan, diff, serialisable, pull, push, remove, loadIndex,
    client, settings, run, AbsError,
    // the stored handle
    loadHandle, saveHandle, forgetHandle, handleState,
  };
  root.ABSH_FOLDER = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : self);
