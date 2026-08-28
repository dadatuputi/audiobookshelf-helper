/* Audiobookshelf Helper - background
 *
 * The extension itself CANNOT write to a USB device. Two hard limits:
 *   - downloads.download({filename}) rejects absolute paths and "../", so
 *     everything would land under the Downloads folder;
 *   - Firefox does not implement showDirectoryPicker().
 * So the filesystem work is delegated to the native messaging host, and this
 * script is only an API client plus a bridge.
 *
 * Pure logic lives in lib.js (globalThis.ABSH) so it can be unit tested
 * without a browser.
 */

const HOST = "io.github.abshelper";

async function cfg() {
  return browser.storage.local.get(ABSH.DEFAULTS);
}

async function api(path) {
  const c = await cfg();
  if (!c.absUrl || !c.apiKey) {
    throw new Error("Set the server URL and API key in the extension options.");
  }
  const r = await fetch(ABSH.baseUrl(c.absUrl) + path, {
    headers: { Authorization: "Bearer " + c.apiKey }
  });
  if (!r.ok) throw new Error(`Audiobookshelf ${path} responded ${r.status}`);
  return r.json();
}

async function listLibraries() {
  const d = await api("/api/libraries");
  const libs = Array.isArray(d) ? d : (d.libraries || []);
  return libs.filter(l => l.mediaType === "book").map(l => ({ id: l.id, name: l.name }));
}

async function listBooks(libraryId) {
  const c = await cfg();
  const id = libraryId || c.libraryId;
  if (!id) throw new Error("No library selected.");
  const d = await api(`/api/libraries/${encodeURIComponent(id)}/items?limit=0`);
  return ABSH.sortBooks((d.results || []).map(ABSH.normalizeBook));
}

async function sync(items) {
  const c = await cfg();
  return browser.runtime.sendNativeMessage(HOST, ABSH.buildSyncPayload(c, items));
}

async function ping() {
  try {
    const r = await browser.runtime.sendNativeMessage(HOST, { cmd: "ping" });
    return { ok: true, ...r };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
}

browser.runtime.onMessage.addListener(async (msg) => {
  try {
    switch (msg && msg.type) {
      case "libraries": return { ok: true, data: await listLibraries() };
      case "books":     return { ok: true, data: await listBooks(msg.libraryId) };
      case "sync":      return { ok: true, data: await sync(msg.items) };
      case "ping":      return { ok: true, data: await ping() };
      case "openPicker":
        if (browser.action && browser.action.openPopup) {
          await browser.action.openPopup().catch(() => {});
        }
        return { ok: true };
      default:
        return { ok: false, error: `unknown message type ${msg && msg.type}` };
    }
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
});
