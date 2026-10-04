/* Chrome without the helper: the extension writing to a folder itself.
 *
 * A headless browser cannot drive the native folder picker. What it can do
 * is hand the extension a real FileSystemDirectoryHandle from somewhere else
 * and let everything after the picker run for real:
 *
 *  - the origin private file system (navigator.storage.getDirectory()) is a
 *    real browser filesystem with the same handle API, and is always granted
 *    - the stand-in for a player whose folder the user picked and allowed;
 *  - a real directory dropped onto the options page (over CDP) gives a real
 *    local handle that Chrome has granted read access to and not write - the
 *    state a picked folder is in after Chrome restarts, which is the
 *    "allow access again" path.
 *
 * Either one is stored through ABSH_FOLDER.saveHandle, the very function the
 * Choose button calls with the picker's answer, so nothing in the shipped
 * extension exists for these tests: the only step skipped is the dialog.
 * That the Choose button does open Chrome's picker is checked separately,
 * through CDP's file-chooser interception.
 *
 * And the helper is the reference: the same books go through the real helper
 * into a real directory, and through the folder backend into the browser's
 * filesystem, and the two results are compared file for file. */
import { test, expect, chromium } from "@playwright/test";
import { createServer } from "node:http";
import {
  mkdtempSync, mkdirSync, writeFileSync, readFileSync, readdirSync, statSync, existsSync
} from "node:fs";
import { execFileSync } from "node:child_process";
import { tmpdir } from "node:os";
import { resolve, join, dirname, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { createHash } from "node:crypto";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const distChrome = resolve(root, "extension/dist/chrome");
const identity = JSON.parse(readFileSync(resolve(root, "extension/identity.json"), "utf8"));
const HOST_NAME = identity.hostName;
const EXT_ID = (() => {
  const hex = createHash("sha256").update(Buffer.from(identity.chromeKey, "base64"))
    .digest("hex").slice(0, 32);
  return [...hex].map((c) => String.fromCharCode(97 + parseInt(c, 16))).join("");
})();
const LAUNCH = process.env.ABSH_CHROMIUM_PATH
  ? { executablePath: process.env.ABSH_CHROMIUM_PATH }
  : { channel: "chromium" };
// No test reaches GitHub; see extension.spec.js.
process.env.ABSH_UPDATE_API = "http://127.0.0.1:9";

const RUNNER = readFileSync(resolve(root, "tests/js/parity-runner.js"), "utf8");
const fixture = (n) => JSON.parse(readFileSync(resolve(root, "tests/fixtures/parity", n), "utf8"));

const OPTIONS = `chrome-extension://${EXT_ID}/options.html`;
const POPUP = `chrome-extension://${EXT_ID}/popup.html`;
const BASE = "/audiobookshelf";

/* ----------------------------------------------------------- the server */

function m4aWithTags(title, author) {
  const atom = (name, payload) => {
    const head = Buffer.alloc(8);
    head.writeUInt32BE(payload.length + 8, 0);
    head.write(name, 4, "latin1");
    return Buffer.concat([head, payload]);
  };
  const data = (text) => {
    const pre = Buffer.alloc(8);
    pre.writeUInt32BE(1, 0);
    return atom("data", Buffer.concat([pre, Buffer.from(text, "utf8")]));
  };
  const ilst = Buffer.concat([atom("\xa9nam", data(title)), atom("aART", data(author))]);
  const meta = atom("meta", Buffer.concat([Buffer.alloc(4), atom("ilst", ilst)]));
  return Buffer.concat([atom("ftyp", Buffer.from("M4A ")), atom("moov", atom("udta", meta))]);
}

const padded = (title, author, n = 2048) => {
  const t = m4aWithTags(title, author);
  return Buffer.concat([t, Buffer.alloc(n - t.length, 7)]);
};

/* A multi-file book comes from Audiobookshelf as a zip. Built by Python's
 * zipfile, deflated, which is what the helper itself reads. */
const HOBBIT_ZIP = Buffer.from(execFileSync("python3", ["-c", `
import base64, io, sys, zipfile
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("The Hobbit/Disc 1/01 - An Unexpected Party.m4b", bytes(range(256)) * 9)
    z.writestr("The Hobbit/Disc 1/02 - Roast Mutton.m4b", bytes(range(200)) * 7)
    z.writestr("The Hobbit/Disc 2/10 - A Warm Welcome.m4b", b"x" * 3000)
    z.writestr("The Hobbit/cover.jpg", b"not audio")
sys.stdout.write(base64.b64encode(buf.getvalue()).decode())
`]).toString(), "base64");

const BOOKS = [
  { id: "bk1", title: "Redwall", author: "Brian Jacques" },
  { id: "bk2", title: "Holes", author: "Louis Sachar" },
  { id: "bk3", title: "The Hobbit", author: "J.R.R. Tolkien", zip: true },
];

function startAbs() {
  const uploads = [];
  return new Promise((res) => {
    const srv = createServer((req, rep) => {
      const send = (obj) => {
        rep.writeHead(200, { "Content-Type": "application/json" });
        rep.end(JSON.stringify(obj));
      };
      if (!req.url.startsWith(BASE)) { rep.writeHead(404); return rep.end("no"); }
      const url = req.url.slice(BASE.length) || "/";
      if (req.method === "POST" && url.startsWith("/api/upload")) {
        const chunks = [];
        req.on("data", (c) => chunks.push(c));
        req.on("end", () => {
          const raw = Buffer.concat(chunks).toString("binary");
          const field = (n) => (new RegExp(`name="${n}"\\r\\n\\r\\n([^\\r]*)`).exec(raw) || [])[1];
          uploads.push({ names: [...raw.matchAll(/filename="([^"]+)"/g)].map((m) => m[1]),
                         title: field("title"), author: field("author"),
                         library: field("library"), folder: field("folder") });
          send({ id: "li_new" });
        });
        return;
      }
      if (url.startsWith("/api/me")) return send({ username: "tester" });
      const dl = /\/api\/items\/([^/]+)\/download/.exec(url);
      if (dl) {
        const book = BOOKS.find((b) => b.id === dl[1]);
        if (!book) { rep.writeHead(404); return rep.end("no"); }
        const body = book.zip ? HOBBIT_ZIP : padded(book.title, book.author);
        rep.writeHead(200, {
          "Content-Type": book.zip ? "application/zip" : "audio/mp4",
          "Content-Disposition": `attachment; filename="${book.title}.${book.zip ? "zip" : "m4b"}"`,
          "Content-Length": String(body.length),
        });
        return rep.end(body);
      }
      const entity = (b) => ({ id: b.id, relPath: `${b.author}/${b.title}`, size: 2048,
                               media: { numTracks: 1, metadata: { title: b.title, authorName: b.author } } });
      if (url.includes("/personalized")) {
        return send([{ id: "recent", label: "Recent", type: "book", entities: BOOKS.map(entity), total: 3 }]);
      }
      if (url.includes("/items")) return send({ results: BOOKS.map(entity) });
      if (url.startsWith("/api/libraries/")) {
        return send({ library: { id: "lib1", folders: [{ id: "fol1", fullPath: "/audiobooks" }] } });
      }
      if (url.startsWith("/api/libraries")) {
        return send({ libraries: [{ id: "lib1", name: "Audiobooks", mediaType: "book" }] });
      }
      if (url.startsWith("/library/")) {
        rep.writeHead(200, { "Content-Type": "text/html" });
        const card = (b, i) => `<div class="shelf"><div id="book-card-${i}">` +
                               `<img alt="${b.title}, Cover" src="${BASE}/x.jpg"></div></div>`;
        return rep.end('<!doctype html><html><body><div id="app"><div id="toolbar"></div>' +
                       BOOKS.map(card).join("") + "</div><script>" +
                       `fetch(${JSON.stringify(BASE)} + "/api/libraries/lib1/personalized")` +
                       ".then(r => r.json());</script></body></html>");
      }
      rep.writeHead(404); rep.end("no");
    });
    srv.listen(0, "127.0.0.1", () => res({
      srv, uploads, absUrl: `http://127.0.0.1:${srv.address().port}${BASE}` }));
  });
}

/* ---------------------------------------------------------- the browser */

/** A profile with the server's origin granted (seeded: headless Chromium
 *  draws no permission bubble). The helper is registered only when asked -
 *  most of this file is about a machine that does not have it. */
function makeProfile(absUrl, { helper = false } = {}) {
  const profile = mkdtempSync(join(tmpdir(), "absh-folder-e2e-"));
  if (helper) {
    const dir = join(profile, "NativeMessagingHosts");
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, `${HOST_NAME}.json`), JSON.stringify({
      name: HOST_NAME, description: "test", path: resolve(root, "native/absh_host.py"),
      type: "stdio", allowed_origins: [`chrome-extension://${EXT_ID}/`] }));
  }
  const perms = { api: [], explicit_host: [`${new URL(absUrl).origin}/*`],
                  manifest_permissions: [], scriptable_host: [] };
  mkdirSync(join(profile, "Default"), { recursive: true });
  writeFileSync(join(profile, "Default", "Preferences"), JSON.stringify({
    extensions: { settings: { [EXT_ID]: { granted_permissions: perms, active_permissions: perms } } } }));
  return profile;
}

