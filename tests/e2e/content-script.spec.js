/* The content script's DOM contract, and what the built bundles must contain.
 *
 * The content script is no longer declared in the manifest - it is registered
 * at runtime for the one origin the user grants. extension.spec.js drives that
 * whole path in a real Chromium, including the injection itself; these tests
 * pin the DOM assumptions the script makes, in both browsers.
 *
 * There used to be a Firefox test here that installed the built add-on through
 * playwright-webextext. It cannot run against this manifest:
 * playwright-webextext@0.0.5 crashes on any MV3 add-on with no content_scripts,
 * because overridePermissions() short-circuits into
 * `manifest.optional_permissions.length` when that key is absent
 * (dist/firefox_browser.js, and it means optional_host_permissions anyway).
 * Firefox manifest validation is covered by web-ext lint, which is Mozilla's
 * own addons-linter and the same check AMO runs on submission.
 */
import { test, expect } from "@playwright/test";
import { readFileSync, existsSync } from "node:fs";
import { resolve, dirname } from "node:path";
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

/* A content script always has the extension APIs beside it - the polyfill is
 * injected with it, by registration and by the fallback alike. These tests
 * inject the file into a bare page instead, so the page has to supply the
 * little of that surface the script touches, or the script dies on the first
 * line that reaches for it and the DOM contract goes untested. */
const stubBrowser = (target) => target.addInitScript(() => {
  window.browser = {
    runtime: {
      sendMessage: () => Promise.reject(new Error("no helper in this harness")),
      onMessage: { addListener() {} },
    },
  };
});

/* These run under both projects and do not need the extension installed -
 * they pin the DOM contract the content script relies on. */
test.describe("content script behaviour", () => {
  test("injects exactly once, even after the toolbar re-renders", async ({ page }) => {
    await stubRoute(page);
    await stubBrowser(page);
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
    await stubBrowser(page);
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
    // AMO requires an explicit id for a signed add-on
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
