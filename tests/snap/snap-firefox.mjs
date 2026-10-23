/**
 * Ubuntu's Firefox Snap, reaching the helper the way it does for a user.
 *
 * A Snap cannot start a program outside its sandbox. It asks the desktop
 * portal to: the portal reads the manifest install.py wrote to the real
 * ~/.mozilla/native-messaging-hosts, checks the user allowed it, and starts
 * the helper outside the Snap. Nothing else in the suite runs that path - the
 * other Firefox tests use Playwright's own Firefox, which is not a Snap.
 *
 * Run by .github/workflows/snap-firefox.yml, which installs the Snap, the
 * helper (from a packaged release, with install.py) and the allow, and runs
 * this on the user's own session bus. It launches the Snap itself, installs
 * the add-on over Firefox's debugging protocol, opens Settings the same way,
 * and waits for the Helper row to say it reached the helper.
 *
 *   node tests/snap/snap-firefox.mjs <expected helper release>
 */
import { spawn } from "node:child_process";
import { cpSync, mkdirSync, rmSync, writeFileSync, createWriteStream } from "node:fs";
import { randomUUID } from "node:crypto";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import net from "node:net";
import { installTemporaryAddon, FIREFOX_PREFS } from "../real/firefox-addon.mjs";
import { RdpClient, openExtensionPage } from "../real/firefox-rdp.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const want = process.argv[2] || "";
const GECKO_ID = JSON.parse(
  (await import("node:fs")).readFileSync(join(root, "extension/identity.json"), "utf8")).geckoId;

// The Snap reads and writes freely only in its own corner of home. The
// profile and the add-on go there; the helper does not, and must not.
const work = join(homedir(), "snap", "firefox", "common", "absh-ci");
rmSync(work, { recursive: true, force: true });
const profile = join(work, "profile");
const addon = join(work, "addon");
mkdirSync(profile, { recursive: true });
cpSync(join(root, "extension/dist/firefox"), addon, { recursive: true });

const uuid = randomUUID();
const prefs = { ...FIREFOX_PREFS,
                "extensions.webextensions.uuids": JSON.stringify({ [GECKO_ID]: uuid }) };
// widget.use-xdg-desktop-portal.native-messaging is left at the Snap's own
// default: what is being tested is what a user gets.
writeFileSync(join(profile, "user.js"), Object.entries(prefs)
  .map(([k, v]) => `user_pref(${JSON.stringify(k)}, ${JSON.stringify(v)});`).join("\n") + "\n");

const port = await new Promise((res, rej) => {
  const s = net.createServer();
  s.on("error", rej);
  s.listen(0, "127.0.0.1", () => { const { port } = s.address(); s.close(() => res(port)); });
});

const marker = `absh-snap-${uuid.slice(0, 8)}`;
const logPath = resolve(process.env.SNAP_LOG || "snap-firefox.log");
const log = createWriteStream(logPath);
const ff = spawn("snap", ["run", "firefox", "--headless", "--no-remote", "--profile", profile,
                          "-start-debugger-server", String(port), `data:text/plain,${marker}`], {
  env: { ...process.env, MOZ_LOG: "NativeMessagingPortal:5,timestamp" },
  stdio: ["ignore", "pipe", "pipe"],
});
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

let rdp;
try {
  const installed = await installTemporaryAddon(port, addon);
  const id = installed.addon.uuid || uuid;
  rdp = await RdpClient.open(port);
  const tab = await openExtensionPage(rdp, { marker, url: `moz-extension://${id}/options.html`,
                                             timeout: 60_000 });
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
  }));
  console.log(`Helper row: ${got.state} · ${got.summary}${got.note ? ` · ${got.note}` : ""}`);
  if (got.state === "err") fail(`the Snap did not reach the helper: ${got.summary} ${got.note}`);
  if (want && !got.summary.includes(want)) {
    fail(`the helper that answered is not the one install.py installed ` +
         `(wanted ${want}, Settings says "${got.summary}")`);
  }
  console.log("OK: the Firefox Snap started the helper through the desktop portal");
} catch (e) {
  fail(exited || String(e && e.stack || e));
} finally {
  rdp?.close();
  try { ff.kill(); } catch { /* gone */ }
}
process.exit(0);
