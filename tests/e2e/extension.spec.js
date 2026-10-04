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
  mkdtempSync, mkdirSync, writeFileSync, existsSync, readFileSync, readdirSync, statSync,
  copyFileSync, chmodSync
} from "node:fs";
import { execFileSync } from "node:child_process";
import { tmpdir } from "node:os";
import { resolve, join, dirname, basename } from "node:path";
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

/* The options page and the popup now ask the helper about updates, and the
 * helper asks GitHub. No test here should depend on GitHub, or spend its rate
 * limit, so the helper's release feed is a refused local port unless a test
 * stands one up. Chromium passes this on to the helper it spawns. */
process.env.ABSH_UPDATE_API = "http://127.0.0.1:9";

const BOOKS = [
  { id: "bk1", title: "Redwall", author: "Brian Jacques", relPath: "Brian Jacques/Redwall" },
  { id: "bk2", title: "Holes", author: "Louis Sachar", relPath: "Louis Sachar/Holes" }
];

/** A stand-in Audiobookshelf. The helper now talks to this directly - the
 *  same client the CLI uses - so it needs the real endpoint surface. */
const UPLOADS = [];

/* Audiobookshelf behind a reverse proxy at a subpath - the deployment that
 * broke the in-page UI. Everything the stub serves lives under it. */
const BASE = "/audiobookshelf";

