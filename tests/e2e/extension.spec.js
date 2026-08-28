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

/** A stand-in Audiobookshelf: just the two endpoints the extension calls. */
function startAbs() {
  return new Promise((res) => {
    const srv = createServer((req, rep) => {
      const send = (obj) => {
        rep.writeHead(200, { "Content-Type": "application/json" });
        rep.end(JSON.stringify(obj));
      };
      if (req.url.startsWith("/api/libraries/")) {
        return send({
          results: BOOKS.map((b) => ({
            id: b.id, relPath: b.relPath, size: 2048,
            media: { numTracks: 1, metadata: { title: b.title, authorName: b.author } }
          }))
        });
      }
      if (req.url.startsWith("/api/libraries")) {
        return send({ libraries: [{ id: "lib1", name: "Audiobooks", mediaType: "book" }] });
      }
      if (req.url.startsWith("/library/")) {
        rep.writeHead(200, { "Content-Type": "text/html" });
        return rep.end('<!doctype html><html><body><div id="app">' +
                       '<div id="toolbar" role="toolbar"></div></div></body></html>');
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
  await page.fill("#localRoot", lib);
  await page.selectOption("#sourceMode", "local");
  await page.click("#save");
  await expect(page.locator("#msg")).toHaveText("saved");
  return page;
}

function makeLibrary() {
  const base = mkdtempSync(join(tmpdir(), "absh-lib-"));
  const lib = join(base, "library");
  const dev = join(base, "device");
  mkdirSync(dev, { recursive: true });
  for (const b of BOOKS) {
    const d = join(lib, b.relPath);
    mkdirSync(d, { recursive: true });
    writeFileSync(join(d, `${b.title}.m4b`), Buffer.alloc(2048, 7));
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

  test("saved settings survive a reload", async () => {
    const page = await env.ctx.newPage();
    await page.goto(`chrome-extension://${EXT_ID}/options.html`);
    await expect(page.locator("#absUrl")).toHaveValue(env.absUrl);
    await expect(page.locator("#devicePath")).toHaveValue(env.dev);
    await expect(page.locator("#sourceMode")).toHaveValue("local");
    await expect(page.locator("#renameM4b")).toBeChecked();
    await page.close();
  });

  test("popup reaches the native host and lists the library", async () => {
    const page = await env.ctx.newPage();
    await page.goto(`chrome-extension://${EXT_ID}/popup.html`);

    // Proves the host was spawned by the browser and answered over stdio.
    await expect(page.locator("#status")).toContainText("helper ok", { timeout: 20_000 });

    await expect(page.locator("#library option")).toHaveCount(1);
    await expect(page.locator("#list li")).toHaveCount(BOOKS.length);
    await expect(page.locator("#list li").first()).toContainText("Redwall");
    await page.close();
  });

  test("syncing a book writes it to the device, renamed", async () => {
    const page = await env.ctx.newPage();
    await page.goto(`chrome-extension://${EXT_ID}/popup.html`);
    await expect(page.locator("#list li")).toHaveCount(BOOKS.length, { timeout: 20_000 });

    expect(deviceFiles(env.dev)).toEqual([]);

    // Tick Redwall only.
    const redwall = page.locator("#list li", { hasText: "Redwall" });
    await redwall.locator("input[type=checkbox]").check();
    await expect(page.locator("#sync")).toBeEnabled();
    await page.click("#sync");

    await expect(page.locator("#status")).toContainText("copied 1 file", { timeout: 30_000 });

    // The .m4b became .m4a on the way, which is the whole point of the tool.
    expect(deviceFiles(env.dev)).toEqual(["Brian Jacques - Redwall.m4a"]);
    expect(statSync(join(env.dev, "AUDIOBOOKS", "Brian Jacques - Redwall.m4a")).size).toBe(2048);
    await page.close();
  });

  test("the on-device shelf shows what is there, and the library marks it", async () => {
    const page = await env.ctx.newPage();
    await page.goto(`chrome-extension://${EXT_ID}/popup.html`);
    await expect(page.locator("#list li")).toHaveCount(BOOKS.length, { timeout: 20_000 });

    // Library row carries the annotation.
    await expect(page.locator("#list li", { hasText: "Redwall" }).locator(".chip.on"))
      .toHaveText("on device");
    await expect(page.locator("#list li", { hasText: "Holes" }).locator(".chip.on"))
      .toHaveCount(0);

    await page.click("#tab-device");
    await expect(page.locator("#device-list li")).toHaveCount(1);
    await expect(page.locator("#device-list li").first()).toContainText("Redwall");
    await expect(page.locator("#device-summary")).toContainText("1 book");
    await expect(page.locator("#device-count")).toHaveText("1");
    await page.close();
  });

  test("the toolbar button is registered for that server, and only that server", async () => {
    const page = await env.ctx.newPage();
    await page.goto(`chrome-extension://${EXT_ID}/options.html`);

    const scripts = await page.evaluate(() => chrome.scripting.getRegisteredContentScripts());
    expect(scripts).toHaveLength(1);
    expect(scripts[0].matches).toEqual([`${env.absUrl}/library/*`]);
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

  test("removing from the shelf deletes it from the device but not the library", async () => {
    const page = await env.ctx.newPage();
    await page.goto(`chrome-extension://${EXT_ID}/popup.html`);
    await expect(page.locator("#list li")).toHaveCount(BOOKS.length, { timeout: 20_000 });
    await page.click("#tab-device");

    const row = page.locator("#device-list li").first();
    const del = row.locator("button.danger");

    // First click arms, second confirms - one stray click must not delete.
    await del.click();
    await expect(del).toHaveText("Confirm?");
    expect(deviceFiles(env.dev)).toEqual(["Brian Jacques - Redwall.m4a"]);

    await del.click();
    await expect(page.locator("#status")).toContainText("removed", { timeout: 20_000 });

    expect(deviceFiles(env.dev)).toEqual([]);
    // The source library is untouched.
    expect(existsSync(join(env.lib, "Brian Jacques/Redwall/Redwall.m4b"))).toBe(true);

    await expect(page.locator("#device-list li")).toHaveCount(1);
    await expect(page.locator("#device-list li")).toContainText("No books");
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

  test("the popup explains what to do instead of failing obscurely", async () => {
    const page = await ctx.newPage();
    await page.goto(`chrome-extension://${EXT_ID}/popup.html`);
    await expect(page.locator("#status")).toContainText("Grant access", { timeout: 20_000 });
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
