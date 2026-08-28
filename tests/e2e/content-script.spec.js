/* End-to-end check of the content script against a stand-in Audiobookshelf page.
 *
 * Both browsers load the *real* built extension:
 *   chromium  - launchPersistentContext with --load-extension (core Playwright)
 *   firefox   - playwright-webextext, which installs a temporary add-on over
 *               the remote debugging protocol. Core Playwright cannot do this
 *               (launchPersistentContext ignores extensions on Firefox), but
 *               the add-on has a browser_specific_settings.gecko.id, which is
 *               what webextext requires for MV3.
 */
import { test, expect, chromium, firefox } from "@playwright/test";
// CommonJS module using Object.defineProperty(exports, ...) - node's ESM
// lexer cannot see the named exports, so import the default and destructure.
import webextext from "playwright-webextext";
const { withExtension } = webextext;
import { readFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve, join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const distChrome = resolve(root, "extension/dist/chrome");
const distFirefox = resolve(root, "extension/dist/firefox");
const contentJs = resolve(root, "extension/src/content.js");

/* The content script only needs #toolbar to exist on a /library/ URL. */
const PAGE = `<!doctype html><html><body>
  <div id="app">
    <div id="toolbar" role="toolbar" aria-label="Library Toolbar"></div>
    <div id="shelf"></div>
  </div>
</body></html>`;

const stubRoute = (target) =>
  target.route("**/library/**", (r) =>
    r.fulfill({ status: 200, contentType: "text/html", body: PAGE }));

async function assertButtonInToolbar(page) {
  const btn = page.locator("#absh-sync-btn");
  await expect(btn).toBeVisible({ timeout: 20_000 });
  await expect(btn).toContainText("Sync to device");
  expect(await btn.evaluate((el) => el.closest("#toolbar") !== null)).toBe(true);
}

test.describe("real extension load", () => {
  test("chromium loads the built extension and injects the button", async ({ browserName }) => {
    test.skip(browserName !== "chromium", "chromium project only - CI installs one browser per leg");
    const ctx = await chromium.launchPersistentContext(
      mkdtempSync(join(tmpdir(), "absh-cr-")),
      {
        // MV3 extensions do not load in the old headless mode; the
        // "chromium" channel selects the new one, which supports them.
        channel: "chromium",
        headless: true,
        args: [
          `--disable-extensions-except=${distChrome}`,
          `--load-extension=${distChrome}`
        ]
      }
    );
    try {
      const page = await ctx.newPage();
      await stubRoute(page);
      await page.goto("https://abs.test/library/main");
      await assertButtonInToolbar(page);
    } finally {
      await ctx.close();
    }
  });

  test("firefox loads the built add-on and injects the button", async ({ browserName }) => {
    test.skip(browserName !== "firefox", "firefox project only - CI installs one browser per leg");
    // webextext requires a persistent context for MV3 add-ons.
    const ff = withExtension(firefox, distFirefox);
    const ctx = await ff.launchPersistentContext(
      mkdtempSync(join(tmpdir(), "absh-ff-")),
      { headless: true }
    );
    try {
      const page = await ctx.newPage();
      await stubRoute(page);
      await page.goto("https://abs.test/library/main");
      await assertButtonInToolbar(page);
    } finally {
      await ctx.close();
    }
  });
});

/* These run under both projects and do not need the extension installed -
 * they pin the DOM contract the content script relies on. */
test.describe("content script behaviour", () => {
  test("injects exactly once, even after the toolbar re-renders", async ({ page }) => {
    await stubRoute(page);
    await page.goto("https://abs.test/library/main");
    await page.addScriptTag({ content: readFileSync(contentJs, "utf8") });
    await expect(page.locator("#absh-sync-btn")).toHaveCount(1);

    // Vue replaces the toolbar on navigation; the MutationObserver should put
    // the button back without ever duplicating it.
    await page.evaluate(() => {
      document.getElementById("toolbar")
        .replaceWith(Object.assign(document.createElement("div"), { id: "toolbar" }));
    });
    await expect(page.locator("#absh-sync-btn")).toHaveCount(1);
  });

  test("does nothing when there is no toolbar", async ({ page }) => {
    await page.route("**/library/**", (r) =>
      r.fulfill({ status: 200, contentType: "text/html", body: "<html><body></body></html>" }));
    await page.goto("https://abs.test/library/empty");
    await page.addScriptTag({ content: readFileSync(contentJs, "utf8") });
    await expect(page.locator("#absh-sync-btn")).toHaveCount(0);
  });
});

test.describe("built artefacts", () => {
  test("both manifests are valid and browser-appropriate", () => {
    const ff = JSON.parse(readFileSync(resolve(distFirefox, "manifest.json"), "utf8"));
    const cr = JSON.parse(readFileSync(resolve(distChrome, "manifest.json"), "utf8"));
    expect(ff.manifest_version).toBe(3);
    expect(cr.manifest_version).toBe(3);
    expect(ff.background.scripts).toContain("background.js");
    expect(cr.background.service_worker).toBe("background.js");
    // webextext needs this to install an MV3 add-on temporarily
    expect(ff.browser_specific_settings.gecko.id).toBeTruthy();
    expect(cr.browser_specific_settings).toBeUndefined();
  });
});
