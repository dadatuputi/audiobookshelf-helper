/**
 * The in-page UI in Firefox, against a real Audiobookshelf.
 *
 * Firefox is where this add-on diverges most - background.scripts instead of a
 * service worker, a gecko id instead of one derived from a key - and until now
 * the only thing standing behind it was web-ext lint, which checks the
 * manifest and never loads the add-on. Every bug that reached a real machine
 * today was of a kind lint cannot see.
 *
 * Needs `node tests/real/setup.mjs` first; skips itself without state.json.
 */
import { test, expect, firefox } from "@playwright/test";
import { existsSync, mkdtempSync, mkdirSync, writeFileSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";
import net from "node:net";
import { installTemporaryAddon, seedGrantedPermissions, FIREFOX_PREFS } from "./firefox-addon.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const statePath = join(here, "state.json");
const HAVE = existsSync(statePath);
const state = HAVE ? JSON.parse(readFileSync(statePath, "utf8")) : null;

const identity = JSON.parse(execFileSync("python3", ["-c",
  "import sys, json; sys.path.insert(0, 'extension'); import identity; " +
  "d = identity.load(); print(json.dumps({'gecko': d['geckoId'], 'host': d['hostName']}))",
], { cwd: root }).toString());
const GECKO_ID = identity.gecko;
const HOST_NAME = identity.host;
const distFirefox = resolve(root, "extension/dist/firefox");

/* moz-extension:// is addressed by a per-profile UUID, not the add-on id, so
 * the options page is normally unreachable from a test. Pinning the UUID up
 * front through extensions.webextensions.uuids makes it addressable - which is
 * what lets this suite configure the add-on the way a user does, and inspect
 * what it registered when something goes wrong. */
const EXT_UUID = "8f2a1c34-5b6d-4e7f-9a0b-1c2d3e4f5a6b";

/** A free port for the debugger server, so parallel runs cannot collide. */
const freePort = () => new Promise((res, rej) => {
  const s = net.createServer();
  s.on("error", rej);
  s.listen(0, "127.0.0.1", () => { const { port } = s.address(); s.close(() => res(port)); });
});

test.describe("Firefox, against a real Audiobookshelf", () => {
  test.skip(!HAVE, "run: node tests/real/setup.mjs");
  test.skip(({ browserName }) => browserName !== "firefox", "firefox only");

  /** @type {import('@playwright/test').BrowserContext} */
  let ctx;
  let home;

  test.beforeAll(async () => {
    const profile = mkdtempSync(join(tmpdir(), "absh-ff-"));

    // Firefox reads native-messaging manifests only from fixed per-user paths -
    // there is no per-profile location, unlike Chromium's user-data-dir - so
    // point the browser's HOME at a throwaway directory and write it there.
    home = mkdtempSync(join(tmpdir(), "absh-ffhome-"));
    const nmDir = join(home, ".mozilla", "native-messaging-hosts");
    mkdirSync(nmDir, { recursive: true });
    writeFileSync(join(nmDir, `${HOST_NAME}.json`), JSON.stringify({
      name: HOST_NAME,
      description: "Audiobookshelf Helper native host (real-server test)",
      path: resolve(root, "native/absh_host.py"),
      type: "stdio",
      // Firefox keys on the add-on id; Chrome keys on the extension origin.
      allowed_extensions: [GECKO_ID],
    }, null, 2));

    seedGrantedPermissions(profile, GECKO_ID, {
      origins: [`${new URL(state.absUrl).origin}/*`],
    });

    const port = await freePort();
    ctx = await firefox.launchPersistentContext(profile, {
      headless: true,
      ...(process.env.ABSH_FIREFOX_PATH
        ? { executablePath: process.env.ABSH_FIREFOX_PATH } : {}),
      args: ["-start-debugger-server", String(port)],
      firefoxUserPrefs: {
        ...FIREFOX_PREFS,
        "extensions.webextensions.uuids": JSON.stringify({ [GECKO_ID]: EXT_UUID }),
      },
      env: { ...process.env, HOME: home },
    });

    await installTemporaryAddon(port, distFirefox);

    // Configure it exactly as a user would. Omitting this was the whole
    // failure the first time: with no server URL there is nothing to register
    // a content script for, so the add-on installed and then did nothing, and
    // every test timed out looking for a button that was never going to exist.
    const opt = await ctx.newPage();
    await opt.goto(`moz-extension://${EXT_UUID}/options.html`);
    await opt.fill("#absUrl", state.absUrl);
    await opt.fill("#apiKey", state.token);
    await opt.fill("#devicePath", state.device);
    await opt.click("#save");
    await opt.waitForTimeout(2000);
    await opt.close();
  });

  test.afterAll(async () => { await ctx?.close(); });

  async function libraryPage() {
    const page = await ctx.newPage();
    const url = `${state.absUrl}/library/${state.libraryId}`;
    await page.goto(url, { waitUntil: "networkidle" });
    const pw = page.locator('input[type="password"]');
    if (await pw.count()) {
      await page.fill('input[type="text"]', state.username);
      await pw.fill(state.password);
      await pw.press("Enter");
      await page.waitForURL(/\/library\//, { timeout: 30_000 }).catch(() => {});
      await page.goto(url, { waitUntil: "networkidle" });
    }
    await page.locator('[id^="book-card-"]').first()
      .waitFor({ state: "attached", timeout: 30_000 });
    return page;
  }

  test("the content script registers for the path the server is served under", async () => {
    // Checked before the UI tests so a failure here is unambiguous: if this
    // passes and the badges do not appear, the problem is in the page, not in
    // the add-on failing to load or being unconfigured.
    const page = await ctx.newPage();
    await page.goto(`moz-extension://${EXT_UUID}/options.html`);
    const scripts = await page.evaluate(() =>
      browser.scripting.getRegisteredContentScripts());
    expect(scripts.length).toBe(2);
    for (const sc of scripts) {
      expect(sc.matches).toEqual([`${state.absUrl}/library/*`]);
    }
    await page.close();
  });

  test("the toolbar button appears on the real library page", async () => {
    const page = await libraryPage();
    const btn = page.locator("#absh-sync-btn");
    await expect(btn).toBeVisible({ timeout: 30_000 });
    await expect(btn).toContainText("Sync to device");
    await page.close();
  });

  test("every real book card gets a badge", async () => {
    const page = await libraryPage();
    await expect(page.locator(".absh-badge").first())
      .toBeVisible({ timeout: 30_000 });
    const cards = await page.locator('[id^="book-card-"]').count();
    const badges = await page.locator(".absh-badge").count();
    expect(cards).toBeGreaterThan(0);
    expect(badges).toBe(cards);
    await page.close();
  });

  test("a badge is attached to the book it belongs to", async () => {
    const page = await libraryPage();
    await expect(page.locator(".absh-badge").first())
      .toBeVisible({ timeout: 30_000 });
    const pairs = await page.evaluate(() =>
      [...document.querySelectorAll('[id^="book-card-"]')].map((card) => ({
        title: (card.querySelector("img[alt]")?.getAttribute("alt") || "")
          .replace(/,\s*cover\s*$/i, ""),
        id: card.querySelector(".absh-badge")?.dataset?.abshId || null,
      })));
    expect(pairs.length).toBeGreaterThan(0);
    for (const p of pairs) expect(p.id, `no id for ${p.title}`).toBeTruthy();
    expect(new Set(pairs.map((p) => p.id)).size)
      .toBe(new Set(pairs.map((p) => p.title)).size);
    await page.close();
  });
});
