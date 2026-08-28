/* The content script's DOM contract, plus proof that the built Firefox add-on
 * actually installs into a real Firefox.
 *
 * The content script is no longer declared in the manifest - it is registered
 * at runtime for the one origin the user grants, so "load the add-on and see
 * the button" is only true after a grant. Chromium covers that whole path in
 * extension.spec.js, where the grant can be seeded. Here Firefox proves the
 * built add-on is installable (playwright-webextext installs a temporary
 * add-on over the remote debugging protocol; core Playwright ignores
 * extensions in launchPersistentContext on Firefox), and both browsers pin the
 * DOM assumptions the script makes.
 */
import { test, expect, firefox } from "@playwright/test";
// CommonJS module using Object.defineProperty(exports, ...) - node's ESM
// lexer cannot see the named exports, so import the default and destructure.
// playwright-webextext@0.0.5 is compiled with TypeScript's importHelpers but
// declares no dependencies, so its dist requires 'tslib' without pulling it
// in. We add tslib to devDependencies to compensate.
import webextext from "playwright-webextext";
const { withExtension } = webextext;
import { readFileSync, mkdtempSync, existsSync } from "node:fs";
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

test.describe("real add-on load", () => {
  test("firefox installs the built add-on, and injects nothing without a grant",
       async ({ browserName }) => {
    test.skip(browserName !== "firefox", "firefox project only - CI installs one browser per leg");
    // webextext requires a persistent context for MV3 add-ons, and throws if
    // Firefox refuses the manifest - so getting this far is the load assertion.
    const ff = withExtension(firefox, distFirefox);
    const ctx = await ff.launchPersistentContext(
      mkdtempSync(join(tmpdir(), "absh-ff-")),
      { headless: true }
    );
    try {
      const page = await ctx.newPage();
      await stubRoute(page);
      await page.goto("https://abs.test/library/main");
      await expect(page.locator("#toolbar")).toBeVisible();
      // No host permission has been granted, so no script may run here.
      await expect(page.locator("#absh-sync-btn")).toHaveCount(0);
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

  test("neither manifest asks for host access up front", () => {
    // The single biggest store-review risk was <all_urls> plus a content
    // script matching every site. Both are now requested at runtime instead.
    for (const m of [
      JSON.parse(readFileSync(resolve(distFirefox, "manifest.json"), "utf8")),
      JSON.parse(readFileSync(resolve(distChrome, "manifest.json"), "utf8"))
    ]) {
      expect(m.host_permissions).toBeUndefined();
      expect(m.content_scripts).toBeUndefined();
      expect(m.optional_host_permissions).toEqual(["*://*/*"]);
      expect(m.permissions).toContain("scripting");
    }
  });

  test("both bundles ship the icons the stores require", () => {
    for (const dir of [distFirefox, distChrome]) {
      const m = JSON.parse(readFileSync(resolve(dir, "manifest.json"), "utf8"));
      for (const size of ["16", "48", "128"]) {
        expect(m.icons[size]).toBeTruthy();
        expect(existsSync(resolve(dir, m.icons[size]))).toBe(true);
      }
    }
  });
});