async function launch(profile) {
  const ctx = await chromium.launchPersistentContext(profile, {
    ...LAUNCH, headless: true,
    args: [`--disable-extensions-except=${distChrome}`, `--load-extension=${distChrome}`] });
  if (!ctx.serviceWorkers().length) await ctx.waitForEvent("serviceworker", { timeout: 30_000 });
  return ctx;
}

async function optionsPage(ctx) {
  const page = await ctx.newPage();
  await page.goto(OPTIONS);
  await page.waitForFunction(() => (document.getElementById("permState")?.textContent || "") !== "",
                             null, { timeout: 15_000 });
  return page;
}

async function configure(ctx, absUrl, devicePath = "") {
  const page = await optionsPage(ctx);
  await page.fill("#absUrl", absUrl);
  await page.fill("#apiKey", "test-key");
  await page.click("#save");
  await expect(page.locator("#msg")).toHaveText("saved");
  // The helper's player path is chosen in the popup now; setup sets it directly.
  await page.evaluate((d) => chrome.storage.local.set({ devicePath: d }), devicePath);
  return page;
}

/** Store a folder of the origin private file system as the player's folder,
 *  through the same call the Choose button makes with the picker's answer. */
const useOpfsFolder = (page, name) => page.evaluate(async (name) => {
  const opfs = await navigator.storage.getDirectory();
  await ABSH_FOLDER.saveHandle(await opfs.getDirectoryHandle(name, { create: true }));
}, name);

