/**
 * Install a temporary add-on into a Playwright-launched Firefox.
 *
 * Playwright has no Firefox extension support - microsoft/playwright#2644 has
 * been open since 2020 - so the only route is Firefox's own remote debugging
 * protocol: launch with -start-debugger-server, open a socket, ask the root
 * actor for the addons actor, then installTemporaryAddon.
 *
 * playwright-webextext packages exactly this, and we do not use it: at 0.0.5
 * it cannot load an MV3 add-on that declares no static content_scripts,
 * because overridePermissions() dereferences manifest.content_scripts[0]
 * unconditionally. Ours declares none on purpose - the content script is
 * registered at runtime for one origin, after the user grants it. The wire
 * format is small enough to own.
 *
 * Framing is `<byte length>:<JSON>`, length in bytes and not characters.
 */
import net from "node:net";
import { writeFileSync, readFileSync, readdirSync, mkdirSync, cpSync } from "node:fs";
import { join } from "node:path";

/** A copy of the built add-on whose one asked-for host permission is declared
 *  instead, because Firefox grants declared ones at install.
 *
 *  This is the compromise the Firefox suite runs under, and it is worth being
 *  exact about. Two controls established the rest: a probe add-on with a
 *  static content script injects into the page here, and a second probe that
 *  registers the same script through browser.scripting injects too - so
 *  temporary add-ons, and runtime registration, both work in this browser.
 *  What the add-on under test never gets is the grant. Its host permission is
 *  optional by design (it asks for the one server the user configures, which
 *  is what keeps the shipped manifest asking for no host at all), that grant
 *  is a click in an extension page, and Playwright cannot open one - nor can
 *  the grant be seeded: a run proved the origin never reaches Firefox's own
 *  permission store however the profile is prepared.
 *
 *  So exactly one manifest field moves, and everything the tests then drive -
 *  the background, the content script, the page hook, the native host - is the
 *  shipped code. What this cannot cover is the permission request itself; the
 *  Chromium suite covers that, on a browser where the grant can be seeded.
 */
export function withDeclaredHostPermission(distDir, outDir) {
  mkdirSync(outDir, { recursive: true });
  cpSync(distDir, outDir, { recursive: true });
  const file = join(outDir, "manifest.json");
  const manifest = JSON.parse(readFileSync(file, "utf8"));
  manifest.host_permissions = manifest.optional_host_permissions;
  delete manifest.optional_host_permissions;
  writeFileSync(file, JSON.stringify(manifest, null, 2));
  return outDir;
}

/** A throwaway add-on that does the simplest possible thing: a static content
 *  script, on every site, with the host permission declared up front rather
 *  than asked for.
 *
 *  It is a control, not a feature, and it stays because it is the thing that
 *  tells you the harness itself is sound: if the real add-on draws nothing and
 *  this marks the page, the browser is fine and the fault is ours. Run once
 *  with its script registered through browser.scripting instead of named in
 *  the manifest, it still marked the page - which is how the host permission,
 *  and not runtime registration, was established as what Firefox was
 *  withholding.
 */
export function writeProbeAddon(dir) {
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, "manifest.json"), JSON.stringify({
    manifest_version: 3,
    name: "absh injection probe",
    version: "1.0",
    // Declared, not requested: Firefox grants these at install, so the probe
    // depends on no grant arriving from anywhere.
    host_permissions: ["<all_urls>"],
    content_scripts: [{
      matches: ["<all_urls>"],
      js: ["probe.js"],
      run_at: "document_start",
    }],
    browser_specific_settings: { gecko: { id: "absh-probe@example.invalid" } },
  }, null, 2));
  writeFileSync(join(dir, "probe.js"),
    'document.documentElement.dataset.abshProbe = "ran";\n');
  return dir;
}

/** Whether Firefox's own permission store holds this origin.
 *
 *  The store is a binary key-value file, but a granted origin is written into
 *  it as plain text, so looking for the string is enough to tell a grant that
 *  landed from one that never did. Nothing else can answer that from outside
 *  the browser: permissions.contains() lives in the add-on, and the add-on
 *  cannot be reached, which is the whole difficulty here.
 */
export function grantInStore(profileDir, origin) {
  const dir = join(profileDir, "extension-store-permissions");
  let names;
  try {
    names = readdirSync(dir);
  } catch {
    // Distinct from "granted nothing": no store at all means Firefox never
    // got as far as writing permissions, which is a different failure.
    return "no-store";
  }
  for (const name of names) {
    if (readFileSync(join(dir, name), "latin1").includes(origin)) return "present";
  }
  return "absent";
}

/** Put the add-on's settings in the profile before Firefox starts.
 *
 *  The obvious route - open moz-extension://<uuid>/options.html and fill the
 *  form - does not work under Playwright. Its Firefox is stock, and Juggler
 *  cannot drive moz-extension:// documents: the navigation commits and then
 *  waits for a load event that never arrives. DuckDuckGo's Firefox extension
 *  harness has to patch omni.ja to "let Juggler interact with moz-extension://
 *  pages", which is the same limitation seen from the other side.
 *
 *  So write storage.local directly. With the IndexedDB backend turned off
 *  (see FIREFOX_PREFS) storage.local is a single JSON file per add-on, keyed
 *  by add-on id, and an add-on installed afterwards reads it as its own - the
 *  same values the options page would have saved, reaching the add-on through
 *  the same API.
 */
export function seedLocalStorage(profileDir, addonId, data) {
  writeFileSync(storageFile(profileDir, addonId, true), JSON.stringify(data, null, 2));
}