function startAbs() {
  return new Promise((res) => {
    const srv = createServer((req, rep) => {
      const send = (obj) => {
        rep.writeHead(200, { "Content-Type": "application/json" });
        rep.end(JSON.stringify(obj));
      };
      // Audiobookshelf is very often reverse-proxied under a subpath. Serving
      // the stub at the root hid a bug that made the whole in-page UI silently
      // absent for anyone deployed that way, so the stub lives under one now.
      if (!req.url.startsWith(BASE)) { rep.writeHead(404); return rep.end("no"); }
      req.url = req.url.slice(BASE.length) || "/";

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
      // What the real library page fetches. Returns a bare ARRAY of shelves,
      // each {id,label,type,entities,total} - not {results}. Verified against
      // Audiobookshelf 2.36.0.
      if (req.url.includes("/personalized")) {
        const entity = (b) => ({
          id: b.id, relPath: b.relPath, size: 2048,
          media: { numTracks: 1, metadata: { title: b.title, authorName: b.author } }
        });
        return send([
          { id: "recently-added", label: "Recently Added", type: "book",
            entities: [entity(BOOKS[0])], total: 1 },
          { id: "recent-series", label: "Recent Series", type: "book",
            entities: [entity(BOOKS[1])], total: 1 }
        ]);
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
        // The page fetches its own items, as Audiobookshelf does. That call is
        // the only source of item ids - the DOM never carries them - so a stub
        // that skipped it could never produce a badge, which is why the badge
        // path went uncovered for so long.
        const card = (b) =>
          `<div id="book-card-0"><img alt="${b.title}, Cover" src="${BASE}/placeholder.jpg"></div>`;
        return rep.end('<!doctype html><html><body><div id="app">' +
                       '<div id="toolbar" role="toolbar"></div>' +
                       // Two shelves, each with its own #book-card-0. Card ids
                       // are not unique on a real page; anything that maps a
                       // card by index is wrong here, which is the point.
                       `<div class="shelf">${card(BOOKS[0])}</div>` +
                       `<div class="shelf">${card(BOOKS[1])}</div>` +
                       '</div><script>' +
                       `fetch(${JSON.stringify(BASE)} + "/api/libraries/lib1/personalized")` +
                       '.then(r => r.json());' +
                       '</script></body></html>');
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
function makeProfile(grantOrigin, hostPath = resolve(root, "native/absh_host.py")) {
  const profile = mkdtempSync(join(tmpdir(), "absh-e2e-"));
  // Chromium looks under the user-data-dir, not ~/.config, when one is given.
  const dir = join(profile, "NativeMessagingHosts");
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, `${HOST_NAME}.json`), JSON.stringify({
    name: HOST_NAME,
    description: "Audiobookshelf Helper native host (test)",
    path: hostPath,
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
  // The options page fills its fields from storage asynchronously; typing
  // before that resolves used to have the values overwritten, and Save then
  // stored an empty server URL. permState carries text in every branch once
  // that pass is done, so it is the signal that the page is ready.
  await page.waitForFunction(
    () => (document.getElementById("permState")?.textContent || "") !== "",
    null, { timeout: 15_000 });
  await page.fill("#absUrl", absUrl);
  await page.fill("#apiKey", "test-key");
  await page.click("#save");
  await expect(page.locator("#msg")).toContainText("Saved URL and API key");
  // The player is chosen in the popup now; the popup tests below drive that.
  // Setup only needs it set, so it goes straight into storage.
  await page.evaluate((d) => chrome.storage.local.set({ devicePath: d }), dev);
  return page;
}

/** The popup, after its first pass: the player strip has a name in it. */
async function popupPage(ctx) {
  const page = await ctx.newPage();
  await page.goto(`chrome-extension://${EXT_ID}/popup.html`);
  await expect(page.locator("#playerName")).not.toHaveText("", { timeout: 20_000 });
  return page;
}

const stored = (page, key) =>
  page.evaluate((k) => chrome.storage.local.get(k).then((s) => s[k]), key);

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
    const absUrl = `http://127.0.0.1:${port}${BASE}`;
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
    expect(granted.origins).toEqual([`${new URL(env.absUrl).origin}/*`]);
    expect(granted.origins).not.toContain("*://*/*");
    await page.close();
  });

  test("the popup names the player and finds it with Detect", async () => {
    const page = await popupPage(env.ctx);
    await expect(page.locator("#playerName")).toHaveText(basename(env.dev));
    await expect(page.locator("#playerName")).not.toHaveClass(/missing/);
    await expect(page.locator("#playerPanel")).toBeHidden();
    await page.click("#playerToggle");
    // Assert the device itself is offered, by path. "at least one option" passed
    // on any machine with a stray directory under /mnt while never once finding
    // the device it claimed to - which is how this read green locally and red
    // on a runner where /mnt is empty.
    await expect(page.locator(`.player-pick[title="${env.dev}"]`))
      .toHaveCount(1, { timeout: 20_000 });
    await expect(page.locator(`.player-pick[title="${env.dev}"]`)).toHaveClass(/current/);
    await page.close();
  });

  test("the options page keeps the server; the popup keeps the player", async () => {
    const page = await env.ctx.newPage();
    await page.goto(`chrome-extension://${EXT_ID}/options.html`);
    await expect(page.locator("#absUrl")).toHaveValue(env.absUrl);
    await expect(page.locator("#renameM4b")).toBeChecked();
    // The player is not a setting on this page any more.
    await expect(page.locator("#devicePath")).toHaveCount(0);
    await expect(page.locator("#detect")).toHaveCount(0);
    await page.close();
    const pop = await popupPage(env.ctx);
    await expect(pop.locator("#devicePath")).toHaveValue(env.dev);
    await pop.close();
  });

  test("a player typed into the popup is kept even if it closes straight away", async () => {
    // A popup closes the moment it loses focus, so there is no Save button to
    // forget: what is typed is stored as it is typed.
    const page = await popupPage(env.ctx);
    await page.click("#playerToggle");
    const elsewhere = join(tmpdir(), "absh-typed-player");
    try {
      await page.fill("#devicePath", elsewhere);
      await expect.poll(() => stored(page, "devicePath"), { timeout: 5_000 }).toBe(elsewhere);
      await page.close();
      // Reopened, it is still the player on record: the card names it as the
      // one that isn't plugged in, and the suggestion beside it is only that.
      const again = await popupPage(env.ctx);
      await expect(again.locator("#playerWas")).toHaveText(basename(elsewhere),
                                                           { timeout: 20_000 });
      expect(await stored(again, "devicePath")).toBe(elsewhere);
      await again.close();
    } finally {
      const fix = await env.ctx.newPage();
      await fix.goto(`chrome-extension://${EXT_ID}/popup.html`);
      await fix.evaluate((d) => chrome.storage.local.set({ devicePath: d }), env.dev);
      await fix.close();
    }
  });

  test("when the saved player is not plugged in, the popup offers the one that is", async () => {
    const page0 = await env.ctx.newPage();
    await page0.goto(`chrome-extension://${EXT_ID}/popup.html`);
    await page0.evaluate((d) => chrome.storage.local.set({ devicePath: d }),
                         join(tmpdir(), "absh-unplugged-player"));
    await page0.close();
    const page = await popupPage(env.ctx);
    try {
      await expect(page.locator("#playerPanel")).toBeVisible({ timeout: 20_000 });
      await expect(page.locator("#playerName")).toHaveClass(/missing/);
      await expect(page.locator("#playerNote")).toContainText("isn't plugged in");
      // The one plugged in is offered, in the machine's colour - offered, not
      // taken: nothing is saved until Use.
      const pick = page.locator(`.player-pick[title="${env.dev}"]`);
      await expect(pick).toHaveClass(/guess/, { timeout: 20_000 });
      await expect(page.locator("#devicePath")).toHaveValue(env.dev);
      await expect(page.locator("#devicePath")).toHaveClass(/machine/);
      expect(await stored(page, "devicePath")).not.toBe(env.dev);
      // Choosing it is yours, and Use puts it back, with the shelf.
      await pick.click();
      await expect(pick).toHaveClass(/picked/);
      await expect(page.locator("#devicePath")).not.toHaveClass(/machine/);
      await expect(page.locator("#use")).toContainText(`Use ${basename(env.dev)}`);
      await page.click("#use");
      await expect.poll(() => stored(page, "devicePath")).toBe(env.dev);
      await expect(page.locator("#playerPanel")).toBeHidden();
      await expect(page.locator("#playerName")).toHaveText(basename(env.dev));
      await expect(page.locator("#n-server")).not.toHaveText("", { timeout: 20_000 });
    } finally {
      await page.evaluate((d) => chrome.storage.local.set({ devicePath: d }), env.dev);
      await page.close();
    }
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
    await expect(page.locator("#act")).toContainText(`Copy 1 to ${basename(env.dev)} →`);
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
    await expect(page.locator("#act")).toContainText(`← Upload 1 to ${new URL(env.absUrl).host}`);
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

  test("book cards get a badge saying whether the book is on the device", async () => {
    // Never asserted before this: the stubs carried #book-card- divs, but the
    // only thing checked was the toolbar button, so the badges could be - and
    // were - absent on a real server with nobody the wiser.
    const lib = await env.ctx.newPage();
    await lib.goto(`${env.absUrl}/library/main`);
    const badges = lib.locator(".absh-badge");
    await expect(badges.first()).toBeVisible({ timeout: 20_000 });
    expect(await badges.count()).toBe(2);       // one per card on the stub page
    // Each badge offers an action: copy it over, or take it off.
    const buttons = lib.locator(".absh-badge .absh-mini");
    expect(await buttons.count()).toBeGreaterThan(0);
    await lib.close();
  });

  test("removing deletes from the device but never from the library", async () => {
    const page = await env.ctx.newPage();
    await page.goto(`chrome-extension://${EXT_ID}/popup.html`);
    await expect(page.locator("#n-device")).toHaveText("1", { timeout: 20_000 });
    await page.locator('.tab[data-tab="device"]').click();

    await page.locator("#list li", { hasText: "Redwall" })
      .locator("input[type=checkbox]").check();
    await expect(page.locator("#act")).toContainText(`Remove 1 from ${basename(env.dev)}`);
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
    absUrl = `http://127.0.0.1:${srv.address().port}${BASE}`;
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
    // It opens the player strip on it, saying so, so picking another is a click.
    await expect(page.locator("#playerPanel")).toBeVisible({ timeout: 20_000 });
    await expect(page.locator("#playerNote")).toContainText("isn't plugged in");
    await expect(page.locator("#playerNote")).toHaveClass(/err/);
    await expect(page.locator("#playerName")).toHaveClass(/missing/);
    // And it must not claim an empty player - or claim anything about one:
    // the shelf below used to say everything was already on the device.
    await expect(page.locator("#n-device")).toHaveText("");
    await expect(page.locator("#list")).toHaveText("Choose your player to see what is on it.");
    await page.close();
  });
});

/* The whole point of the mount watcher: plug the player in and the page that is
 * already open catches up on its own. */
test.describe("when the player is plugged in while the page is open", () => {
  test.skip(({ browserName }) => browserName !== "chromium",
            "chromium project only - needs --load-extension and a native host");

  let ctx, srv, absUrl, lib, dev;

  test.beforeAll(async () => {
    ({ srv } = await startAbs());
    absUrl = `http://127.0.0.1:${srv.address().port}${BASE}`;
    ({ lib } = makeLibrary());
    // A device that is not there yet. The helper watches for it, so this is
    // also what it is told to look at - see absh/mounts.py on why a named root
    // is watched by looking rather than through the mount table.
    dev = join(mkdtempSync(join(tmpdir(), "absh-unplugged-")), "player");
    process.env.ABSH_DEVICE_ROOTS = dev;
    ctx = await launch(makeProfile(`${absUrl}/*`));
    await (await configure(ctx, { absUrl, lib, dev })).close();
  });

  test.afterAll(async () => {
    await ctx?.close();
    srv?.close();
    delete process.env.ABSH_DEVICE_ROOTS;
  });

  test("the page catches up on its own, with no reload and no minute-long wait",
       async () => {
    const page = await ctx.newPage();
    await page.goto(`${absUrl}/library/lib1`);
    // Badges appear either way; with no device they cannot say anything about
    // one, which is what the quiet badge is.
    await expect(page.locator(".absh-badge").first())
      .toBeVisible({ timeout: 20_000 });
    await expect(page.locator(".absh-badge .absh-mini")).toHaveCount(0);

    // Plug it in. Nothing touches the page, and nothing asks it to look again.
    mkdirSync(join(dev, "AUDIOBOOKS"), { recursive: true });

    // The backstop is five minutes away, so arriving inside twenty seconds can
    // only be the event.
    await expect(page.locator(".absh-badge .absh-mini").first())
      .toBeVisible({ timeout: 20_000 });
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
    absUrl = `http://127.0.0.1:${srv.address().port}${BASE}`;
    ({ lib, dev } = makeLibrary());
    ctx = await launch(makeProfile(null));      // no seeded grant
    await (await configure(ctx, { absUrl, dev, lib })).close();
  });

  test.afterAll(async () => { await ctx?.close(); srv?.close(); });

  test("the options page says access is missing and offers the button", async () => {
    const page = await ctx.newPage();
    await page.goto(`chrome-extension://${EXT_ID}/options.html`);
    await expect(page.locator("#permState")).toContainText("Not granted yet");
    await expect(page.locator("#permState")).toContainText(new URL(absUrl).origin);
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

  test("a saved naming template is what the page shows next time", async () => {
    // It carries a default in the markup, and the page used to fill only
    // empty fields from storage - so it showed the default on every load, and
    // the next Save wrote it back over what the user had chosen.
    const page = await ctx.newPage();
    await page.goto(`chrome-extension://${EXT_ID}/options.html`);
    await page.waitForFunction(
      () => (document.getElementById("permState")?.textContent || "") !== "",
      null, { timeout: 15_000 });
    // Naming works, so its row starts closed.
    await expect(page.locator("#sec-naming")).toHaveAttribute("data-state", "ok");
    await expect(page.locator("#folderTemplate")).toBeHidden();
    await page.click("#sec-naming .sec-head");
    await page.fill("#folderTemplate", "{title}");
    await page.click("#saveNaming");
    // Saying what changed, not just that something did.
    await expect(page.locator("#msgNaming")).toHaveText("Saved folder template.");
    await expect(page.locator("#sum-naming")).toHaveText("{title} · .m4b → .m4a");

    await page.reload();
    await expect(page.locator("#folderTemplate")).toHaveValue("{title}");
    await page.close();
  });

  test("the folder on the player is set in the popup and kept", async () => {
    const page = await popupPage(ctx);
    if (await page.locator("#playerPanel").isHidden()) await page.click("#playerToggle");
    await page.fill("#subdir", "BOOKS");
    await expect.poll(() => stored(page, "subdir"), { timeout: 5_000 }).toBe("BOOKS");
    await page.close();
    const again = await popupPage(ctx);
    await expect(again.locator("#subdir")).toHaveValue("BOOKS");
    await again.close();
  });
});

/* ------------------------------------------------------------ helper updates
 *
 * A stand-in for GitHub's release endpoints - as much as the helper reads -
 * so what the options page shows is decided here rather than by whatever was
 * last published. Counts its requests, so a test can say what a visit costs.
 *
 * A release given a `dir` serves every file in it - the archive, SHA256SUMS
 * and its signature, as signedRelease() writes them. One without carries only
 * a placeholder archive, for a release that is looked at and never installed. */
const REPO = /^REPO = "([^"]+)"/m.exec(readFileSync(resolve(root, "absh/update.py"), "utf8"))[1];
const ASSET = (tag) => `audiobookshelf-helper-native-${tag.replace(/^v/, "")}.zip`;

function startFeed(releases) {
  const requests = [];
  const filesOf = (r) => (r.dir ? readdirSync(r.dir) : [ASSET(r.tag)]);
  return new Promise((res) => {
    const srv = createServer((req, rep) => {
      requests.push(req.url);
      const json = (obj) => {
        rep.writeHead(200, { "Content-Type": "application/json" });
        rep.end(JSON.stringify(obj));
      };
      const host = req.headers.host;
      const release = (r) => ({
        tag_name: r.tag, prerelease: r.prerelease, draft: false,
        assets: filesOf(r).map((name) => ({
          name, browser_download_url: `http://${host}/dl/${r.tag}/${name}`
        }))
      });
      const path = req.url.split("?")[0];
      const base = `/repos/${REPO}/releases`;
      if (path === base) return json(releases.map(release));
      if (path.startsWith(`${base}/tags/`)) {
        const r = releases.find((x) => x.tag === path.slice(`${base}/tags/`.length));
        if (r) return json(release(r));
      }
      const [, tag, name] = /^\/dl\/([^/]+)\/([^/]+)$/.exec(path) || [];
      const r = releases.find((x) => x.tag === tag && x.dir);
      if (r && filesOf(r).includes(name)) {
        const body = readFileSync(join(r.dir, name));
        rep.writeHead(200, { "Content-Type": "application/octet-stream",
                             "Content-Length": String(body.length) });
        return rep.end(body);
      }
      rep.writeHead(404); rep.end("no");
    });
    srv.listen(0, "127.0.0.1", () => res({
      srv, requests, api: `http://127.0.0.1:${srv.address().port}`,
      listings: () => requests.filter((u) => u.split("?")[0].endsWith("/releases")).length
    }));
  });
}

/** A signed release in `dir`, made with the Python suite's own helpers and a
 *  throwaway key, and the KEYS line an installation needs to trust it. The
 *  key never leaves the one python process that makes and uses it. */
function signedRelease(dir, tag) {
  mkdirSync(dir, { recursive: true });
  const out = execFileSync("python3", ["-c", `
import json, sys
from pathlib import Path
sys.path[:0] = ["tests/python", "."]
import test_update as T
from absh import signing
out, tag = Path(sys.argv[1]), sys.argv[2]
blob = T.build_native_zip(out / "build.zip", tag.lstrip("v")).read_bytes()
(out / "build.zip").unlink()
name = T.native_name(tag)
(out / name).write_bytes(blob)
manifest = signing.make_manifest(tag, {name: T.sha(blob)})
(out / signing.MANIFEST_NAME).write_bytes(manifest)
(out / signing.SIGNATURE_NAME).write_bytes(
    signing.sign_manifest((T.TRUSTED,), manifest).encode())
print(json.dumps(T.pin_line(T.TRUSTED)))
`, dir, tag], { cwd: root, encoding: "utf8" });
  return `KEYS = ${out.trim()}\n`;
}

/** What the installation at `dir` says about why it cannot update itself -
 *  asked of absh itself, so the test compares against the real sentence. */
function refuseReason(dir) {
  return execFileSync("python3", ["-c",
    "import sys; from pathlib import Path; from absh import update\n" +
    "print(update.refuse_reason(Path(sys.argv[1])))", dir],
  { cwd: root, encoding: "utf8" }).trim();
}

/** Set environment for the helpers a browser will spawn, and put it back. */
function withEnv(vars) {
  const saved = Object.fromEntries(Object.keys(vars).map((k) => [k, process.env[k]]));
  const apply = (vals) => {
    for (const [k, v] of Object.entries(vals)) {
      if (v === undefined) delete process.env[k]; else process.env[k] = v;
    }
  };
  apply(vars);
  return () => apply(saved);
}

async function optionsPage(ctx) {
  const page = await ctx.newPage();
  await page.goto(`chrome-extension://${EXT_ID}/options.html`);
  return page;
}

/* This repository is a git checkout, so the helper every other test runs is
 * one that must not update itself - the state every developer is in. */
test.describe("the helper's version, from a checkout", () => {
  test.skip(({ browserName }) => browserName !== "chromium",
            "chromium project only - needs --load-extension and a native host");

  let ctx, srv, feed, restoreEnv;

  test.beforeAll(async () => {
    ({ srv } = await startAbs());
    const absUrl = `http://127.0.0.1:${srv.address().port}${BASE}`;
    const { lib, dev } = makeLibrary();
    feed = await startFeed([{ tag: "v1.0.0-alpha.9", prerelease: true }]);
    // A named device root is watched by looking, on every platform - see
    // mounts.is_polling - which makes "this helper polls" deterministic here.
    restoreEnv = withEnv({ ABSH_UPDATE_API: feed.api, ABSH_DEVICE_ROOTS: dev });
    ctx = await launch(makeProfile(`${absUrl}/*`));
    await (await configure(ctx, { absUrl, dev, lib })).close();
  });

  test.afterAll(async () => {
    await ctx?.close();
    srv?.close();
    feed?.srv.close();
    restoreEnv?.();
  });

  test("the options page shows the version, and why it cannot update itself", async () => {
    const page = await optionsPage(ctx);
    await expect(page.locator("#helperVersion")).toHaveText("Helper version dev",
                                                            { timeout: 20_000 });
    // The helper's own reason, and what the latest release is regardless.
    await expect(page.locator("#updateState")).toContainText("git checkout");
    await expect(page.locator("#updateState")).toContainText("v1.0.0-alpha.9");
    // No button that could only fail.
    await expect(page.locator("#sum-helper")).toHaveText("dev · can't update itself");
    await page.click("#sec-helper .sec-head");
    await expect(page.locator("#update")).toBeHidden();
    await expect(page.locator("#checkUpdate")).toBeVisible();
    await page.close();
  });

  test("asks the release feed once a day, not once a visit", async () => {
    // configure() opened the page once already; that was the day's check.
    await expect.poll(() => feed.listings(), { timeout: 20_000 }).toBe(1);
    for (let i = 0; i < 2; i++) {
      const page = await optionsPage(ctx);
      await expect(page.locator("#updateState")).toContainText("v1.0.0-alpha.9",
                                                               { timeout: 20_000 });
      await page.close();
    }
    expect(feed.listings()).toBe(1);

    // Asking is always allowed.
    const page = await optionsPage(ctx);
    await expect(page.locator("#checkUpdate")).toBeEnabled({ timeout: 20_000 });
    await page.click("#sec-helper .sec-head");
    await page.click("#checkUpdate");
    await expect.poll(() => feed.listings(), { timeout: 20_000 }).toBe(2);
    await expect(page.locator("#checkedAt")).toContainText("Checked");
    await page.close();
  });

  test("says a newly plugged-in player can take a moment, when the helper polls", async () => {
    const page = await popupPage(ctx);
    await page.click("#playerToggle");
    await expect(page.locator("#pollNote")).toBeVisible({ timeout: 20_000 });
    await expect(page.locator("#pollNote")).toContainText("couple of seconds");
    await page.close();
  });
});

/* The whole update, through the browser: a real installed copy, a release
 * feed offering a newer one, the button, and then proof that the helper
 * answering afterwards is the new one - which only a fresh process can be,
 * since the old one holds the old code in memory. */
test.describe("updating an installed helper from the options page", () => {
  test.skip(({ browserName }) => browserName !== "chromium",
            "chromium project only - needs --load-extension and a native host");

  let ctx, srv, feed, restoreEnv, install, pinned;

  test.beforeAll(async () => {
    ({ srv } = await startAbs());
    const absUrl = `http://127.0.0.1:${srv.address().port}${BASE}`;
    const { lib, dev } = makeLibrary();

    // The newer release, signed with a throwaway key...
    const base = mkdtempSync(join(tmpdir(), "absh-installed-"));
    const releaseDir = join(base, "v1.0.0-alpha.2");
    pinned = signedRelease(releaseDir, "v1.0.0-alpha.2");

    // ...and an installation as install.py leaves one, trusting that key: the
    // package beside the host script, stamped with the release it came from.
    install = join(base, "install");
    mkdirSync(join(install, "absh"), { recursive: true });
    copyFileSync(resolve(root, "native/absh_host.py"), join(install, "absh_host.py"));
    for (const f of readdirSync(resolve(root, "absh")).filter((n) => n.endsWith(".py"))) {
      copyFileSync(resolve(root, "absh", f), join(install, "absh", f));
    }
    writeFileSync(join(install, "absh", "version.py"),
                  'RELEASE = "1.0.0-alpha.1"\n\n\ndef release():\n    return RELEASE\n\n\n' +
                  'def is_release():\n    return RELEASE != "dev"\n');
    writeFileSync(join(install, "absh", "release_keys.py"), pinned);

    // Registered the way install.py registers one: the browser starts a
    // launcher that execs an absolute interpreter on the script.
    const python = execFileSync("python3", ["-c", "import sys; print(sys.executable)"],
                                { encoding: "utf8" }).trim();
    const launcher = join(base, "absh_host.sh");
    writeFileSync(launcher, `#!/bin/sh\nexec "${python}" "${join(install, "absh_host.py")}" "$@"\n`);
    chmodSync(launcher, 0o755);

    feed = await startFeed([{ tag: "v1.0.0-alpha.2", prerelease: true, dir: releaseDir },
                            { tag: "v1.0.0-alpha.1", prerelease: true }]);
    // No named device root: on Linux the helper is then told about mounts by
    // the kernel, so the polling note must stay away.
    restoreEnv = withEnv({ ABSH_UPDATE_API: feed.api, ABSH_DEVICE_ROOTS: undefined });
    ctx = await launch(makeProfile(`${absUrl}/*`, launcher));
    await (await configure(ctx, { absUrl, dev, lib })).close();
  });

  test.afterAll(async () => {
    await ctx?.close();
    srv?.close();
    feed?.srv.close();
    restoreEnv?.();
  });

  test("the popup mentions it in one line", async () => {
    const page = await ctx.newPage();
    await page.goto(`chrome-extension://${EXT_ID}/popup.html`);
    await expect(page.locator("#updateHint"))
      .toHaveText("Helper v1.0.0-alpha.2 is available - update it in Options",
                  { timeout: 20_000 });
    await page.close();
  });

  test("a copy that pins no signing key says so, in the helper's words, with no button",
       async () => {
    // What every copy built before the maintainer pins a key will show.
    const keys = join(install, "absh", "release_keys.py");
    writeFileSync(keys, "KEYS = []\n");
    try {
      const why = refuseReason(install);
      expect(why).toContain("pins no release-signing key");
      const page = await optionsPage(ctx);
      await expect(page.locator("#updateState"))
        .toHaveText(`v1.0.0-alpha.2 (prerelease) is available, but it can't be installed ` +
                    `from here: ${why}`, { timeout: 20_000 });
      await expect(page.locator("#update")).toBeHidden();
      await page.close();
    } finally {
      writeFileSync(keys, pinned);
    }
  });

  test("the options page offers it, installs it, and the new helper answers", async () => {
    const page = await optionsPage(ctx);
    await expect(page.locator("#helperVersion")).toHaveText("Helper version 1.0.0-alpha.1",
                                                            { timeout: 20_000 });
    await expect(page.locator("#updateState"))
      .toContainText("v1.0.0-alpha.2 (prerelease) is available", { timeout: 20_000 });
    if (process.platform === "linux") await expect(page.locator("#pollNote")).toBeHidden();

    const btn = page.locator("#update");
    await expect(btn).toHaveText("Update to v1.0.0-alpha.2");
    await btn.click();

    await expect(page.locator("#updateSteps li").first()).toContainText("signed by trusted key");
    await expect(page.locator("#updateSteps")).toContainText("downloading");
    await expect(page.locator("#updateState"))
      .toContainText("Updated to v1.0.0-alpha.2", { timeout: 60_000 });
    // Reported by a helper started after the swap: the old process could only
    // ever have said alpha.1.
    await expect(page.locator("#helperVersion")).toHaveText("Helper version 1.0.0-alpha.2");
    await expect(page.locator("#update")).toBeHidden();
    expect(readFileSync(join(install, "absh", "version.py"), "utf8"))
      .toContain('"1.0.0-alpha.2"');

    // And the rest of the extension carries on against the new helper.
    const popup = await ctx.newPage();
    await popup.goto(`chrome-extension://${EXT_ID}/popup.html`);
    await expect(popup.locator("#status")).toContainText("helper ok (1.0.0-alpha.2",
                                                         { timeout: 20_000 });
    await expect(popup.locator("#updateHint")).toBeHidden();
    await popup.close();
    await page.close();
  });
});