const message = (page, msg) => page.evaluate((m) => chrome.runtime.sendMessage(m), msg);

/** {files: {path: size}, index: entries without timestamps} for an OPFS folder. */
const opfsState = (page, name) => page.evaluate(async (name) => {
  const opfs = await navigator.storage.getDirectory();
  const dev = await opfs.getDirectoryHandle(name, { create: true });
  const files = {};
  let index = null;
  const walk = async (d, parts) => {
    for await (const [n, h] of d.entries()) {
      const p = parts.concat(n);
      if (h.kind === "directory") await walk(h, p);
      else if (p.join("/") === ".absh/index.json") index = JSON.parse(await (await h.getFile()).text());
      else files[p.join("/")] = (await h.getFile()).size;
    }
  };
  await walk(dev, []);
  return { files, index };
}, name);

/** The same for a real directory, written by the helper. */
function dirState(dir) {
  const files = {};
  let index = null;
  const walk = (d) => {
    for (const n of readdirSync(d)) {
      const p = join(d, n);
      const rel = relative(dir, p).split(sep).join("/");
      if (statSync(p).isDirectory()) walk(p);
      else if (rel === ".absh/index.json") index = JSON.parse(readFileSync(p, "utf8"));
      else files[rel] = statSync(p).size;
    }
  };
  if (existsSync(dir)) walk(dir);
  return { files, index };
}

