/* End-to-end check of the content script against a stand-in Audiobookshelf page.
 *
 * Chromium can load an unpacked MV3 extension (--load-extension), so there the
 * real extension is exercised. Playwright cannot install a temporary add-on in
 * Firefox, so that project verifies the same DOM contract by evaluating the
 * built content script in the page. Both assert the identical outcome, which is
 * what actually matters: the button appears in #toolbar and survives re-render.
 */
import { test, expect, chromium } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const distChrome = resolve(root, "extension/dist/chrome");
const contentJs = resolve(root, "extension/src/content.js");
const contentCss = resolve(root, "extension/src/content.css");

/* Minimal stand-in for the ABS library page: the content script only needs
 * #toolbar to exist at a /library/ URL. */
const PAGE = `<!doctype html><html><body>
  <div id="app">
    <div id="toolbar" role="toolbar" aria-label="Library Toolbar"></div>
    <div id="shelf"></div>
  </div>
</body></html>`;

async function serve(page) {
  await page.route("**/library/**", (r) =>
    r.fulfill({ status: 200, contentType: "text/html", body: PAGE }));
}

test.describe("content script", () => {
  test("chromium: real extension injects the sync button", async () => {
    const ctx = await chromium.launchPersistentContext("", {
      headless: true,
      args: [
        `--disable-extensions-except=${distChrome}`,
        `--load-extension=${distChrome}`
      ]
    });
    try {
      const page = await ctx.newPage();
      await serve(page);
      await page.goto("https://abs.test/library/main");
      const btn = page.locator("#absh-sync-btn");
      await expect(btn).toBeVisible({ timeout: 15_000 });
      await expect(btn).toContainText("Sync to device");
      // must live inside the ABS toolbar, not floating loose in the body
      expect(await btn.evaluate((el) => el.closest("#toolbar") !== null)).toBe(true);
    } finally {
      await ctx.close();
    }
  });

  test("injects exactly once, even after the toolbar re-renders", async ({ page, browserName }) => {
    await serve(page);
    await page.goto("https://abs.test/library/main");
    await page.addStyleTag({ content: readFileSync(contentCss, "utf8") });
    await page.addScriptTag({ content: readFileSync(contentJs, "utf8") });

    await expect(page.locator("#absh-sync-btn")).toHaveCount(1);

    // Vue replaces the toolbar on navigation; the MutationObserver should
    // put the button back without ever duplicating it.
    await page.evaluate(() => {
      const bar = document.getElementById("toolbar");
      bar.replaceWith(Object.assign(document.createElement("div"), { id: "toolbar" }));
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
    const ff = JSON.parse(readFileSync(resolve(root, "extension/dist/firefox/manifest.json"), "utf8"));
    const cr = JSON.parse(readFileSync(resolve(root, "extension/dist/chrome/manifest.json"), "utf8"));
    expect(ff.manifest_version).toBe(3);
    expect(cr.manifest_version).toBe(3);
    expect(ff.background.scripts).toContain("background.js");
    expect(cr.background.service_worker).toBe("background.js");
    expect(ff.browser_specific_settings.gecko.id).toBeTruthy();
    expect(cr.browser_specific_settings).toBeUndefined();
  });
});
