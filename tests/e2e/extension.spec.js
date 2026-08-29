/* The whole loop, in a real browser, against a real native host.
 *
 * Everything here was previously untested: the popup and options pages had
 * never been opened, the background message handlers had never run, and no
 * browser had ever spawned the native host. This drives all of it - options ->
 * grant -> pick a book -> sync -> see it on the device shelf -> delete it -
 * and asserts against the actual files on disk.
 *
 * Chromium only. Firefox cannot load an extension *and* register a native host
 * under Playwright; the Firefox load path is covered by content-script.spec.js.
 */
import { test, expect, chromium } from "@playwright/test";
import { createServer } from "node:http";
import {
  mkdtempSync, mkdirSync, writeFileSync, existsSync, readFileSync, readdirSync, statSync
} from "node:fs";
import { tmpdir } from "node:os";
import { resolve, join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { createHash } from "node:crypto";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const distChrome = resolve(root, "extension/dist/chrome");
const identity = JSON.parse(readFileSync(resolve(root, "extension/identity.json"), "utf8"));
const HOST_NAME = identity.hostName;

/* The id is derived from the pinned public key, so it is the same on every
 * machine - which is the only reason the host manifest can name it up front. */
const EXT_ID = chromeIdFromKey(identity.chromeKey);

function chromeIdFromKey(b64) {
  // Same mapping as extension/identity.py: sha256(key) -> first 32 hex -> a-p.
  const hex = createHash("sha256").update(Buffer.from(b64, "base64")).digest("hex").slice(0, 32);
  return [...hex].map((c) => String.fromCharCode(97 + parseInt(c, 16))).join("");
}

/* Some environments ship a prebuilt Chromium at a fixed path; CI installs the
 * revision Playwright expects and uses the channel instead. */
const LAUNCH = process.env.ABSH_CHROMIUM_PATH
  ? { executablePath: process.env.ABSH_CHROMIUM_PATH }
  : { channel: "chromium" };

const BOOKS = [
  { id: "bk1", title: "Redwall", author: "Brian Jacques", relPath: "Brian Jacques/Redwall" },
  { id: "bk2", title: "Holes", author: "Louis Sachar", relPath: "Louis Sachar/Holes" }
];

/** A stand-in Audiobookshelf. The helper now talks to this directly - the
 *  same client the CLI uses - so it needs the real endpoint surface. */
const UPLOADS = [];

function startAbs() {
  return new Promise((res) => {
    const srv = createServer((req, rep) => {
      const send = (obj) => {
        rep.writeHead(200, { "Content-Type": "application/json" });
        rep.end(JSON.stringify(obj));
      };
      if (req.method === "POST" && req.url.startsWith("/api/upload")) {
        const chunks = [];
        req.on("data", (c) => chunks.push(c));
        req.on("end", () => {
          const raw = Buffer.concat(chunks).toString("binary");
          const names = [...raw.matchAll(/filename="([^"]+)"/g)].map((m) => m[1]);
          const title = /name="title"\r\n\r\n([^\r]*)/.exec(raw);
          UPLOADS.push({ names, title: title && title[1] });
          send({ id: "li_new", ok: true });
        });
        return;
      }
      if (req.url.startsWith("/api/me")) return send({ username: "tester" });
      if (/\/api\/items\/[^/]+\/download/.test(req.url)) {
        const id = /\/api\/items\/([^/]+)\/download/.exec(req.url)[1];
        const book = BOOKS.find((b) => b.id === id);
        if (!book) { rep.writeHead(404); return rep.end("no"); }
        const tagged = m4aWithTags(book.title, book.author);
        const body = Buffer.concat([tagged, Buffer.alloc(2048 - tagged.length, 7)]);
        rep.writeHead(200, {
          "Content-Type": "audio/mp4",
          "Content-Disposition": `attachment; filename="${book.title}.m4b"`,
          "Content-Length": String(body.length),
        });
        return rep.end(body);
      }
      if (req.url.includes("/items")) {
        return send({
          results: BOOKS.map((b) => ({
            id: b.id, relPath: b.relPath, size: 2048,
            media: { numTracks: 1, metadata: { title: b.title, authorName: b.author } }
          }))
        });
      }
      if (req.url.startsWith("/api/libraries/")) {
        return send({ library: { id: "lib1", name: "Audiobooks",
                                 folders: [{ id: "fol1", fullPath: "/audiobooks" }] } });
      }
      if (req.url.startsWith("/api/libraries")) {
        return send({ libraries: [{ id: "lib1", name: "Audiobooks", mediaType: "book" }] });
      }
      if (req.url.startsWith("/library/")) {
        rep.writeHead(200, { "Content-Type": "text/html" });
        return rep.end('<!doctype html><html><body><div id="app">' +
                       '<div id="toolbar" role="toolbar"></div>' +
                       '<div id="book-card-0"></div><div id="book-card-1"></div>' +
                       '</div></body></html>');
      }
      rep.writeHead(404); rep.end("no");
    });
    srv.listen(0, "127.0.0.1", () => res({ srv, port: srv.address().port }));
  });
}

/** Profile with our real native host registered for this extension id.
 *
 * `grantOrigin` stands in for the user having pressed Grant access in an
 * earlier session: headless Chromium draws no permission bubble, so
 * permissions.request() never resolves there and the grant has to be seeded.
 * The un-granted first-run state is covered by its own context below.
 */
function makeProfile(grantOrigin) {
  const profile = mkdtempSync(join(tmpdir(), "absh-e2e-"));
  // Chromium looks under the user-data-dir, not ~/.config, when one is given.
  const dir = join(profile, "NativeMessagingHosts");
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, `${HOST_NAME}.json`), JSON.stringify({
    name: HOST_NAME,
    description: "Audiobookshelf Helper native host (test)",
    path: resolve(root, "native/absh_host.py"),
    type: "stdio",
    allowed_origins: [`chrome-extension://${EXT_ID}/`]
  }, null, 2));

  if (grantOrigin) {
    const pref = join(profile, "Default");
    mkdirSync(pref, { recursive: true });
    const perms = { api: [], explicit_host: [grantOrigin],
                    manifest_permissions: [], scriptable_host: [] };
    writeFileSync(join(pref, "Preferences"), JSON.stringify({
      extensions: { settings: { [EXT_ID]: {
        granted_permissions: perms, active_permissions: perms
      } } }
    }));
  }
  return profile;
}