const entriesOf = (index) => Object.fromEntries(Object.entries((index && index.entries) || {})
  .map(([k, { syncedAt, ...rest }]) => [k, rest]));     // eslint-disable-line no-unused-vars

const opfsWrite = (page, name, rel, b64) => page.evaluate(async ([name, rel, b64]) => {
  let d = await (await navigator.storage.getDirectory()).getDirectoryHandle(name, { create: true });
  const parts = rel.split("/");
  for (const p of parts.slice(0, -1)) d = await d.getDirectoryHandle(p, { create: true });
  const w = await (await d.getFileHandle(parts[parts.length - 1], { create: true })).createWritable();
  await w.write(Uint8Array.from(atob(b64), (c) => c.charCodeAt(0)));
  await w.close();
}, [name, rel, b64]);

/* ===================================================================== */

test.describe("without the helper, the folder backend does what the helper does", () => {
  test.skip(({ browserName }) => browserName !== "chromium", "Chromium only - Firefox has no folder picker");

  let srv, uploads, absUrl, folderCtx, helperCtx, helperDev;

  test.beforeAll(async () => {
    ({ srv, uploads, absUrl } = await startAbs());
    // The reference: the real helper, into a real directory.
    helperDev = join(mkdtempSync(join(tmpdir(), "absh-folder-ref-")), "player");
    mkdirSync(helperDev);
    helperCtx = await launch(makeProfile(absUrl, { helper: true }));
    await (await configure(helperCtx, absUrl, helperDev)).close();
    // The subject: no helper installed at all.
    folderCtx = await launch(makeProfile(absUrl));
    await (await configure(folderCtx, absUrl)).close();
  });

  test.afterAll(async () => {
    await folderCtx?.close();
    await helperCtx?.close();
    srv?.close();
  });

  test("the options page offers it to Chrome users, and says what they give up", async () => {
    const page = await optionsPage(folderCtx);
    const box = page.locator("#folderBox");
    await expect(page.locator("h2#folder")).toHaveText("Without the helper (Chrome only)");
    await expect(box).toContainText("If you can't install the helper, or would rather not");
    await expect(box).toContainText("Whenever the helper is installed and answering, it is used instead");
    await page.locator("#folderBox summary").click();
    for (const loss of ["Finding the player", "Plugging in and out", "Staying allowed",
                        "Free space", "Some tags", "command line"]) {
      await expect(box.locator("li", { hasText: loss })).toBeVisible();
    }
    await expect(page.locator("#folderState")).toHaveText("No folder chosen.");
    await page.close();
  });

  test("Choose opens Chrome's own folder picker", async () => {
    const page = await optionsPage(folderCtx);
    // The dialog cannot be answered headless, but CDP can intercept it, which
    // proves the button reaches showDirectoryPicker from a real click.
    const cdp = await folderCtx.newCDPSession(page);
    await cdp.send("Page.enable");
    await cdp.send("Page.setInterceptFileChooserDialog", { enabled: true });
    const opened = new Promise((r) => cdp.once("Page.fileChooserOpened", r));
    await page.click("#folderPick");
    expect(await opened).toMatchObject({ mode: "selectSingle" });
    // Intercepting cancels it; a cancelled picker is not an error to show.
    await expect(page.locator("#folderState")).toHaveText("No folder chosen.");
    await page.close();
  });

  test("before a folder is chosen, the popup says the helper is missing and offers the folder",
       async () => {
    const page = await folderCtx.newPage();
    await page.goto(POPUP);
    await expect(page.locator("#status")).toContainText("Native helper not reachable", { timeout: 20_000 });
    await expect(page.locator("a", { hasText: "choose your player's folder in Options" })).toBeVisible();
    await page.close();
  });

  test("once a folder is chosen, the options page and popup say it is in use", async () => {
    const page = await optionsPage(folderCtx);
    await useOpfsFolder(page, "player");
    await page.reload();
    await expect(page.locator("#folderState")).toHaveText(
      "Using “player”. There is no AUDIOBOOKS folder in it yet; one is made on the first copy.");
    await expect(page.locator("#folderUse")).toHaveText(
      "The helper isn't answering, so books go to this folder.");
    await expect(page.locator("#folderAllow")).toBeHidden();

    const popup = await folderCtx.newPage();
    await popup.goto(POPUP);
    await expect(popup.locator("#status")).toHaveText(
      "using the folder “player”, not the helper (tags: builtin)", { timeout: 20_000 });
    await expect(popup.locator("#list li")).toHaveCount(BOOKS.length);
    await popup.close();
    await page.close();
  });

  test("a pull through the popup writes the exact names, renamed", async () => {
    const page = await folderCtx.newPage();
    await page.goto(POPUP);
    await expect(page.locator("#list li")).toHaveCount(BOOKS.length, { timeout: 20_000 });
    for (const t of ["Redwall", "The Hobbit"]) {
      await page.locator("#list li", { hasText: t }).locator("input[type=checkbox]").check();
    }
    await page.click("#act");
    await expect(page.locator("#status")).toContainText("copied 4 file(s)", { timeout: 30_000 });

    const opts = await optionsPage(folderCtx);
    const st = await opfsState(opts, "player");
    expect(st.files).toEqual({
      "AUDIOBOOKS/Brian Jacques - Redwall.m4a": 2048,
      "AUDIOBOOKS/J.R.R. Tolkien - The Hobbit/001 - 01 - An Unexpected Party.m4a": 2304,
      "AUDIOBOOKS/J.R.R. Tolkien - The Hobbit/002 - 02 - Roast Mutton.m4a": 1400,
      "AUDIOBOOKS/J.R.R. Tolkien - The Hobbit/003 - 10 - A Warm Welcome.m4a": 3000,
    });
    expect(Object.keys(st.index.entries).sort())
      .toEqual(["Brian Jacques - Redwall.m4a", "J.R.R. Tolkien - The Hobbit"]);
    await opts.close();
    await page.close();
  });

  test("and the helper, given the same books, writes exactly the same", async () => {
    const page = await optionsPage(helperCtx);
    const r = await message(page, { type: "pull", ids: ["bk1", "bk3"] });
    expect(r.ok).toBe(true);
    await page.close();
    const opts = await optionsPage(folderCtx);
    const mine = await opfsState(opts, "player");
    const ref = dirState(helperDev);
    expect(mine.files).toEqual(ref.files);
    expect(entriesOf(mine.index)).toEqual(entriesOf(ref.index));
    await opts.close();
  });

  test("books only on the player are classified as the helper classifies them", async () => {
    const sideLoaded = {
      // The server has this one: matched by its tags, not offered for upload.
      "AUDIOBOOKS/Holes_rip.m4a": m4aWithTags("Holes", "Louis Sachar"),
      // The server has never heard of this one.
      "AUDIOBOOKS/Scruffy_rip.m4a": m4aWithTags("The Silmarillion", "J.R.R. Tolkien"),
      "AUDIOBOOKS/Clutter.db": Buffer.from("not a book"),
    };
    const opts = await optionsPage(folderCtx);
    for (const [rel, body] of Object.entries(sideLoaded)) {
      await opfsWrite(opts, "player", rel, body.toString("base64"));
      mkdirSync(dirname(join(helperDev, rel)), { recursive: true });
      writeFileSync(join(helperDev, rel), body);
    }
    const mine = await message(opts, { type: "status" });
    const ref = await message(await optionsPage(helperCtx), { type: "status" });
    expect(mine.ok && ref.ok).toBe(true);
    for (const k of ["both", "serverOnly", "deviceOnly", "onDeviceBytes"]) {
      expect(mine.data[k], k).toEqual(ref.data[k]);
    }
    expect(mine.data.deviceOnly.map((e) => e.name)).toEqual(["Scruffy_rip.m4a"]);
    expect(mine.data.both.map((b) => [b.name, b.matchedBy])).toEqual([
      ["Brian Jacques - Redwall.m4a", "id"], ["Holes_rip.m4a", "tags"],
      ["J.R.R. Tolkien - The Hobbit", "id"]]);
    // What Chrome cannot tell us is left empty rather than made up.
    expect(mine.data.free).toEqual({});

    const popup = await folderCtx.newPage();
    await popup.goto(POPUP);
    await expect(popup.locator("#n-device")).toHaveText("3", { timeout: 20_000 });
    await expect(popup.locator("#n-only")).toHaveText("1");
    await expect(popup.locator("#n-server")).toHaveText("");
    await expect(popup.locator("#free")).toHaveText("");
    await popup.close();
    await opts.close();
  });

  test("a book only on the player uploads, with the rename undone", async () => {
    const page = await folderCtx.newPage();
    await page.goto(POPUP);
    await expect(page.locator("#n-only")).toHaveText("1", { timeout: 20_000 });
    await page.locator('.tab[data-tab="only"]').click();
    const row = page.locator("#list li").first();
    await expect(row).toContainText("The Silmarillion");
    await expect(row).toContainText("J.R.R. Tolkien");
    await row.locator("input[type=checkbox]").check();
    await page.click("#act");
    await expect(page.locator("#status")).toContainText("uploaded 1", { timeout: 30_000 });
    expect(uploads).toEqual([{ names: ["Scruffy_rip.m4b"], title: "The Silmarillion",
                               author: "J.R.R. Tolkien", library: "lib1", folder: "fol1" }]);
    await page.close();
  });

  test("the library page shows badges from the folder, with no helper", async () => {
    const lib = await folderCtx.newPage();
    await lib.goto(`${absUrl}/library/main`);
    // Redwall and The Hobbit as pulled, Holes by the tags of a side-loaded file.
    await expect(lib.locator(".absh-badge.absh-on")).toHaveCount(3, { timeout: 20_000 });
    await expect(lib.locator(".absh-badge.absh-on", { hasText: "on device" })).toHaveCount(3);
    await expect(lib.locator("#absh-panel")).toContainText("1 on your device, not in this library");
    await expect(lib.locator("#absh-folder-notice")).toHaveCount(0);
    await lib.close();
  });

  test("removing deletes from the folder, and the index forgets it", async () => {
    const page = await folderCtx.newPage();
    await page.goto(POPUP);
    await expect(page.locator("#n-device")).toHaveText("3", { timeout: 20_000 });
    await page.locator('.tab[data-tab="device"]').click();
    await page.locator("#list li", { hasText: "The Hobbit" }).locator("input[type=checkbox]").check();
    await expect(page.locator("#act")).toContainText("Remove 1 from device");
    await page.click("#act");
    await expect(page.locator("#status")).toContainText("removed 1", { timeout: 20_000 });

    const opts = await optionsPage(folderCtx);
    const mine = await opfsState(opts, "player");
    expect(Object.keys(mine.files).filter((f) => f.includes("Hobbit"))).toEqual([]);
    expect(Object.keys(mine.index.entries)).not.toContain("J.R.R. Tolkien - The Hobbit");

    // The helper, asked the same, ends in the same place.
    const ref = await message(await optionsPage(helperCtx),
                              { type: "remove", names: ["J.R.R. Tolkien - The Hobbit"] });
    expect(ref.ok).toBe(true);
    expect(mine.files).toEqual(dirState(helperDev).files);
    expect(entriesOf(mine.index)).toEqual(entriesOf(dirState(helperDev).index));
    await opts.close();
  });

  test("the parity fixtures hold against Chrome's real filesystem too", async () => {
    // tests/js/folder.test.js runs these against an in-memory stand-in; this
    // is the same runner, in the extension's own page, on the origin private
    // file system - real handles, real writables, real DecompressionStream.
    const page = await optionsPage(folderCtx);
    await page.evaluate(RUNNER);
    const naming = fixture("naming.json");
    const device = fixture("device.json");
    const got = await page.evaluate(async ({ naming, device }) => {
      const F = globalThis.ABSH_FOLDER;
      const P = globalThis.PARITY;
      const opfs = await navigator.storage.getDirectory();
      let n = 0;
      const fresh = () => opfs.getDirectoryHandle(`parity-${n++}`, { create: true });
      const out = {};
      for (const c of naming.pull) out[`pull: ${c.name}`] = await P.pull(F, await fresh(), c);
      for (const c of naming.push) out[`push: ${c.name}`] = await P.push(F, await fresh(), c);
      for (const c of naming.remove) out[`remove: ${c.name}`] = await P.remove(F, await fresh(), c);
      for (const c of device.cases) out[`device: ${c.name}`] = await P.device(F, await fresh(), c, device.server);
      return out;
    }, { naming, device });
    const want = {};
    for (const c of naming.pull) want[`pull: ${c.name}`] = c.expect;
    for (const c of naming.push) want[`push: ${c.name}`] = c.expect;
    for (const c of naming.remove) want[`remove: ${c.name}`] = c.expect;
    for (const c of device.cases) want[`device: ${c.name}`] = c.expect;
    for (const k of Object.keys(want)) expect.soft(got[k], k).toEqual(want[k]);
    expect(Object.keys(got).length).toBe(Object.keys(want).length);
    await page.close();
  });
});

