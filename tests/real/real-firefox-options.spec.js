/**
 * The options page in a real Firefox, used the way a user uses it.
 *
 * real-firefox.spec.js configures the add-on by writing its storage into the
 * profile, because Playwright cannot drive an extension page there. So until
 * this file the Firefox options page had never been clicked: fields that do
 * not save, a Detect that cannot reach the helper, or a permission line that
 * says the wrong thing would all have passed. This drives it over Firefox's
 * debugging protocol instead - see firefox-rdp.mjs for how, and for the one
 * thing that route cannot do.
 *
 * Its own Firefox and its own empty profile, so it can start from a first run
 * and change settings without touching the suite beside it. The add-on is the
 * shipped build, manifest and all: nothing here is declared up front, so the
 * page is seen as a new user sees it, before access is granted.
 *
 * Needs `node tests/real/setup.mjs` first, like the rest of tests/real.
 */
import { test, expect, firefox } from "@playwright/test";
import { existsSync, mkdtempSync, mkdirSync, writeFileSync, readFileSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";
import net from "node:net";
import { installTemporaryAddon, readLocalStorage, FIREFOX_PREFS } from "./firefox-addon.mjs";
import { RdpClient, openExtensionPage } from "./firefox-rdp.mjs";
import { until } from "./shared.mjs";

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

// Pinned so the page's address is known before install. Firefox reports the
// UUID it used when the add-on installs, and that report wins if they differ.
const PINNED_UUID = randomUUID();

// What the tab shows before it is moved to the options page, so it can be
// picked out of every other tab the debugger lists.
const MARKER = `absh-options-${PINNED_UUID.slice(0, 8)}`;

const freePort = () => new Promise((res, rej) => {
  const s = net.createServer();
  s.on("error", rej);
  s.listen(0, "127.0.0.1", () => { const { port } = s.address(); s.close(() => res(port)); });
});

test.describe("Firefox options page, driven", () => {
  test.skip(!HAVE, "run: node tests/real/setup.mjs");
  test.skip(({ browserName }) => browserName !== "firefox", "firefox only");
  // One page, walked through in order: a step that fails leaves nothing
  // meaningful for the next one to check.
  test.describe.configure({ mode: "serial" });

  let ctx, rdp, page, tab, popPage, popTab, disconnect, profile, device, origin;

  test.beforeAll(async () => {
    profile = mkdtempSync(join(tmpdir(), "absh-ffopt-"));
    origin = `${new URL(state.absUrl).origin}/*`;

    // The device Detect should find. Its own directory, not the one the other
    // Firefox suite copies books onto, so neither can disturb the other.
    device = mkdtempSync(join(tmpdir(), "absh-ffopt-dev-"));
    mkdirSync(join(device, "AUDIOBOOKS"));

    // Firefox finds native-messaging manifests only under HOME, so give it a
    // HOME of its own - as real-firefox.spec.js does, for the same reason.
    const home = mkdtempSync(join(tmpdir(), "absh-ffopt-home-"));
    const nmDir = join(home, ".mozilla", "native-messaging-hosts");
    mkdirSync(nmDir, { recursive: true });
    writeFileSync(join(nmDir, `${HOST_NAME}.json`), JSON.stringify({
      name: HOST_NAME,
      description: "Audiobookshelf Helper native host (options-page test)",
      path: resolve(root, "native/absh_host.py"),
      type: "stdio",
      allowed_extensions: [GECKO_ID],
    }, null, 2));

    const port = await freePort();
    ctx = await firefox.launchPersistentContext(profile, {
      headless: true,
      ...(process.env.ABSH_FIREFOX_PATH
        ? { executablePath: process.env.ABSH_FIREFOX_PATH } : {}),
      args: ["-start-debugger-server", String(port)],
      firefoxUserPrefs: {
        ...FIREFOX_PREFS,
        "extensions.webextensions.uuids": JSON.stringify({ [GECKO_ID]: PINNED_UUID }),
      },
      // Nothing removable is mounted on a runner, so name the volume Detect
      // should offer. Firefox passes its environment to the helper it spawns.
      env: { ...process.env, HOME: home, ABSH_DEVICE_ROOTS: device },
    });

    const installed = await installTemporaryAddon(port, distFirefox);
    disconnect = installed.disconnect;
    const uuid = installed.addon.uuid || PINNED_UUID;

    // A tab Playwright can open and the debugger can recognise. Playwright's
    // part ends here: everything after happens over RDP.
    page = await ctx.newPage();
    await page.goto(`data:text/plain,${MARKER}`);

    rdp = await RdpClient.open(port);
    tab = await openExtensionPage(rdp, {
      marker: MARKER, url: `moz-extension://${uuid}/options.html`,
    });
    // load() fills the fields from storage and then writes permState in every
    // branch, so text there means the page has finished starting up.
    await tab.waitFor("the options page finishing its first load",
      () => document.getElementById("permState").textContent !== "");

    // The toolbar popup, reached the same way in a tab of its own: the player
    // is chosen there, not on the options page.
    popPage = await ctx.newPage();
    await popPage.goto(`data:text/plain,${MARKER}-popup`);
    popTab = await openExtensionPage(rdp, {
      marker: `${MARKER}-popup`, url: `moz-extension://${uuid}/popup.html`,
    });
    // Nothing is saved yet, so its first pass ends asking for the server.
    await popTab.waitFor("the popup finishing its first pass",
      () => document.getElementById("status").textContent.includes("Set up your server first"));
  });

  test.afterAll(async () => {
    rdp?.close();
    // Juggler never saw this tab arrive where it is, so do not let it hold up
    // the shutdown if it cannot close it either.
    await Promise.race([Promise.all([page?.close().catch(() => {}),
                                     popPage?.close().catch(() => {})]),
                        new Promise((r) => setTimeout(r, 5_000))]);
    await ctx?.close();
    disconnect?.();
  });

  test("it is the add-on's own page, starting from a first run", async () => {
    const s = await tab.evaluate(() => ({
      href: location.href,
      // The page's own handle on the add-on, which an ordinary page lacks.
      api: typeof browser !== "undefined" && typeof browser.storage?.local?.get === "function",
      absUrl: document.getElementById("absUrl").value,
      // The player moved to the popup; this page must not still offer it.
      deviceField: !!document.getElementById("devicePath"),
      renameM4b: document.getElementById("renameM4b").checked,
      permState: document.getElementById("permState").textContent,
      grantDisabled: document.getElementById("grant").disabled,
    }));
    expect(s.href).toMatch(/^moz-extension:\/\/[^/]+\/options\.html$/);
    expect(s.api, "the page has no browser.storage - not an extension page").toBe(true);
    expect(s.absUrl).toBe("");
    expect(s.deviceField).toBe(false);
    expect(s.renameM4b).toBe(true);
    expect(s.permState).toBe("Set the server URL first.");
    expect(s.grantDisabled).toBe(true);
  });

  test("typing the server URL asks for that one origin, and says it is not granted", async () => {
    await tab.fill("#absUrl", state.absUrl);
    const perm = await tab.waitFor("permState naming the server",
      (o) => {
        const t = document.getElementById("permState").textContent;
        return t.includes(o) ? t : null;
      }, [origin]);
    expect(perm).toBe(`Not granted yet for ${origin}`);
    expect(await tab.evaluate(() => document.getElementById("grant").disabled)).toBe(false);

    // The shipped manifest holds no host at all; the page must not have been
    // handed one on the way.
    const held = await tab.evaluate(() => browser.permissions.getAll());
    expect(held.origins).not.toContain(origin);
    expect(held.origins).not.toContain("*://*/*");
    expect(await tab.evaluate((o) => browser.permissions.contains({ origins: [o] }), origin))
      .toBe(false);
  });

  test("the popup finds the player through the helper, and keeps the choice", async () => {
    await popTab.click("#playerToggle");
    // The device itself, by path - not merely "some player", which a stray
    // mount on a developer's machine would satisfy.
    const offered = await popTab.waitFor("the device appearing in the popup's player list",
      (dev) => {
        const b = [...document.querySelectorAll(".player-pick")].find((x) => x.title === dev);
        return b ? { text: b.textContent } : null;
      }, [device], { timeout: 30_000 });
    expect(offered.text).toContain("has your books");
    expect(await popTab.evaluate(() => document.getElementById("detect").textContent)).toBe("Detect");

    // Picking it, and the folder on it, is the whole choice: there is no Save
    // in a popup, which closes the moment it loses focus.
    await popTab.click(`.player-pick[title="${device}"]`);
    await popTab.click("#playerToggle");
    await popTab.fill("#subdir", "BOOKS");
    const stored = await until(() => {
      const s = readLocalStorage(profile, GECKO_ID);
      return s.devicePath === device && s.subdir === "BOOKS" ? s : null;
    }, { timeout: 15_000 });
    expect(stored, "the popup's choice never reached storage.local").toBeTruthy();
  });

  test("saved settings reach storage and survive a reload", async () => {
    await tab.fill("#apiKey", "test-key");
    // Away from its default: the template carries one in the markup, and a
    // page that ignored what was saved for it read back fine until something
    // other than the default was saved. The box is turned off for the same
    // reason.
    await tab.fill("#folderTemplate", "{title}");
    await tab.click("#renameM4b");
    expect(await tab.evaluate(() => document.getElementById("renameM4b").checked)).toBe(false);

    await tab.click("#save");
    await tab.waitFor("the page saying it saved",
      () => document.getElementById("msg").textContent === "saved");

    // The add-on's storage, read from the profile: proof the values left the
    // form, independent of the page reading them back.
    const stored = await until(() => {
      const s = readLocalStorage(profile, GECKO_ID);
      return s.folderTemplate === "{title}" ? s : null;
    }, { timeout: 15_000 });
    expect(stored, "storage.local never received the save").toBeTruthy();
    // The player and its folder are the popup's, and Save here leaves them be.
    expect(stored).toMatchObject({
      absUrl: state.absUrl, apiKey: "test-key", devicePath: device,
      subdir: "BOOKS", folderTemplate: "{title}", renameM4b: false,
    });

    await tab.reload();
    await tab.waitFor("the reloaded page finishing its load",
      () => document.getElementById("permState").textContent !== "");
    const after = await tab.evaluate(() => Object.fromEntries(
      ["absUrl", "apiKey", "folderTemplate"]
        .map((k) => [k, document.getElementById(k).value])
        .concat([["renameM4b", document.getElementById("renameM4b").checked]])));
    expect(after).toEqual({
      absUrl: state.absUrl, apiKey: "test-key", folderTemplate: "{title}", renameM4b: false,
    });
  });

  test("without the grant, nothing is registered and the page says why", async () => {
    // The background records why when the saved URL changes, a beat after
    // the save; the page reads that on load. So wait for the record, then
    // load the page once more to see what a user opening it would see.
    const why = `access has not been granted for ${origin}`;
    const recorded = await until(() => {
      const e = readLocalStorage(profile, GECKO_ID).registrationError || "";
      return e.includes(origin) ? e : null;
    }, { timeout: 15_000 });
    expect(recorded, "the background never recorded why it registered nothing").toBe(why);

    await tab.reload();
    const reg = await tab.waitFor("regState naming the missing grant",
      (o) => {
        const t = document.getElementById("regState").textContent;
        return t.includes(o) ? t : null;
      }, [origin]);
    expect(reg).toBe(`In-page UI not registered - ${why}`);

    // The registration table itself, which until now no Firefox test could
    // read: browser.scripting answers only inside an extension page.
    expect(await tab.evaluate(() => browser.scripting.getRegisteredContentScripts()))
      .toEqual([]);
  });
});
