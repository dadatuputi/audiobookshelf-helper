/**
 * A sandboxed Firefox - Ubuntu's Snap, or Flathub's Flatpak - and the helper.
 *
 * Neither can start a program outside its sandbox. Where Ubuntu's desktop
 * portal is present it does it for them: it reads the manifest install.py
 * wrote to the real ~/.mozilla/native-messaging-hosts, checks the user allowed
 * it, and starts the helper outside the sandbox. Where there is no portal -
 * most systems a Flatpak runs on - nothing can, and the extension must say so
 * plainly rather than leave someone guessing. The other Firefox suites use
 * Playwright's own Firefox, which is neither.
 *
 * Run by .github/workflows/sandboxed-firefox.yml, which installs the browser,
 * the helper (a packaged release, with install.py) and, where it applies, the
 * allow. This launches the browser itself, installs the add-on over Firefox's
 * debugging protocol, opens Settings the same way, and checks the Helper row.
 *
 *   node tests/sandbox/firefox.mjs <snap|flatpak> <answers|absent> [helper release]
 *
 *   answers  the installed helper answers, and Firefox's log shows the portal
 *            starting it
 *   absent   Settings says the helper is not installed, and links the README's
 *            "Sandboxed browsers on Linux"
 */
import { spawn } from "node:child_process";
import { cpSync, mkdirSync, rmSync, writeFileSync, readFileSync, createWriteStream } from "node:fs";
import { randomUUID } from "node:crypto";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import net from "node:net";
import { connect, installTemporaryAddon, FIREFOX_PREFS } from "../real/firefox-addon.mjs";
import { RdpClient, openExtensionPage } from "../real/firefox-rdp.mjs";

const [kind, expect, want = ""] = process.argv.slice(2);
// What a user can turn on: whether Firefox asks the portal (see install.py).
// Unset, the package's own default stands.
const PORTAL_PREF = process.env.PORTAL_PREF;
if (!["snap", "flatpak"].includes(kind) || !["answers", "absent"].includes(expect)) {
  console.error("usage: node tests/sandbox/firefox.mjs <snap|flatpak> <answers|absent> [release]");
  process.exit(2);
}
const name = kind === "snap" ? "Firefox Snap" : "Firefox Flatpak";
const LOG_MODULES = "NativeMessagingPortal:5,timestamp";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const GECKO_ID = JSON.parse(readFileSync(join(root, "extension/identity.json"), "utf8")).geckoId;

// Each sandbox reads and writes freely only in a corner of home of its own,
// seen at the same path inside. The profile and the add-on go there; the
// helper does not, and must not.
const own = kind === "snap" ? join(homedir(), "snap", "firefox", "common")
                            : join(homedir(), ".var", "app", "org.mozilla.firefox");
const work = join(own, "absh-ci");
rmSync(work, { recursive: true, force: true });
const profile = join(work, "profile");
const addon = join(work, "addon");
mkdirSync(profile, { recursive: true });
cpSync(join(root, "extension/dist/firefox"), addon, { recursive: true });

const uuid = randomUUID();
// Playwright's Firefox, which the other suites use, turns on what its own
// driver needs; a stock Firefox serves the debugging protocol only with
// these too - they are what Mozilla's web-ext sets - and first-run screens off.
const prefs = { ...FIREFOX_PREFS,
                "devtools.chrome.enabled": true,
                "browser.shell.checkDefaultBrowser": false,
                "browser.startup.homepage_override.mstone": "ignore",
                "browser.aboutwelcome.enabled": false,
                "datareporting.policy.dataSubmissionEnabled": false,
                "toolkit.telemetry.reportingpolicy.firstRun": false,
                "extensions.webextensions.uuids": JSON.stringify({ [GECKO_ID]: uuid }) };
// widget.use-xdg-desktop-portal.native-messaging is left at the package's own
// default unless PORTAL_PREF says otherwise: what is tested is what a user
// gets, or what install.py tells them to set.
if (PORTAL_PREF) prefs["widget.use-xdg-desktop-portal.native-messaging"] = Number(PORTAL_PREF);
writeFileSync(join(profile, "user.js"), Object.entries(prefs)
  .map(([k, v]) => `user_pref(${JSON.stringify(k)}, ${JSON.stringify(v)});`).join("\n") + "\n");

const port = await new Promise((res, rej) => {
  const s = net.createServer();
  s.on("error", rej);
  s.listen(0, "127.0.0.1", () => { const { port } = s.address(); s.close(() => res(port)); });
});