/* ===================================================================== */

test.describe("when Chrome has paused access to the folder", () => {
  test.skip(({ browserName }) => browserName !== "chromium", "Chromium only - Firefox has no folder picker");

  let srv, absUrl, ctx, real;

  test.beforeAll(async () => {
    ({ srv, absUrl } = await startAbs());
    ctx = await launch(makeProfile(absUrl));
    const page = await configure(ctx, absUrl);
    // A real directory, dropped onto the page: Chrome grants it read access
    // and not write, which is exactly where a chosen folder stands after a
    // restart. Measured, not simulated.
    real = mkdtempSync(join(tmpdir(), "absh-paused-"));
    mkdirSync(join(real, "AUDIOBOOKS"));
    await page.evaluate(() => {
      document.addEventListener("dragover", (e) => e.preventDefault());
      document.addEventListener("drop", (e) => {
        e.preventDefault();
        e.dataTransfer.items[0].getAsFileSystemHandle()
          .then((h) => ABSH_FOLDER.saveHandle(h).then(() => { window.__dropped = h.name; }));
      });
    });
    const cdp = await ctx.newCDPSession(page);
    const data = { items: [], files: [real], dragOperationsMask: 1 };
    for (const type of ["dragEnter", "dragOver", "drop"]) {
      await cdp.send("Input.dispatchDragEvent", { type, x: 200, y: 200, data });
    }
    await page.waitForFunction(() => window.__dropped, null, { timeout: 10_000 });
    expect(await page.evaluate(async () => {
      const h = await ABSH_FOLDER.loadHandle();
      return [await h.queryPermission({ mode: "read" }), await h.queryPermission({ mode: "readwrite" })];
    })).toEqual(["granted", "prompt"]);
    await page.close();
  });

  test.afterAll(async () => { await ctx?.close(); srv?.close(); });

  const folderName = () => real.split(sep).pop();

  test("the options page says so, and offers the one click that restores it", async () => {
    const page = await optionsPage(ctx);
    await expect(page.locator("#folderState")).toContainText(
      `Chrome has paused access to “${folderName()}”`);
    await expect(page.locator("#folderState")).toContainText("Click Allow access to carry on");
    await expect(page.locator("#folderAllow")).toBeVisible();
    await expect(page.locator("#folderAllow")).toHaveText(`Allow access to “${folderName()}”`);
    await expect(page.locator("#folderAllow")).toBeFocused();
    await page.close();
  });

  test("the popup says so, and sends you to Options rather than failing", async () => {
    const page = await ctx.newPage();
    await page.goto(POPUP);
    await expect(page.locator("#status")).toHaveText(
      `Chrome has paused access to the folder “${folderName()}”. Allow it again in Options.`,
      { timeout: 20_000 });
    await expect(page.locator("#status")).toHaveClass(/err/);
    await expect(page.locator("a", { hasText: "in Options" })).toBeVisible();
    // Nothing claims the player is empty.
    await expect(page.locator("#n-device")).toHaveText("");
    await page.close();
  });

  test("the library page says so, and its button opens Options", async () => {
    const lib = await ctx.newPage();
    await lib.goto(`${absUrl}/library/main`);
    const notice = lib.locator("#absh-folder-notice");
    await expect(notice).toContainText(`Chrome needs your OK again to use the folder “${folderName()}”`,
                                       { timeout: 20_000 });
    const [opts] = await Promise.all([
      ctx.waitForEvent("page", { predicate: (p) => p.url().startsWith(OPTIONS) }),
      notice.locator("button", { hasText: "Open Options" }).click(),
    ]);
    await expect(opts.locator("#folderAllow")).toBeVisible({ timeout: 15_000 });
    await opts.close();
    await lib.close();
  });

  test("a copy asked for anyway is refused with the reason, not half done", async () => {
    const page = await optionsPage(ctx);
    const r = await message(page, { type: "pull", ids: ["bk1"] });
    expect(r).toMatchObject({ ok: false, code: "folder-access" });
    expect(readdirSync(join(real, "AUDIOBOOKS"))).toEqual([]);
    await page.close();
  });
});

