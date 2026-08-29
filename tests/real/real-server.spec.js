/**
 * The in-page UI against a real Audiobookshelf.
 *
 * tests/e2e stubs the server, which is fine for the helper and the popup but
 * useless for the content script: a stub only renders what we already believe
 * the page renders. Both of the bugs that made badges never appear on a stock
 * install were invisible to it - the /personalized response shape, and card
 * ids repeating once there is more than one shelf.
 *
 * Run `node tests/real/setup.mjs` first. Without state.json these skip, so the
 * ordinary suite still runs anywhere.
 */
import { test, expect, chromium } from "@playwright/test";
import { existsSync, mkdtempSync, mkdirSync, writeFileSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const statePath = join(here, "state.json");
const HAVE = existsSync(statePath);
const state = HAVE ? JSON.parse(readFileSync(statePath, "utf8")) : null;

// identity.py owns the add-on id and host name; derive them from it rather
// than duplicating two strings that must never drift.
const identity = JSON.parse(execFileSync("python3", ["-c",
  "import sys, json; sys.path.insert(0, 'extension'); import identity; " +
  "d = identity.load(); " +
  "print(json.dumps({'ext': identity.chrome_ids()[0], 'host': d['hostName']}))",
], { cwd: root }).toString());
const EXT_ID = identity.ext;
const HOST_NAME = identity.host;
const distChrome = resolve(root, "extension/dist/chrome");

test.describe("against a real Audiobookshelf", () => {
  test.skip(!HAVE, "run: node tests/real/setup.mjs");
  test.skip(({ browserName }) => browserName !== "chromium",
            "chromium only - needs --load-extension");

  /** @type {import('@playwright/test').BrowserContext} */
  let ctx;

  test.beforeAll(async () => {
    const profile = mkdtempSync(join(tmpdir(), "absh-real-"));
    const nm = join(profile, "NativeMessagingHosts");
    mkdirSync(nm, { recursive: true });
    writeFileSync(join(nm, `${HOST_NAME}.json`), JSON.stringify({
      name: HOST_NAME,
      description: "Audiobookshelf Helper native host (real-server test)",
      path: resolve(root, "native/absh_host.py"),
      type: "stdio",
      allowed_origins: [`chrome-extension://${EXT_ID}/`],
    }, null, 2));

    // Headless Chromium draws no permission bubble, so permissions.request()
    // never resolves; seed the grant the way tests/e2e does.
    const pref = join(profile, "Default");
    mkdirSync(pref, { recursive: true });
    const origin = `${new URL(state.absUrl).origin}/*`;
    const perms = { api: [], explicit_host: [origin],
                    manifest_permissions: [], scriptable_host: [] };
    writeFileSync(join(pref, "Preferences"), JSON.stringify({
      extensions: { settings: { [EXT_ID]: {
        granted_permissions: perms, active_permissions: perms } } }
    }));

    ctx = await chromium.launchPersistentContext(profile, {
      headless: true,
      // `playwright install chromium` also brings a headless shell, and that
      // shell cannot load MV3 extensions - the service worker never appears
      // and every test times out waiting for it. Ask for the full build by
      // channel, exactly as tests/e2e does.
      ...(process.env.ABSH_CHROMIUM_PATH
        ? { executablePath: process.env.ABSH_CHROMIUM_PATH }
        : { channel: "chromium" }),
      args: [`--disable-extensions-except=${distChrome}`,
             `--load-extension=${distChrome}`],
    });
    if (!ctx.serviceWorkers().length) {
      await ctx.waitForEvent("serviceworker", { timeout: 30_000 });
    }

    // Configure exactly as a user would.
    const opt = await ctx.newPage();
    await opt.goto(`chrome-extension://${EXT_ID}/options.html`);
    await opt.fill("#absUrl", state.absUrl);
    await opt.fill("#apiKey", state.token);
    await opt.fill("#devicePath", state.device);
    await opt.click("#save");
    await opt.waitForTimeout(1500);
    await opt.close();
  });

  test.afterAll(async () => { await ctx?.close(); });

  /** Open the library, logging in first only if the session needs it.
   *  The context is shared, so after the first test the app is already
   *  authenticated and /login redirects away before any form exists. */
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
    // Settle before returning. The shelves render after their own fetch, the
    // page hook forwards the ids from that response, and only then does the
    // content script badge anything - so a card being attached is not yet the
    // state any of these tests mean to assert on.
    await page.locator('[id^="book-card-"]').first()
      .waitFor({ state: "attached", timeout: 30_000 });
    await page.locator(".absh-badge").first()
      .waitFor({ state: "attached", timeout: 30_000 });
    return page;
  }

  test("the content script registers for the path the server is served under", async () => {
    const page = await ctx.newPage();
    await page.goto(`chrome-extension://${EXT_ID}/options.html`);
    const scripts = await page.evaluate(() =>
      chrome.scripting.getRegisteredContentScripts());
    expect(scripts.length).toBe(2);
    // Audiobookshelf's own default is /audiobookshelf, so the pattern must
    // carry the path. Dropping it registered a pattern nothing ever matched.
    for (const sc of scripts) {
      expect(sc.matches).toEqual([`${state.absUrl}/library/*`]);
    }
    await page.close();
  });

  test("the toolbar button appears on the real library page", async () => {
    const page = await libraryPage();
    const btn = page.locator("#absh-sync-btn");
    await expect(btn).toBeVisible({ timeout: 20_000 });
    await expect(btn).toContainText("Sync to device");
    await page.close();
  });

  test("every real book card gets a badge", async () => {
    const page = await libraryPage();
    await expect(page.locator(".absh-badge").first())
      .toBeVisible({ timeout: 20_000 });

    const cards = await page.locator('[id^="book-card-"]').count();
    const badges = await page.locator(".absh-badge").count();
    expect(cards).toBeGreaterThan(0);
    expect(badges).toBe(cards);

    // Each badge offers an action rather than rendering empty.
    expect(await page.locator(".absh-badge .absh-mini").count()).toBe(badges);
    await page.close();
  });

  test("a badge is attached to the book it belongs to", async () => {
    // The mapping is the part that was wrong: card ids repeat across shelves,
    // so an index-based match silently badged the wrong book.
    const page = await libraryPage();
    await expect(page.locator(".absh-badge").first())
      .toBeVisible({ timeout: 20_000 });

    const pairs = await page.evaluate(() =>
      [...document.querySelectorAll('[id^="book-card-"]')].map((card) => {
        const img = card.querySelector("img[alt]");
        const badge = card.querySelector(".absh-badge");
        return {
          title: (img?.getAttribute("alt") || "").replace(/,\s*cover\s*$/i, ""),
          id: badge?.dataset?.abshId || null,
        };
      }));

    expect(pairs.length).toBeGreaterThan(0);
    for (const p of pairs) expect(p.id, `no id for ${p.title}`).toBeTruthy();
    // Distinct books must map to distinct items - the index bug produced
    // duplicates, because two shelves both start at book-card-0.
    const distinctTitles = new Set(pairs.map((p) => p.title));
    const distinctIds = new Set(pairs.map((p) => p.id));
    expect(distinctIds.size).toBe(distinctTitles.size);
    await page.close();
  });

  test("the popup lists the real library", async () => {
    const page = await ctx.newPage();
    await page.goto(`chrome-extension://${EXT_ID}/popup.html`);
    for (const title of state.titles) {
      await expect(page.locator("body")).toContainText(title, { timeout: 20_000 });
    }
    await page.close();
  });
});
