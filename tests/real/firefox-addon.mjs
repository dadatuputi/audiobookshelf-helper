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
import { writeFileSync } from "node:fs";
import { join } from "node:path";

/** Grant optional permissions up front. Headless Firefox draws no doorhanger,
 *  so permissions.request() never resolves there - the same reason the
 *  Chromium suite seeds its grant into Preferences. Firefox keeps them in
 *  extension-preferences.json, keyed by add-on id. */
export function seedGrantedPermissions(profileDir, addonId, { origins = [], permissions = [] }) {
  writeFileSync(
    join(profileDir, "extension-preferences.json"),
    JSON.stringify({ [addonId]: { permissions, origins } }, null, 2)
  );
}

export const FIREFOX_PREFS = {
  "devtools.debugger.remote-enabled": true,
  "devtools.debugger.prompt-connection": false,
  "xpinstall.signatures.required": false,
  "xpinstall.whitelist.required": false,
  // Temporary add-ons are removed on shutdown; nothing here should outlive the run.
  "extensions.autoDisableScopes": 0,
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
  try {
    const rdp = new RDP(socket);
    await rdp.greeting;
    const root = await rdp.send({ to: "root", type: "getRoot" });
    const actor = root.addonsActor;
    if (!actor) throw new Error(`no addonsActor in ${JSON.stringify(root).slice(0, 200)}`);
    const res = await rdp.send({ to: actor, type: "installTemporaryAddon", addonPath });
    if (res.error) throw new Error(`installTemporaryAddon: ${res.error} ${res.message || ""}`);
    return res.addon || res;
  } finally {
    socket.end();
  }
}