/** Read that same file back.
 *
 *  It is how the add-on's own writes become visible from outside: the
 *  background records what it registered a content script for, and with the
 *  JSON backend that lands here. It is the only way this suite can see the
 *  registration table at all - browser.scripting is reachable only from an
 *  extension page, and Playwright cannot open one. Returns {} until the
 *  add-on has written anything; the file is flushed a beat after each set().
 */
export function readLocalStorage(profileDir, addonId) {
  try {
    return JSON.parse(readFileSync(storageFile(profileDir, addonId), "utf8"));
  } catch {
    return {};
  }
}

function storageFile(profileDir, addonId, create = false) {
  const dir = join(profileDir, "browser-extension-data", addonId);
  if (create) mkdirSync(dir, { recursive: true });
  return join(dir, "storage.js");
}

export const FIREFOX_PREFS = {
  "devtools.debugger.remote-enabled": true,
  "devtools.debugger.prompt-connection": false,
  "xpinstall.signatures.required": false,
  "xpinstall.whitelist.required": false,
  // Temporary add-ons are removed on shutdown; nothing here should outlive the run.
  "extensions.autoDisableScopes": 0,
  // Keep storage.local in its JSON file rather than IndexedDB, so the test can
  // write the add-on's settings into the profile before launch - see
  // seedLocalStorage. Only the backend changes; the storage API does not.
  "extensions.webextensions.ExtensionStorageIDB.enabled": false,
  // Stand in for the user allowing the add-on on the site. Seeding the file
  // Firefox migrates grants from does not work - a run proved the origin never
  // reaches Firefox's own permission store - and the click that would grant it
  // is in an extension page Playwright cannot open. These are Firefox's own
  // switches for granting a manifest's host permissions without asking.
  "extensions.originControls.grantByDefault": true,
  "extensions.webextOptionalPermissionPrompts": false,
};

class RDP {
  constructor(socket) {
    this.socket = socket;
    this.buffer = Buffer.alloc(0);
    this.waiters = [];
    // The server speaks first, with an unsolicited greeting from the root
    // actor. Claim it with a waiter queued before anything is sent, so replies
    // stay paired with their requests instead of each being one behind.
    this.greeting = new Promise((resolve, reject) => {
      this.waiters.push({ resolve, reject });
      setTimeout(() => reject(new Error("no RDP greeting")), 30_000);
    });
    socket.on("data", (chunk) => this._onData(chunk));
  }

  _onData(chunk) {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    // A packet is `<length>:<payload>`; payloads can span several chunks, and
    // several packets can arrive in one.
    for (;;) {
      const colon = this.buffer.indexOf(0x3a); // ":"
      if (colon < 0) return;
      const len = Number(this.buffer.subarray(0, colon).toString("ascii"));
      if (!Number.isInteger(len)) return;
      const start = colon + 1;
      if (this.buffer.length < start + len) return;
      const body = this.buffer.subarray(start, start + len).toString("utf8");
      this.buffer = this.buffer.subarray(start + len);
      let msg;
      try { msg = JSON.parse(body); } catch { continue; }
      const w = this.waiters.shift();
      if (w) w.resolve(msg);
    }
  }

  send(obj) {
    const payload = Buffer.from(JSON.stringify(obj), "utf8");
    this.socket.write(`${payload.length}:`);
    this.socket.write(payload);
    return new Promise((resolve, reject) => {
      this.waiters.push({ resolve, reject });
      setTimeout(() => reject(new Error(`RDP timeout: ${JSON.stringify(obj).slice(0, 80)}`)), 30_000);
    });
  }
}

/** Connect once the debugger server is listening; it is not up immediately. */
async function connect(port, tries = 60) {
  for (let i = 0; i < tries; i++) {
    try {
      return await new Promise((resolve, reject) => {
        const s = net.connect({ port, host: "127.0.0.1" });
        s.once("connect", () => resolve(s));
        s.once("error", reject);
      });
    } catch {
      await new Promise((r) => setTimeout(r, 500));
    }
  }
  throw new Error(`no Firefox debugger server on :${port}`);
}

export async function installTemporaryAddon(port, addonPath) {
  const socket = await connect(port);
  let keep = false;
  try {
    const rdp = new RDP(socket);
    await rdp.greeting;
    const root = await rdp.send({ to: "root", type: "getRoot" });
    const actor = root.addonsActor;
    if (!actor) throw new Error(`no addonsActor in ${JSON.stringify(root).slice(0, 200)}`);
    const res = await rdp.send({ to: actor, type: "installTemporaryAddon", addonPath });
    if (res.error) throw new Error(`installTemporaryAddon: ${res.error} ${res.message || ""}`);
    // Insist on an id. A reply that is merely not an error is not proof the
    // add-on loaded, and a silent non-install shows up much later as every
    // test timing out on a button that was never going to appear.
    const addon = res.addon || res;
    if (!addon || !addon.id) {
      throw new Error(`installTemporaryAddon returned no addon: ${JSON.stringify(res).slice(0, 300)}`);
    }
    // Firefox hands back the add-on's own manifestURL - moz-extension://<uuid>/
    // manifest.json - which is the authoritative UUID for its pages. Only
    // best-effort use is made of it: see seedLocalStorage for why those pages
    // are not reliably reachable from Playwright.
    const m = /^moz-extension:\/\/([^/]+)\//.exec(addon.manifestURL || "");
    addon.uuid = m ? m[1] : null;
    keep = true;
    // web-ext holds its connection open for the whole session, and a temporary
    // add-on's lifetime is not clearly independent of it. Closing the socket
    // straight after installing is a risk with no upside, so hand the caller
    // the disconnect instead of taking it here.
    return { addon, disconnect: () => socket.end() };
  } finally {
    if (!keep) socket.end();
  }
}