// The tab the debugger picks out by its address. A file the sandbox can read,
// not a data: address: Firefox will not open a top-level data: address
// handed to it on the command line.
const marker = `absh-${kind}-${uuid.slice(0, 8)}`;
const markerFile = join(work, `${marker}.html`);
writeFileSync(markerFile, `<!doctype html><title>${marker}</title>${marker}`);

const args = ["--headless", "--no-remote", "--profile", profile,
              "-start-debugger-server", String(port), `file://${markerFile}`];
const [cmd, argv] = kind === "snap"
  ? ["snap", ["run", "firefox", ...args]]
  // flatpak run passes only the environment it is told to.
  : ["flatpak", ["run", `--env=MOZ_LOG=${LOG_MODULES}`, "org.mozilla.firefox", ...args]];

const logPath = resolve(process.env.SANDBOX_LOG || `${kind}-firefox.log`);
const log = createWriteStream(logPath);
const ff = spawn(cmd, argv, { env: { ...process.env, MOZ_LOG: LOG_MODULES },
                              stdio: ["ignore", "pipe", "pipe"] });
ff.stdout.pipe(log);
ff.stderr.pipe(log);
let exited = null;
ff.on("exit", (code, sig) => { exited = `Firefox exited (${code ?? sig})`; });

const fail = (msg) => {
  console.error(`FAIL: ${msg}`);
  console.error(`Firefox's output, with the portal's log lines, is in ${logPath}`);
  try { ff.kill(); } catch { /* gone */ }
  process.exit(1);
};

const portalLines = async () => {
  await new Promise((r) => log.end(r));
  const lines = readFileSync(logPath, "utf8").split("\n")
    .filter((l) => /NativeMessagingPortal/.test(l));
  for (const l of lines.slice(0, 12)) console.log(`  ${l}`);
  return lines;
};

let rdp;
try {
  // A sandboxed browser's first start sets itself up, which takes a while.
  (await connect(port, 240)).end();
  const installed = await installTemporaryAddon(port, addon);
  const id = installed.addon.uuid || uuid;
  rdp = await RdpClient.open(port);
  const tab = await openExtensionPage(rdp, { marker, url: `moz-extension://${id}/options.html`,
                                             timeout: 60_000 }).catch(async (e) => {
    const { tabs } = await rdp.request("root", "listTabs").catch(() => ({ tabs: [] }));
    throw new Error(`${e.message}; open tabs: ${JSON.stringify(tabs.map((t) => t.url))}`);
  });
  // The Helper row settles once the ping has come back or failed. Through the
  // portal the first one starts the helper, so allow it time.
  await tab.waitFor("the Helper row to settle", () => {
    const s = document.getElementById("sec-helper")?.dataset.state;
    return s === "ok" || s === "warn" || s === "err";
  }, [], { timeout: 60_000 });
  const got = await tab.evaluate(() => ({
    state: document.getElementById("sec-helper").dataset.state,
    summary: document.getElementById("sum-helper").textContent,
    note: document.getElementById("updateState")?.textContent || "",
    sandboxLink: document.getElementById("helperSandbox")?.getAttribute("href") || "",
  }));
  console.log(`Helper row: ${got.state} · ${got.summary}${got.note ? ` · ${got.note}` : ""}`);

  if (expect === "answers") {
    if (got.state === "err") fail(`the ${name} did not reach the helper: ${got.summary} ${got.note}`);
    if (want && !got.summary.includes(want)) {
      fail(`the helper that answered is not the one install.py installed ` +
           `(wanted ${want}, Settings says "${got.summary}")`);
    }
    // And that it was the portal that started it, by name: a helper reached
    // any other way would leave no such line in Firefox's log.
    const lines = await portalLines();
    if (!lines.some((l) => /starting io\.github\.abshelper/.test(l))) {
      fail("the helper answered, but Firefox's log shows no portal starting it");
    }
    console.log(`OK: the ${name} started the helper through the desktop portal`);
  } else {
    // No portal: what a user sees must say what is wrong and where to read
    // about it, not leave them reinstalling a helper that is already there.
    await portalLines();
    if (got.state !== "err" || got.summary !== "Not installed") {
      fail(`expected "Not installed" with no portal, Settings says ${got.state} · ${got.summary}`);
    }
    if (!/README\.md#sandboxed-browsers-on-linux$/.test(got.sandboxLink)) {
      fail(`Settings does not link "Sandboxed browsers on Linux" (href: "${got.sandboxLink}")`);
    }
    console.log(`OK: with no portal, the ${name}'s Settings says "Not installed" and links the README`);
  }
} catch (e) {
  fail(exited || String(e && e.stack || e));
} finally {
  rdp?.close();
  try { ff.kill(); } catch { /* gone */ }
}
process.exit(0);
