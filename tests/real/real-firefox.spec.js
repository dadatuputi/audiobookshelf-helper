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
import { copyFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";
import net from "node:net";
import { installTemporaryAddon, seedGrantedPermissions, seedLocalStorage,
         readLocalStorage, FIREFOX_PREFS } from "./firefox-addon.mjs";
import { cardFor, deviceFiles, until } from "./shared.mjs";

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

/* Playwright's stock Firefox cannot drive moz-extension:// documents, so the
 * add-on is configured through the profile rather than through its options
 * page - see seedLocalStorage. The UUID Firefox reports at install time is
 * kept only for the best-effort pass at that page. */
let EXT_UUID = null;

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
  let profile;
  let disconnect;

  test.beforeAll(async () => {
    profile = mkdtempSync(join(tmpdir(), "absh-ff-"));

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

    // Configure it before it starts. Omitting this was the whole failure the
    // first time round: with no server URL there is nothing to register a
    // content script for, so the add-on installs and then does nothing, and
    // every test times out looking for a button that was never going to
    // exist. runtime.onInstalled fires after the temporary install and reads
    // exactly these values.
    seedLocalStorage(profile, GECKO_ID, {
      absUrl: state.absUrl,
      apiKey: state.token,
      devicePath: state.device,
      renameM4b: true,
      folderTemplate: "{author} - {title}",
      subdir: "AUDIOBOOKS",
    });

    const port = await freePort();
    ctx = await firefox.launchPersistentContext(profile, {
      headless: true,
      ...(process.env.ABSH_FIREFOX_PATH
        ? { executablePath: process.env.ABSH_FIREFOX_PATH } : {}),
      args: ["-start-debugger-server", String(port)],
      firefoxUserPrefs: FIREFOX_PREFS,
      env: { ...process.env, HOME: home },
    });

    const installed = await installTemporaryAddon(port, distFirefox);
    disconnect = installed.disconnect;
    EXT_UUID = installed.addon.uuid;

    // Belt and braces: if this build of Firefox does let Playwright reach an
    // extension page, save the same settings through the options page - the
    // path a real user takes. It is bounded and its failure is ignored; the
    // seeded profile above is what the run actually relies on.
    if (EXT_UUID) {
      const opt = await ctx.newPage();
      try {
        await opt.goto(`moz-extension://${EXT_UUID}/options.html`,
                       { waitUntil: "commit", timeout: 15_000 });
        await opt.fill("#absUrl", state.absUrl, { timeout: 5_000 });
        await opt.fill("#apiKey", state.token, { timeout: 5_000 });
        await opt.fill("#devicePath", state.device, { timeout: 5_000 });
        await opt.click("#save", { timeout: 5_000 });
        await opt.waitForTimeout(1000);
      } catch (e) {
        console.log("options page unreachable (expected on stock Firefox): " +
                    String(e.message).split("\n")[0]);
      }
      await opt.close().catch(() => {});
    }
  });

  test.afterAll(async () => {
    await ctx?.close();
    disconnect?.();
  });

  async function libraryPage() {
    const page = await ctx.newPage();
    const url = `${state.absUrl}/library/${state.libraryId}`;

    // Up to three passes, because two different things send this back to
    // /login: a context with no session yet, and a login whose token has not
    // been stored by the time we navigate away. The second cost a whole CI
    // run - the first library page of the job landed back on the login form
    // and the wait for book cards timed out there 45s later, while every
    // later test in the same job passed.
    // Bounded by the clock rather than a pass count, so a page that will
    // never come good still fails with a useful message inside the test's own
    // timeout instead of being cut off by it.
    const deadline = Date.now() + 90_000;
    const left = () => Math.max(1_000, Math.min(20_000, deadline - Date.now()));
    let cards = false;
    while (!cards && Date.now() < deadline) {
      await page.goto(url, { waitUntil: "domcontentloaded" });

      // The app may redirect to /login and render the form a moment later.
      // Sampling once for a password field raced that: on a slow load the
      // field was not there yet, login was skipped, and the run then waited
      // out its timeout for book cards on the login page.
      await page.locator('input[type="password"], [id^="book-card-"]').first()
        .waitFor({ state: "attached", timeout: left() }).catch(() => {});

      if (await page.locator('input[type="password"]').count()) {
        await page.fill('input[type="text"]', state.username);
        await page.fill('input[type="password"]', state.password);
        await page.press('input[type="password"]', "Enter");
        // Let the app leave the form under its own steam; navigating while it
        // is still storing the session throws that session away.
        await page.locator('input[type="password"]').first()
          .waitFor({ state: "detached", timeout: left() }).catch(() => {});
        continue;
      }

      cards = await page.locator('[id^="book-card-"]').first()
        .waitFor({ state: "attached", timeout: left() })
        .then(() => true).catch(() => false);
    }
    if (!cards) {
      throw new Error(`no book cards after signing in; ended on ${page.url()}`);
    }
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
    // Audiobookshelf's own default is /audiobookshelf, so the pattern must
    // carry the path. Dropping it registered a pattern nothing ever matched -
    // no button, no badges, no error, and nothing on the page to say why.
    expect(new URL(state.absUrl).pathname.replace(/\/+$/, ""),
           "fixture must serve under a base path or this proves nothing")
      .not.toBe("");

    // The registration table itself is behind browser.scripting, reachable
    // only from an extension page, and Playwright cannot open one. The
    // background writes what it registered into storage.local, though, and
    // that is a file - so read what the add-on itself recorded.
    const recorded = await until(() => {
      const s = readLocalStorage(profile, GECKO_ID);
      return s.registeredPattern || s.registrationError ? s : null;
    }, { timeout: 30_000 });
    expect(recorded, "the add-on never recorded a registration").toBeTruthy();
    expect(recorded.registrationError || "").toBe("");
    expect(recorded.registeredPattern).toBe(`${state.absUrl}/library/*`);

    // And then the effect, which is what the user sees: the content script,
    // which draws the button, and the MAIN-world hook, which is the only
    // thing that can give a badge its library item id.
    const page = await libraryPage();
    await expect(page.locator("#absh-sync-btn")).toBeVisible({ timeout: 30_000 });
    const withIds = await page.locator(".absh-badge[data-absh-id]").count();
    expect(withIds, "the page hook never supplied an item id").toBeGreaterThan(0);
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

    // Put everything back, so the run is repeatable. Deleting the library item
    // alone is not enough: the upload also wrote a file into the library
    // folder, and leaving it there made the next run fail deterministically -
    // the upload had nowhere to land, and the book was no longer device-only.
    await fetch(`${state.absUrl}/api/items/${arrived.id}`, {
      method: "DELETE", headers: { Authorization: `Bearer ${state.token}` },
    }).catch(() => {});
    if (state.libraryFolder) {
      rmSync(join(state.libraryFolder, only.author), { recursive: true, force: true });
    }
    rmSync(dst, { recursive: true, force: true });
  });

});