async function launch(profile) {
  const ctx = await chromium.launchPersistentContext(profile, {
    ...LAUNCH,
    headless: true,
    args: [`--disable-extensions-except=${distChrome}`, `--load-extension=${distChrome}`]
  });
  // Wait for the background service worker so the first message is not a race.
  if (!ctx.serviceWorkers().length) {
    await ctx.waitForEvent("serviceworker", { timeout: 30_000 });
  }
  return ctx;
}

async function configure(ctx, { absUrl, dev, lib }) {
  const page = await ctx.newPage();
  await page.goto(`chrome-extension://${EXT_ID}/options.html`);
  await page.fill("#absUrl", absUrl);
  await page.fill("#apiKey", "test-key");
  await page.fill("#devicePath", dev);
  await page.click("#save");
  await expect(page.locator("#msg")).toHaveText("saved");
  return page;
}

/** A real MP4 atom tree with a metadata block, so the helper can read tags. */
function m4aWithTags(title, author) {
  const atom = (name, payload) => {
    const head = Buffer.alloc(8);
    head.writeUInt32BE(payload.length + 8, 0);
    head.write(name, 4, "latin1");
    return Buffer.concat([head, payload]);
  };
  const data = (text) => {
    const b = Buffer.from(text, "utf8");
    const pre = Buffer.alloc(8);
    pre.writeUInt32BE(1, 0);
    return atom("data", Buffer.concat([pre, b]));
  };
  const ilst = Buffer.concat([atom("\xa9nam", data(title)), atom("aART", data(author))]);
  const meta = atom("meta", Buffer.concat([Buffer.alloc(4), atom("ilst", ilst)]));
  return Buffer.concat([atom("ftyp", Buffer.from("M4A ")),
                        atom("moov", atom("udta", meta))]);
}

