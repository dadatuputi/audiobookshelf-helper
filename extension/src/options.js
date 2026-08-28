/* Options page.
 *
 * Also where the host permission is granted. The extension ships with no host
 * permissions at all - it asks for the one origin the user configures, and a
 * permissions.request() has to come from a real user gesture, which is what
 * the Grant access button is. */
const FIELDS = ["absUrl", "apiKey", "devicePath", "subdir", "folderTemplate",
                "localRoot", "sourceMode"];
const CHECKS = ["renameM4b"];

const $ = (id) => document.getElementById(id);

function note(el, msg, cls) {
  el.textContent = msg;
  el.className = "note " + (cls || "");
}

function originOf(url) {
  try {
    return ABSH.originPattern(url);
  } catch {
    return null;
  }
}

async function refreshPermissionState() {
  const url = $("absUrl").value.trim();
  const pattern = originOf(url);
  const btn = $("grant");
  const out = $("permState");

  if (!pattern) {
    btn.disabled = true;
    note(out, url ? "Enter a full URL, e.g. http://media.local:13378" : "Set the server URL first.",
         url ? "err" : "");
    return false;
  }
  const granted = await browser.permissions.contains({ origins: [pattern] });
  btn.disabled = granted;
  note(out, granted ? `Access granted for ${pattern}` : `Not granted yet for ${pattern}`,
       granted ? "ok" : "warn");
  return granted;
}

async function load() {
  const d = await browser.storage.local.get(ABSH.DEFAULTS);
  for (const k of FIELDS) if ($(k)) $(k).value = d[k] || "";
  for (const k of CHECKS) $(k).checked = !!d[k];
  await refreshPermissionState();
}

$("grant").addEventListener("click", async () => {
  const pattern = originOf($("absUrl").value.trim());
  if (!pattern) return;
  try {
    const ok = await browser.permissions.request({ origins: [pattern] });
    if (!ok) {
      note($("permState"), "Permission was declined.", "err");
      return;
    }
    // The toolbar button is registered against this origin, so tell the
    // background to (re)register now that the grant exists.
    await browser.runtime.sendMessage({ type: "permissionChanged" });
    await refreshPermissionState();
  } catch (e) {
    note($("permState"), String(e.message || e), "err");
  }
});

$("absUrl").addEventListener("input", refreshPermissionState);

$("save").addEventListener("click", async () => {
  const o = {};
  for (const k of FIELDS) o[k] = $(k).value.trim();
  for (const k of CHECKS) o[k] = $(k).checked;
  await browser.storage.local.set(o);
  const m = $("msg");
  m.textContent = "saved";
  setTimeout(() => { m.textContent = ""; }, 1500);
  await refreshPermissionState();
});

load();
