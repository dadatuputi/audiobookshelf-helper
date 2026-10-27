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

  /* A collection page: the heading row the app draws, and a helper that
   * answers the few messages the page asks. 12 books of 1GB, none on it. */
  const GB = 1024 ** 3;
  const groupPage = async (page, free, tags = null) => {
    await page.route("**/collection/**", (r) => r.fulfill({ status: 200, contentType: "text/html",
      body: `<!doctype html><html><body><div id="page-wrapper"><div class="row">
               <h1>Twelve</h1><button>Play All</button></div></div></body></html>` }));
    await page.addInitScript(({ GB, free, tags }) => {
      const ids = Array.from({ length: 12 }, (_, i) => `li_${i}`);
      window.SENT = [];
      const answer = (m) => {
        if (m.type === "status") {
          return { backend: "native", both: [], deviceOnly: [], free: { free },
                   serverOnly: ids.map((id) => ({ id, title: id, size: GB })) };
        }
        if (m.type === "group") return ids;
        if (m.type === "tagsAsk") return tags;
        if (m.type === "jobAdd") return { current: null, queue: [], last: null };
        return null;
      };
      window.browser = { runtime: {
        sendMessage: (m) => { window.SENT.push(m); return Promise.resolve({ ok: true, data: answer(m) }); },
        onMessage: { addListener() {} },
      } };
    }, { GB, free, tags });
    await page.goto("https://abs.test/collection/col_1");
    await page.addScriptTag({ content: readFileSync(contentJs, "utf8") });
  };

  test("a collection's books copy in one click, asked once more when there are many",
    async ({ page }) => {
    await groupPage(page, 40 * GB);
    const box = page.locator("#absh-group");
    const go = box.locator("button", { hasText: "Copy collection to" });
    await expect(go).toHaveText("Copy collection to the player · 12 books, 12GB");
    // In the heading's own row, beside the page's buttons.
    expect(await box.evaluate((el) => el.parentElement.querySelector(":scope > h1") !== null))
      .toBe(true);
    expect(await page.evaluate(() => SENT.find((m) => m.type === "group")))
      .toMatchObject({ kind: "collection", id: "col_1" });

    // More than ten: asked in place, and No leaves it alone.
    await go.click();
    await expect(box).toContainText("Copy 12 books, 12GB to the player?");
    await box.locator("button", { hasText: "No" }).click();
    await expect(go).toBeVisible();
    expect(await page.evaluate(() => SENT.some((m) => m.type === "jobAdd"))).toBe(false);

    await go.click();
    await box.locator("button", { hasText: "Yes, copy 12 books" }).click();
    const add = await page.evaluate(() => SENT.find((m) => m.type === "jobAdd"));
    expect(add.op).toBe("pull");
    expect(add.items.map((i) => i.ref)).toEqual(Array.from({ length: 12 }, (_, i) => `li_${i}`));
  });

  test("asks about tags before a copy, and a let-go question copies nothing", async ({ page }) => {
    await groupPage(page, 40 * GB, { ask: true, write: false });
    const box = page.locator("#absh-group");
    const go = box.locator("button", { hasText: "Copy collection to" });
    await go.click();
    await box.locator("button", { hasText: "Yes, copy 12 books" }).click();
    const ask = page.locator("#absh-ask");
    await expect(ask).toContainText("Embed Metadata");
    await page.keyboard.press("Escape");
    await expect(ask).toHaveCount(0);
    expect(await page.evaluate(() => SENT.some((m) => m.type === "jobAdd"))).toBe(false);

    await go.click();
    await box.locator("button", { hasText: "Yes, copy 12 books" }).click();
    await ask.locator("input[type=checkbox]").check();
    await ask.locator("button", { hasText: "Embed tags" }).click();
    const add = await page.evaluate(() => SENT.find((m) => m.type === "jobAdd"));
    expect(add.writeTags).toBe(true);
    expect(await page.evaluate(() => SENT.find((m) => m.type === "tagsRemember")))
      .toMatchObject({ value: "always" });
  });

  test("a book changed on the server since it was copied offers an update", async ({ page }) => {
    await page.route("**/item/**", (r) => r.fulfill({ status: 200, contentType: "text/html",
      body: `<!doctype html><html><body><div class="row"><button class="abs-btn bg-success">Play</button></div></body></html>` }));
    await page.addInitScript(() => {
      window.SENT = [];
      const answer = (m) => {
        if (m.type === "status") {
          return { backend: "native", serverOnly: [], deviceOnly: [], free: {},
                   both: [{ name: "A - B.m4a", itemId: "li_1", title: "B", stale: true,
                            item: { id: "li_1", title: "B" } }] };
        }
        if (m.type === "tagsAsk") return { ask: false, write: true };
        if (m.type === "jobAdd") return { current: null, queue: [], last: null };
        return null;
      };
      window.browser = { runtime: {
        sendMessage: (m) => { window.SENT.push(m); return Promise.resolve({ ok: true, data: answer(m) }); },
        onMessage: { addListener() {} },
      } };
    });
    await page.goto("https://abs.test/item/li_1");
    await page.addScriptTag({ content: readFileSync(contentJs, "utf8") });
    const item = page.locator("#absh-item");
    await expect(item).toContainText("changed in Audiobookshelf since it was copied");
    await item.locator("button", { hasText: "Update on" }).click();
    await expect.poll(() => page.evaluate(() => SENT.find((m) => m.type === "jobAdd")))
      .toMatchObject({ op: "pull", writeTags: true, items: [{ ref: "li_1" }] });
    expect((await page.evaluate(() => SENT.find((m) => m.type === "jobAdd"))).force).toBeUndefined();
  });

  test("a book already on the player can be copied again from its page", async ({ page }) => {
    await page.route("**/item/**", (r) => r.fulfill({ status: 200, contentType: "text/html",
      body: `<!doctype html><html><body><div class="row"><button class="abs-btn bg-success">Play</button></div></body></html>` }));
    await page.addInitScript(() => {
      window.SENT = [];
      const answer = (m) => {
        if (m.type === "status") {
          return { backend: "native", serverOnly: [], deviceOnly: [], free: {},
                   both: [{ name: "A - B.m4a", itemId: "li_1", title: "B", stale: false,
                            item: { id: "li_1", title: "B" } }] };
        }
        if (m.type === "tagsAsk") return { ask: false, write: true };
        if (m.type === "jobAdd") return { current: null, queue: [], last: null };
        return null;
      };
      window.browser = { runtime: {
        sendMessage: (m) => { window.SENT.push(m); return Promise.resolve({ ok: true, data: answer(m) }); },
        onMessage: { addListener() {} },
      } };
    });
    await page.goto("https://abs.test/item/li_1");
    await page.addScriptTag({ content: readFileSync(contentJs, "utf8") });
    const item = page.locator("#absh-item");
    await expect(item).toContainText("✓ On");
    await expect(item.locator("button", { hasText: "Update on" })).toHaveCount(0);
    await item.locator("button", { hasText: "Copy again" }).click();
    await expect.poll(() => page.evaluate(() => SENT.find((m) => m.type === "jobAdd")))
      .toMatchObject({ op: "pull", force: true, items: [{ ref: "li_1", title: "B" }] });
  });

  test("a collection too big for the player says so instead of starting", async ({ page }) => {
    await groupPage(page, 5 * GB);
    const box = page.locator("#absh-group");
    await expect(box).toContainText("Needs 12GB; the player has 5.0GB free");
    await expect(box.locator("button")).toBeDisabled();
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