function makeLibrary() {
  const base = mkdtempSync(join(tmpdir(), "absh-lib-"));
  const lib = join(base, "library");
  const dev = join(base, "device");
  mkdirSync(dev, { recursive: true });
  for (const b of BOOKS) {
    const d = join(lib, b.relPath);
    mkdirSync(d, { recursive: true });
    // Padded so the size assertion stays meaningful.
    const tagged = m4aWithTags(b.title, b.author);
    writeFileSync(join(d, `${b.title}.m4b`),
                  Buffer.concat([tagged, Buffer.alloc(2048 - tagged.length, 7)]));
  }
  return { lib, dev };
}

function deviceFiles(dev) {
  const d = join(dev, "AUDIOBOOKS");
  return existsSync(d) ? readdirSync(d).sort() : [];
}

test.describe("full loop in a real browser", () => {
  test.skip(({ browserName }) => browserName !== "chromium",
            "chromium project only - needs --load-extension and a native host");

  /** @type {{ctx: import('@playwright/test').BrowserContext, srv: any, dev: string, lib: string}} */
  let env;

  test.beforeAll(async () => {
    const { srv, port } = await startAbs();
    const { lib, dev } = makeLibrary();
    const absUrl = `http://127.0.0.1:${port}`;
    // The runner has nothing removable mounted, so name the temp device as the
    // volume to consider. Chromium inherits this and passes it to the native
    // host it spawns.
    process.env.ABSH_DEVICE_ROOTS = dev;
    const ctx = await launch(makeProfile(`${absUrl}/*`));
    env = { ctx, srv, lib, dev, absUrl };
    const page = await configure(ctx, env);
    await page.close();
  });

  test.afterAll(async () => {
    await env?.ctx?.close();
    env?.srv?.close();
  });

  test("the options page reports the grant, scoped to one origin", async () => {
    const page = await env.ctx.newPage();
    await page.goto(`chrome-extension://${EXT_ID}/options.html`);

    await expect(page.locator("#permState")).toContainText("Access granted");
    await expect(page.locator("#grant")).toBeDisabled();

    // Exactly the one server - the shipped manifest asks for no host at all.
    const granted = await page.evaluate(() => chrome.permissions.getAll());
    expect(granted.origins).toEqual([`${env.absUrl}/*`]);
    expect(granted.origins).not.toContain("*://*/*");
    await page.close();
  });

  test("Detect finds the player so nobody types its path", async () => {
    const page = await env.ctx.newPage();
    await page.goto(`chrome-extension://${EXT_ID}/options.html`);
    await page.click("#detect");
    // Assert the device itself is offered, by path. "at least one option" passed
    // on any machine with a stray directory under /mnt while never once finding
    // the device it claimed to - which is how this read green locally and red
    // on a runner where /mnt is empty.
    await expect(page.locator(`#deviceList option[value="${env.dev}"]`))
      .toHaveCount(1, { timeout: 20_000 });
    await page.close();
  });

  test("saved settings survive a reload", async () => {
    const page = await env.ctx.newPage();
    await page.goto(`chrome-extension://${EXT_ID}/options.html`);
    await expect(page.locator("#absUrl")).toHaveValue(env.absUrl);
    await expect(page.locator("#devicePath")).toHaveValue(env.dev);
    await expect(page.locator("#renameM4b")).toBeChecked();
    await page.close();
  });

  test("popup reaches the native host and lists what can be pulled", async () => {
    const page = await env.ctx.newPage();
    await page.goto(`chrome-extension://${EXT_ID}/popup.html`);

    // Proves the host was spawned by the browser and answered over stdio.
    await expect(page.locator("#status")).toContainText("helper ok", { timeout: 20_000 });
    await expect(page.locator("#list li")).toHaveCount(BOOKS.length, { timeout: 20_000 });
    await expect(page.locator("#n-server")).toHaveText(String(BOOKS.length));
    await page.close();
  });

  test("pulling a book writes it to the device, renamed", async () => {
    const page = await env.ctx.newPage();
    await page.goto(`chrome-extension://${EXT_ID}/popup.html`);
    await expect(page.locator("#list li")).toHaveCount(BOOKS.length, { timeout: 20_000 });

    expect(deviceFiles(env.dev)).toEqual([]);

    await page.locator("#list li", { hasText: "Redwall" })
      .locator("input[type=checkbox]").check();
    await expect(page.locator("#act")).toBeEnabled();
    await expect(page.locator("#act")).toContainText("Copy 1 to device");
    await page.click("#act");

    await expect(page.locator("#status")).toContainText("copied 1 file", { timeout: 30_000 });

    // The .m4b became .m4a on the way, which is the whole point of the tool.
    expect(deviceFiles(env.dev)).toEqual(["Brian Jacques - Redwall.m4a"]);
    expect(statSync(join(env.dev, "AUDIOBOOKS", "Brian Jacques - Redwall.m4a")).size).toBe(2048);
    await page.close();
  });

  test("the pulled book moves from To pull to On device", async () => {
    const page = await env.ctx.newPage();
    await page.goto(`chrome-extension://${EXT_ID}/popup.html`);
    await expect(page.locator("#n-device")).toHaveText("1", { timeout: 20_000 });
    await expect(page.locator("#n-server")).toHaveText(String(BOOKS.length - 1));

    // The one still on the server only.
    await expect(page.locator("#list li")).toContainText("Holes");

    await page.locator('.tab[data-tab="device"]').click();
    await expect(page.locator("#list li")).toHaveCount(1);
    await expect(page.locator("#list li").first()).toContainText("Redwall");
    await page.close();
  });

  test("a book only on the device is offered for upload, and uploads", async () => {
    // Something the server has never heard of, with its own tags.
    writeFileSync(join(env.dev, "AUDIOBOOKS", "scruffy_rip.m4a"),
                  m4aWithTags("The Silmarillion", "J.R.R. Tolkien"));

    const page = await env.ctx.newPage();
    await page.goto(`chrome-extension://${EXT_ID}/popup.html`);
    await expect(page.locator("#n-only")).toHaveText("1", { timeout: 20_000 });

    await page.locator('.tab[data-tab="only"]').click();
    const row = page.locator("#list li").first();
    // Identified from its tags, not its filename.
    await expect(row).toContainText("The Silmarillion");
    await expect(row).toContainText("J.R.R. Tolkien");

    await row.locator("input[type=checkbox]").check();
    await expect(page.locator("#act")).toContainText("Upload 1 to server");
    await page.click("#act");
    await expect(page.locator("#status")).toContainText("uploaded 1", { timeout: 30_000 });

    // The rename is undone on the way back to the server.
    expect(UPLOADS.length).toBe(1);
    expect(UPLOADS[0].names).toEqual(["scruffy_rip.m4b"]);
    expect(UPLOADS[0].title).toBe("The Silmarillion");
    await page.close();
  });

  test("the toolbar button is registered for that server, and only that server", async () => {
    const page = await env.ctx.newPage();
    await page.goto(`chrome-extension://${EXT_ID}/options.html`);

    const scripts = await page.evaluate(() => chrome.scripting.getRegisteredContentScripts());
    // Two: the page-world hook that captures item ids, and the content script.
    expect(scripts).toHaveLength(2);
    for (const sc of scripts) {
      expect(sc.matches).toEqual([`${env.absUrl}/library/*`]);
    }
    expect(scripts.find((sc) => sc.world === "MAIN").js).toEqual(["page-hook.js"]);
    await page.close();

    // And it actually injects on a real page from that origin.
    const lib = await env.ctx.newPage();
    await lib.goto(`${env.absUrl}/library/main`);
    const btn = lib.locator("#absh-sync-btn");
    await expect(btn).toBeVisible({ timeout: 20_000 });
    await expect(btn).toContainText("Sync to device");
    expect(await btn.evaluate((el) => el.closest("#toolbar") !== null)).toBe(true);
    await lib.close();
  });

  test("removing deletes from the device but never from the library", async () => {
    const page = await env.ctx.newPage();
    await page.goto(`chrome-extension://${EXT_ID}/popup.html`);
    await expect(page.locator("#n-device")).toHaveText("1", { timeout: 20_000 });
    await page.locator('.tab[data-tab="device"]').click();

    await page.locator("#list li", { hasText: "Redwall" })
      .locator("input[type=checkbox]").check();
    await expect(page.locator("#act")).toContainText("Remove 1 from device");
    await page.click("#act");
    await expect(page.locator("#status")).toContainText("removed", { timeout: 20_000 });

    expect(deviceFiles(env.dev)).not.toContain("Brian Jacques - Redwall.m4a");
    // The source library is untouched.
    expect(existsSync(join(env.lib, "Brian Jacques/Redwall/Redwall.m4b"))).toBe(true);
    await page.close();
  });
});