/* ===================================================================== */

test.describe("with the helper installed, the helper is used", () => {
  test.skip(({ browserName }) => browserName !== "chromium", "Chromium only - needs a native host");

  let srv, absUrl, ctx, dev;

  test.beforeAll(async () => {
    ({ srv, absUrl } = await startAbs());
    dev = join(mkdtempSync(join(tmpdir(), "absh-both-")), "player");
    mkdirSync(dev);
    ctx = await launch(makeProfile(absUrl, { helper: true }));
    const page = await configure(ctx, absUrl, dev);
    await useOpfsFolder(page, "unused");
    await page.close();
  });

  test.afterAll(async () => { await ctx?.close(); srv?.close(); });

  test("a pull goes through the helper to its device, not to the chosen folder", async () => {
    const page = await optionsPage(ctx);
    await expect(page.locator("#folderUse")).toHaveText(
      "The helper is installed and answering, so it is used, not this folder.", { timeout: 20_000 });
    const r = await message(page, { type: "pull", ids: ["bk1"] });
    expect(r.ok).toBe(true);
    expect(Object.keys(dirState(dev).files)).toEqual(["AUDIOBOOKS/Brian Jacques - Redwall.m4a"]);
    expect((await opfsState(page, "unused")).files).toEqual({});
    await page.close();

    const popup = await ctx.newPage();
    await popup.goto(POPUP);
    await expect(popup.locator("#status")).toContainText("helper ok", { timeout: 20_000 });
    await popup.close();
  });

  test("unless the user asks for the folder outright", async () => {
    const page = await optionsPage(ctx);
    await page.check("#folderAlways");
    await expect(page.locator("#folderUse")).toHaveText(
      "Books go to this folder, as you chose, even though the helper may be installed.");
    const r = await message(page, { type: "pull", ids: ["bk2"] });
    expect(r.ok).toBe(true);
    expect((await opfsState(page, "unused")).files)
      .toEqual({ "AUDIOBOOKS/Louis Sachar - Holes.m4a": 2048 });
    expect(Object.keys(dirState(dev).files)).toEqual(["AUDIOBOOKS/Brian Jacques - Redwall.m4a"]);
    await page.uncheck("#folderAlways");
    await page.close();
  });
});
