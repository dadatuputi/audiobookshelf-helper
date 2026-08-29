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
import { copyFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";
import { cardFor, deviceFiles, until } from "./shared.mjs";

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
    await page.goto(url, { waitUntil: "domcontentloaded" });

    // The app may redirect to /login and render the form a moment later.
    // Sampling once for a password field raced that: on a slow load the field
    // was not there yet, login was skipped, and the run then waited out its
    // timeout for book cards on the login page. Wait for whichever arrives.
    await page.locator('input[type="password"], [id^="book-card-"]').first()
      .waitFor({ state: "attached", timeout: 45_000 });

    if (await page.locator('input[type="password"]').count()) {
      await page.fill('input[type="text"]', state.username);
      await page.fill('input[type="password"]', state.password);
      await page.press('input[type="password"]', "Enter");
      await page.waitForURL(/\/library\//, { timeout: 45_000 }).catch(() => {});
      await page.goto(url, { waitUntil: "domcontentloaded" });
    }

    await page.locator('[id^="book-card-"]').first()
      .waitFor({ state: "attached", timeout: 45_000 });
    // An actionable badge, not merely a badge: a card whose book is not yet
    // identified carries a quiet "?" placeholder, and waiting on that would
    // return before the mapping has landed.
    try {
      await page.locator(".absh-badge .absh-mini").first()
        .waitFor({ state: "attached", timeout: 45_000 });
    } catch (e) {
      // Say which half broke. A missing toolbar button means no content script
      // ran at all; a badge with no button means it ran but never got the
      // library. Guessing between those cost several rounds.
      const d = await page.evaluate(() => ({
        url: location.pathname,
        cards: document.querySelectorAll('[id^="book-card-"]').length,
        script: !!document.getElementById("absh-sync-btn"),
        badges: document.querySelectorAll(".absh-badge").length,
        quiet: document.querySelectorAll(".absh-badge.absh-quiet").length,
        note: document.getElementById("absh-note")?.textContent || "",
      })).catch(() => null);
      throw new Error(`no actionable badge; page state: ${JSON.stringify(d)}`);
    }
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

  /* ------------------------------------------------------------------ *
   * The tests above prove the content script ran. These prove the thing *
   * actually works: each click drives the page, the content script, the *
   * background, the native host, the absh engine and the Audiobookshelf *
   * API, and is checked against the bytes that land on disk.            *
   * ------------------------------------------------------------------ */

  test("copying a book from its card really puts it on the device", async () => {
    const title = state.titles[0];
    const page = await libraryPage();
    const card = cardFor(page, title);
    await expect(card).toBeVisible({ timeout: 30_000 });

    expect(deviceFiles(state.device), "device should start empty").toEqual([]);

    const copy = card.locator(".absh-badge .absh-mini");
    await expect(copy).toHaveAttribute("title", /copy to the device/i, { timeout: 30_000 });
    await copy.click();

    // The real assertion: a file on disk, put there through the native host.
    const files = await until(() => {
      const f = deviceFiles(state.device);
      return f.length ? f : null;
    });
    expect(files, "nothing reached the device").not.toBeNull();
    expect(files.join(" ")).toContain(title);
    // .m4b becomes .m4a on the way. That rename is the whole point of the tool,
    // so assert it here rather than trusting the unit test alone.
    expect(files.some((f) => f.endsWith(".m4a")), `no .m4a in ${files}`).toBe(true);

    // And the page catches up on its own, without a reload.
    await expect(card.locator(".absh-badge .absh-dot"))
      .toHaveText(/on device/i, { timeout: 45_000 });
    await page.close();
  });

  test("removing it from the card really takes it off the device", async () => {
    const title = state.titles[0];
    const page = await libraryPage();
    const card = cardFor(page, title);
    await expect(card.locator(".absh-badge .absh-dot"))
      .toHaveText(/on device/i, { timeout: 45_000 });

    await card.locator(".absh-badge .absh-danger").click();

    const gone = await until(() => (deviceFiles(state.device).length === 0 ? true : null));
    expect(gone, `still on the device: ${deviceFiles(state.device)}`).toBe(true);

    // The badge offers to copy it again, so the page reflects the new state.
    await expect(card.locator(".absh-badge .absh-mini"))
      .toHaveAttribute("title", /copy to the device/i, { timeout: 45_000 });
    await page.close();
  });


  test("a book only on the device is offered for upload, and really uploads", async () => {
    // Put a book on the device that the library has never seen. That is the
    // only state in which the panel and its Upload button exist, and it is the
    // one direction of the tool nothing had exercised end to end.
    const only = state.deviceOnly;
    const dst = join(state.device, "AUDIOBOOKS", only.folder);
    mkdirSync(dst, { recursive: true });
    copyFileSync(only.source, join(dst, only.file));

    const page = await libraryPage();
    const panel = page.locator("#absh-panel");
    await expect(panel).toBeVisible({ timeout: 60_000 });
    await expect(panel).toContainText(only.title);

    await panel.locator("button.absh-primary").first().click();

    // The assertion is the server's own library, not the page.
    const items = async () => {
      const r = await fetch(
        `${state.absUrl}/api/libraries/${state.libraryId}/items?limit=0&minified=1`,
        { headers: { Authorization: `Bearer ${state.token}` } });
      return (await r.json()).results || [];
    };
    const arrived = await until(async () => {
      const got = await items().catch(() => []);
      return got.find((i) => (i.media?.metadata?.title || "") === only.title) || null;
    }, { timeout: 60_000 });
    expect(arrived, `${only.title} never reached the server`).toBeTruthy();

    await page.close();

    // Put the server back, so the run is repeatable: with the book in the
    // library it is no longer device-only and the panel would never appear.
    await fetch(`${state.absUrl}/api/items/${arrived.id}`, {
      method: "DELETE", headers: { Authorization: `Bearer ${state.token}` },
    }).catch(() => {});
    rmSync(dst, { recursive: true, force: true });
  });

});