/* A device that is not plugged in must say so. The host reports a refused
 * command as ok:false rather than by failing, so it is easy to render that as
 * "nothing on the device" - which is a lie, and the exact kind that sends
 * someone hunting through their player's folders. */
test.describe("when the device is not mounted", () => {
  test.skip(({ browserName }) => browserName !== "chromium",
            "chromium project only - needs --load-extension and a native host");

  let ctx, srv, absUrl, lib;

  test.beforeAll(async () => {
    ({ srv } = await startAbs());
    absUrl = `http://127.0.0.1:${srv.address().port}`;
    ({ lib } = makeLibrary());
    ctx = await launch(makeProfile(`${absUrl}/*`));
    await (await configure(ctx, {
      absUrl, lib, dev: join(tmpdir(), "absh-not-a-real-device-xyz")
    })).close();
  });

  test.afterAll(async () => { await ctx?.close(); srv?.close(); });

  test("the popup says the device is missing rather than showing it empty", async () => {
    const page = await ctx.newPage();
    await page.goto(`chrome-extension://${EXT_ID}/popup.html`);
    await expect(page.locator("#status")).toContainText("not mounted", { timeout: 20_000 });
    await expect(page.locator("#status")).toHaveClass(/err/);
    // And it must not claim an empty player.
    await expect(page.locator("#n-device")).toHaveText("");
    await page.close();
  });
});

/* First run, before the user has granted anything: the add-on has to explain
 * itself rather than fail with a bare network error. */
test.describe("before access is granted", () => {
  test.skip(({ browserName }) => browserName !== "chromium",
            "chromium project only - needs --load-extension");

  let ctx, srv, absUrl, dev, lib;

  test.beforeAll(async () => {
    ({ srv } = await startAbs());
    absUrl = `http://127.0.0.1:${srv.address().port}`;
    ({ lib, dev } = makeLibrary());
    ctx = await launch(makeProfile(null));      // no seeded grant
    await (await configure(ctx, { absUrl, dev, lib })).close();
  });

  test.afterAll(async () => { await ctx?.close(); srv?.close(); });

  test("the options page says access is missing and offers the button", async () => {
    const page = await ctx.newPage();
    await page.goto(`chrome-extension://${EXT_ID}/options.html`);
    await expect(page.locator("#permState")).toContainText("Not granted yet");
    await expect(page.locator("#permState")).toContainText(absUrl);
    await expect(page.locator("#grant")).toBeEnabled();
    await page.close();
  });

  test("the popup still works: the helper talks to the server, not the page", async () => {
    // Reading the library needs no browser permission at all now - the helper
    // holds the Audiobookshelf client. The grant is only for the in-page UI.
    const page = await ctx.newPage();
    await page.goto(`chrome-extension://${EXT_ID}/popup.html`);
    await expect(page.locator("#status")).toContainText("helper ok", { timeout: 20_000 });
    await expect(page.locator("#list li")).toHaveCount(BOOKS.length);
    await page.close();
  });

  test("no content script is registered without the grant", async () => {
    const page = await ctx.newPage();
    await page.goto(`chrome-extension://${EXT_ID}/options.html`);
    const scripts = await page.evaluate(() =>
      chrome.scripting.getRegisteredContentScripts().catch(() => []));
    expect(scripts).toEqual([]);
    await page.close();
  });
});
